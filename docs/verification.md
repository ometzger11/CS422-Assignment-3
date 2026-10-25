# DART verification report

Environment: Ubuntu 26.04.1, kernel `7.0.0-38-generic` (x86_64, KVM/QEMU
guest), gcc 15.2.0, Python 3.14.4. Checks were run on 2026-10-24.

**Status in one line:** the module builds cleanly, its logic passes every
unit and simulation check, and the real module (srcversion
`FCCEEC0EB4834471791D5A8`) has been loaded and run: Test C (netem) passed
and Test D (the A3 experiment) completed on 2026-10-24. Sections 1–3 were
written before the module was loaded; section 4 records the real-kernel
runs.

## 1. Build and static checks: PASS

| Command | Result |
|---|---|
| `make clean && make` | builds `hello.ko` and `tcp_dart.ko`, no compiler warnings |
| `make W=1` (extra warnings) | no warnings in `tcp_dart.c` |
| `scripts/checkpatch.pl --strict --file tcp_dart.c` | 0 errors, 0 warnings, 0 checks |
| `modinfo tcp_dart.ko` | `vermagic: 7.0.0-38-generic`, license GPL, params `stats`, `debug` |
| `nm -u tcp_dart.ko`, checked against `Module.symvers` | every import exists in this kernel: `tcp_register/unregister_congestion_control`, `tcp_slow_start`, `tcp_reno_undo_cwnd` (GPL-only, hence `MODULE_LICENSE("GPL")`), `sysfs_emit_at`, `param_ops_bool`, `_printk`, `___ratelimit` |
| `BUILD_BUG_ON(sizeof(struct dart) > ICSK_CA_PRIV_SIZE)` | compiles: the 32-byte state fits in the 104-byte area |

The only build messages are Kbuild's environment notes: the compiler binary
name differs from the one that built the kernel (same gcc 15.2.0), and BTF
is skipped because `pahole` isn't installed. Neither affects the module.

The callbacks and their semantics were checked against the Linux 7.0
sources (`tcp_cong.c`, `tcp_input.c`, `tcp_output.c`, `tcp_vegas.c`) and the
installed 7.0.0-38 headers. Only headers are installed locally.

## 2. Logic tests in userspace (`make sim`): PASS, 168/168 checks

`tests/dart_sim.c` compiles the **real** `tcp_dart.c` against a small mock
of the kernel API (`tests/shim/`, with helpers copied from Linux 7.0). It
then calls DART's callbacks the way the TCP core does.

**Unit tests (15, all pass):** registration and required ops;
LOW/HIGH values, and HIGH > LOW for base RTTs from 0 to 5 s; ssthresh =
max(2, ⌊0.7·cwnd⌋), including cwnd = 2³¹−1 with no overflow; undo; RTT
sample filtering (`rtt_us = -1` ignored, 0 µs accepted, min filters); Rule A
(+2 once per round, not when application-limited, respects
`snd_cwnd_clamp`); Rule B (hold, both boundaries inclusive); Rule C
(plateau → ⌊0.9·cwnd⌋ exactly once per round, ssthresh lowered so slow start
doesn't restart; +25% → hold; exactly +5% → decrease; +5.25% → hold; no
previous rate → hold); floor of 2 and growth back afterwards; slow start
(doubles below HIGH, exits at HIGH without cutting or growing on that ACK,
no mid-round growth afterwards); fewer than 3 samples → Reno +1 / plain
slow start; state resets (Disorder keeps the round, CWR/Recovery/Loss reset
it, base RTT kept); `TX_START`/`CWND_RESTART` reset, `COMPLETE_CWR` does
not; Loss state only slow-starts to ssthresh; sequence and `delivered`
counter wraparound; read-only `stats` parameter.

**Packet-level scenarios (7, all pass):** one DART sender, a FIFO
bottleneck, ACKs per packet, and simplified fast recovery and RTO that call
`ssthresh`/`set_state` like `tcp_input.c`. The pass criteria were written
before the first run. Two were wrong and were changed after it. "Rule A must
run in the constant-capacity scenarios" is not true: DART settles in Rule B
there. "No loss at all with a deep buffer" failed because of the slow-start
overshoot below; it became "no loss after slow start".

| Scenario | Results (from `make sim`) |
|---|---|
| low_rtt: 40 Mbit/s, 10 ms, 500-packet buffer | no loss; slow-start exit at cwnd 320; 17 decreases; then hold; second half: 100% utilization, median queue 5.1 ms (HIGH = 5 ms) |
| high_rtt: 40 Mbit/s, 80 ms, 1000 packets | slow-start overshoot to cwnd 1280 → 1 loss at 1.1 s; then 100% utilization, median queue 5.7 ms (LOW 4 / HIGH 12 ms), no further loss |
| shallow_buffer: 15 packets | 1 loss in slow start; afterwards holds below the overflow point: 100% utilization, no more loss |
| random_loss: 0.5% | 100 losses, every ssthresh = ⌊0.7·cwnd⌋; median cwnd 33 (an AIMD(2, 0.3) estimate gives about 34) |
| cross_traffic: 20 Mbit/s CBR during 15–25 s | cwnd drops while the CBR runs, queue stays ≤ 1.5·HIGH, grows back after; no loss after slow start |
| delay_pulse: +25 ms path delay during 10–15 s | cwnd 80 → 2–4 during the pulse (DART can't tell this delay from its own queue); back to 79 one second after the pulse |
| app_limited: application sends 12 of 40 Mbit/s | no growth, no decrease, no loss; delivers exactly the application rate |

The following properties are checked on every simulated ACK: cwnd ≥ 2
outside Loss, no `tcp_snd_cwnd_set(≤0)`, every ssthresh correct, and
decreases at least one RTT apart.

**Mutation check.** Five deliberately broken copies of `tcp_dart.c` were
all caught (failing checks in brackets): loss factor 0.5 (10),
plateau test inverted (24), Rule A ignoring cwnd-limited (2), no ssthresh
adjustment after a decrease (10), decisions on every ACK (48).

**What the simulation does not cover:** delayed ACKs, PRR, SACK details,
TSO/GRO, pacing, and timing in a real kernel. It shows the logic does what
the design says; the netem test (Test C) is still needed for real-kernel
behaviour.

## 3. Python integration: PASS without dart

| Check | Result |
|---|---|
| `python3 scripts/dart_flow.py selftest --algo reno` (and `--algo cubic`) | PASS: 256 MiB (cubic: 128 MiB) over loopback, byte count + SHA-256 match, algorithm confirmed with `getsockopt`, cwnd within bounds, RTT measured, no kernel WARN |
| `python3 scripts/dart_flow.py selftest` (dart) | FAIL as expected: `dart` is not in `tcp_available_congestion_control` until the module is loaded |
| `dart_flow.py sink` / `send` / `report` on loopback | samples and runs CSVs in the A2 format, JSON summaries, report checks run |
| `client/runner.py -n 1 --reps 2 --duration 10 --algos cubic,reno` | real 10 s tests against `185.93.2.193:5201`: 4/4 ok, `algo_actual` recorded, order rotated (rep 0: cubic, reno; rep 1: reno, cubic). The first server picked refused (busy) and was skipped, as in A2. Kept out of `results/`: it has no dart or bbr runs. |
| `client/plots.py` on A2's committed results (3 algorithms, 10 servers) | all plots and both summary tables, including ALL rows |
| `client/plots.py` on a 4-algorithm rendering fixture | the dart style, the per-algorithm scatter rows and the comparison chart render correctly. The fixture relabels real cubic/reno rows; it is not a measurement and is not in the repo. |
| `bash -n` on all shell scripts, `py_compile` on all Python | OK |

matplotlib and numpy are not installed on this VM. The plot checks used
PyPI wheels (hash-verified) unpacked into a scratch directory on
`PYTHONPATH`; nothing was installed system-wide.

## 4. Real-kernel runs (2026-10-24): PASS

The module was loaded with `sudo scripts/dart_module.sh load`. Every
`environment.txt` under `results/` records `available_cc: reno cubic dart bbr`
and module srcversion `FCCEEC0EB4834471791D5A8`, the same as the built
`tcp_dart.ko` (`modinfo -F srcversion tcp_dart.ko`).

| Acceptance item | Status | Evidence |
|---|---|---|
| `make` builds `tcp_dart.ko` for the running kernel | **done** | section 1; srcversion matches the loaded module |
| module registers; `dart` in available list | **done** | `results/*/environment.txt` |
| a Python data socket selects `dart`; Linux reports `dart` | **done** | `results/a3/runs.csv`: `algo_actual` = `algo_requested` in all 131 ok runs (33 of them dart) |
| a transfer completes correctly with dart, no kernel warnings | **done** | `results/netem-20261024-041537/report.txt`: all bytes delivered in every scenario; no WARNING/BUG/Oops in the kernel log |
| RTT measurements collected, rounds evaluated | **done** | netem checks "DART evaluated rounds"; A3 batch: 1819 rounds (`results/a3/report/table_dart_decisions.md`) |
| reacts to queueing delay | **done** | netem `queue` scenario: median queueing delay 5.98 ms vs 1228 ms (reno) and 489 ms (cubic), same 19.09 Mbit/s |
| 0.7 loss response | **done** | netem: all 42 logged losses have ssthresh = max(2, floor(0.7 × cwnd)) |
| experiments and plots | **done** | `results/a3`: 10 destinations × 3 reps × 4 algorithms (120 runs used); `results/a3/plots`, `results/a3/report` |
| Tests A + B (`dart_flow.py selftest`) with the module loaded | **output not saved** | re-run at the demo (section 5) |

The A3 internet runs also showed a behaviour the simulator could not:
non-congestive RTT inflation at low sending rates on this VM's access link,
which makes DART (and CUBIC's HyStart) stall in some runs. See
[algorithm.md](algorithm.md#known-behaviour-and-limitations) and
`results/a3/report/README.md`.

## 5. Demo checklist (in the lab VM, after a snapshot)

```sh
cd ~/cs422/kernel-module
make && make sim                                   # expect "168 checks, 0 failed"
sudo scripts/dart_module.sh load                   # [root] insmod + allow dart, bbr
scripts/dart_module.sh status                      # loaded, allowed, counters
python3 scripts/dart_flow.py selftest              # Tests A + B, expect RESULT: PASS
cd client && ./run_all.sh -n 1 --reps 1 --out /tmp/demo   # 4 x 10 s live tests (optional)
```

## 6. Finding to decide on: slow-start overshoot

Checking the slow-start delay only at round boundaries, as the design
specifies, reacts about one round late. Round *k*'s RTT samples come from
packets sent during round *k−1*, and the earliest of those were sent before
the queue built up. By then cwnd has doubled twice. In simulation, DART
left slow start at cwnd 320 (BDP 34 packets), 1280 (BDP 267) and 640 (BDP
134 and 67). The last two overflowed the 1000- and 500-packet buffers, with
one loss in each of high_rtt, cross_traffic and delay_pulse.

A one-block variant that leaves slow start mid-round, as soon as it has 3
samples all ≥ HIGH, exited at 163 / 643 / 323 and had no slow-start loss in
those scenarios. It was tested only in a scratch copy. Adopting it would
change the design ("check delay feedback at round boundaries"), so it is
left for you to decide; `docs/algorithm.md` describes both.
