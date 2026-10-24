Batch: first test 2026-10-24T04:23:30+00:00, last test 2026-10-24T04:54:21+00:00. 20 destinations tried, 10 completed every algorithm × repetition and are used, 10 dropped. 153 test attempts: 131 ok, 22 failed; 120 ok runs used, 11 ok runs from dropped destinations not used.

Destinations were drawn at random from listed_iperf3_servers.json and tested one at a time, in the order below. At each destination, every repetition runs all four algorithms back to back (1 s pause between tests), and the first algorithm rotates by one each repetition, so no algorithm is always first or last. A failed test is retried immediately; a destination is dropped if its first test fails or one test fails 3 times in a row. * marks a run that succeeded on a retry.

| # | Destination | First test (UTC) | Rep 1 | Rep 2 | Rep 3 | Failed attempts |
|---:|---|---|---|---|---|---:|
| 1 | speedtest.sea11.us.leaseweb.net:5201 | 04:24:38 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic → reno | 0 |
| 2 | 185.180.12.40:5201 | 04:27:18 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic → reno | 0 |
| 3 | a209.speedtest.wobcom.de:5201 | 04:29:59 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic → reno | 0 |
| 4 | a204.speedtest.wobcom.de:5201 | 04:32:39 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic → reno | 0 |
| 5 | 148.230.60.200:30001 | 04:35:18 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic → reno | 0 |
| 6 | 213.158.175.240:5201 | 04:38:03 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic → reno | 0 |
| 7 | speedtest1.flowjamaica.com:5506 | 04:41:20 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic → reno | 0 |
| 8 | speedtest.syd12.au.leaseweb.net:5201 | 04:43:57 | cubic → reno → bbr* → dart | reno → bbr → dart* → cubic | bbr → dart → cubic → reno* | 3 |
| 9 | 185.76.9.135:5201 | 04:46:51 | cubic → reno → bbr → dart | reno → bbr → dart* → cubic* | bbr → dart → cubic → reno* | 3 |
| 10 | 105.235.237.2:5201 | 04:51:08 | cubic → reno → bbr → dart | reno → bbr → dart → cubic | bbr → dart → cubic* → reno* | 2 |

Dropped destinations (not plotted):

| Destination | First test (UTC) | Ok | Failed | Last error |
|---|---|---:|---:|---|
| speedtest.level66.services:5201 | 04:23:30 | 0 | 1 | Connection failed: [Errno 32] Broken pipe |
| speedtest.tyo11.jp.leaseweb.net:5201 | 04:23:33 | 4 | 3 | Server denied access (busy or rate-limited) |
| 185.152.66.67:5201 | 04:24:31 | 0 | 1 | Server denied access (busy or rate-limited) |
| iperf.online.net:5200 | 04:24:32 | 0 | 1 | Server denied access (busy or rate-limited) |
| 189.25.97.217:30001 | 04:24:34 | 0 | 1 | Connection failed: [Errno 32] Broken pipe |
| 217.61.40.96:5201 | 04:27:17 | 0 | 1 | Server denied access (busy or rate-limited) |
| 185.59.221.51:5201 | 04:29:58 | 0 | 1 | Server denied access (busy or rate-limited) |
| speedtest.shinternet.ch:5200 | 04:40:49 | 0 | 1 | Timed out waiting on the server |
| la.speedtest.clouvider.net:5200 | 04:49:30 | 7 | 3 | Server denied access (busy or rate-limited) |
| 66.35.27.207:30001 | 04:51:07 | 0 | 1 | Connection failed: [Errno 111] Connection refused |
