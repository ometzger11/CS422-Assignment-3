# CS422-Assignment-3: DART TCP congestion control

DART (Delay-Aware Reno with Throughput feedback) is a Linux TCP congestion
control module. It is Reno with two signals checked once per RTT: queueing
delay above the connection's minimum RTT, and whether the delivery rate is
still improving. It grows while queueing is low, holds while it is moderate,
and backs off by 10% when the queue is high and throughput has stopped
improving. On loss, ssthresh becomes 70% of cwnd, and the Linux core runs
the usual recovery. See [docs/algorithm.md](docs/algorithm.md).

Built and tested on Ubuntu 26.04.1 with kernel `7.0.0-38-generic` (x86_64, KVM
guest); see [Tested environment](#tested-environment) and
[Required kernel features](#required-kernel-features).
[docs/verification.md](docs/verification.md) lists what has been verified.

## Layout

```
tcp_dart.c              the module (registers "dart")
Makefile                make -> tcp_dart.ko (and hello.ko); make sim -> userspace tests
hello.c                 hello-world module from the setup step
tests/dart_sim.c        unit tests + packet-level simulation of tcp_dart.c (no root)
tests/shim/             minimal stand-in for the kernel headers, used only by dart_sim
scripts/dart_module.sh  load / unload / status
scripts/dart_flow.py    loopback self-test (Tests A, B); sender/receiver for Test C
scripts/netem_test.sh   Test C: emulated bottleneck with network namespaces + netem
client/                 the A2 iperf3 client, runner and plots, with dart added
client/report_plots.py  report figures and tables (see Report figures)
results/a3/             the A3 experiment: raw CSVs, plots/ (every server), report/
docs/algorithm.md       how DART works, and its known limitations
docs/verification.md    tests run, results, open items
docs/csv-format.md      runs.csv / samples.csv columns
```

`client/` is a copy of `CS422-Assignment-2` at commit `160f87e`. A2's own
repository and results are unchanged. To see exactly what changed for A3:
`for f in client/*; do diff -u "../CS422-Assignment-2/${f#client/}" "$f"; done`.

## Requirements

- Headers for the running kernel (`linux-headers-$(uname -r)`), `gcc`, `make`.
- Python 3. For plots: `sudo apt install python3-matplotlib` (pulls in numpy).
- For Test C: `ip`, `tc` (iproute2) and `ethtool`.

## Tested environment

| | |
|---|---|
| OS | Ubuntu 26.04.1 LTS, x86_64, KVM/QEMU guest behind libvirt NAT |
| Kernel | `7.0.0-38-generic` (`#38-Ubuntu SMP PREEMPT_DYNAMIC Fri Sep  4 09:10:14 UTC 2026`) |
| Headers | `linux-headers-7.0.0-38-generic` |
| Compiler | gcc 15.2.0 (`Ubuntu 15.2.0-16ubuntu1`), the same version that built the kernel |
| Python | 3.14.4; matplotlib 3.10.7 and numpy 2.3.5 from `python3-matplotlib` |
| Module | `tcp_dart.ko` srcversion `FCCEEC0EB4834471791D5A8`, the build used for every result in `results/` |

## Required kernel features

| Feature | Why | On the tested kernel |
|---|---|---|
| `CONFIG_MODULES`, `CONFIG_MODULE_UNLOAD` | load and unload `tcp_dart.ko` | `y`, `y` |
| `CONFIG_TCP_CONG_ADVANCED` | pluggable congestion control (`tcp_register_congestion_control`) | `y` |
| `CONFIG_TCP_CONG_CUBIC`, `CONFIG_TCP_CONG_BBR` | the CUBIC and BBR baselines | `y`, `m` (`dart_module.sh load` runs `modprobe tcp_bbr`) |
| `CONFIG_NET_NS`, `CONFIG_VETH`, `CONFIG_NET_SCH_NETEM` | Test C only (`netem_test.sh`) | `y`, `m`, `m` |
| Unsigned modules allowed | `tcp_dart.ko` is not signed | `CONFIG_MODULE_SIG=y` but `CONFIG_MODULE_SIG_FORCE` unset; lockdown `none` (no Secure Boot) |
| Kernel headers for the running kernel | out-of-tree build (`make -C /lib/modules/$(uname -r)/build`) | installed |

Check a machine with
`grep -E 'CONFIG_(MODULES|MODULE_UNLOAD|TCP_CONG_ADVANCED|TCP_CONG_BBR|MODULE_SIG_FORCE)=' /boot/config-$(uname -r)`
and `cat /sys/kernel/security/lockdown`.

- **Secure Boot.** Loading the unsigned module taints the kernel (`O` out-of-tree
  plus `E` unsigned, 12288 in `/proc/sys/kernel/tainted`), which is harmless. With
  Secure Boot on, lockdown refuses unsigned modules: either sign the module with an
  enrolled MOK key (`kmodsign sha512 MOK.priv MOK.der tcp_dart.ko`) or turn Secure
  Boot off in the VM.
- **Kernel version.** The newest kernel API DART uses is the
  `tcp_snd_cwnd()`/`tcp_snd_cwnd_set()` pair, added in Linux 5.18. Older kernels
  will not build it. Only `7.0.0-38-generic` has been tested; `make` builds for the
  running kernel, and `vermagic` must match it.
- **BTF.** Kbuild skips BTF generation for the module because `pahole` is not
  installed. DART doesn't use BTF, so this changes nothing.

**Root is needed only for** loading and unloading the module and for Test C.
Do both only inside the lab VM, and take a VM snapshot first: a bug in a
kernel module can crash the whole machine. Nothing here changes the default
congestion control or anything that survives a reboot.

## Build

```sh
make          # tcp_dart.ko (+ hello.ko) for the running kernel
make sim      # userspace unit tests + simulation of tcp_dart.c, no root
make clean
```

To build for a different kernel tree: `make KDIR=/path/to/build`.

## Load and unload

```sh
sudo scripts/dart_module.sh load     # insmod tcp_dart.ko, modprobe tcp_bbr,
                                     # add bbr and dart to the allowed list
scripts/dart_module.sh status        # loaded? allowed? DART's counters
sudo scripts/dart_module.sh unload   # remove dart from the allowed list, rmmod
```

The script does the following by hand:

```sh
sudo insmod tcp_dart.ko
sudo modprobe tcp_bbr
sudo sysctl -w net.ipv4.tcp_allowed_congestion_control="$(cat /proc/sys/net/ipv4/tcp_allowed_congestion_control) bbr dart"
cat /proc/sys/net/ipv4/tcp_available_congestion_control    # ... dart
# later
sudo rmmod tcp_dart    # refuses while any socket still uses dart
```

Without the allowed-list step, only root (`CAP_NET_ADMIN`) can select `dart`
on a socket.

## Using DART from Python

```python
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, b"dart")   # before connect()
s.connect((host, port))
s.getsockopt(socket.IPPROTO_TCP, socket.TCP_CONGESTION, 16)        # b'dart\x00...'
```

The A2 client already does this on the iperf3 **data** socket (the control
socket keeps the default). It records the algorithm Linux reports in
`runs.csv` (`algo_actual`), and now fails the run if that differs from the
one requested.

## Tests

| Test | Command | Root |
|---|---|---|
| Logic: unit tests + simulated bottleneck | `make sim` | no |
| A + B: registered, selectable, loopback transfer, kernel log clean | `python3 scripts/dart_flow.py selftest` | no (module loaded) |
| C: emulated bottleneck, dart vs reno vs cubic | `sudo scripts/netem_test.sh` | yes |
| D: A2 pipeline with four algorithms | `cd client && ./run_all.sh ...` | no (module loaded) |

Test C runs four scenarios: 10 ms RTT, 80 ms RTT, a 20 Mbit/s link with a
deep buffer, and 0.5% random loss. Each runs 20 s per algorithm, about 5
minutes in total. Results go to `results/netem-<time>/`: `report.txt`, plots
(if matplotlib is installed), and the kernel log with DART's debug lines.
Use `--only queue` or `--duration 10` for quicker runs.

Test D: smoke test first, then the full run:

```sh
cd client
./run_all.sh -n 1 --reps 1 --out ../results/smoke          # 1 server, 1 rep, 4 x 10 s
./run_all.sh -n 10 --reps 3 --out ../results/a3            # the real experiment
```

`run_all.sh` defaults to 10-second tests, a 0.2 s sampling interval, and
`cubic,reno,bbr,dart`. Algorithms are interleaved within each repetition,
and the first algorithm rotates every repetition, so no algorithm always runs
first or last. `runs.csv` rows are in execution order. `environment.txt`
records the kernel, the DART module version, and DART's counters before and
after the batch.

## Report figures

The report's figures and tables come from the committed `results/a3` data
(10 destinations × 3 repetitions × 4 algorithms) and need no root:

```sh
cd client
python3 plots.py --runs ../results/a3/runs.csv --samples ../results/a3/samples.csv \
    --out ../results/a3/plots                      # every server, plus summary tables
python3 report_plots.py --runs ../results/a3/runs.csv --samples ../results/a3/samples.csv \
    --env ../results/a3/environment.txt --rep-server 185.76.9.135:5201 \
    --out ../results/a3/report                     # report figures (.pdf + .png) and tables
```

[results/a3/report/README.md](results/a3/report/README.md) lists every figure and
table, the function that draws it, and a draft caption.

## Watching DART

```sh
cat /sys/module/tcp_dart/parameters/stats                  # decision counters, all DART sockets
ss -tin dst <server>                                       # cwnd, ssthresh, rtt of live sockets
echo 1 | sudo tee /sys/module/tcp_dart/parameters/debug    # log ss_exit/decrease/loss (rate-limited)
journalctl -k -f | grep tcp_dart
echo 0 | sudo tee /sys/module/tcp_dart/parameters/debug
```

The counters are explained in [docs/algorithm.md](docs/algorithm.md#decisions).
