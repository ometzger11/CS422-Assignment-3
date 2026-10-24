#!/usr/bin/env python3
"""
Small TCP sender/receiver for testing DART without iperf3.

  selftest  Tests A + B on loopback: dart is registered and selectable, a
            transfer completes with the right bytes, cwnd/RTT look sane,
            DART's decision counters moved, and the kernel logged no warning.
  sink      Receiver: counts and hashes everything it receives and, once the
            sender closes its side, replies with "<bytes> <sha256>".
  send      Sender: sends for --duration seconds with --algo, samples TCP_INFO
            every --interval seconds (samples.csv format plus ssthresh and
            ca_state), checks the receiver's byte count and hash, and prints
            a one-line JSON summary.
  report    Summarize `send` results (used by scripts/netem_test.sh).

No root needed, but dart must already be loaded (scripts/dart_module.sh).
"""

import argparse
import csv
import hashlib
import json
import os
import random
import re
import select
import socket
import statistics
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "client"))
import tcpinfo  # noqa: E402
from iperftest import SAMPLE_COLUMNS as A2_COLUMNS  # noqa: E402

STATS = "/sys/module/tcp_dart/parameters/stats"
AVAILABLE = "/proc/sys/net/ipv4/tcp_available_congestion_control"
ALLOWED = "/proc/sys/net/ipv4/tcp_allowed_congestion_control"
TAINTED = "/proc/sys/kernel/tainted"
TAINT_WARN = 1 << 9          # set by any kernel WARN()
TCP_CA_STATES = ["Open", "Disorder", "CWR", "Recovery", "Loss"]

# Same columns as the A2 samples.csv, plus two that matter for DART
SAMPLE_COLUMNS = A2_COLUMNS + ["ssthresh_segs", "ca_state"]

# The bytes we send: a fixed pseudo-random 1 MiB block, repeated
BLOCK = random.Random(422).randbytes(1 << 20)


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# DART's global decision counters, or None if the module isn't loaded
def read_stats():
    text = read_file(STATS)
    if text is None:
        return None
    return {k: int(v) for k, v in (kv.split("=") for kv in text.split())}


def stats_delta(before, after):
    if before is None or after is None:
        return None
    return {k: after[k] - before.get(k, 0) for k in after}


# ---------------------------------------------------------------- receiver

def serve_conn(conn):
    digest = hashlib.sha256()
    total = 0
    with conn:
        while True:
            chunk = conn.recv(1 << 18)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        conn.sendall(f"{total} {digest.hexdigest()}\n".encode())


def sink(bind, port, once=False, ready=None):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(8)
    if ready:
        ready(srv.getsockname()[1])
    with srv:
        while True:
            conn, _ = srv.accept()
            t = threading.Thread(target=serve_conn, args=(conn,), daemon=True)
            t.start()
            if once:
                t.join()
                return


# ---------------------------------------------------------------- sender

# Sends for `duration` seconds (or `nbytes` bytes), sampling TCP_INFO every
# `interval` seconds. Returns a summary dict; raises OSError on socket errors.
def send(host, port, algo, duration=None, nbytes=None, interval=0.05,
         run_id="", writer=None):
    stats_before = read_stats()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if algo:
            tcpinfo.set_congestion(sock, algo)   # must happen before connect()
        sock.settimeout(10)
        sock.connect((host, port))
        algo_actual = tcpinfo.get_congestion(sock)
        sock.setblocking(False)

        digest = hashlib.sha256()
        sent = 0
        offset = 0
        samples = []
        start = time.monotonic()
        end = start + duration if duration else None
        next_sample = start + interval
        prev = {"t": start, "acked": 0, "retrans": 0}

        def sample(now):
            info = tcpinfo.read_tcp_info(sock)
            dt = now - prev["t"]
            if dt <= 0:
                return
            row = {
                "run_id": run_id,
                "t_s": round(now - start, 3),
                "bytes_acked": info["bytes_acked"],
                "goodput_mbps": round((info["bytes_acked"] - prev["acked"]) * 8 / dt / 1e6, 3),
                "cwnd_segs": info["snd_cwnd"],
                "srtt_ms": info["rtt"] / 1000,
                "rttvar_ms": info["rttvar"] / 1000,
                "min_rtt_ms": info["min_rtt"] / 1000,
                "total_retrans": info["total_retrans"],
                "retrans_delta": info["total_retrans"] - prev["retrans"],
                "pacing_rate_mbps": round(info["pacing_rate"] * 8 / 1e6, 3),
                "ssthresh_segs": info["snd_ssthresh"],
                "ca_state": info["ca_state"],
            }
            samples.append(row)
            if writer is not None:
                writer.writerow([row[c] for c in SAMPLE_COLUMNS])
            prev.update(t=now, acked=info["bytes_acked"], retrans=info["total_retrans"])

        while True:
            now = time.monotonic()
            if now >= next_sample:
                sample(now)
                while next_sample <= now:
                    next_sample += interval
            if (end and now >= end) or (nbytes is not None and sent >= nbytes):
                break
            wait = next_sample - now
            if end:
                wait = min(wait, end - now)
            _, writable, _ = select.select([], [sock], [], max(wait, 0))
            if not writable:
                continue
            chunk = memoryview(BLOCK)[offset:]
            if nbytes is not None:
                chunk = chunk[:nbytes - sent]
            try:
                n = sock.send(chunk)
            except BlockingIOError:
                continue
            digest.update(chunk[:n])
            sent += n
            offset = (offset + n) % len(BLOCK)

        # Let the receiver drain, then compare its count and hash with ours
        sock.setblocking(True)
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(60)
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = sock.recv(256)
            if not chunk:
                break
            reply += chunk
        sample(time.monotonic())
        algo_after = tcpinfo.get_congestion(sock)
    finally:
        sock.close()
    elapsed = time.monotonic() - start

    parts = reply.decode(errors="replace").split()
    got_bytes = int(parts[0]) if len(parts) == 2 and parts[0].isdigit() else None
    verified = got_bytes == sent and parts[1:] == [digest.hexdigest()]
    return summarize(samples, {
        "run_id": run_id,
        "algo": algo,
        "algo_actual": algo_actual,
        "algo_after": algo_after,
        "bytes_sent": sent,
        "bytes_received": got_bytes,
        "verified": verified,
        "elapsed_s": round(elapsed, 3),
        "dart_stats": stats_delta(stats_before, read_stats()),
    })


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))] if values else None


def summarize(samples, result):
    if not samples:
        return result
    cwnd = [s["cwnd_segs"] for s in samples]
    srtt = [s["srtt_ms"] for s in samples]
    queue = [s["srtt_ms"] - s["min_rtt_ms"] for s in samples]
    last = samples[-1]
    result.update({
        "samples": len(samples),
        "goodput_mbps": round(last["bytes_acked"] * 8 / last["t_s"] / 1e6, 2) if last["t_s"] else None,
        "srtt_median_ms": round(statistics.median(srtt), 2),
        "min_rtt_ms": last["min_rtt_ms"],
        "queue_median_ms": round(statistics.median(queue), 2),
        "queue_p95_ms": round(pct(queue, 95), 2),
        "cwnd_min": min(cwnd),
        "cwnd_median": statistics.median(cwnd),
        "cwnd_max": max(cwnd),
        "cwnd_last_quarter_median": statistics.median(cwnd[len(cwnd) * 3 // 4:]),
        "retrans": last["total_retrans"],
        "rtt_measured": all(s["srtt_ms"] > 0 for s in samples[1:]) and last["min_rtt_ms"] > 0,
    })
    return result


# ---------------------------------------------------------------- selftest

class Checks:
    def __init__(self):
        self.failed = 0

    def report(self, status, name, detail=""):
        if status == "FAIL":
            self.failed += 1
        print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))

    def check(self, ok, name, detail=""):
        self.report("PASS" if ok else "FAIL", name, detail)
        return ok


def kernel_log_since(epoch):
    try:
        out = subprocess.run(["journalctl", "-k", "-q", "--no-pager", "-o", "cat",
                              "--since", f"@{int(epoch)}"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def selftest(algo, nbytes):
    c = Checks()
    t0 = time.time()
    taint0 = int(read_file(TAINTED) or 0)
    print(f"Test A: registration ({algo})")
    available = (read_file(AVAILABLE) or "").split()
    if not c.check(algo in available, "listed in tcp_available_congestion_control",
                   " ".join(available)):
        print(f"  Load it first: sudo {REPO}/scripts/dart_module.sh load")
        print("RESULT: FAIL")
        return 1
    if algo == "dart":
        version = read_file("/sys/module/tcp_dart/srcversion")
        c.check(version is not None and read_stats() is not None,
                "tcp_dart module and stats present", f"srcversion {version}")
    allowed = (read_file(ALLOWED) or "").split()
    c.report("PASS" if algo in allowed else "INFO", "in tcp_allowed_congestion_control",
             "yes" if algo in allowed else "no: only root can select it")
    with socket.socket() as s:
        try:
            tcpinfo.set_congestion(s, algo)
        except OSError as e:
            c.check(False, "setsockopt(TCP_CONGESTION)", str(e))
            print("RESULT: FAIL")
            return 1
        c.check(tcpinfo.get_congestion(s) == algo, "getsockopt(TCP_CONGESTION)",
                tcpinfo.get_congestion(s))

    print(f"Test B: loopback transfer of {nbytes >> 20} MiB")
    port = []
    ready = threading.Event()
    threading.Thread(target=sink, args=("127.0.0.1", 0, True,
                                        lambda p: (port.append(p), ready.set())),
                     daemon=True).start()
    ready.wait(5)
    r = send("127.0.0.1", port[0], algo, nbytes=nbytes, interval=0.005)
    c.check(r["algo_actual"] == algo and r["algo_after"] == algo,
            "data socket used the algorithm", f"{r['algo_actual']} / {r['algo_after']}")
    c.check(r["verified"], "receiver got every byte, same SHA-256",
            f"{r['bytes_received']} of {r['bytes_sent']} bytes in {r['elapsed_s']} s")
    if r.get("samples"):
        c.check(1 <= r["cwnd_min"] and r["cwnd_max"] < 1 << 31, "cwnd within bounds",
                f"min {r['cwnd_min']}, max {r['cwnd_max']} over {r['samples']} samples")
        c.check(r["rtt_measured"], "RTT measured",
                f"min_rtt {r['min_rtt_ms']} ms, median srtt {r['srtt_median_ms']} ms")
    else:
        c.check(False, "TCP_INFO samples", "none taken")
    if algo == "dart":
        d = r["dart_stats"] or {}
        c.check(d.get("rounds", 0) > 0, "DART evaluated rounds",
                " ".join(f"{k}={v}" for k, v in d.items() if v))

    print("Kernel health")
    taint1 = int(read_file(TAINTED) or 0)
    c.check(not (taint1 & TAINT_WARN) or (taint0 & TAINT_WARN),
            "no new kernel WARN (taint flag W)", f"tainted={taint1}")
    log = kernel_log_since(t0 - 1)
    if log is None:
        c.report("SKIP", "kernel log", "cannot read it (try sudo dmesg)")
    else:
        bad = [l for l in log.splitlines()
               if re.search(r"WARNING|BUG|Oops|Call Trace", l)]
        c.check(not bad, "kernel log since start of test", "; ".join(bad[:3]) or "clean")
    print("RESULT: " + ("PASS" if not c.failed else f"FAIL ({c.failed} checks)"))
    return 1 if c.failed else 0


# ---------------------------------------------------------------- report

LOSS_LINE = re.compile(r"tcp_dart: \S+ loss cwnd=(\d+) ssthresh=(\d+)")


def report(summary_path, kernel_log=None):
    with open(summary_path) as f:
        runs = [json.loads(line) for line in f if line.strip()]
    c = Checks()
    print(f"{'scenario':<10} {'algo':<6} {'Mbit/s':>7} {'srtt':>7} {'queue':>7} "
          f"{'q p95':>7} {'cwnd min/med/max':>18} {'retx':>6}  DART decisions")
    for r in runs:
        d = r.get("dart_stats") or {}
        decisions = " ".join(f"{k}={d[k]}" for k in ("ss_exit", "grow", "hold", "decrease",
                                                     "hold_rate_up", "hold_no_rate", "loss")
                             if d.get(k)) if r["algo"] == "dart" else ""
        cw = f"{r.get('cwnd_min')}/{r.get('cwnd_median')}/{r.get('cwnd_max')}"
        print(f"{r['label']:<10} {r['algo']:<6} {r.get('goodput_mbps', 0):>7} "
              f"{r.get('srtt_median_ms', 0):>7} {r.get('queue_median_ms', 0):>7} "
              f"{r.get('queue_p95_ms', 0):>7} {cw:>18} {r.get('retrans', 0):>6}  {decisions}")

    print("\nChecks")
    for r in runs:
        name = f"{r['label']}/{r['algo']}"
        c.check(r["verified"] and r["algo_actual"] == r["algo"] == r["algo_after"],
                f"{name}: right algorithm, all bytes delivered")
    darts = {r["label"]: r for r in runs if r["algo"] == "dart"}
    for label, r in darts.items():
        d = r.get("dart_stats") or {}
        c.check(d.get("rounds", 0) > 0, f"{label}/dart: DART evaluated rounds")
        c.check(r.get("cwnd_last_quarter_median", 0) >= 4,
                f"{label}/dart: not stuck at the minimum cwnd",
                f"median cwnd over the last quarter = {r.get('cwnd_last_quarter_median')}")
    if "loss" in darts:
        d = darts["loss"].get("dart_stats") or {}
        c.check(d.get("loss", 0) > 0, "loss/dart: ssthresh() called on loss",
                f"{d.get('loss', 0)} times")
    if "queue" in darts:
        d = darts["queue"].get("dart_stats") or {}
        c.check(d.get("ss_exit", 0) + d.get("decrease", 0) > 0,
                "queue/dart: reacted to queueing delay",
                f"ss_exit={d.get('ss_exit', 0)} decrease={d.get('decrease', 0)}")
        others = [r for r in runs if r["label"] == "queue" and r["algo"] in ("reno", "cubic")]
        if others:
            worst = min(o["queue_median_ms"] for o in others)
            c.check(darts["queue"]["queue_median_ms"] < worst,
                    "queue/dart: lower median queueing delay than reno/cubic",
                    f"{darts['queue']['queue_median_ms']} ms vs {worst} ms")
    if kernel_log:
        with open(kernel_log) as f:
            losses = [tuple(map(int, m.groups())) for m in map(LOSS_LINE.search, f) if m]
        wrong = [(cw, ss) for cw, ss in losses if ss != max(2, cw * 7 // 10)]
        if losses:
            c.check(not wrong, "every logged loss: ssthresh = max(2, floor(0.7 * cwnd))",
                    f"{len(losses)} logged" + (f", wrong: {wrong[:3]}" if wrong else ""))
        else:
            c.report("SKIP", "ssthresh rule in kernel log", "no loss lines logged")
    print("RESULT: " + ("PASS" if not c.failed else f"FAIL ({c.failed} checks)"))
    return 1 if c.failed else 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("selftest", help="Tests A and B on loopback")
    p.add_argument("--algo", default="dart")
    p.add_argument("--mbytes", type=int, default=256, help="MiB to transfer")

    p = sub.add_parser("sink", help="receiver")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5299)

    p = sub.add_parser("send", help="sender")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=5299)
    p.add_argument("--algo", default="dart")
    p.add_argument("--duration", type=float, default=20)
    p.add_argument("--interval", type=float, default=0.05)
    p.add_argument("--label", default="", help="scenario name (report grouping)")
    p.add_argument("--samples", help="append TCP_INFO samples to this CSV")
    p.add_argument("--runs", help="append a runs.csv row (A2 format) to this CSV")
    p.add_argument("--summary", help="append the JSON summary to this file")

    p = sub.add_parser("report", help="summarize send results")
    p.add_argument("summary")
    p.add_argument("--kernel-log", help="kernel log with DART debug lines")

    args = ap.parse_args()
    if args.cmd == "selftest":
        return selftest(args.algo, args.mbytes << 20)
    if args.cmd == "sink":
        sink(args.bind, args.port)
        return 0
    if args.cmd == "report":
        return report(args.summary, args.kernel_log)

    run_id = f"{args.label or 'run'}-{args.algo}-{int(time.time())}"
    start_utc = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    samples_file = writer = None
    if args.samples:
        new = not os.path.exists(args.samples)
        samples_file = open(args.samples, "a", newline="")
        writer = csv.writer(samples_file)
        if new:
            writer.writerow(SAMPLE_COLUMNS)
    try:
        r = send(args.host, args.port, args.algo, duration=args.duration,
                 interval=args.interval, run_id=run_id, writer=writer)
    finally:
        if samples_file:
            samples_file.close()
    r["label"] = args.label
    if args.runs:
        new = not os.path.exists(args.runs)
        with open(args.runs, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["run_id", "server", "port", "algo_requested", "algo_actual",
                            "rep", "start_utc", "status", "error"])
            w.writerow([run_id, args.label or args.host, args.port, args.algo,
                        r["algo_actual"], 0, start_utc,
                        "ok" if r["verified"] else "failed",
                        "" if r["verified"] else "byte count or hash mismatch"])
    line = json.dumps(r)
    print(line)
    if args.summary:
        with open(args.summary, "a") as f:
            f.write(line + "\n")
    return 0 if r["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
