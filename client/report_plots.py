"""
Report figures and tables for A3, from runs.csv + samples.csv (format:
docs/csv-format.md). Builds on plots.py, which makes the per-server figures
and summary tables for every destination; this script picks out one
representative destination and adds the cross-server views.

Outputs (all in --out; figures as .pdf and .png):
  fig1_timeseries_grid_<server>  goodput / cwnd / RTT / retransmits vs time for
                                 the representative server: one column per
                                 algorithm, every repetition overlaid
  scatter_<server>               plots.plot_scatter for the same server, on the
                                 same goodput axis as fig1
  timeseries_<server>            plots.plot_timeseries (the A2 layout: one page
                                 per repetition, algorithms overlaid), plus one
                                 PNG per page
  fig3_aggregate                 per-run goodput, RTT, queueing delay and
                                 retransmits per algorithm, all servers
  fig4_startup                   first 3 s of every run: cwnd and sRTT - min RTT
  table_aggregate.csv/.md        per-algorithm statistics over all servers
  table_test_order.md            destinations, repetitions and execution order
  table_dart_decisions.md        DART's decision counters over the batch

Usage:
  python3 report_plots.py --runs runs.csv --samples samples.csv \\
      --env environment.txt --rep-server 185.76.9.135:5201 --out report/
"""

import argparse
import csv
import os
import re
from collections import defaultdict

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np

import plots  # also applies the shared matplotlib style
from plots import algo_sort_key, fmt, style

STALL_MBPS = 5.0        # a run whose mean goodput is below this is "stalled"
STARTUP_S = 3.0         # window shown in fig4
BOOT_RESAMPLES = 10000  # bootstrap resamples for the CI of the mean
SEED = 422              # fixed, so the CI and the jitter are reproducible

# Repetitions are told apart by line style; color stays with the algorithm
REP_STYLES = ["solid", (0, (4, 2)), (0, (1, 1.5))]
INK_MUTED = "#6b6a66"
REF_LINE = "#a3a29d"

# Delay thresholds drawn as references in fig4 (ms)
DART_HIGH_MAX_MS = 20   # tcp_dart.c DART_HIGH_MAX_US
HYSTART_MAX_MS = 16     # Linux tcp_cubic.c HYSTART_DELAY_MAX

# Meaning of each counter in /sys/module/tcp_dart/parameters/stats (tcp_dart.c)
DART_COUNTERS = {
    "rounds": "rounds evaluated",
    "few_samples": "fewer than 3 RTT samples: Reno-style round",
    "slow_start": "stayed in slow start",
    "ss_exit": "left slow start: queue_delay >= HIGH",
    "grow": "Rule A: cwnd += 2",
    "not_cwnd_limited": "Rule A, but not cwnd-limited: no growth",
    "hold": "Rule B: hold",
    "decrease": "Rule C, rate plateaued: cwnd x 0.9",
    "hold_rate_up": "Rule C, rate still improving: hold",
    "hold_no_rate": "Rule C, no valid rate to compare: hold",
    "loss": "ssthresh() calls (loss or ECN)",
}


def save(fig, out_dir, stem):
    path = os.path.join(out_dir, stem + ".pdf")
    fig.savefig(path)
    fig.savefig(os.path.join(out_dir, stem + ".png"), dpi=plots.PNG_DPI)
    plt.close(fig)
    return path


def run_mean(r):
    return np.nanmean(r["samples"]["goodput_mbps"])


def stalled(r):
    return run_mean(r) < STALL_MBPS


def by_algo(runs):
    groups = defaultdict(list)
    for r in runs:
        groups[r["algo"]].append(r)
    return {a: sorted(groups[a], key=lambda r: (r["label"], r["rep"]))
            for a in sorted(groups, key=algo_sort_key)}


def top_of(runs, col):
    """Upper y-limit shared by every run: the same rule as plots.plot_timeseries."""
    vals = np.concatenate([r["samples"][col] for r in runs])
    top = np.nanmax(vals) if np.isfinite(vals).any() else 1
    return top * 1.08 if top > 0 else 1


def plot_grid(server, server_runs, out_dir):
    """Rows: goodput, cwnd, sRTT, retransmits. Columns: algorithms. Every rep
    of an algorithm in its column; each row shares its y-axis, so the columns
    compare directly. Returns (path, goodput axis top)."""
    panels = [
        ("goodput_mbps", "Goodput (Mbit/s)"),
        ("cwnd_segs", "cwnd (segments)"),
        ("srtt_ms", "Smoothed RTT (ms)"),
        ("retrans_delta", "Retransmits / interval"),
    ]
    algos = by_algo(server_runs)
    reps = sorted({r["rep"] for r in server_runs})
    base_rtt = np.nanmin(np.concatenate([r["samples"]["min_rtt_ms"] for r in server_runs]))
    tops = {col: top_of(server_runs, col) for col, _ in panels}
    tmax = max(np.nanmax(r["samples"]["t_s"]) for r in server_runs)

    fig, axes = plt.subplots(len(panels), len(algos), figsize=(7.5, 7.4),
                             sharex=True, sharey="row", squeeze=False)
    for c, (algo, rs) in enumerate(algos.items()):
        color = style(algo)["color"]
        means = " · ".join(f"{run_mean(r):.0f}" for r in sorted(rs, key=lambda r: r["rep"]))
        axes[0][c].set_title(f"{algo}\n{means} Mbit/s", fontsize=8.5)
        for row, (col, _) in enumerate(panels):
            ax = axes[row][c]
            if col == "srtt_ms":
                ax.axhline(base_rtt, color=REF_LINE, linewidth=0.8, zorder=1)
            for r in rs:
                s = r["samples"]
                ls = REP_STYLES[r["rep"] % len(REP_STYLES)]
                if col == "retrans_delta":
                    ax.step(s["t_s"], s[col], where="post", color=color,
                            linestyle=ls, linewidth=1.0)
                else:
                    ax.plot(s["t_s"], s[col], color=color, linestyle=ls, linewidth=1.2)
            if col == "retrans_delta":
                # Mostly zeros plus rare bursts: symlog keeps small counts visible
                ax.set_yscale("symlog", linthresh=1)
                ax.set_ylim(0, max(tops[col] * 1.5, 10))
            else:
                ax.set_ylim(0, tops[col])
            if row == len(panels) - 1:
                ax.set_xlabel("Time (s)")
    for row, (_, ylabel) in enumerate(panels):
        axes[row][0].set_ylabel(ylabel)
    axes[-1][0].set_xlim(0, tmax)

    handles = [Line2D([], [], color=INK_MUTED, linestyle=REP_STYLES[i % len(REP_STYLES)],
                      linewidth=1.2, label=f"rep {i + 1}") for i in reps]
    handles.append(Line2D([], [], color=REF_LINE, linewidth=0.8,
                          label=f"base RTT {base_rtt:.0f} ms (min over all runs)"))
    fig.legend(handles=handles, ncol=len(handles), loc="upper center",
               bbox_to_anchor=(0.5, 0.968), fontsize=8)
    fig.suptitle(f"{server} (10 s tests, 0.2 s samples). Under each algorithm: "
                 f"mean goodput of rep {' · '.join(str(i + 1) for i in reps)}.",
                 y=0.995, fontsize=9.5)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    return save(fig, out_dir, f"fig1_timeseries_grid_{plots.safe_name(server)}"), \
        tops["goodput_mbps"]


def run_metrics(r):
    s = r["samples"]
    return {
        "goodput": run_mean(r),
        "srtt": np.nanmedian(s["srtt_ms"]),
        "queue": np.nanmedian(s["srtt_ms"] - s["min_rtt_ms"]),
        "retrans": np.nansum(s["retrans_delta"]),
    }


def plot_aggregate(runs, n_servers, out_dir):
    """One point per run (every server and rep), a box per algorithm."""
    panels = [
        ("goodput", "Mean goodput per run (Mbit/s)"),
        ("srtt", "Median sRTT per run (ms)"),
        ("queue", "Median sRTT − min RTT per run (ms)"),
        ("retrans", "Retransmits per run (segments)"),
    ]
    algos = by_algo(runs)
    metrics = {a: [run_metrics(r) for r in rs] for a, rs in algos.items()}
    rng = np.random.default_rng(SEED)

    fig, axes = plt.subplots(2, 2, figsize=(7.5, 5.8))
    for ax, (key, ylabel) in zip(axes.flat, panels):
        for i, (algo, ms) in enumerate(metrics.items()):
            st = style(algo)
            vals = np.array([m[key] for m in ms])
            ax.boxplot(vals, positions=[i], widths=0.5, showfliers=False,
                       medianprops={"color": "#2b2b29", "linewidth": 1.4},
                       boxprops={"color": REF_LINE}, whiskerprops={"color": REF_LINE},
                       capprops={"color": REF_LINE})
            x = i + rng.uniform(-0.17, 0.17, len(vals))
            ax.scatter(x, vals, s=20, color=st["color"], marker=st["marker"],
                       edgecolors="white", linewidths=0.6, alpha=0.9, zorder=3)
        ax.set_xticks(range(len(metrics)))
        ax.set_xticklabels(list(metrics))
        ax.set_ylabel(ylabel)
        ax.grid(axis="x", visible=False)
        if key == "retrans":
            ax.set_yscale("symlog", linthresh=1)
            ax.set_ylim(bottom=0)
        else:
            ax.set_ylim(bottom=0)
        if key == "goodput":
            # Label outside the plot area so it can't cover any points
            ax.axhline(STALL_MBPS, color=REF_LINE, linewidth=0.8, zorder=1)
            ax.text(1.01, STALL_MBPS, f"stalled\n< {STALL_MBPS:g}",
                    transform=ax.get_yaxis_transform(), ha="left", va="center",
                    fontsize=7, color=INK_MUTED)
    n_runs = len(next(iter(algos.values())))
    fig.suptitle(f"Per-run statistics: {n_servers} destinations × "
                 f"{n_runs // n_servers} repetitions = {n_runs} runs per algorithm")
    fig.tight_layout()
    return save(fig, out_dir, "fig3_aggregate")


def plot_startup(runs, out_dir):
    """First STARTUP_S seconds of every run. cwnd on a log axis shows where
    slow start ends; sRTT - min RTT shows the delay each algorithm saw then."""
    algos = by_algo(runs)
    fig, axes = plt.subplots(2, len(algos), figsize=(7.5, 4.8),
                             sharex=True, sharey="row", squeeze=False)
    for c, (algo, rs) in enumerate(algos.items()):
        color = style(algo)["color"]
        n_stalled = sum(stalled(r) for r in rs)
        axes[0][c].set_title(f"{algo}\n{n_stalled} of {len(rs)} runs stalled", fontsize=9)
        # Draw the other runs first so the stalled ones stay on top
        for r in sorted(rs, key=stalled):
            s = r["samples"]
            keep = s["t_s"] <= STARTUP_S + 1e-9
            t = s["t_s"][keep]
            lw, alpha = (1.4, 0.95) if stalled(r) else (0.8, 0.3)
            axes[0][c].plot(t, s["cwnd_segs"][keep], color=color, linewidth=lw, alpha=alpha)
            axes[1][c].plot(t, (s["srtt_ms"] - s["min_rtt_ms"])[keep], color=color,
                            linewidth=lw, alpha=alpha)
        axes[1][c].set_xlabel("Time (s)")
    for c, algo in enumerate(algos):
        ref = {"dart": (DART_HIGH_MAX_MS, "DART HIGH ≤ 20 ms"),
               "cubic": (HYSTART_MAX_MS, "HyStart ≤ 16 ms")}.get(algo)
        if ref:
            axes[1][c].axhline(ref[0], color="#2b2b29", linewidth=0.8, zorder=4)
            axes[1][c].text(STARTUP_S * 0.98, ref[0] * 1.15, ref[1], ha="right", va="bottom",
                            fontsize=7.5, color="#2b2b29", zorder=5,
                            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1,
                                  "alpha": 0.85})
    axes[0][0].set_yscale("log")
    axes[0][0].set_ylim(1, 1e4)
    axes[0][0].set_ylabel("cwnd (segments, log)")
    # symlog: linear below 10 ms, log above, so the 16-20 ms thresholds and the
    # 100+ ms delays are both readable
    for ax in axes[1]:
        ax.set_yscale("symlog", linthresh=10)
    axes[1][0].set_ylim(0, 1000)
    axes[1][0].set_ylabel("sRTT − min RTT (ms)")
    axes[-1][0].set_xlim(0, STARTUP_S)

    handles = [Line2D([], [], color=INK_MUTED, linewidth=1.4,
                      label=f"stalled run (mean goodput < {STALL_MBPS:g} Mbit/s)"),
               Line2D([], [], color=INK_MUTED, linewidth=0.8, alpha=0.3, label="other run")]
    fig.legend(handles=handles, ncol=2, loc="upper center", bbox_to_anchor=(0.5, 0.955),
               fontsize=8)
    fig.suptitle(f"First {STARTUP_S:g} s of every run (all destinations)", y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return save(fig, out_dir, "fig4_startup")


def write_aggregate_table(groups, out_dir):
    """Per algorithm over every server. Pooled columns come from
    plots.summary_row, so they match the ALL rows of plots.py's summaries."""
    every = [r for rs in groups.values() for r in rs]
    algos = by_algo(every)
    # Destinations where each algorithm had the highest mean goodput
    best = defaultdict(int)
    for rs in groups.values():
        per_algo = by_algo(rs)
        best[max(per_algo, key=lambda a: np.mean([run_mean(r) for r in per_algo[a]]))] += 1

    rng = np.random.default_rng(SEED)
    rows = []
    for algo, rs in algos.items():
        pooled = plots.summary_row("ALL", algo, rs)
        means = np.array([run_mean(r) for r in rs])
        boot = means[rng.integers(0, len(means), (BOOT_RESAMPLES, len(means)))].mean(axis=1)
        rows.append({
            "algo": algo,
            "runs": len(rs),
            "mean_run_mbps": means.mean(),
            "ci95_lo": np.percentile(boot, 2.5),
            "ci95_hi": np.percentile(boot, 97.5),
            "median_run_mbps": np.median(means),
            "p95_mbps": pooled["p95_mbps"],
            "median_srtt_ms": pooled["median_srtt_ms"],
            "p95_srtt_ms": pooled["p95_srtt_ms"],
            "median_queue_ms": pooled["median_queue_ms"],
            "p95_queue_ms": pooled["p95_queue_ms"],
            "retrans_per_run": pooled["mean_retrans_per_run"],
            "stalled_runs": sum(m < STALL_MBPS for m in means),
            "best_servers": best[algo],
        })

    cols = list(rows[0])
    csv_path = os.path.join(out_dir, "table_aggregate.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r[c]) for c in cols])

    md_path = os.path.join(out_dir, "table_aggregate.md")
    with open(md_path, "w") as f:
        f.write("| Algo | Runs | Mean goodput [95% CI] | Median run | p95 goodput "
                "| Median sRTT | p95 sRTT | Median sRTT − min | p95 sRTT − min "
                "| Retransmits / run | Stalled runs | Best on |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for r in rows:
            f.write(f"| {r['algo']} | {r['runs']} "
                    f"| {fmt(r['mean_run_mbps'])} [{fmt(r['ci95_lo'])}, {fmt(r['ci95_hi'])}] "
                    f"| {fmt(r['median_run_mbps'])} | {fmt(r['p95_mbps'])} "
                    f"| {fmt(r['median_srtt_ms'])} | {fmt(r['p95_srtt_ms'])} "
                    f"| {fmt(r['median_queue_ms'])} | {fmt(r['p95_queue_ms'])} "
                    f"| {fmt(r['retrans_per_run'])} | {r['stalled_runs']} "
                    f"| {r['best_servers']} of {len(groups)} |\n")
        f.write(f"\nGoodput in Mbit/s, RTT in ms. Each run is one 10 s test; "
                f"{len(groups)} destinations × 3 repetitions per algorithm. "
                f"Mean goodput = mean over runs of each run's mean goodput, with a "
                f"95% percentile-bootstrap CI ({BOOT_RESAMPLES} resamples of the runs, "
                f"seed {SEED}). Median run = median of the per-run means. p95 goodput, "
                f"sRTT and sRTT − min RTT are over all 0.2 s samples pooled (the same "
                f"numbers as the ALL rows of summary_goodput.md / summary_rtt_retrans.md); "
                f"percentiles use numpy's default linear interpolation. Retransmits are "
                f"segments (tcp_info total_retrans). Stalled = mean goodput < "
                f"{STALL_MBPS:g} Mbit/s. Best on = destinations where the algorithm had "
                f"the highest mean goodput.\n")
    return csv_path, md_path


def write_test_order(runs_path, used, out_dir):
    """Execution order from runs.csv, whose rows are written in that order."""
    rows = list(csv.DictReader(open(runs_path, newline="")))
    servers = defaultdict(list)
    for row in rows:
        servers[f'{row["server"]}:{row["port"]}'].append(row)

    base = None   # algorithm order of rep 0 at the first accepted server
    lines = []
    problems = []
    n = 0
    for label, srows in servers.items():
        if label not in used:
            continue
        n += 1
        cells = []
        retried = 0
        for rep in sorted({int(r["rep"]) for r in srows}):
            order = []
            for r in srows:
                if int(r["rep"]) != rep:
                    continue
                if r["status"] != "ok":
                    retried += 1
                    continue
                failed_before = any(int(x["rep"]) == rep and x["status"] != "ok"
                                    and x["algo_requested"] == r["algo_requested"]
                                    for x in srows)
                order.append(r["algo_requested"] + ("*" if failed_before else ""))
            plain = [a.rstrip("*") for a in order]
            if base is None:
                base = plain
            expected = base[rep % len(base):] + base[:rep % len(base)]
            if plain != expected:
                problems.append(f"{label} rep {rep + 1}: {plain} != {expected}")
            cells.append(" → ".join(order))
        first = srows[0]["start_utc"][11:19]
        lines.append(f"| {n} | {label} | {first} | " + " | ".join(cells) + f" | {retried} |")

    dropped = []
    for label, srows in servers.items():
        if label in used:
            continue
        ok = sum(r["status"] == "ok" for r in srows)
        errors = [r["error"] for r in srows if r["status"] != "ok"]
        dropped.append(f"| {label} | {srows[0]['start_utc'][11:19]} | {ok} | "
                       f"{len(srows) - ok} | {errors[-1] if errors else ''} |")

    n_ok = sum(r["status"] == "ok" for r in rows)
    n_used = sum(r["status"] == "ok" for r in rows if f'{r["server"]}:{r["port"]}' in used)
    path = os.path.join(out_dir, "table_test_order.md")
    with open(path, "w") as f:
        f.write(f"Batch: first test {rows[0]['start_utc']}, last test {rows[-1]['start_utc']}. "
                f"{len(servers)} destinations tried, {len(used)} completed every algorithm "
                f"× repetition and are used, {len(servers) - len(used)} dropped. "
                f"{len(rows)} test attempts: {n_ok} ok, {len(rows) - n_ok} failed; "
                f"{n_used} ok runs used, {n_ok - n_used} ok runs from dropped "
                f"destinations not used.\n\n")
        f.write("Destinations were drawn at random from listed_iperf3_servers.json and "
                "tested one at a time, in the order below. At each destination, every "
                "repetition runs all four algorithms back to back (1 s pause between "
                "tests), and the first algorithm rotates by one each repetition, so no "
                "algorithm is always first or last. A failed test is retried immediately; "
                "a destination is dropped if its first test fails or one test fails 3 "
                "times in a row. * marks a run that succeeded on a retry.\n\n")
        f.write("| # | Destination | First test (UTC) | "
                + " | ".join(f"Rep {i + 1}" for i in range(len(cells)))
                + " | Failed attempts |\n")
        f.write("|---:|---|---|" + "---|" * len(cells) + "---:|\n")
        f.write("\n".join(lines) + "\n\n")
        f.write("Dropped destinations (not plotted):\n\n")
        f.write("| Destination | First test (UTC) | Ok | Failed | Last error |\n")
        f.write("|---|---|---:|---:|---|\n")
        f.write("\n".join(dropped) + "\n")
        if problems:
            f.write("\nWARNING, order differs from the rotation:\n"
                    + "\n".join(f"- {p}" for p in problems) + "\n")
    for p in problems:
        print("WARNING: order differs from the rotation:", p)
    return path


def read_env_stats(env_path, key):
    with open(env_path) as f:
        for line in f:
            if line.startswith(key + ":"):
                return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}
    return {}


def write_dart_decisions(env_path, runs_path, out_dir):
    before = read_env_stats(env_path, "dart_stats_before")
    after = read_env_stats(env_path, "dart_stats_after")
    rows = list(csv.DictReader(open(runs_path, newline="")))
    dart = [r for r in rows if r["algo_requested"] == "dart"]
    dart_ok = sum(r["status"] == "ok" for r in dart)
    path = os.path.join(out_dir, "table_dart_decisions.md")
    with open(path, "w") as f:
        f.write("| Counter | Meaning | Count in this batch |\n|---|---|---:|\n")
        for name, meaning in DART_COUNTERS.items():
            if name in after:
                f.write(f"| {name} | {meaning} | {after[name] - before.get(name, 0)} |\n")
        f.write(f"\nDifference of /sys/module/tcp_dart/parameters/stats before and after "
                f"the batch (environment.txt). The counters are global, so they cover "
                f"every DART connection in the batch: {len(dart)} DART test attempts "
                f"({dart_ok} ok), including those on destinations that were later "
                f"dropped. They can't be split per run.\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs.csv")
    ap.add_argument("--samples", default="samples.csv")
    ap.add_argument("--env", default="environment.txt")
    ap.add_argument("--rep-server", required=True,
                    help="representative destination, as host:port")
    ap.add_argument("--out", default="report")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    runs = plots.load(args.runs, args.samples)
    if not runs:
        raise SystemExit("No successful runs with samples found.")
    groups = plots.by_server(runs)
    if args.rep_server not in groups:
        raise SystemExit(f"{args.rep_server} is not a complete destination; "
                         f"choose one of: {', '.join(groups)}")
    rep_runs = groups[args.rep_server]

    grid, goodput_top = plot_grid(args.rep_server, rep_runs, args.out)
    print(grid)
    print(plots.plot_scatter(args.rep_server, rep_runs, args.out,
                             goodput_max=goodput_top, png=True))
    print(plots.plot_timeseries(args.rep_server, rep_runs, args.out, png=True))
    every = [r for rs in groups.values() for r in rs]
    print(plot_aggregate(every, len(groups), args.out))
    print(plot_startup(every, args.out))
    for p in write_aggregate_table(groups, args.out):
        print(p)
    print(write_test_order(args.runs, set(groups), args.out))
    print(write_dart_decisions(args.env, args.runs, args.out))


if __name__ == "__main__":
    main()
