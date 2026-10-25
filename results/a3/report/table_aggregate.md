| Algo | Runs | Mean goodput [95% CI] | Median run | p95 goodput | Median sRTT | p95 sRTT | Median sRTT − min | p95 sRTT − min | Retransmits / run | Stalled runs | Best on |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| cubic | 30 | 31.18 [19.09, 44.96] | 16.29 | 127.58 | 209.61 | 671.29 | 72.64 | 583.60 | 7.47 | 8 | 0 of 10 |
| reno | 30 | 57.90 [47.20, 69.89] | 46.48 | 154.47 | 232.90 | 543.79 | 72.05 | 429.47 | 17.07 | 0 | 5 of 10 |
| bbr | 30 | 60.37 [45.93, 75.60] | 46.85 | 164.07 | 202.22 | 455.61 | 45.80 | 337.16 | 46.70 | 1 | 5 of 10 |
| dart | 30 | 28.52 [16.03, 42.82] | 7.10 | 135.58 | 205.11 | 478.36 | 69.46 | 293.20 | 3.80 | 15 | 0 of 10 |

Goodput in Mbit/s, RTT in ms. Each run is one 10 s test; 10 destinations × 3 repetitions per algorithm. Mean goodput = mean over runs of each run's mean goodput, with a 95% percentile-bootstrap CI (10000 resamples of the runs, seed 422). Median run = median of the per-run means. p95 goodput, sRTT and sRTT − min RTT are over all 0.2 s samples pooled (the same numbers as the ALL rows of summary_goodput.md / summary_rtt_retrans.md); percentiles use numpy's default linear interpolation. Retransmits are segments (tcp_info total_retrans). Stalled = mean goodput < 5 Mbit/s. Best on = destinations where the algorithm had the highest mean goodput.
