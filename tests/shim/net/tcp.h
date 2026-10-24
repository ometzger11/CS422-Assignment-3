/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Userspace stand-in for the kernel API that tcp_dart.c uses, so that
 * tests/dart_sim.c can compile the real tcp_dart.c and drive its callbacks
 * without loading anything into the kernel. The TCP helpers below are
 * copied from Linux 7.0 (include/net/tcp.h, net/ipv4/tcp_cong.c).
 *
 * This is a test harness, not a TCP stack: struct tcp_sock only has the
 * fields DART reads or writes.
 */
#ifndef DART_SHIM_NET_TCP_H
#define DART_SHIM_NET_TCP_H

#include <errno.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t s32;
typedef int64_t s64;

#define U32_MAX		((u32)~0U)
#define USEC_PER_SEC	1000000UL
#define PAGE_SIZE	4096

#define likely(x)	__builtin_expect(!!(x), 1)
#define unlikely(x)	__builtin_expect(!!(x), 0)
#define READ_ONCE(x)	(x)
#define BUILD_BUG_ON(cond)	_Static_assert(!(cond), #cond)

#define min(a, b)	((a) < (b) ? (a) : (b))
#define max(a, b)	((a) > (b) ? (a) : (b))
#define min_t(t, a, b)	min((t)(a), (t)(b))
#define max_t(t, a, b)	max((t)(a), (t)(b))
#define clamp(v, lo, hi)	min(max(v, lo), hi)

static inline u64 div_u64(u64 dividend, u32 divisor)
{
	return dividend / divisor;
}

/* Counts what the kernel would report as a WARN; tests require 0. */
static int shim_warnings;
/* Set to print the module's (normally rate-limited) debug lines. */
static bool shim_verbose;

#ifndef pr_fmt
#define pr_fmt(fmt) fmt
#endif
#define pr_info_ratelimited(fmt, ...)					\
	do {								\
		if (shim_verbose)					\
			printf(pr_fmt(fmt), ##__VA_ARGS__);		\
	} while (0)

/* ---- module / moduleparam / sysfs ---- */

struct module;
#define THIS_MODULE	((struct module *)0)
#define __init
#define __exit
#define __read_mostly

/* Swallow the trailing ';' with a harmless redeclaration. */
#define MODULE_LICENSE(s)		extern int shim_modinfo
#define MODULE_AUTHOR(s)		extern int shim_modinfo
#define MODULE_DESCRIPTION(s)		extern int shim_modinfo
#define MODULE_PARM_DESC(name, s)	extern int shim_modinfo
#define module_param(name, type, perm)	extern int shim_modinfo

struct kernel_param {
	const char *name;
};

struct kernel_param_ops {
	unsigned int flags;
	int (*set)(const char *val, const struct kernel_param *kp);
	int (*get)(char *buffer, const struct kernel_param *kp);
	void (*free)(void *arg);
};

/* The simulator reaches parameters and init/exit through these pointers. */
#define module_param_cb(name, ops, arg, perm)				\
	const struct kernel_param_ops *shim_param_##name = (ops)
#define module_init(fn)	int (*shim_module_init)(void) = (fn)
#define module_exit(fn)	void (*shim_module_exit)(void) = (fn)

typedef struct {
	long counter;
} atomic_long_t;

static inline void atomic_long_inc(atomic_long_t *v)
{
	v->counter++;
}

static inline long atomic_long_read(const atomic_long_t *v)
{
	return v->counter;
}

static inline int sysfs_emit_at(char *buf, int at, const char *fmt, ...)
	__attribute__((format(printf, 3, 4)));
static inline int sysfs_emit_at(char *buf, int at, const char *fmt, ...)
{
	va_list ap;
	int n;

	va_start(ap, fmt);
	n = vsnprintf(buf + at, PAGE_SIZE - at, fmt, ap);
	va_end(ap);
	return n;
}

/* ---- sockets ---- */

enum tcp_ca_state {
	TCP_CA_Open = 0,
	TCP_CA_Disorder = 1,
	TCP_CA_CWR = 2,
	TCP_CA_Recovery = 3,
	TCP_CA_Loss = 4,
};

enum tcp_ca_event {
	CA_EVENT_TX_START,
	CA_EVENT_CWND_RESTART,
	CA_EVENT_COMPLETE_CWR,
	CA_EVENT_LOSS,
	CA_EVENT_ECN_NO_CE,
	CA_EVENT_ECN_IS_CE,
};

struct ack_sample {
	u32 pkts_acked;
	s32 rtt_us;
	u32 in_flight;
};

struct sock;

#define TCP_CA_NAME_MAX		16
#define TCP_INFINITE_SSTHRESH	0x7fffffff

struct tcp_congestion_ops {
	void (*cong_avoid)(struct sock *sk, u32 ack, u32 acked);
	u32 (*ssthresh)(struct sock *sk);
	void (*set_state)(struct sock *sk, u8 new_state);
	void (*cwnd_event)(struct sock *sk, enum tcp_ca_event ev);
	void (*pkts_acked)(struct sock *sk, const struct ack_sample *sample);
	u32 (*undo_cwnd)(struct sock *sk);
	char name[TCP_CA_NAME_MAX];
	struct module *owner;
	u32 flags;
	void (*init)(struct sock *sk);
	void (*release)(struct sock *sk);
};

struct sock {
	int sk_unused;
};

struct inet_connection_sock {
	struct sock sk;		/* first: a sock pointer is an icsk pointer */
	const struct tcp_congestion_ops *icsk_ca_ops;
	u8 icsk_ca_state;
	u64 icsk_ca_priv[104 / sizeof(u64)];	/* same size as Linux 7.0 */
};

#define ICSK_CA_PRIV_SIZE \
	sizeof(((struct inet_connection_sock *)0)->icsk_ca_priv)

struct tcp_sock {
	struct inet_connection_sock inet_conn;	/* first */
	u32 snd_cwnd;
	u32 snd_cwnd_cnt;
	u32 snd_cwnd_clamp;
	u32 snd_ssthresh;
	u32 snd_nxt;
	u32 snd_una;
	u32 delivered;
	u32 prior_cwnd;
	u32 max_packets_out;
	u64 tcp_mstamp;
	bool is_cwnd_limited;
};

#define inet_csk(sk)	((struct inet_connection_sock *)(sk))
#define tcp_sk(sk)	((struct tcp_sock *)(sk))

static inline void *inet_csk_ca(const struct sock *sk)
{
	return (void *)inet_csk(sk)->icsk_ca_priv;
}

/* ---- helpers copied from Linux 7.0 ---- */

static inline bool before(u32 seq1, u32 seq2)
{
	return (s32)(seq1 - seq2) < 0;
}
#define after(seq2, seq1)	before(seq1, seq2)

static inline u32 tcp_stamp_us_delta(u64 t1, u64 t0)
{
	return max_t(s64, t1 - t0, 0);
}

static inline u32 tcp_snd_cwnd(const struct tcp_sock *tp)
{
	return tp->snd_cwnd;
}

static inline void tcp_snd_cwnd_set(struct tcp_sock *tp, u32 val)
{
	if ((int)val <= 0) {	/* WARN_ON_ONCE() in the kernel */
		shim_warnings++;
		printf("  WARN: tcp_snd_cwnd_set(%d)\n", (int)val);
	}
	tp->snd_cwnd = val;
}

static inline bool tcp_in_slow_start(const struct tcp_sock *tp)
{
	return tcp_snd_cwnd(tp) < tp->snd_ssthresh;
}

static inline bool tcp_is_cwnd_limited(const struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);

	if (tp->is_cwnd_limited)
		return true;

	/* If in slow start, ensure cwnd grows to twice what was ACKed. */
	if (tcp_in_slow_start(tp))
		return tcp_snd_cwnd(tp) < 2 * tp->max_packets_out;

	return false;
}

static inline u32 tcp_slow_start(struct tcp_sock *tp, u32 acked)
{
	u32 cwnd = min(tcp_snd_cwnd(tp) + acked, tp->snd_ssthresh);

	acked -= cwnd - tcp_snd_cwnd(tp);
	tcp_snd_cwnd_set(tp, min(cwnd, tp->snd_cwnd_clamp));

	return acked;
}

static inline u32 tcp_reno_undo_cwnd(struct sock *sk)
{
	const struct tcp_sock *tp = tcp_sk(sk);

	return max(tcp_snd_cwnd(tp), tp->prior_cwnd);
}

static struct tcp_congestion_ops *shim_registered;

/* Same checks as tcp_validate_congestion_control() */
static inline int tcp_register_congestion_control(struct tcp_congestion_ops *ca)
{
	if (!ca->ssthresh || !ca->undo_cwnd || !ca->cong_avoid)
		return -EINVAL;
	if (shim_registered)
		return -EEXIST;
	shim_registered = ca;
	return 0;
}

static inline void tcp_unregister_congestion_control(struct tcp_congestion_ops *ca)
{
	if (shim_registered == ca)
		shim_registered = NULL;
}

#endif /* DART_SHIM_NET_TCP_H */
