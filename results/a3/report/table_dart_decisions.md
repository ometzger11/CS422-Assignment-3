| Counter | Meaning | Count in this batch |
|---|---|---:|
| rounds | rounds evaluated | 1819 |
| few_samples | fewer than 3 RTT samples: Reno-style round | 150 |
| slow_start | stayed in slow start | 187 |
| ss_exit | left slow start: queue_delay >= HIGH | 28 |
| grow | Rule A: cwnd += 2 | 504 |
| not_cwnd_limited | Rule A, but not cwnd-limited: no growth | 69 |
| hold | Rule B: hold | 166 |
| decrease | Rule C, rate plateaued: cwnd x 0.9 | 397 |
| hold_rate_up | Rule C, rate still improving: hold | 199 |
| hold_no_rate | Rule C, no valid rate to compare: hold | 119 |
| loss | ssthresh() calls (loss or ECN) | 28 |

Difference of /sys/module/tcp_dart/parameters/stats before and after the batch (environment.txt). The counters are global, so they cover every DART connection in the batch: 35 DART test attempts (33 ok), including those on destinations that were later dropped. They can't be split per run.
