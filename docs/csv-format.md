# CSV output format

Two files. `run_id` links them.

## runs.csv — one row per test attempt (written by the runner)

| column | meaning |
|---|---|
| run_id | unique id, e.g. `20261021T041200-cubic-r1-s3` |
| server | host / IP from the server list |
| port | port actually used |
| algo_requested | `cubic` / `reno` / `bbr` / `dart` |
| algo_actual | what `tcpinfo.get_congestion()` reported on the data socket (empty if the run failed before connecting; a mismatch with algo_requested fails the run) |
| rep | repetition number (0..N-1, when plotting this will be readjusted to 1-base) |
| start_utc | ISO timestamp when the test started |
| status | `ok` or `failed` |
| error | short reason if failed (timeout, access denied, DNS, ...), else empty |

Example:
```
run_id,server,port,algo_requested,algo_actual,rep,start_utc,status,error
20261021T041200-cubic-r1-s3,speedtest.chi11.us.leaseweb.net,5201,cubic,cubic,1,2026-10-21T04:12:00Z,ok,
20261021T041300-cubic-r1-s4,41.110.39.130,5201,cubic,,1,2026-10-21T04:13:00Z,failed,access denied (server busy)
```

Rows are written in execution order. Within each repetition the algorithms
are interleaved, and the starting algorithm rotates by one every repetition
(rep 0: cubic, reno, bbr, dart; rep 1: reno, bbr, dart, cubic; ...).

## samples.csv — one row per sampling interval during a run (written inside run_test)

| column | unit | source (tcp_info) |
|---|---|---|
| run_id | | |
| t_s | seconds since data started | |
| bytes_acked | bytes, cumulative | `bytes_acked` |
| goodput_mbps | Mbit/s over the last interval | Δbytes_acked × 8 / Δt / 1e6 |
| cwnd_segs | segments | `snd_cwnd` |
| srtt_ms | ms | `rtt` / 1000 |
| rttvar_ms | ms | `rttvar` / 1000 |
| min_rtt_ms | ms | `min_rtt` / 1000 |
| total_retrans | segments, cumulative | `total_retrans` |
| retrans_delta | segments in this interval | Δtotal_retrans |
| pacing_rate_mbps | Mbit/s | `pacing_rate` × 8 / 1e6 |

Example:
```
run_id,t_s,bytes_acked,goodput_mbps,cwnd_segs,srtt_ms,rttvar_ms,min_rtt_ms,total_retrans,retrans_delta,pacing_rate_mbps
20261021T041200-cubic-r1-s3,0.2,1048576,41.9,40,21.3,4.1,18.9,0,0,98.2
20261021T041200-cubic-r1-s3,0.4,2359296,52.4,58,23.8,3.6,18.9,0,0,121.7
```

## run_test interface (proposal)

```python
result = run_test(addr, port, duration, algo, interval, run_id, samples_writer)
# samples_writer: a csv.writer (or DictWriter) for samples.csv; run_test appends rows to it
# returns {"status": "ok" | "failed", "error": str | None, "algo_actual": str | None}
```
The runner writes the runs.csv row from the returned dict.
Kernel version (`uname -r`) and the duration/interval used get recorded once per batch, not per row.

## Extra columns from scripts/dart_flow.py

`dart_flow.py send` (used by `scripts/netem_test.sh`) writes the same
columns, plus:

| column | unit | source (tcp_info) |
|---|---|---|
| ssthresh_segs | segments | `snd_ssthresh` (2147483647 = not set yet, i.e. initial slow start) |
| ca_state | 0 Open, 1 Disorder, 2 CWR, 3 Recovery, 4 Loss | `ca_state` |

`plots.py` ignores columns it doesn't know, so it plots these files too.
