#!/usr/bin/env bash
# Test C: DART on an emulated bottleneck, next to reno and cubic.
# Needs root; run it only inside the lab VM. The two network namespaces and
# the veth pair it creates are removed on exit; the host's own network and
# congestion-control settings are not touched.
#
#   netns dart-snd 10.77.0.1 [vsnd] ------ [vrcv] 10.77.0.2 netns dart-rcv
#   vsnd egress (data): netem rate RATE limit BUF loss LOSS  -> the bottleneck queue
#   vrcv egress (ACKs): netem delay RTT                      -> the propagation delay
#
# Usage: sudo scripts/netem_test.sh [--duration SEC] [--algos LIST] [--only NAME]
#                                   [--out DIR]
# Writes DIR/{runs.csv,samples.csv,summary.jsonl,kernel.log,report.txt}
# (runs.csv/samples.csv use the A2 format, so client/plots.py can plot them).
set -euo pipefail
cd "$(dirname "$0")/.."

DURATION=20
ALGOS=dart,reno,cubic
ONLY=
OUT=results/netem-$(date +%Y%m%d-%H%M%S)

# name      rate    rtt_ms  buffer_pkts  random_loss
SCENARIOS="
low_rtt     40mbit  10      500          0%
high_rtt    40mbit  80      1000         0%
queue       20mbit  20      1000         0%
loss        40mbit  40      300          0.5%
"

while [ $# -gt 0 ]; do
    case "$1" in
        --duration) DURATION="$2"; shift 2 ;;
        --algos)    ALGOS="$2"; shift 2 ;;
        --only)     ONLY="$2"; shift 2 ;;
        --out)      OUT="$2"; shift 2 ;;
        -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "needs root (network namespaces, tc): sudo $0" >&2
    exit 1
fi
for cmd in ip tc ethtool python3 journalctl; do
    command -v "$cmd" >/dev/null || { echo "missing command: $cmd" >&2; exit 1; }
done
case ",$ALGOS," in
    *,dart,*)
        if [ ! -d /sys/module/tcp_dart ]; then
            echo "tcp_dart is not loaded: sudo scripts/dart_module.sh load" >&2
            exit 1
        fi ;;
esac

S="ip netns exec dart-snd"
R="ip netns exec dart-rcv"
DEBUG_PARAM=/sys/module/tcp_dart/parameters/debug
DEBUG_WAS=

cleanup() {
    for ns in dart-snd dart-rcv; do
        ip netns pids "$ns" 2>/dev/null | xargs -r kill 2>/dev/null || true
        ip netns del "$ns" 2>/dev/null || true
    done
    if [ -n "$DEBUG_WAS" ]; then
        echo "$DEBUG_WAS" > "$DEBUG_PARAM"
    fi
    if [ -n "${SUDO_USER:-}" ] && [ -d "$OUT" ]; then
        chown -R "$SUDO_USER": "$OUT"
    fi
}
trap cleanup EXIT

mkdir -p "$OUT"
START=$(date +%s)
TAINT_BEFORE=$(cat /proc/sys/kernel/tainted)

# Topology
ip netns add dart-snd
ip netns add dart-rcv
ip link add vsnd netns dart-snd type veth peer name vrcv netns dart-rcv
$S ip addr add 10.77.0.1/24 dev vsnd
$R ip addr add 10.77.0.2/24 dev vrcv
for ns in "$S" "$R"; do $ns ip link set lo up; done
$S ip link set vsnd up
$R ip link set vrcv up
# One MSS per packet, so netem's limit and loss count real packets
$S ethtool -K vsnd tso off gso off >/dev/null
$R ethtool -K vrcv tso off gso off gro off >/dev/null 2>&1 || true
# Each connection starts fresh: no ssthresh/RTT cached from the previous run
$S sysctl -q -w net.ipv4.tcp_no_metrics_save=1

# Log DART's slow-start exits, decreases and losses during the test
if [ -w "$DEBUG_PARAM" ]; then
    DEBUG_WAS=$(cat "$DEBUG_PARAM")
    echo 1 > "$DEBUG_PARAM"
fi

$R python3 scripts/dart_flow.py sink --bind 10.77.0.2 --port 5299 &
for _ in $(seq 50); do
    $S bash -c 'echo > /dev/tcp/10.77.0.2/5299' 2>/dev/null && break
    sleep 0.1
done

{
    echo "date_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "kernel: $(uname -r)"
    echo "dart_module: $(cat /sys/module/tcp_dart/srcversion 2>/dev/null || echo not loaded)"
    echo "settings: duration=$DURATION algos=$ALGOS (run in this order per scenario)"
    echo "scenarios (name rate rtt_ms buffer_pkts loss):$SCENARIOS"
} > "$OUT/environment.txt"

echo "$SCENARIOS" | while read -r name rate rtt buf loss; do
    [ -n "$name" ] || continue
    [ -z "$ONLY" ] || [ "$ONLY" = "$name" ] || continue
    echo "=== $name: $rate, RTT ${rtt} ms, buffer $buf packets, loss $loss"
    $S tc qdisc replace dev vsnd root netem rate "$rate" limit "$buf" loss "$loss"
    $R tc qdisc replace dev vrcv root netem delay "${rtt}ms" limit 100000
    for algo in ${ALGOS//,/ }; do
        $S ip tcp_metrics flush all 2>/dev/null || true
        $S python3 scripts/dart_flow.py send --host 10.77.0.2 --port 5299 \
            --algo "$algo" --duration "$DURATION" --label "$name" \
            --samples "$OUT/samples.csv" --runs "$OUT/runs.csv" \
            --summary "$OUT/summary.jsonl" < /dev/null > "$OUT/$name-$algo.json" \
            || echo "    $algo: transfer failed (see $OUT/$name-$algo.json)"
        echo "    $algo done"
        sleep 1
    done
done

journalctl -k -q --no-pager -o short-monotonic --since "@$START" > "$OUT/kernel.log" || true
set +e
{
    python3 scripts/dart_flow.py report "$OUT/summary.jsonl" --kernel-log "$OUT/kernel.log" \
        && rc=0 || rc=$?
    echo
    echo "Kernel health"
    if grep -Eq "WARNING|BUG:|Oops|Call Trace" "$OUT/kernel.log"; then
        echo "  [FAIL] kernel log has warnings: see $OUT/kernel.log"
        rc=1
    else
        echo "  [PASS] no WARNING/BUG/Oops in the kernel log"
    fi
    echo "  tainted before=$TAINT_BEFORE after=$(cat /proc/sys/kernel/tainted)"
    exit $rc
} | tee "$OUT/report.txt"
rc=${PIPESTATUS[0]}
set -e

if python3 -c "import matplotlib" 2>/dev/null; then
    python3 client/plots.py --runs "$OUT/runs.csv" --samples "$OUT/samples.csv" \
        --out "$OUT/plots" >/dev/null && echo "Plots: $OUT/plots"
fi
echo "Results: $OUT"
exit "$rc"
