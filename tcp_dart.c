// SPDX-License-Identifier: GPL-2.0
/*
 * DART: Delay-Aware Reno with Throughput feedback
 *
 * CS 422 Assignment 3, Option 1. DART is Reno plus two signals that are
 * evaluated once per round trip (a "round"):
 *
 *   queue_delay = (min RTT in this round) - (min RTT ever seen = base RTT)
 *   rate        = packets delivered in this round / length of the round
 *
 * Slow start is standard Linux slow start, except that it ends early when
 * queue_delay >= HIGH. In congestion avoidance, at the end of each round:
 *
 *   queue_delay <  LOW           Rule A: cwnd += 2 (only if cwnd-limited)
 *   LOW <= queue_delay <= HIGH   Rule B: hold cwnd
 *   queue_delay >  HIGH          Rule C: if rate is not more than 5% above
 *                                the previous round's rate, cwnd *= 0.9;
 *                                otherwise hold
 *
 * where LOW  = clamp(5%  of base RTT, 2 ms, 10 ms)
 *       HIGH = clamp(15% of base RTT, 5 ms, 20 ms).
 *
 * Loss recovery is left to the TCP core: DART only supplies the new
 * ssthresh (70% of cwnd) and Reno's undo rule. See docs/algorithm.md.
 */

#define pr_fmt(fmt) "tcp_dart: " fmt

#include <linux/module.h>
#include <linux/sysfs.h>
#include <net/tcp.h>

#define DART_LOW_MIN_US		2000U
#define DART_LOW_MAX_US		10000U
#define DART_HIGH_MIN_US	5000U
#define DART_HIGH_MAX_US	20000U

#define DART_GROW_SEGS		2U	/* Rule A: segments added per round */
#define DART_DECREASE_PCT	90U	/* Rule C: cwnd = 90% of cwnd */
#define DART_LOSS_PCT		70U	/* on loss: ssthresh = 70% of cwnd */
#define DART_PLATEAU_PCT	105U	/* "improved" = rate > 105% of last round */
#define DART_MIN_RTT_SAMPLES	3U	/* fewer per round: no delay decision */
#define DART_MIN_CWND		2U

#define DART_RTT_NONE		U32_MAX	/* no RTT sample yet */

/* Per-connection state. Lives in inet_csk_ca(sk); nothing is allocated. */
struct dart {
	u32 base_rtt_us;	/* min RTT over the connection */
	u32 round_rtt_us;	/* min RTT in the current round */
	u32 round_rtt_cnt;	/* valid RTT samples in the current round */
	u32 round_end_seq;	/* round ends when an ACK goes past this */
	u64 round_start_us;	/* tp->tcp_mstamp when the round started */
	u32 round_delivered;	/* tp->delivered when the round started */
	u32 prev_rate;		/* last round's rate (pkts/s), 0 = not valid */
};

/*
 * Decision counters for testing, shared by all DART connections and
 * readable by anyone at /sys/module/tcp_dart/parameters/stats. Each is
 * bumped at most once per round or loss event, never per ACK.
 */
enum dart_stat {
	DART_STAT_ROUNDS,	/* rounds evaluated */
	DART_STAT_FEW_SAMPLES,	/* too few RTT samples: Reno-style round */
	DART_STAT_SS,		/* stayed in slow start */
	DART_STAT_SS_EXIT,	/* left slow start because queue_delay >= HIGH */
	DART_STAT_GROW,		/* Rule A: cwnd += 2 */
	DART_STAT_NOT_LIMITED,	/* Rule A, but not cwnd-limited: no growth */
	DART_STAT_HOLD,		/* Rule B */
	DART_STAT_DECREASE,	/* Rule C, rate plateaued: cwnd *= 0.9 */
	DART_STAT_RATE_UP,	/* Rule C, rate still improving: hold */
	DART_STAT_NO_RATE,	/* Rule C, no valid rate to compare: hold */
	DART_STAT_LOSS,		/* ssthresh() calls (loss or ECN) */
	DART_STAT_MAX
};

static const char * const dart_stat_names[DART_STAT_MAX] = {
	[DART_STAT_ROUNDS]	= "rounds",
	[DART_STAT_FEW_SAMPLES]	= "few_samples",
	[DART_STAT_SS]		= "slow_start",
	[DART_STAT_SS_EXIT]	= "ss_exit",
	[DART_STAT_GROW]	= "grow",
	[DART_STAT_NOT_LIMITED]	= "not_cwnd_limited",
	[DART_STAT_HOLD]	= "hold",
	[DART_STAT_DECREASE]	= "decrease",
	[DART_STAT_RATE_UP]	= "hold_rate_up",
	[DART_STAT_NO_RATE]	= "hold_no_rate",
	[DART_STAT_LOSS]	= "loss",
};

static atomic_long_t dart_stats[DART_STAT_MAX];

static void dart_count(enum dart_stat stat)
{
	atomic_long_inc(&dart_stats[stat]);
}

static int dart_stats_get(char *buf, const struct kernel_param *kp)
{
	int i, len = 0;

	for (i = 0; i < DART_STAT_MAX; i++)
		len += sysfs_emit_at(buf, len, "%s=%ld%c", dart_stat_names[i],
				     atomic_long_read(&dart_stats[i]),
				     i + 1 < DART_STAT_MAX ? ' ' : '\n');
	return len;
}

/* The kernel calls ->set without a NULL check for "insmod ... stats=x". */
static int dart_stats_set(const char *val, const struct kernel_param *kp)
{
	return -EPERM;
}

static const struct kernel_param_ops dart_stats_ops = {
	.set = dart_stats_set,
	.get = dart_stats_get,
};
module_param_cb(stats, &dart_stats_ops, NULL, 0444);
MODULE_PARM_DESC(stats, "DART decision counters (read-only)");

static bool debug;
module_param(debug, bool, 0644);
MODULE_PARM_DESC(debug, "Log slow-start exits, decreases and losses (rate-limited)");

#define dart_dbg(fmt, ...)						\
	do {								\
		if (unlikely(READ_ONCE(debug)))				\
			pr_info_ratelimited(fmt, ##__VA_ARGS__);	\
	} while (0)

/* floor(val * pct / 100) without overflowing 32 bits */
static u32 dart_pct_of(u32 val, u32 pct)
{
	return (u32)div_u64((u64)val * pct, 100);
}

/* LOW = 5% and HIGH = 15% of the base RTT, each clamped (microseconds). */
static void dart_thresholds(u32 base_rtt_us, u32 *low_us, u32 *high_us)
{
	u32 five_pct = base_rtt_us / 20;

	*low_us = clamp(five_pct, DART_LOW_MIN_US, DART_LOW_MAX_US);
	*high_us = clamp(five_pct * 3, DART_HIGH_MIN_US, DART_HIGH_MAX_US);
	if (*high_us <= *low_us)	/* not possible with the values above */
		*high_us = *low_us + 1;
}

/* Start a new measurement round at the current send point. */
static void dart_new_round(struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);
	struct dart *ca = inet_csk_ca(sk);

	ca->round_end_seq = tp->snd_nxt;
	ca->round_start_us = tp->tcp_mstamp;
	ca->round_delivered = tp->delivered;
	ca->round_rtt_us = DART_RTT_NONE;
	ca->round_rtt_cnt = 0;
}

/* Throw away the current round and the rate history (keeps base RTT). */
static void dart_restart(struct sock *sk)
{
	struct dart *ca = inet_csk_ca(sk);

	ca->prev_rate = 0;
	dart_new_round(sk);
}

static void dart_init(struct sock *sk)
{
	struct dart *ca = inet_csk_ca(sk);

	ca->base_rtt_us = DART_RTT_NONE;
	dart_restart(sk);
}

/* Collect RTT samples; called for every ACK that (s)acks new data. */
static void dart_pkts_acked(struct sock *sk, const struct ack_sample *sample)
{
	struct dart *ca = inet_csk_ca(sk);
	u32 rtt_us;

	/* < 0: no RTT for this ACK (e.g. only retransmitted data was acked).
	 * Skip it rather than treating it as a zero RTT.
	 */
	if (sample->rtt_us < 0)
		return;

	rtt_us = sample->rtt_us;
	ca->base_rtt_us = min(ca->base_rtt_us, rtt_us);
	ca->round_rtt_us = min(ca->round_rtt_us, rtt_us);
	ca->round_rtt_cnt++;
}

/*
 * Delivery rate of the round that just ended, in packets per second, from
 * the kernel's delivered-packet counter (includes SACKed and retransmitted
 * packets, so it is not the same as application goodput). Returns 0 when
 * the round says nothing reliable about the network.
 */
static u32 dart_round_rate(const struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);
	const struct dart *ca = inet_csk_ca(sk);
	u32 delivered = tp->delivered - ca->round_delivered;	/* wrap-safe */
	u32 interval_us = tcp_stamp_us_delta(tp->tcp_mstamp, ca->round_start_us);

	if (!delivered || !interval_us)
		return 0;
	/* A round lasts about one RTT; much shorter means ACK compression. */
	if (interval_us < ca->base_rtt_us / 2)
		return 0;
	/* Not cwnd-limited: the rate reflects the application, not the path. */
	if (!tcp_is_cwnd_limited(sk))
		return 0;

	return (u32)min_t(u64, div_u64((u64)delivered * USEC_PER_SEC, interval_us),
			  U32_MAX);
}

/* Called once per round, on the ACK that completes it. */
static void dart_round_end(struct sock *sk, u32 acked)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct dart *ca = inet_csk_ca(sk);
	bool cwnd_limited = tcp_is_cwnd_limited(sk);
	u32 cwnd = tcp_snd_cwnd(tp);
	u32 rate = dart_round_rate(sk);
	u32 queue_delay_us, low_us, high_us;

	dart_count(DART_STAT_ROUNDS);

	if (ca->round_rtt_cnt < DART_MIN_RTT_SAMPLES) {
		/* Too few samples to tell queueing from delayed ACKs (startup,
		 * tiny cwnd): behave like Reno for this round.
		 */
		dart_count(DART_STAT_FEW_SAMPLES);
		if (cwnd_limited) {
			if (tcp_in_slow_start(tp))
				tcp_slow_start(tp, acked);
			else
				tcp_snd_cwnd_set(tp, min(cwnd + 1,
							 tp->snd_cwnd_clamp));
		}
		goto out;
	}

	/* max(0, round - base); base is the min of all samples, so >= 0 anyway */
	queue_delay_us = ca->round_rtt_us > ca->base_rtt_us ?
			 ca->round_rtt_us - ca->base_rtt_us : 0;
	dart_thresholds(ca->base_rtt_us, &low_us, &high_us);

	if (tcp_in_slow_start(tp)) {
		if (queue_delay_us >= high_us) {
			/* A queue is building: stop doubling but keep cwnd. */
			tp->snd_ssthresh = max(cwnd, DART_MIN_CWND);
			dart_count(DART_STAT_SS_EXIT);
			dart_dbg("%p ss_exit cwnd=%u base_rtt=%uus queue_delay=%uus\n",
				 sk, cwnd, ca->base_rtt_us, queue_delay_us);
		} else {
			dart_count(DART_STAT_SS);
			if (cwnd_limited)
				tcp_slow_start(tp, acked);
		}
	} else if (queue_delay_us < low_us) {
		/* Rule A: little queueing, probe for more bandwidth. */
		if (cwnd_limited) {
			tcp_snd_cwnd_set(tp, min(cwnd + DART_GROW_SEGS,
						 tp->snd_cwnd_clamp));
			dart_count(DART_STAT_GROW);
		} else {
			dart_count(DART_STAT_NOT_LIMITED);
		}
	} else if (queue_delay_us <= high_us) {
		/* Rule B: moderate queueing, hold. */
		dart_count(DART_STAT_HOLD);
	} else if (!rate || !ca->prev_rate) {
		/* Rule C needs two consecutive valid rates: hold, measure again. */
		dart_count(DART_STAT_NO_RATE);
	} else if ((u64)rate * 100 > (u64)ca->prev_rate * DART_PLATEAU_PCT) {
		/* Rule C: the queue is still buying throughput, hold. */
		dart_count(DART_STAT_RATE_UP);
	} else {
		/* Rule C: high queueing and no throughput gain, back off. */
		cwnd = max(dart_pct_of(cwnd, DART_DECREASE_PCT), DART_MIN_CWND);
		tcp_snd_cwnd_set(tp, cwnd);
		/* keep cwnd >= ssthresh so this does not restart slow start */
		tp->snd_ssthresh = min(tp->snd_ssthresh, cwnd);
		dart_count(DART_STAT_DECREASE);
		dart_dbg("%p decrease cwnd=%u base_rtt=%uus queue_delay=%uus rate=%u prev_rate=%u\n",
			 sk, cwnd, ca->base_rtt_us, queue_delay_us, rate,
			 ca->prev_rate);
	}
out:
	ca->prev_rate = rate;
}

/*
 * Called by the TCP core for ACKs that may grow cwnd: in Open, Disorder
 * and Loss states, never during fast recovery or CWR.
 */
static void dart_cong_avoid(struct sock *sk, u32 ack, u32 acked)
{
	struct tcp_sock *tp = tcp_sk(sk);
	struct dart *ca = inet_csk_ca(sk);

	/* After an RTO the core slow-starts back up to ssthresh. Allow only
	 * that; DART's rounds restart once the connection is Open again.
	 */
	if (inet_csk(sk)->icsk_ca_state == TCP_CA_Loss) {
		if (tcp_in_slow_start(tp) && tcp_is_cwnd_limited(sk))
			tcp_slow_start(tp, acked);
		return;
	}

	/* Mid-round: only slow start grows cwnd, one segment per segment acked */
	if (!after(ack, ca->round_end_seq)) {
		if (tcp_in_slow_start(tp) && tcp_is_cwnd_limited(sk))
			tcp_slow_start(tp, acked);
		return;
	}

	dart_round_end(sk, acked);
	dart_new_round(sk);
}

/* New ssthresh after loss or ECN; the core then runs recovery down to it. */
static u32 dart_ssthresh(struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);
	u32 cwnd = tcp_snd_cwnd(tp);
	u32 ssthresh = max(dart_pct_of(cwnd, DART_LOSS_PCT), DART_MIN_CWND);

	dart_count(DART_STAT_LOSS);
	dart_dbg("%p loss cwnd=%u ssthresh=%u\n", sk, cwnd, ssthresh);
	return ssthresh;
}

/*
 * Called before every congestion state change. Rounds that overlap a loss
 * or ECN episode would mix recovery behaviour into the measurement, so
 * start over when entering or leaving CWR, Recovery or Loss. Open <->
 * Disorder (a few dupacks or reordering) keeps the current round.
 */
static void dart_set_state(struct sock *sk, u8 new_state)
{
	if (new_state >= TCP_CA_CWR ||
	    inet_csk(sk)->icsk_ca_state >= TCP_CA_CWR)
		dart_restart(sk);
}

/* Sending again after an idle period: the old round and rate are stale. */
static void dart_cwnd_event(struct sock *sk, enum tcp_ca_event ev)
{
	if (ev == CA_EVENT_TX_START || ev == CA_EVENT_CWND_RESTART)
		dart_restart(sk);
}

static struct tcp_congestion_ops tcp_dart __read_mostly = {
	.init		= dart_init,
	.ssthresh	= dart_ssthresh,
	.cong_avoid	= dart_cong_avoid,
	/* undo a spurious loss response: cwnd = max(cwnd, prior_cwnd) */
	.undo_cwnd	= tcp_reno_undo_cwnd,
	.pkts_acked	= dart_pkts_acked,
	.set_state	= dart_set_state,
	.cwnd_event	= dart_cwnd_event,

	.owner		= THIS_MODULE,
	.name		= "dart",
};

static int __init dart_register(void)
{
	BUILD_BUG_ON(sizeof(struct dart) > ICSK_CA_PRIV_SIZE);
	return tcp_register_congestion_control(&tcp_dart);
}

static void __exit dart_unregister(void)
{
	tcp_unregister_congestion_control(&tcp_dart);
}

module_init(dart_register);
module_exit(dart_unregister);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Faisal, Owen, and Trevor");
MODULE_DESCRIPTION("DART: Delay-Aware Reno with Throughput feedback");
