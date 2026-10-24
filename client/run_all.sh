#!/usr/bin/env bash
# Full pipeline: record environment -> run tests -> make plots.
#
# Usage: ./run_all.sh [-n SERVERS] [--duration SEC] [--interval SEC] [--reps N]
#                     [--servers FILE] [--max-attempts N] [--out DIR]
#                     [--algos LIST]
# dart needs tcp_dart.ko loaded first: sudo ../scripts/dart_module.sh load
set -euo pipefail
cd "$(dirname "$0")"

N=10
DURATION=10
INTERVAL=0.2
REPS=3
SERVERS=listed_iperf3_servers.json
MAX_ATTEMPTS=30
OUT="${RESULTS_DIR:-../results}"
ALGOS=cubic,reno,bbr,dart

while [ $# -gt 0 ]; do
    case "$1" in
        -n)             N="$2"; shift 2 ;;
        --duration)     DURATION="$2"; shift 2 ;;
        --interval)     INTERVAL="$2"; shift 2 ;;
        --reps)         REPS="$2"; shift 2 ;;
        --servers)      SERVERS="$2"; shift 2 ;;
        --max-attempts) MAX_ATTEMPTS="$2"; shift 2 ;;
        --out)          OUT="$2"; shift 2 ;;
        --algos)        ALGOS="$2"; shift 2 ;;
        -h|--help)  sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$OUT"
REPO="$(pwd)"

# 1. Record the host environment (the kernel decides TCP behavior, not the container)
{
    echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "kernel: $(uname -r)"
    echo "arch: $(uname -m)"
    echo "available_cc: $(cat /proc/sys/net/ipv4/tcp_available_congestion_control)"
    echo "allowed_cc: $(cat /proc/sys/net/ipv4/tcp_allowed_congestion_control)"
    echo "default_cc: $(cat /proc/sys/net/ipv4/tcp_congestion_control)"
    echo "python: $(python3 --version 2>&1)"
    echo "dart_module: $(cat /sys/module/tcp_dart/srcversion 2>/dev/null || echo not loaded)"
    echo "dart_stats_before: $(cat /sys/module/tcp_dart/parameters/stats 2>/dev/null)"
    echo "settings: n=$N duration=$DURATION interval=$INTERVAL reps=$REPS servers=$SERVERS max_attempts=$MAX_ATTEMPTS"
    echo "algos: $ALGOS (interleaved; the first algorithm rotates by one each rep)"
} > "$OUT/environment.txt"
cat "$OUT/environment.txt"

# Make sure every algorithm can actually be selected on a socket here
ALGOS="$ALGOS" python3 - <<'PY' || exit 1
import os, socket, sys, tcpinfo
bad = []
for algo in os.environ["ALGOS"].split(","):
    with socket.socket() as s:
        try:
            tcpinfo.set_congestion(s, algo)
        except OSError as e:
            bad.append(f"{algo} ({e.strerror})")
if bad:
    sys.exit("ERROR: can't select " + ", ".join(bad) + ".\n"
             "On the host: sudo ../scripts/dart_module.sh load "
             "(loads tcp_dart and tcp_bbr and allows both)")
PY

# 2. Run the tests (writes runs.csv and samples.csv into $OUT)
python3 "$REPO/runner.py" -n "$N" --duration "$DURATION" --interval "$INTERVAL" \
    --reps "$REPS" --servers "$SERVERS" --max-attempts "$MAX_ATTEMPTS" \
    --runs "$OUT/runs.csv" --samples "$OUT/samples.csv" --algos "$ALGOS"
echo "dart_stats_after: $(cat /sys/module/tcp_dart/parameters/stats 2>/dev/null)" \
    >> "$OUT/environment.txt"

# 3. Plots and summary tables
python3 "$REPO/plots.py" --runs "$OUT/runs.csv" --samples "$OUT/samples.csv" \
    --out "$OUT/plots"

echo "Done. Results in $OUT/"