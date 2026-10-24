// SPDX-License-Identifier: GPL-2.0
/*
 * Drive the real tcp_dart.c in userspace (no root, no kernel module).
 *
 * Part 1: unit tests that call DART's callbacks with hand-made ACK
 *         sequences and check every decision branch.
 * Part 2: a small packet-level simulation (one sender, one FIFO
 *         bottleneck, fixed propagation delay) that runs DART through
 *         the Test C scenarios and checks how cwnd behaves.
 *
 * The TCP core is reduced to what DART depends on: ACK clocking, one
 * pkts_acked + cong_avoid call per ACK, a simplified fast recovery and RTO
 * that call ssthresh/set_state the way tcp_input.c does, and Linux's
 * cwnd-limited tracking. There is no PRR, SACK scoreboard, delayed ACK,
 * pacing or TSO. Results show DART's logic works as designed; they are
 * not a substitute for testing the module in a real kernel.
 *
 * Usage: tests/dart_sim [-v] [-o DIR]
 *   -v      print DART's debug lines and extra details
 *   -o DIR  write one CSV time series per scenario into DIR
 */
#include "../tcp_dart.c"

#include <stdlib.h>
#include <unistd.h>

static int n_checks, n_failed;

#define CHECK(cond, ...)						\
	do {								\
		n_checks++;						\
		if (!(cond)) {						\
			n_failed++;					\
			printf("    FAIL %s:%d: ", __func__, __LINE__);	\
			printf(__VA_ARGS__);				\
			printf("\n");					\
		}							\
	} while (0)

static long nstat(enum dart_stat s)
{
	return atomic_long_read(&dart_stats[s]);
}

/* ------------------------------------------------------------------ */
/* Part 1: unit tests                                                  */
/* ------------------------------------------------------------------ */

/* One connection; sequence numbers count packets, not bytes. */
struct conn {
	struct tcp_sock tp;
	struct sock *sk;
	struct dart *ca;	/* white-box view of DART's private state */
	u64 now;
	u32 cwnd_before_last;	/* cwnd before the last ACK of conn_round() */
	int cwnd_changes;	/* ACKs in the last conn_round() that changed cwnd */
};

static void conn_open(struct conn *c, u32 cwnd, u32 ssthresh)
{
	memset(c, 0, sizeof(*c));
	c->sk = (struct sock *)&c->tp;
	c->ca = inet_csk_ca(c->sk);
	c->tp.inet_conn.icsk_ca_ops = &tcp_dart;
	c->tp.snd_cwnd = cwnd;
	c->tp.snd_ssthresh = ssthresh;
	c->tp.snd_cwnd_clamp = U32_MAX;
	c->tp.snd_una = 5000;
	c->tp.snd_nxt = 5000 + cwnd;
	c->tp.is_cwnd_limited = true;
	c->now = 10 * USEC_PER_SEC;
	c->tp.tcp_mstamp = c->now;
	tcp_dart.init(c->sk);
}

static void conn_set_state(struct conn *c, u8 state)
{
	tcp_dart.set_state(c->sk, state);	/* called before the change */
	c->tp.inet_conn.icsk_ca_state = state;
}

/* One ACK for `pkts` packets, `dt` us after the previous one. */
static void conn_ack(struct conn *c, u32 pkts, s32 rtt_us, u32 dt)
{
	struct tcp_sock *tp = &c->tp;
	struct ack_sample s = { .pkts_acked = pkts, .rtt_us = rtt_us };
	u8 state = tp->inet_conn.icsk_ca_state;

	c->now += dt;
	tp->tcp_mstamp = c->now;
	tp->snd_una += pkts;
	tp->delivered += pkts;
	tcp_dart.pkts_acked(c->sk, &s);
	/* tcp_cong_control(): no cong_avoid while reducing cwnd */
	if (state != TCP_CA_CWR && state != TCP_CA_Recovery)
		tcp_dart.cong_avoid(c->sk, tp->snd_una, pkts);
	/* bulk sender: fill the new window */
	if (before(tp->snd_nxt, tp->snd_una + tp->snd_cwnd))
		tp->snd_nxt = tp->snd_una + tp->snd_cwnd;
}

/*
 * ACK one packet at a time, `dt` us apart (so the delivery rate is
 * 1e6/dt packets/s), until DART finishes its current round.
 * `rtt_us` is the RTT sample on every ACK; `valid` ACKs carry it and the
 * rest carry -1 (no sample).
 */
static void conn_round_n(struct conn *c, s32 rtt_us, u32 dt, u32 valid)
{
	long rounds = nstat(DART_STAT_ROUNDS);
	u32 i = 0;

	c->cwnd_changes = 0;
	while (nstat(DART_STAT_ROUNDS) == rounds && i < 100000) {
		u32 before_ack = c->tp.snd_cwnd;

		c->cwnd_before_last = before_ack;
		conn_ack(c, 1, i < valid ? rtt_us : -1, dt);
		if (c->tp.snd_cwnd != before_ack)
			c->cwnd_changes++;
		i++;
	}
}

static void conn_round(struct conn *c, s32 rtt_us, u32 dt)
{
	conn_round_n(c, rtt_us, dt, U32_MAX);
}

static void test_registration(void)
{
	CHECK(shim_module_init() == 0, "register failed");
	CHECK(shim_registered == &tcp_dart, "not registered");
	CHECK(strcmp(tcp_dart.name, "dart") == 0, "name is %s", tcp_dart.name);
	CHECK(tcp_dart.init && tcp_dart.pkts_acked && tcp_dart.cong_avoid &&
	      tcp_dart.ssthresh && tcp_dart.undo_cwnd && tcp_dart.set_state &&
	      tcp_dart.cwnd_event, "missing callback");
	CHECK(sizeof(struct dart) <= ICSK_CA_PRIV_SIZE, "state too big: %zu",
	      sizeof(struct dart));
	shim_module_exit();
	CHECK(shim_registered == NULL, "not unregistered");
	shim_module_init();
}

static void test_thresholds(void)
{
	static const struct { u32 base, low, high; } cases[] = {
		{ 0, 2000, 5000 },
		{ 1000, 2000, 5000 },
		{ 20000, 2000, 5000 },
		{ 40000, 2000, 6000 },
		{ 100000, 5000, 15000 },
		{ 200000, 10000, 20000 },
		{ 1000000, 10000, 20000 },
		{ U32_MAX, 10000, 20000 },
	};
	u32 low, high, base;
	bool ordered = true;
	size_t i;

	for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		dart_thresholds(cases[i].base, &low, &high);
		CHECK(low == cases[i].low && high == cases[i].high,
		      "base=%u: got LOW=%u HIGH=%u, want %u/%u", cases[i].base,
		      low, high, cases[i].low, cases[i].high);
	}
	for (base = 0; base < 5000000; base += 997) {
		dart_thresholds(base, &low, &high);
		if (!(high > low && low >= DART_LOW_MIN_US &&
		      high <= DART_HIGH_MAX_US))
			ordered = false;
	}
	CHECK(ordered, "HIGH > LOW does not hold for every base RTT");
}

static void test_ssthresh(void)
{
	static const struct { u32 cwnd, want; } cases[] = {
		{ 1, 2 }, { 2, 2 }, { 3, 2 }, { 10, 7 }, { 15, 10 },
		{ 100, 70 }, { 1001, 700 }, { 0x7fffffff, 1503238552 },
	};
	struct conn c;
	size_t i;

	conn_open(&c, 10, 5);
	for (i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		u32 got;

		c.tp.snd_cwnd = cases[i].cwnd;
		got = tcp_dart.ssthresh(c.sk);
		CHECK(got == cases[i].want, "cwnd=%u: ssthresh=%u, want %u",
		      cases[i].cwnd, got, cases[i].want);
		CHECK(c.tp.snd_cwnd == cases[i].cwnd, "ssthresh() changed cwnd");
	}
}

static void test_undo(void)
{
	struct conn c;

	conn_open(&c, 35, 24);
	c.tp.prior_cwnd = 50;
	CHECK(tcp_dart.undo_cwnd(c.sk) == 50, "undo should restore prior_cwnd");
	c.tp.prior_cwnd = 20;
	CHECK(tcp_dart.undo_cwnd(c.sk) == 35, "undo must not shrink cwnd");
}

static void test_rtt_samples(void)
{
	struct conn c;
	struct ack_sample none = { .pkts_acked = 1, .rtt_us = -1 };
	struct ack_sample zero = { .pkts_acked = 1, .rtt_us = 0 };
	struct ack_sample a = { .pkts_acked = 1, .rtt_us = 30000 };
	struct ack_sample b = { .pkts_acked = 1, .rtt_us = 25000 };

	conn_open(&c, 10, 5);
	CHECK(c.ca->base_rtt_us == DART_RTT_NONE &&
	      c.ca->round_rtt_us == DART_RTT_NONE, "init should leave RTTs unset");
	tcp_dart.pkts_acked(c.sk, &none);
	CHECK(c.ca->round_rtt_cnt == 0 && c.ca->base_rtt_us == DART_RTT_NONE,
	      "rtt_us = -1 must be ignored");
	tcp_dart.pkts_acked(c.sk, &a);
	tcp_dart.pkts_acked(c.sk, &b);
	tcp_dart.pkts_acked(c.sk, &a);
	CHECK(c.ca->round_rtt_cnt == 3, "cnt=%u", c.ca->round_rtt_cnt);
	CHECK(c.ca->round_rtt_us == 25000 && c.ca->base_rtt_us == 25000,
	      "min filter: round=%u base=%u", c.ca->round_rtt_us, c.ca->base_rtt_us);
	dart_new_round(c.sk);
	CHECK(c.ca->round_rtt_us == DART_RTT_NONE && c.ca->round_rtt_cnt == 0 &&
	      c.ca->base_rtt_us == 25000, "new round keeps base RTT only");
	tcp_dart.pkts_acked(c.sk, &zero);
	CHECK(c.ca->round_rtt_cnt == 1 && c.ca->base_rtt_us == 0,
	      "a measured 0 us RTT is a valid sample");
}

static void test_rule_a(void)
{
	struct conn c;
	long grow = nstat(DART_STAT_GROW);

	conn_open(&c, 20, 10);			/* congestion avoidance */
	conn_round(&c, 20000, 500);		/* sets base RTT, queue 0 */
	CHECK(c.tp.snd_cwnd == 22, "cwnd=%u, want 22", c.tp.snd_cwnd);
	conn_round(&c, 21500, 500);		/* queue 1.5 ms < LOW 2 ms */
	CHECK(c.tp.snd_cwnd == 24, "cwnd=%u, want 24", c.tp.snd_cwnd);
	CHECK(c.cwnd_changes == 1, "cwnd changed on %d ACKs of one round",
	      c.cwnd_changes);
	CHECK(nstat(DART_STAT_GROW) - grow == 2, "grow count");
	CHECK(!tcp_in_slow_start(&c.tp), "growth re-entered slow start");

	/* not cwnd-limited: no growth */
	conn_open(&c, 20, 10);
	c.tp.is_cwnd_limited = false;
	c.tp.max_packets_out = 5;
	grow = nstat(DART_STAT_NOT_LIMITED);
	conn_round(&c, 20000, 500);
	CHECK(c.tp.snd_cwnd == 20, "grew while not cwnd-limited: %u", c.tp.snd_cwnd);
	CHECK(nstat(DART_STAT_NOT_LIMITED) - grow == 1, "not_cwnd_limited count");

	/* snd_cwnd_clamp */
	conn_open(&c, 20, 10);
	c.tp.snd_cwnd_clamp = 21;
	conn_round(&c, 20000, 500);
	CHECK(c.tp.snd_cwnd == 21, "clamp ignored: %u", c.tp.snd_cwnd);
}

static void test_rule_b(void)
{
	struct conn c;
	long hold = nstat(DART_STAT_HOLD);

	conn_open(&c, 20, 10);
	conn_round(&c, 20000, 500);		/* base 20 ms, grow to 22 */
	conn_round(&c, 23000, 500);		/* queue 3 ms: LOW..HIGH */
	CHECK(c.tp.snd_cwnd == 22, "cwnd=%u, want 22 (hold)", c.tp.snd_cwnd);
	conn_round(&c, 25000, 500);		/* queue 5 ms == HIGH: hold */
	CHECK(c.tp.snd_cwnd == 22, "cwnd=%u, want 22 (hold at HIGH)", c.tp.snd_cwnd);
	conn_round(&c, 22000, 500);		/* queue 2 ms == LOW: hold */
	CHECK(c.tp.snd_cwnd == 22, "cwnd=%u, want 22 (hold at LOW)", c.tp.snd_cwnd);
	CHECK(nstat(DART_STAT_HOLD) - hold == 3, "hold count");
}

static void test_rule_c(void)
{
	struct conn c;
	long dec = nstat(DART_STAT_DECREASE);
	long up = nstat(DART_STAT_RATE_UP);
	long norate = nstat(DART_STAT_NO_RATE);

	/* plateau: same delivery rate, queue 10 ms > HIGH 5 ms */
	conn_open(&c, 40, 10);
	conn_round(&c, 20000, 500);		/* grow to 42, rate 2000 pkt/s */
	CHECK(c.ca->prev_rate == 2000, "rate=%u, want 2000", c.ca->prev_rate);
	conn_round(&c, 30000, 500);
	CHECK(c.tp.snd_cwnd == 37, "cwnd=%u, want floor(42*0.9)=37", c.tp.snd_cwnd);
	CHECK(c.cwnd_changes == 1, "cwnd changed on %d ACKs of one round",
	      c.cwnd_changes);
	CHECK(!tcp_in_slow_start(&c.tp), "decrease re-entered slow start");
	conn_round(&c, 30000, 500);
	CHECK(c.tp.snd_cwnd == 33, "cwnd=%u, want 33", c.tp.snd_cwnd);
	CHECK(nstat(DART_STAT_DECREASE) - dec == 2, "one decrease per round");

	/* ssthresh is lowered with cwnd so slow start does not restart */
	conn_open(&c, 40, 40);
	conn_round(&c, 20000, 500);		/* 42 */
	conn_round(&c, 30000, 500);		/* 37 */
	CHECK(c.tp.snd_cwnd == 37 && c.tp.snd_ssthresh == 37,
	      "cwnd=%u ssthresh=%u", c.tp.snd_cwnd, c.tp.snd_ssthresh);

	/* rate still improving (+25%): hold */
	conn_open(&c, 40, 10);
	conn_round(&c, 20000, 500);		/* 2000 pkt/s */
	conn_round(&c, 30000, 400);		/* 2500 pkt/s */
	CHECK(c.tp.snd_cwnd == 42, "cwnd=%u, want hold at 42", c.tp.snd_cwnd);
	CHECK(nstat(DART_STAT_RATE_UP) - up == 1, "hold_rate_up count");
	conn_round(&c, 30000, 400);		/* 2500 again: plateau */
	CHECK(c.tp.snd_cwnd == 37, "cwnd=%u, want 37", c.tp.snd_cwnd);

	/* the 5% boundary: +5% is a plateau, more than 5% is not */
	conn_open(&c, 40, 10);
	conn_round(&c, 20000, 500);		/* 2000 pkt/s */
	conn_round(&c, 30000, 476);		/* 2100 pkt/s = +5.0% */
	CHECK(c.tp.snd_cwnd == 37, "+5%%: cwnd=%u, want 37", c.tp.snd_cwnd);
	conn_open(&c, 40, 10);
	conn_round(&c, 20000, 500);
	conn_round(&c, 30000, 475);		/* 2105 pkt/s = +5.25% */
	CHECK(c.tp.snd_cwnd == 42, "+5.25%%: cwnd=%u, want 42", c.tp.snd_cwnd);

	/* no previous rate (history cleared by recovery): hold, then act */
	conn_open(&c, 40, 10);
	conn_round(&c, 20000, 500);
	conn_set_state(&c, TCP_CA_Recovery);
	conn_set_state(&c, TCP_CA_Open);
	CHECK(c.ca->prev_rate == 0, "recovery should clear the rate history");
	conn_round(&c, 30000, 500);
	CHECK(c.tp.snd_cwnd == 42, "cwnd=%u, want hold (no rate)", c.tp.snd_cwnd);
	CHECK(nstat(DART_STAT_NO_RATE) - norate == 1, "hold_no_rate count");
	conn_round(&c, 30000, 500);
	CHECK(c.tp.snd_cwnd == 37, "cwnd=%u, want 37", c.tp.snd_cwnd);
}

static void test_min_cwnd_and_regrowth(void)
{
	struct conn c;
	u32 lowest = U32_MAX;
	int i;

	/* 10 ms between ACKs so that a round of a few packets still lasts
	 * about one RTT (shorter rounds are rejected as ACK compression)
	 */
	conn_open(&c, 4, 2);
	conn_round(&c, 20000, 10000);		/* base RTT, grow to 6 */
	for (i = 0; i < 20; i++) {		/* persistent high delay */
		conn_round(&c, 40000, 10000);
		lowest = min(lowest, c.tp.snd_cwnd);
	}
	CHECK(lowest == 2 && c.tp.snd_cwnd >= 2,
	      "lowest cwnd=%u final=%u, want floor of 2", lowest, c.tp.snd_cwnd);
	for (i = 0; i < 10; i++)		/* delay goes away */
		conn_round(&c, 20000, 10000);
	CHECK(c.tp.snd_cwnd >= 20, "cwnd=%u: did not grow back", c.tp.snd_cwnd);
}

static void test_slow_start(void)
{
	struct conn c;
	long exits = nstat(DART_STAT_SS_EXIT);

	/* low delay: normal slow start, cwnd roughly doubles per round */
	conn_open(&c, 10, TCP_INFINITE_SSTHRESH);
	conn_round(&c, 20000, 500);
	CHECK(c.tp.snd_cwnd == 21, "cwnd=%u, want 21", c.tp.snd_cwnd);
	conn_round(&c, 23000, 500);		/* 3 ms < HIGH: keep going */
	CHECK(tcp_in_slow_start(&c.tp) && c.tp.snd_cwnd > 40,
	      "cwnd=%u, should still be slow-starting", c.tp.snd_cwnd);

	/* queue_delay >= HIGH: exit slow start, no cut, no extra growth */
	conn_round(&c, 26000, 500);
	CHECK(nstat(DART_STAT_SS_EXIT) - exits == 1, "no ss_exit");
	CHECK(!tcp_in_slow_start(&c.tp), "still in slow start");
	CHECK(c.tp.snd_ssthresh == c.tp.snd_cwnd, "ssthresh=%u cwnd=%u",
	      c.tp.snd_ssthresh, c.tp.snd_cwnd);
	CHECK(c.tp.snd_cwnd == c.cwnd_before_last,
	      "the exit ACK changed cwnd %u -> %u", c.cwnd_before_last,
	      c.tp.snd_cwnd);

	/* in congestion avoidance mid-round ACKs never grow cwnd */
	conn_ack(&c, 1, 26000, 500);
	conn_ack(&c, 1, 26000, 500);
	CHECK(c.tp.snd_cwnd == c.cwnd_before_last, "mid-round CA growth");
}

static void test_few_samples(void)
{
	struct conn c;
	long few = nstat(DART_STAT_FEW_SAMPLES);

	conn_open(&c, 20, 10);
	conn_round_n(&c, 20000, 500, 2);	/* only 2 RTT samples */
	CHECK(c.tp.snd_cwnd == 21, "cwnd=%u, want Reno-style +1", c.tp.snd_cwnd);
	CHECK(c.ca->base_rtt_us == 20000, "samples still feed base RTT");
	CHECK(nstat(DART_STAT_FEW_SAMPLES) - few == 1, "few_samples count");

	conn_open(&c, 10, TCP_INFINITE_SSTHRESH);
	conn_round_n(&c, 20000, 500, 0);	/* no RTT samples at all */
	CHECK(c.tp.snd_cwnd == 21, "cwnd=%u, want plain slow start", c.tp.snd_cwnd);
}

static void test_state_resets(void)
{
	static const u8 reset_states[] = { TCP_CA_CWR, TCP_CA_Recovery, TCP_CA_Loss };
	struct conn c;
	size_t i;

	conn_open(&c, 20, 10);
	conn_round(&c, 20000, 500);		/* prev_rate set */
	conn_ack(&c, 1, 21000, 500);
	conn_ack(&c, 1, 21000, 500);
	conn_set_state(&c, TCP_CA_Disorder);
	CHECK(c.ca->round_rtt_cnt == 2 && c.ca->prev_rate,
	      "Open->Disorder should keep the round");
	conn_set_state(&c, TCP_CA_Open);
	CHECK(c.ca->round_rtt_cnt == 2 && c.ca->prev_rate,
	      "Disorder->Open should keep the round");

	for (i = 0; i < sizeof(reset_states); i++) {
		conn_ack(&c, 1, 21000, 500);
		c.tp.snd_nxt += 7;
		conn_set_state(&c, reset_states[i]);
		CHECK(c.ca->round_rtt_cnt == 0 && c.ca->prev_rate == 0 &&
		      c.ca->round_end_seq == c.tp.snd_nxt,
		      "entering state %u should restart", reset_states[i]);
		conn_ack(&c, 1, 21000, 500);
		conn_set_state(&c, TCP_CA_Open);
		CHECK(c.ca->round_rtt_cnt == 0 && c.ca->round_end_seq == c.tp.snd_nxt,
		      "leaving state %u should restart", reset_states[i]);
		CHECK(c.ca->base_rtt_us == 20000, "base RTT must survive resets");
	}

	conn_round(&c, 20000, 500);
	conn_ack(&c, 1, 21000, 500);
	tcp_dart.cwnd_event(c.sk, CA_EVENT_COMPLETE_CWR);
	CHECK(c.ca->round_rtt_cnt == 1 && c.ca->prev_rate, "COMPLETE_CWR reset");
	tcp_dart.cwnd_event(c.sk, CA_EVENT_TX_START);
	CHECK(c.ca->round_rtt_cnt == 0 && c.ca->prev_rate == 0, "TX_START no reset");
	conn_round(&c, 20000, 500);
	conn_ack(&c, 1, 21000, 500);
	tcp_dart.cwnd_event(c.sk, CA_EVENT_CWND_RESTART);
	CHECK(c.ca->round_rtt_cnt == 0 && c.ca->prev_rate == 0,
	      "CWND_RESTART no reset");
}

static void test_loss_state(void)
{
	struct conn c;
	long rounds;
	int i;

	conn_open(&c, 10, 20);
	conn_set_state(&c, TCP_CA_Loss);
	c.tp.snd_cwnd = 1;
	rounds = nstat(DART_STAT_ROUNDS);
	for (i = 0; i < 100; i++)
		conn_ack(&c, 1, 20000, 500);
	CHECK(c.tp.snd_cwnd == 20, "cwnd=%u: Loss state should slow start to "
	      "ssthresh=20 and stop", c.tp.snd_cwnd);
	CHECK(nstat(DART_STAT_ROUNDS) == rounds, "DART decided during Loss");
}

static void test_wraparound(void)
{
	struct conn c;
	long grow = nstat(DART_STAT_GROW);

	conn_open(&c, 20, 10);
	c.tp.snd_una = 0xfffffff0;
	c.tp.snd_nxt = c.tp.snd_una + 20;
	c.tp.delivered = 0xfffffff8;
	tcp_dart.init(c.sk);
	conn_round(&c, 20000, 500);
	conn_round(&c, 20000, 500);
	conn_round(&c, 20000, 500);
	CHECK(nstat(DART_STAT_GROW) - grow == 3, "rounds lost at seq wrap");
	CHECK(c.ca->prev_rate == 2000, "rate=%u across delivered wrap",
	      c.ca->prev_rate);
}

static void test_stats_param(void)
{
	char buf[PAGE_SIZE];
	int len = shim_param_stats->get(buf, NULL);

	CHECK(len > 0 && (size_t)len == strlen(buf), "bad length %d", len);
	CHECK(strncmp(buf, "rounds=", 7) == 0 && strstr(buf, " decrease=") &&
	      strstr(buf, " loss=") && buf[len - 1] == '\n', "format: %s", buf);
	CHECK(shim_param_stats->set("1", NULL) == -EPERM, "stats must be read-only");
}

/* ------------------------------------------------------------------ */
/* Part 2: packet-level simulation                                     */
/* ------------------------------------------------------------------ */

#define SIM_SAMPLE_US	10000	/* record a sample every 10 ms */
#define SIM_RTO_US	200000
#define SIM_CROSS	U32_MAX	/* queue entry that is cross traffic */
#define SIM_NEVER	UINT64_MAX
#define PKT_ACKED	1
#define PKT_RETX	2

struct sim;

struct scenario {
	const char *name;
	const char *what;
	u32 service_us;		/* bottleneck time per packet (300 = 40 Mbit/s) */
	u32 prop_rtt_us;	/* two-way propagation delay */
	u32 buf_pkts;		/* bottleneck FIFO size, including the one in service */
	u32 duration_ms;
	u32 app_pps;		/* application data rate, 0 = always has data */
	u32 loss_ppm;		/* random drops at the bottleneck */
	u32 cross_gap_us;	/* CBR cross traffic: one packet per gap */
	u32 cross_from_ms, cross_to_ms;
	u32 pulse_us;		/* extra delay on the return path... */
	u32 pulse_from_ms, pulse_to_ms;	/* ...during this interval */
	void (*check)(struct sim *s);
};

struct sample {
	u32 t_ms, cwnd, ssthresh, qlen, delivered;
	long decreases;
	u8 state;
};

struct sim {
	const struct scenario *sc;
	struct conn c;
	u64 now, end;
	/* per packet, indexed by sequence number */
	u64 *sent_at;
	u8 *flags;
	u32 max_pkts;
	bool overflow;
	/* bottleneck FIFO; q[q_head] is being transmitted until tx_done */
	u32 *q;
	u32 q_head, q_len, q_cap;
	u64 tx_done;
	/* return path, FIFO */
	u32 *ack_seq;
	u64 *ack_at;
	u32 a_head, a_len;
	u64 last_ack_at;
	/* dropped packets the sender has not noticed yet */
	u32 *lost;
	u32 l_head, l_len;
	u32 inflight, high_seq, cwnd_usage_seq;
	u64 next_cross, next_sample, last_progress;
	u64 rng;
	/* results */
	struct sample *samples;
	u32 nsamples;
	u32 losses, rtos, bad_ssthresh, min_cwnd_open, max_cwnd;
	u32 first_loss_ms, last_loss_ms, ss_exit_cwnd, ss_exit_qlen;
	u64 last_decrease, min_decrease_gap;
	long stats0[DART_STAT_MAX];
};

static u32 sim_rand_ppm(struct sim *s)
{
	s->rng ^= s->rng << 13;		/* xorshift64: deterministic runs */
	s->rng ^= s->rng >> 7;
	s->rng ^= s->rng << 17;
	return s->rng % 1000000;
}

static bool sim_in(const struct sim *s, u32 from_ms, u32 to_ms)
{
	return s->now >= (u64)from_ms * 1000 && s->now < (u64)to_ms * 1000;
}

static u8 sim_state(const struct sim *s)
{
	return s->c.tp.inet_conn.icsk_ca_state;
}

static void sim_enqueue(struct sim *s, u32 id, bool force)
{
	bool full = s->q_len >= s->sc->buf_pkts;

	if (!force && (full || (id != SIM_CROSS && s->sc->loss_ppm &&
				sim_rand_ppm(s) < s->sc->loss_ppm))) {
		if (id != SIM_CROSS)
			s->lost[s->l_head + s->l_len++] = id;
		return;
	}
	if (s->q_head + s->q_len >= s->q_cap) {
		s->overflow = true;
		return;
	}
	s->q[s->q_head + s->q_len++] = id;
	if (s->q_len == 1)
		s->tx_done = s->now + s->sc->service_us;
}

/* tcp_write_xmit() + tcp_cwnd_validate(), much simplified */
static void sim_send(struct sim *s)
{
	struct tcp_sock *tp = &s->c.tp;
	bool limited;

	while (s->inflight < tp->snd_cwnd) {
		u32 seq = tp->snd_nxt;

		if (s->sc->app_pps &&
		    (u64)seq * USEC_PER_SEC > (u64)s->sc->app_pps * s->now)
			break;		/* application has no more data yet */
		if (seq >= s->max_pkts) {
			s->overflow = true;
			return;
		}
		if (s->inflight == 0)
			tcp_dart.cwnd_event(s->c.sk, CA_EVENT_TX_START);
		s->sent_at[seq] = s->now;
		s->flags[seq] = 0;
		tp->snd_nxt++;
		s->inflight++;
		sim_enqueue(s, seq, false);
	}
	limited = s->inflight >= tp->snd_cwnd;
	if (!before(tp->snd_una, s->cwnd_usage_seq) || limited ||
	    (!tp->is_cwnd_limited && s->inflight > tp->max_packets_out)) {
		tp->is_cwnd_limited = limited;
		tp->max_packets_out = s->inflight;
		s->cwnd_usage_seq = tp->snd_nxt;
	}
}

static void sim_retransmit(struct sim *s, u32 seq)
{
	s->flags[seq] |= PKT_RETX;
	s->sent_at[seq] = s->now;
	s->inflight++;
	sim_enqueue(s, seq, true);	/* retransmissions are never dropped */
}

/* tcp_init_cwnd_reduction() / tcp_enter_loss(): ask DART for ssthresh */
static void sim_enter(struct sim *s, u8 state)
{
	struct tcp_sock *tp = &s->c.tp;
	u32 cwnd = tp->snd_cwnd;
	u32 want = max((u32)((u64)cwnd * 7 / 10), 2U);
	u32 ssthresh;

	tp->prior_cwnd = cwnd;
	ssthresh = tcp_dart.ssthresh(s->c.sk);
	if (ssthresh != want) {
		s->bad_ssthresh++;
		printf("    ssthresh(%u) = %u, want %u\n", cwnd, ssthresh, want);
	}
	tp->snd_ssthresh = ssthresh;
	if (sim_state(s) != state)
		conn_set_state(&s->c, state);
	s->high_seq = tp->snd_nxt;
	/* Recovery: PRR would bring cwnd down to ssthresh over one RTT */
	tcp_snd_cwnd_set(tp, state == TCP_CA_Loss ? 1 : ssthresh);
	if (!s->losses++)
		s->first_loss_ms = s->now / 1000;
	s->last_loss_ms = s->now / 1000;
}

static void sim_ack(struct sim *s, u32 seq)
{
	struct tcp_sock *tp = &s->c.tp;
	struct ack_sample sample = { .pkts_acked = 1 };
	u32 old_una = tp->snd_una;
	long dec = nstat(DART_STAT_DECREASE);
	long exits = nstat(DART_STAT_SS_EXIT);

	if (s->flags[seq] & PKT_ACKED)
		return;
	s->flags[seq] |= PKT_ACKED;
	s->inflight--;
	s->last_progress = s->now;
	tp->tcp_mstamp = s->now;
	tp->delivered++;
	while (tp->snd_una != tp->snd_nxt && (s->flags[tp->snd_una] & PKT_ACKED))
		tp->snd_una++;

	/* tcp_clean_rtx_queue(): Karn's rule for retransmitted packets */
	sample.rtt_us = (s->flags[seq] & PKT_RETX) ? -1 :
			(s32)(s->now - s->sent_at[seq]);
	tcp_dart.pkts_acked(s->c.sk, &sample);

	/* tcp_fastretrans_alert(): a drop is noticed once 3 later packets
	 * have been (s)acked; the first one starts fast recovery.
	 */
	while (s->l_len && s->lost[s->l_head] + 3 <= seq) {
		u32 l = s->lost[s->l_head++];

		s->l_len--;
		s->inflight--;
		if (sim_state(s) != TCP_CA_Recovery && sim_state(s) != TCP_CA_Loss)
			sim_enter(s, TCP_CA_Recovery);
		sim_retransmit(s, l);
	}
	if ((sim_state(s) == TCP_CA_Recovery || sim_state(s) == TCP_CA_Loss) &&
	    !before(tp->snd_una, s->high_seq)) {
		if (sim_state(s) == TCP_CA_Recovery)	/* tcp_end_cwnd_reduction() */
			tcp_snd_cwnd_set(tp, tp->snd_ssthresh);
		conn_set_state(&s->c, TCP_CA_Open);
	}

	/* tcp_cong_control(): no growth during fast recovery */
	if (sim_state(s) != TCP_CA_Recovery && tp->snd_una != old_una)
		tcp_dart.cong_avoid(s->c.sk, tp->snd_una, 1);

	if (nstat(DART_STAT_SS_EXIT) != exits && !s->ss_exit_cwnd) {
		s->ss_exit_cwnd = tp->snd_cwnd;
		s->ss_exit_qlen = s->q_len;
	}
	if (nstat(DART_STAT_DECREASE) != dec) {
		if (s->last_decrease)
			s->min_decrease_gap = min(s->min_decrease_gap,
						  s->now - s->last_decrease);
		s->last_decrease = s->now;
	}
	if (sim_state(s) == TCP_CA_Open)
		s->min_cwnd_open = min(s->min_cwnd_open, tp->snd_cwnd);
	s->max_cwnd = max(s->max_cwnd, tp->snd_cwnd);
	sim_send(s);
}

static void sim_rto(struct sim *s)
{
	s->rtos++;
	sim_enter(s, TCP_CA_Loss);
	while (s->l_len) {
		u32 l = s->lost[s->l_head++];

		s->l_len--;
		s->inflight--;
		sim_retransmit(s, l);
	}
	s->last_progress = s->now;
}

static void sim_link_done(struct sim *s)
{
	u32 id = s->q[s->q_head++];

	s->q_len--;
	if (id != SIM_CROSS) {
		u64 at = s->now + s->sc->prop_rtt_us;

		if (s->sc->pulse_us && sim_in(s, s->sc->pulse_from_ms, s->sc->pulse_to_ms))
			at += s->sc->pulse_us;
		at = max(at, s->last_ack_at);	/* the return path is FIFO */
		s->last_ack_at = at;
		s->ack_seq[s->a_head + s->a_len] = id;
		s->ack_at[s->a_head + s->a_len] = at;
		s->a_len++;
	}
	if (s->q_len)
		s->tx_done = s->now + s->sc->service_us;
}

static void sim_sample(struct sim *s)
{
	struct sample *p = &s->samples[s->nsamples++];

	p->t_ms = s->now / 1000;
	p->cwnd = s->c.tp.snd_cwnd;
	p->ssthresh = s->c.tp.snd_ssthresh;
	p->qlen = s->q_len;
	p->delivered = s->c.tp.delivered;
	p->decreases = nstat(DART_STAT_DECREASE) - s->stats0[DART_STAT_DECREASE];
	p->state = sim_state(s);
}

static void sim_run(struct sim *s)
{
	const struct scenario *sc = s->sc;
	struct tcp_sock *tp = &s->c.tp;
	u32 cap = (u32)((u64)sc->duration_ms * 1000 / sc->service_us);
	int i;

	s->max_pkts = 4 * cap + 100000;
	s->q_cap = 3 * s->max_pkts;
	s->sent_at = calloc(s->max_pkts, sizeof(*s->sent_at));
	s->flags = calloc(s->max_pkts, sizeof(*s->flags));
	s->q = calloc(s->q_cap, sizeof(*s->q));
	s->ack_seq = calloc(s->q_cap, sizeof(*s->ack_seq));
	s->ack_at = calloc(s->q_cap, sizeof(*s->ack_at));
	s->lost = calloc(s->q_cap, sizeof(*s->lost));
	s->samples = calloc(sc->duration_ms / 10 + 2, sizeof(*s->samples));
	if (!s->sent_at || !s->flags || !s->q || !s->ack_seq || !s->ack_at ||
	    !s->lost || !s->samples) {
		s->overflow = true;
		return;
	}

	/* a fresh connection, as after tcp_init_transfer() */
	memset(&s->c, 0, sizeof(s->c));
	s->c.sk = (struct sock *)tp;
	s->c.ca = inet_csk_ca(s->c.sk);
	tp->inet_conn.icsk_ca_ops = &tcp_dart;
	tp->snd_cwnd = 10;
	tp->snd_ssthresh = TCP_INFINITE_SSTHRESH;
	tp->snd_cwnd_clamp = U32_MAX;
	tcp_dart.init(s->c.sk);

	for (i = 0; i < DART_STAT_MAX; i++)
		s->stats0[i] = nstat(i);
	s->rng = 0x2545f4914f6cdd1dULL;
	s->end = (u64)sc->duration_ms * 1000;
	s->next_cross = (u64)sc->cross_from_ms * 1000;
	s->min_cwnd_open = U32_MAX;
	s->min_decrease_gap = SIM_NEVER;
	sim_send(s);

	while (!s->overflow) {
		u64 t_tx = s->q_len ? s->tx_done : SIM_NEVER;
		u64 t_ack = s->a_len ? s->ack_at[s->a_head] : SIM_NEVER;
		u64 t_cross = SIM_NEVER, t_app = SIM_NEVER, t_rto = SIM_NEVER;
		u64 t;

		if (sc->cross_gap_us && s->next_cross < (u64)sc->cross_to_ms * 1000)
			t_cross = s->next_cross;
		if (sc->app_pps && s->inflight < tp->snd_cwnd)
			t_app = ((u64)tp->snd_nxt * USEC_PER_SEC + sc->app_pps - 1) /
				sc->app_pps;
		if (s->l_len && !s->q_len && !s->a_len)
			t_rto = s->last_progress + SIM_RTO_US;

		t = min(min(min(t_tx, t_ack), min(t_cross, t_app)),
			min(t_rto, (u64)s->next_sample));
		if (t >= s->end)
			break;
		s->now = t;

		if (t == s->next_sample) {
			sim_sample(s);
			s->next_sample += SIM_SAMPLE_US;
		} else if (t == t_tx) {
			sim_link_done(s);
		} else if (t == t_ack) {
			u32 seq = s->ack_seq[s->a_head++];

			s->a_len--;
			sim_ack(s, seq);
		} else if (t == t_cross) {
			sim_enqueue(s, SIM_CROSS, false);
			s->next_cross += sc->cross_gap_us;
		} else if (t == t_rto) {
			sim_rto(s);
		} else {
			sim_send(s);
		}
	}
}

static void sim_free(struct sim *s)
{
	free(s->sent_at);
	free(s->flags);
	free(s->q);
	free(s->ack_seq);
	free(s->ack_at);
	free(s->lost);
	free(s->samples);
}

/* ---- analysis helpers over the 10 ms samples ---- */

static int cmp_u32(const void *a, const void *b)
{
	u32 x = *(const u32 *)a, y = *(const u32 *)b;

	return x < y ? -1 : x > y;
}

enum { M_CWND, M_QLEN };

static u32 sim_median(const struct sim *s, int what, u32 from_ms, u32 to_ms)
{
	u32 *v = malloc(s->nsamples * sizeof(*v)), n = 0, i, med;

	for (i = 0; i < s->nsamples; i++)
		if (s->samples[i].t_ms >= from_ms && s->samples[i].t_ms < to_ms)
			v[n++] = what == M_CWND ? s->samples[i].cwnd : s->samples[i].qlen;
	if (!n) {
		free(v);
		return 0;
	}
	qsort(v, n, sizeof(*v), cmp_u32);
	med = v[n / 2];
	free(v);
	return med;
}

static const struct sample *sim_at(const struct sim *s, u32 t_ms)
{
	u32 i = min(t_ms / 10, s->nsamples - 1);

	return &s->samples[i];
}

/* DART's delivered packets per second over [from, to) */
static double sim_rate(const struct sim *s, u32 from_ms, u32 to_ms)
{
	return (sim_at(s, to_ms)->delivered - sim_at(s, from_ms)->delivered) *
	       1000.0 / (to_ms - from_ms);
}

/* fraction of the bottleneck DART used over [from, to) */
static double sim_util(const struct sim *s, u32 from_ms, u32 to_ms)
{
	return sim_rate(s, from_ms, to_ms) * s->sc->service_us / 1e6;
}

/* median queueing delay at the bottleneck in us (includes cross traffic) */
static u32 sim_qdelay(const struct sim *s, u32 from_ms, u32 to_ms)
{
	return sim_median(s, M_QLEN, from_ms, to_ms) * s->sc->service_us;
}

static long sim_delta(const struct sim *s, enum dart_stat st)
{
	return nstat(st) - s->stats0[st];
}

static long sim_decreases(const struct sim *s, u32 from_ms, u32 to_ms)
{
	return sim_at(s, to_ms)->decreases - sim_at(s, from_ms)->decreases;
}

static void sim_thresholds(const struct sim *s, u32 *low, u32 *high)
{
	dart_thresholds(s->c.ca->base_rtt_us, low, high);
}

/* ---- per-scenario expectations ---- */

/*
 * A deep buffer can still overflow during the initial slow start (the
 * round-boundary exit reacts about one round late, see docs/algorithm.md),
 * but congestion avoidance must not cause losses.
 */
static void check_no_ca_loss(struct sim *s)
{
	CHECK(s->losses == 0 || s->last_loss_ms < 3000,
	      "loss at %u ms, after slow start", s->last_loss_ms);
}

/* DART alone on a deep buffer: queue kept near LOW..HIGH, link kept busy */
static void check_steady(struct sim *s, u32 from_ms)
{
	u32 to_ms = s->sc->duration_ms - 10, low, high, qd;
	double util = sim_util(s, from_ms, to_ms);

	sim_thresholds(s, &low, &high);
	qd = sim_qdelay(s, from_ms, to_ms);
	check_no_ca_loss(s);
	CHECK(sim_delta(s, DART_STAT_SS_EXIT) >= 1, "slow start never exited on delay");
	CHECK(sim_delta(s, DART_STAT_HOLD) >= 1, "Rule B never ran");
	CHECK(sim_delta(s, DART_STAT_DECREASE) >= 1, "Rule C never decreased");
	CHECK(util >= 0.95, "utilization %.3f < 0.95 after %u ms", util, from_ms);
	CHECK(qd >= low / 2 && qd <= high * 3 / 2,
	      "median queue delay %u us outside [LOW/2, 1.5*HIGH] = [%u, %u]",
	      qd, low / 2, high * 3 / 2);
}

static void check_low_rtt(struct sim *s)
{
	check_steady(s, 10000);
}

static void check_high_rtt(struct sim *s)
{
	check_steady(s, 15000);
}

static void check_shallow(struct sim *s)
{
	double util = sim_util(s, 10000, s->sc->duration_ms - 10);

	CHECK(s->losses >= 1, "no loss with a 15-packet buffer");
	CHECK(sim_delta(s, DART_STAT_LOSS) == s->losses, "ssthresh() calls");
	CHECK(util >= 0.90, "utilization %.3f < 0.90 after recovery", util);
}

static void check_random_loss(struct sim *s)
{
	u32 med = sim_median(s, M_CWND, 15000, 30000);

	CHECK(s->losses >= 10, "only %u losses at 0.5%% drop rate", s->losses);
	CHECK(med >= 8, "median cwnd %u: stuck near the minimum", med);
}

static void check_cross(struct sim *s)
{
	u32 before = sim_median(s, M_CWND, 8000, 15000);
	u32 during = sim_median(s, M_CWND, 17000, 25000);
	u32 after = sim_median(s, M_CWND, 32000, 40000);
	u32 low, high, qd = sim_qdelay(s, 17000, 25000);

	sim_thresholds(s, &low, &high);
	check_no_ca_loss(s);
	CHECK(sim_decreases(s, 15000, 25000) >= 1, "no decrease under cross traffic");
	CHECK(sim_delta(s, DART_STAT_GROW) >= 1, "Rule A never ran");
	CHECK(qd <= high * 3 / 2, "median queue delay %u us > 1.5*HIGH (%u)",
	      qd, high * 3 / 2);
	CHECK(during * 4 <= before * 3, "cwnd %u -> %u: did not yield", before, during);
	CHECK(after * 10 >= before * 9, "cwnd %u -> %u: did not recover", before, after);
}

static void check_pulse(struct sim *s)
{
	u32 before = sim_median(s, M_CWND, 5000, 10000);
	u32 after = sim_median(s, M_CWND, 25000, 30000);

	check_no_ca_loss(s);
	CHECK(sim_decreases(s, 10000, 15000) >= 1, "no decrease during the pulse");
	CHECK(sim_delta(s, DART_STAT_GROW) >= 1, "Rule A never ran");
	CHECK(after * 10 >= before * 9, "cwnd %u -> %u: did not recover", before, after);
}

static void check_app_limited(struct sim *s)
{
	double rate = sim_rate(s, 5000, s->sc->duration_ms - 10);

	CHECK(s->losses == 0, "%u losses", s->losses);
	CHECK(sim_delta(s, DART_STAT_DECREASE) == 0, "decreased while app-limited");
	CHECK(sim_delta(s, DART_STAT_GROW) == 0, "Rule A grew while app-limited");
	CHECK(rate >= s->sc->app_pps * 0.98 && rate <= s->sc->app_pps * 1.02,
	      "delivered %.0f pkt/s, app offers %u", rate, s->sc->app_pps);
	CHECK(s->max_cwnd <= 64, "cwnd grew to %u without being used", s->max_cwnd);
}

static const struct scenario scenarios[] = {
	{ "low_rtt", "40 Mbit/s, 10 ms RTT, 500-pkt buffer",
	  .service_us = 300, .prop_rtt_us = 10000, .buf_pkts = 500,
	  .duration_ms = 20000, .check = check_low_rtt },
	{ "high_rtt", "40 Mbit/s, 80 ms RTT, 1000-pkt buffer",
	  .service_us = 300, .prop_rtt_us = 80000, .buf_pkts = 1000,
	  .duration_ms = 30000, .check = check_high_rtt },
	{ "shallow_buffer", "40 Mbit/s, 40 ms RTT, 15-pkt buffer",
	  .service_us = 300, .prop_rtt_us = 40000, .buf_pkts = 15,
	  .duration_ms = 30000, .check = check_shallow },
	{ "random_loss", "40 Mbit/s, 40 ms RTT, 0.5% random loss",
	  .service_us = 300, .prop_rtt_us = 40000, .buf_pkts = 300,
	  .duration_ms = 30000, .loss_ppm = 5000, .check = check_random_loss },
	{ "cross_traffic", "40 Mbit/s, 40 ms RTT, 20 Mbit/s CBR at 15-25 s",
	  .service_us = 300, .prop_rtt_us = 40000, .buf_pkts = 500,
	  .duration_ms = 40000, .cross_gap_us = 600, .cross_from_ms = 15000,
	  .cross_to_ms = 25000, .check = check_cross },
	{ "delay_pulse", "40 Mbit/s, 20 ms RTT, +25 ms path delay at 10-15 s",
	  .service_us = 300, .prop_rtt_us = 20000, .buf_pkts = 500,
	  .duration_ms = 30000, .pulse_us = 25000, .pulse_from_ms = 10000,
	  .pulse_to_ms = 15000, .check = check_pulse },
	{ "app_limited", "40 Mbit/s, 20 ms RTT, app sends 12 Mbit/s",
	  .service_us = 300, .prop_rtt_us = 20000, .buf_pkts = 500,
	  .duration_ms = 20000, .app_pps = 1000, .check = check_app_limited },
};

static void write_csv(const struct sim *s, const char *dir)
{
	char path[512];
	FILE *f;
	u32 i;

	snprintf(path, sizeof(path), "%s/%s.csv", dir, s->sc->name);
	f = fopen(path, "w");
	if (!f) {
		perror(path);
		return;
	}
	fprintf(f, "t_ms,cwnd,ssthresh,queue_pkts,queue_delay_ms,ca_state,"
		"delivered,decreases\n");
	for (i = 0; i < s->nsamples; i++) {
		const struct sample *p = &s->samples[i];

		fprintf(f, "%u,%u,%u,%u,%.3f,%u,%u,%ld\n", p->t_ms, p->cwnd,
			p->ssthresh, p->qlen, p->qlen * s->sc->service_us / 1000.0,
			p->state, p->delivered, p->decreases);
	}
	fclose(f);
}

static void run_scenario(const struct scenario *sc, const char *csv_dir)
{
	struct sim *s = calloc(1, sizeof(*s));
	int failed = n_failed;
	u32 low, high, to = sc->duration_ms - 10;

	s->sc = sc;
	sim_run(s);
	printf("  %-15s %s\n", sc->name, sc->what);
	CHECK(!s->overflow, "simulation ran out of buffers");
	if (s->overflow)
		goto out;
	sim_thresholds(s, &low, &high);
	printf("    base_rtt=%.1fms LOW=%.1fms HIGH=%.1fms BDP=%u pkts\n"
	       "    slow-start exit at cwnd=%u (queue %u pkts) | losses=%u "
	       "(first %u ms, last %u ms) rtos=%u\n"
	       "    last half: util=%.3f median cwnd=%u queue=%.1fms\n"
	       "    rounds: ss_exit=%ld grow=%ld hold=%ld decrease=%ld "
	       "rate_up=%ld no_rate=%ld few=%ld\n",
	       s->c.ca->base_rtt_us / 1000.0, low / 1000.0, high / 1000.0,
	       s->c.ca->base_rtt_us / sc->service_us,
	       s->ss_exit_cwnd, s->ss_exit_qlen, s->losses, s->first_loss_ms,
	       s->last_loss_ms, s->rtos,
	       sim_util(s, to / 2, to), sim_median(s, M_CWND, to / 2, to),
	       sim_qdelay(s, to / 2, to) / 1000.0,
	       sim_delta(s, DART_STAT_SS_EXIT), sim_delta(s, DART_STAT_GROW),
	       sim_delta(s, DART_STAT_HOLD), sim_delta(s, DART_STAT_DECREASE),
	       sim_delta(s, DART_STAT_RATE_UP), sim_delta(s, DART_STAT_NO_RATE),
	       sim_delta(s, DART_STAT_FEW_SAMPLES));

	/* properties every scenario must have */
	CHECK(shim_warnings == 0, "tcp_snd_cwnd_set() warnings: %d", shim_warnings);
	CHECK(s->bad_ssthresh == 0, "%u wrong ssthresh values", s->bad_ssthresh);
	CHECK(s->min_cwnd_open >= DART_MIN_CWND, "cwnd fell to %u", s->min_cwnd_open);
	CHECK(s->min_decrease_gap == SIM_NEVER ||
	      s->min_decrease_gap >= sc->prop_rtt_us,
	      "two decreases %llu us apart (< one RTT)",
	      (unsigned long long)s->min_decrease_gap);
	sc->check(s);
	if (csv_dir)
		write_csv(s, csv_dir);
out:
	printf("    %s\n", n_failed == failed ? "PASS" : "FAIL");
	sim_free(s);
	free(s);
}

int main(int argc, char **argv)
{
	static const struct { const char *name; void (*fn)(void); } tests[] = {
		{ "registration", test_registration },
		{ "thresholds", test_thresholds },
		{ "ssthresh_on_loss", test_ssthresh },
		{ "undo_cwnd", test_undo },
		{ "rtt_samples", test_rtt_samples },
		{ "rule_a_grow", test_rule_a },
		{ "rule_b_hold", test_rule_b },
		{ "rule_c_plateau", test_rule_c },
		{ "min_cwnd_regrowth", test_min_cwnd_and_regrowth },
		{ "slow_start", test_slow_start },
		{ "few_samples", test_few_samples },
		{ "state_resets", test_state_resets },
		{ "loss_state", test_loss_state },
		{ "seq_wraparound", test_wraparound },
		{ "stats_param", test_stats_param },
	};
	const char *csv_dir = NULL;
	size_t i;
	int opt;

	while ((opt = getopt(argc, argv, "vo:")) != -1) {
		if (opt == 'v')
			shim_verbose = debug = true;
		else if (opt == 'o')
			csv_dir = optarg;
		else
			return 2;
	}

	printf("Part 1: unit tests\n");
	for (i = 0; i < sizeof(tests) / sizeof(tests[0]); i++) {
		int failed = n_failed;

		tests[i].fn();
		printf("  %-20s %s\n", tests[i].name,
		       n_failed == failed ? "PASS" : "FAIL");
	}

	printf("Part 2: packet-level scenarios\n");
	for (i = 0; i < sizeof(scenarios) / sizeof(scenarios[0]); i++)
		run_scenario(&scenarios[i], csv_dir);

	printf("%d checks, %d failed\n", n_checks, n_failed);
	return n_failed ? 1 : 0;
}
