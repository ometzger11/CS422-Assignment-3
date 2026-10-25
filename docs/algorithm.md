# DART: Delay-Aware Reno with Throughput feedback

DART is a small TCP congestion control module (`tcp_dart.c`, registered as
`dart`). It keeps Reno's structure — slow start, additive increase, and the
Linux TCP core's normal loss recovery — and adds two signals that are checked
once per round trip:

- **queueing delay**: how far this round's minimum RTT is above the smallest
  RTT ever seen on the connection;
- **delivery rate**: how many packets were delivered this round, per second.

Little queueing means DART grows. Moderate queueing means it holds. High
queueing *and* no throughput gain from it means DART backs off by 10% before
the queue overflows.

It does not model bandwidth or pace packets like BBR, and it does not try to
classify losses.

## Where DART plugs into Linux

DART is a `struct tcp_congestion_ops` with `cong_avoid`, so the Linux core
keeps doing fast retransmit, PRR, RTO, and undo. DART only answers the
questions below. Call sites are from Linux 7.0 (`net/ipv4/tcp_input.c`,
`tcp_output.c`, `tcp_cong.c`).

| Callback | Called by the core | What DART does |
|---|---|---|
| `init` | connection established (`tcp_init_transfer`) | base RTT unset, start the first round |
| `pkts_acked` | every ACK that (s)acks data (`tcp_clean_rtx_queue`) | collect the RTT sample |
| `cong_avoid` | ACKs that may grow cwnd: states Open, Disorder, Loss; never during fast recovery or CWR (`tcp_cong_control`) | slow start per ACK; DART's decision once per round |
| `ssthresh` | start of fast recovery, ECN CWR, or RTO (`tcp_init_cwnd_reduction`, `tcp_enter_loss`) | return `max(2, floor(0.7 × cwnd))` |
| `undo_cwnd` | the loss turned out spurious (`tcp_undo_cwnd_reduction`) | Reno's rule, `max(cwnd, prior_cwnd)` (we use `tcp_reno_undo_cwnd`) |
| `set_state` | before every congestion-state change | restart the round when entering or leaving CWR/Recovery/Loss |
| `cwnd_event` | `CA_EVENT_TX_START` (sending after the pipe emptied), `CA_EVENT_CWND_RESTART` (after idle) | restart the round |

After `ssthresh` returns, the core shrinks cwnd to the new ssthresh using
PRR, and sets cwnd = ssthresh when recovery ends. DART never multiplies cwnd
by 0.7 itself.

## Per-connection state

All state lives in `inet_csk_ca(sk)`, the 104-byte private area every TCP
socket has on this kernel. `struct dart` is 32 bytes. A `BUILD_BUG_ON`
checks that it fits, and nothing is allocated.

| Field | Meaning |
|---|---|
| `base_rtt_us` | minimum valid RTT over the whole connection (`U32_MAX` = none yet) |
| `round_rtt_us` | minimum valid RTT in the current round (`U32_MAX` = none yet) |
| `round_rtt_cnt` | number of valid RTT samples in the current round |
| `round_end_seq` | `snd_nxt` when the round started; the round ends on the first ACK past it |
| `round_start_us` | `tp->tcp_mstamp` (µs) when the round started |
| `round_delivered` | `tp->delivered` when the round started |
| `prev_rate` | delivery rate of the previous round in packets/s; 0 = no valid measurement |

## Measurements

**RTT samples.** `pkts_acked` receives `sample->rtt_us`, the RTT of the most
recently (s)acked packet. It is `-1` when the ACK gives no usable sample, for
example when only retransmitted data was acked (Karn's rule). DART skips
those samples and never treats them as 0. Every valid sample updates
`base_rtt_us` and `round_rtt_us` (both minimums) and increments
`round_rtt_cnt`. Using the round's minimum, as Vegas does, filters out
delayed ACKs and jitter, which only ever add delay.

**Rounds.** A round starts at `round_end_seq = snd_nxt` and ends on the first
ACK with `after(ack, round_end_seq)`. That is the moment the data that was
outstanding at the start has been acknowledged, about one RTT later.
`after()` is Linux's wraparound-safe sequence comparison. All of DART's
decisions happen on that one ACK, so there is at most one decision per RTT.

**Queueing delay.**
`queue_delay = round_rtt_us - base_rtt_us`. This is never negative, because
the base is the minimum of all samples, including this round's.

**Delivery rate.**
`rate = (tp->delivered - round_delivered) × 10⁶ / (tp->tcp_mstamp - round_start_us)`,
computed in 64-bit integers. The counter subtraction is done in `u32` so it
stays correct when the counter wraps. The round's rate is treated as **not
valid** (0) when:
- no packets were delivered, or the interval is 0;
- the round was shorter than half the base RTT (ACK compression);
- the sender is not cwnd-limited, so the rate reflects the application rather
  than the network.

`tp->delivered` counts *packets* the receiver has (s)acked, including
retransmissions. That is not the application goodput measured by the A2
client (`bytes_acked`, bytes over time). Retransmitted packets count, and
packets are not bytes. DART only compares the rate with its own previous
value, so a consistent packets/second number is enough.

## Thresholds

```
LOW  = clamp(5%  of base RTT, 2 ms, 10 ms)
HIGH = clamp(15% of base RTT, 5 ms, 20 ms)
```

These are computed in integer µs as `base/20` and `3 × (base/20)`, which
cannot overflow. HIGH > LOW for every base RTT; the code also enforces it,
and the simulator checks it for base RTTs from 0 to 5 s.

| base RTT | LOW | HIGH |
|---:|---:|---:|
| ≤ 40 ms | 2 ms | 5 ms (6 ms at 40 ms) |
| 100 ms | 5 ms | 15 ms |
| ≥ 200 ms | 10 ms | 20 ms |

## Decisions

**Every ACK that the core passes to `cong_avoid`:**

1. In the **Loss** state (after an RTO), only standard slow start back up to
   ssthresh is allowed. DART makes no decisions, and its rounds restart once
   the connection is Open again.
2. **Mid-round**, the only growth is standard slow start (`tcp_slow_start`, +1
   segment per segment acked), and only while cwnd-limited. In congestion
   avoidance cwnd does not change mid-round.
3. On the **ACK that ends the round**, DART decides as described next, then
   starts a new round.

**At the end of a round:**

| Condition | Action | Counter |
|---|---|---|
| fewer than 3 RTT samples this round | Reno-style round: slow start on this ACK, or cwnd + 1 in congestion avoidance (if cwnd-limited) | `few_samples` |
| slow start, queue_delay ≥ HIGH | leave slow start: `ssthresh = cwnd`; cwnd is not cut and does not grow on this ACK | `ss_exit` |
| slow start, queue_delay < HIGH | keep slow-starting (this ACK too) | `slow_start` |
| **Rule A**: queue_delay < LOW | cwnd + 2, capped at `snd_cwnd_clamp`, only if cwnd-limited | `grow` / `not_cwnd_limited` |
| **Rule B**: LOW ≤ queue_delay ≤ HIGH | hold | `hold` |
| **Rule C**: queue_delay > HIGH, this round's rate or the previous one not valid | hold and measure again | `hold_no_rate` |
| Rule C, rate > 105% of the previous round | hold: the queue is still buying throughput | `hold_rate_up` |
| Rule C, rate ≤ 105% of the previous round | `cwnd = max(2, floor(0.9 × cwnd))`; `ssthresh = min(ssthresh, cwnd)` | `decrease` |

After every round, `prev_rate` becomes this round's rate (0 if not valid).
Rule C therefore always compares **two consecutive valid rounds**. Its
fallback when the rate is unknown is to hold: no growth, and no cut without
evidence.

The `ssthresh = min(ssthresh, cwnd)` after a decrease keeps cwnd ≥ ssthresh,
so a DART reduction never drops the connection back into slow start.

Three rules keep the two kinds of growth apart. Slow start grows only per
ACK. Congestion avoidance grows only at the end of a round. The ACK that
exits slow start does not grow.

Why fewer than 3 samples falls back to Reno: with only one or two ACKs in a
round, most samples may be delayed ACKs (up to 40 ms extra on Linux), which
would look like queueing. Vegas uses the same cutoff. The fallback also
means DART cannot stall at cwnd = 2: tiny windows grow by 1 per round.

## Loss response

`ssthresh() = max(2, floor(cwnd × 70 / 100))`, using a 64-bit multiply. The
core does the rest:
- **Fast recovery**: PRR brings cwnd down to ssthresh over about one RTT.
- **RTO**: cwnd goes to 1, then slow-starts back to ssthresh in the Loss state.
- **Spurious loss**: undo restores `max(cwnd, prior_cwnd)` and the old ssthresh.

The 0.7 factor (Reno uses 0.5) keeps more of the window. This was meant to
help the long-RTT paths where A2 saw Reno recover slowly.

## Resets

- **Entering or leaving CWR, Recovery or Loss** (`set_state`): drop the
  current round and the rate history. A round that overlaps recovery would
  mix PRR's sending pattern into the RTT and rate.
- **Open ↔ Disorder**: keep the round. Disorder only means a few dupacks or
  reordering.
- **`CA_EVENT_TX_START` / `CA_EVENT_CWND_RESTART`** (sending after an idle
  period): drop the round and rate history.
- The base RTT is kept for the lifetime of the connection.

## Safety properties

- cwnd never goes below 2 from DART, and DART never grows it past
  `snd_cwnd_clamp`. Every change goes through `tcp_snd_cwnd_set()`.
- There is at most one DART decision per round, so DART never cuts cwnd on
  several ACKs within one RTT.
- No division by zero: every divisor is checked first, or is a nonzero
  constant.
- 64-bit intermediates are used for `delivered × 10⁶`, `cwnd × 70`,
  `cwnd × 90`, and the rate comparison. Sequence numbers use `after()`, and
  counters use `u32` subtraction.
- Nothing is allocated, and nothing is logged per ACK. The optional debug
  log (`debug=1`) covers only slow-start exits, decreases and losses, and is
  rate-limited.
- No global defaults are changed. The module only registers `dart`.

## Observability

- `/sys/module/tcp_dart/parameters/stats` (read-only, anyone) holds counters
  of every decision above, summed over all DART sockets. They are
  incremented at most once per round or loss.
- `/sys/module/tcp_dart/parameters/debug` (root, `0644`) enables rate-limited
  log lines such as `tcp_dart: <sk> decrease cwnd=… queue_delay=…us rate=…`
  and `tcp_dart: <sk> loss cwnd=X ssthresh=Y`. Read them with
  `journalctl -k`.

## Relationship to the A2 observations (Q3(b))

These are the motivating observations from our A2 measurements, not general
laws. The numbers below are from the A2 results committed in
`CS422-Assignment-2/results`: 10 servers, 3 runs × 60 s per algorithm,
aarch64 / Linux 6.8. They were re-aggregated with this repo's `plots.py`.

| A2 observation | DART mechanism |
|---|---|
| A shared bottleneck near 42 Mbit/s: CUBIC and Reno p95 goodput pooled over servers was 42.5 and 42.0 Mbit/s | Nothing is hard-coded. DART only compares a round's rate with the previous round's, so it works for any capacity. |
| Throughput plateaus around 20 ms of extra queueing. Pooling CUBIC and Reno samples (after 5 s) by queueing delay (sRTT − min RTT), median goodput was 13.8, 23.1 and 34.2 Mbit/s in the 0–5, 5–10 and 10–20 ms bins, then 41.6 and 41.7 Mbit/s for 20–40 and 40–80 ms. This is a correlation across servers, not proof of cause. | HIGH is capped at 20 ms. Above HIGH, DART asks whether the extra queue still buys throughput (Rule C), and backs off if not. |
| Short-RTT paths build queueing delay without more throughput | LOW and HIGH scale with the base RTT (5% and 15%), with floors of 2 and 5 ms, so short paths are allowed only small queues. |
| Rising RTT comes before some losses | DART exits slow start at HIGH and backs off under high delay, which can happen before the buffer overflows. |
| Reno recovers slowly on long-RTT paths | 0.7 instead of 0.5 on loss, and +2 instead of +1 segment per RTT when queueing is low. |

## Known behaviour and limitations

**Slow start exits late.** DART checks the slow-start delay only at round
boundaries, as specified. The ACKs in round *k* are for packets sent during
round *k−1*, and the earliest of those went out when cwnd was still small.
So the round's minimum RTT only shows a standing queue one round late, by
which point cwnd has doubled twice. In the simulator, DART left slow start
at cwnd 320 on a path with a 34-packet BDP, and at 1280 with a 267-packet
BDP. The second case overflowed a 1000-packet (300 ms) buffer once. Rule C
then drains the queue at −10% per round.

A variant that checks within the round, and exits as soon as it has 3
samples all ≥ HIGH, exits at about half that cwnd (163 / 643 / 323). It
avoided the slow-start loss in all three simulated scenarios where the
round-boundary version overflowed the buffer. It is
**not** in the module; it is a candidate refinement (see
`docs/verification.md`).

**Holding at the top of the band.** After a slow-start overshoot, DART cuts
cwnd until queue_delay ≤ HIGH and then holds (Rule B). With constant
capacity it stays there, near HIGH, rather than at LOW. Rule A only runs
again when the queue drains below LOW, for example when competing traffic
leaves.

**Delay that is not our queue.** A route change or another queue on the path
raises the RTT without being caused by DART. DART reads it as congestion and
backs off, down to small windows; the base RTT is never refreshed. In the
simulator, a +25 ms delay pulse from 10 to 15 s pulled cwnd from 80 down to
2–4 segments for the length of the pulse, and cwnd was back at 79 one second
after the pulse ended. A longer RTT that persists would keep it low.

**Delay that appears only at low sending rates (seen in the A3 internet
runs).** On the lab VM's internet path, RTT is inflated while the sender is
slow. In 15 of 30 DART runs (`results/a3`), sRTT in the first second was
27–144 ms (median about 80 ms) above the base RTT while cwnd was still
small. That is far above HIGH (at most 20 ms), so DART left slow start
early; the largest cwnd in these runs was 20–320 segments. Rule C then kept
cutting, because the rate did not improve. Those runs averaged under
5 Mbit/s for the whole 10 s test. The other 15 DART runs averaged
55.5 Mbit/s; on the same destination, that is a median of 0.85× Reno's mean
(range 0.18–1.54×).

The extra delay is not DART's own queue:
- 80 ms of queue at 40–100 Mbit/s is 280–690 full-size segments. A flight of
  20–60 segments cannot build it.
- On 185.76.9.135, Reno's first RTT samples were also about 200 ms (base
  RTT 109 ms), and they dropped to the base RTT once its flights were large.
- Idle pings, with no TCP flow at all, show the same jitter. It starts at
  the first ISP hop past the LAN router
  (`results/a3/report/path_rtt_probe.txt`).

CUBIC's HyStart is also delay-based, and it stalled in 8 of 30 runs; Reno
stalled in none. The exact cause on the access link is not known.

Candidate fixes (none are implemented):
- ignore delay until the flight is large, for example 64 segments or more;
- use the minimum RTT over several rounds;
- require the delay to persist for a few rounds before leaving slow start.

**Competing with loss-based flows.** Reno and CUBIC fill the buffer. DART
sees high delay and backs off, so like Vegas it gets less than its fair
share against them. Rule C only holds while DART's own rate keeps
improving.

**One-round lag.** Each decision acts on measurements from the previous round,
so DART tends to cut one step too many after a large queue.

**Rate is packets, not bytes.** Retransmissions count as delivered. With a
fixed MSS this does not matter for comparing one round with the next.

## Parameters

These are `#define`s at the top of `tcp_dart.c`. Change them and rebuild.

| Name | Value | Meaning |
|---|---|---|
| `DART_LOW_MIN_US` / `DART_LOW_MAX_US` | 2000 / 10000 | clamp for LOW (5% of base RTT) |
| `DART_HIGH_MIN_US` / `DART_HIGH_MAX_US` | 5000 / 20000 | clamp for HIGH (15% of base RTT) |
| `DART_GROW_SEGS` | 2 | Rule A increase per round |
| `DART_DECREASE_PCT` | 90 | Rule C: cwnd becomes 90% |
| `DART_PLATEAU_PCT` | 105 | Rule C: "improved" means more than +5% |
| `DART_LOSS_PCT` | 70 | ssthresh on loss |
| `DART_MIN_RTT_SAMPLES` | 3 | minimum samples per round for a delay decision |
| `DART_MIN_CWND` | 2 | floor for DART's reductions |
