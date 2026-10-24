"""
Linux TCP socket introspection: congestion-control selection and TCP_INFO decoding.

Decodes `struct tcp_info` from <linux/tcp.h>. The kernel copies
min(requested_len, sizeof(struct tcp_info)) bytes, so older kernels return
fewer fields; any field not covered by the returned length is reported as None.

Units (as exposed by the kernel):
  rtt, rttvar, min_rtt, rto, ato  -> microseconds
  snd_cwnd, snd_ssthresh          -> segments (multiply by snd_mss for bytes)
  pacing_rate, delivery_rate      -> bytes/second
  bytes_*                         -> bytes (cumulative over connection lifetime)
  last_* / busy_time etc.         -> milliseconds / microseconds per kernel docs
"""

import socket
import struct

# socket.TCP_CONGESTION / TCP_INFO exist on Linux builds of CPython;
# fall back to the numeric constants from <netinet/tcp.h>.
TCP_INFO = getattr(socket, "TCP_INFO", 11)
TCP_CONGESTION = getattr(socket, "TCP_CONGESTION", 13)

# (name, struct format char) in kernel order. All offsets fall on natural
# alignment boundaries, so no padding is required ('=' = native order, no align).
_FIELDS = [
    # 8 x u8 (the last two bytes are bitfields)
    ("state", "B"), ("ca_state", "B"), ("retransmits", "B"), ("probes", "B"),
    ("backoff", "B"), ("options", "B"), ("wscale", "B"), ("app_limited_bits", "B"),
    # u32 block
    ("rto", "I"), ("ato", "I"), ("snd_mss", "I"), ("rcv_mss", "I"),
    ("unacked", "I"), ("sacked", "I"), ("lost", "I"), ("retrans", "I"), ("fackets", "I"),
    ("last_data_sent", "I"), ("last_ack_sent", "I"),
    ("last_data_recv", "I"), ("last_ack_recv", "I"),
    ("pmtu", "I"), ("rcv_ssthresh", "I"), ("rtt", "I"), ("rttvar", "I"),
    ("snd_ssthresh", "I"), ("snd_cwnd", "I"), ("advmss", "I"), ("reordering", "I"),
    ("rcv_rtt", "I"), ("rcv_space", "I"),
    ("total_retrans", "I"),
    # extended fields (Linux 3.15+ onward, added incrementally)
    ("pacing_rate", "Q"), ("max_pacing_rate", "Q"),
    ("bytes_acked", "Q"), ("bytes_received", "Q"),
    ("segs_out", "I"), ("segs_in", "I"),
    ("notsent_bytes", "I"), ("min_rtt", "I"),
    ("data_segs_in", "I"), ("data_segs_out", "I"),
    ("delivery_rate", "Q"),
    ("busy_time", "Q"), ("rwnd_limited", "Q"), ("sndbuf_limited", "Q"),
    ("delivered", "I"), ("delivered_ce", "I"),
    ("bytes_sent", "Q"), ("bytes_retrans", "Q"),
    ("dsack_dups", "I"), ("reord_seen", "I"),
    ("rcv_ooopack", "I"), ("snd_wnd", "I"),
]

# Precompute (name, fmt, offset, size) for each field.
_LAYOUT = []
_off = 0
for _name, _fmt in _FIELDS:
    _size = struct.calcsize("=" + _fmt)
    _LAYOUT.append((_name, _fmt, _off, _size))
    _off += _size
TCP_INFO_MAX_LEN = _off  # 232 bytes for the fields above

# Request a little extra so newer kernels can't truncate what we know about.
_REQUEST_LEN = 256


def set_congestion(sock, algo):
    """Select the congestion-control algorithm (e.g. "cubic", "reno", "bbr").

    Call before connect(). Raises OSError if the kernel lacks the algorithm
    (ENOENT) or it isn't in net.ipv4.tcp_allowed_congestion_control (EPERM).
    """
    sock.setsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, algo.encode())


def get_congestion(sock):
    """Return the algorithm the kernel is actually using on this socket."""
    raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_CONGESTION, 16)
    return raw.split(b"\0", 1)[0].decode()


def read_tcp_info(sock):
    """Return a dict of decoded tcp_info fields for a live TCP socket.

    Fields the running kernel didn't return are None. The dict also carries
    `_len`, the number of bytes the kernel actually returned.
    """
    raw = sock.getsockopt(socket.IPPROTO_TCP, TCP_INFO, _REQUEST_LEN)
    info = {"_len": len(raw)}
    for name, fmt, off, size in _LAYOUT:
        if off + size <= len(raw):
            info[name] = struct.unpack_from("=" + fmt, raw, off)[0]
        else:
            info[name] = None
    return info


if __name__ == "__main__":
    # Self-test over loopback: open a connection, push some data, dump stats.
    import sys
    import threading

    algo = sys.argv[1] if len(sys.argv) > 1 else None

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def drain():
        conn, _ = srv.accept()
        while conn.recv(1 << 16):
            pass
        conn.close()

    t = threading.Thread(target=drain, daemon=True)
    t.start()

    c = socket.socket()
    if algo:
        set_congestion(c, algo)
    c.connect(srv.getsockname())
    c.sendall(b"x" * (8 << 20))

    info = read_tcp_info(c)
    print(f"congestion control: {get_congestion(c)}")
    print(f"tcp_info bytes returned: {info['_len']} (decoder knows {TCP_INFO_MAX_LEN})")
    for k in ("snd_cwnd", "snd_mss", "rtt", "rttvar", "min_rtt", "retransmits",
              "total_retrans", "lost", "bytes_acked", "bytes_sent",
              "pacing_rate", "delivery_rate"):
        print(f"  {k:14} {info[k]}")
    c.close()
    t.join(timeout=2)