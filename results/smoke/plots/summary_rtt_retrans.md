| Server | Algo | Runs | Median sRTT | p95 sRTT | Median queue | p95 queue | Retransmits / run |
|---|---|---:|---:|---:|---:|---:|---:|
| 102.214.66.19:5201 | cubic | 1 | 326.58 | 379.73 | 106.58 | 159.73 | 19.00 |
| 102.214.66.19:5201 | reno | 1 | 325.16 | 369.66 | 101.33 | 145.59 | 13.00 |
| 102.214.66.19:5201 | bbr | 1 | 219.67 | 333.76 | 2.67 | 97.92 | 217.00 |
| 102.214.66.19:5201 | dart | 1 | 312.38 | 378.67 | 99.99 | 166.67 | 16.00 |

RTTs in ms over all samples. Queue = smoothed RTT minus the connection's min RTT (an estimate of queueing delay). Retransmits are segments, from tcp_info total_retrans.
