import json
import os
import random
import select
import socket
import string
import struct
import time
import cookie
import tcpinfo

# Give up on connect() after this many seconds
CONNECT_TIMEOUT = 10

# Give up on any single send/recv that makes no progress
# for this many seconds (non-responsive/overloaded server)
IO_TIMEOUT = 30

# Send data in chunks not exceeding this size. This TCP
# block size is from iperf_api.h
DATASIZE = 128 * 1024

# iperf3 states that signal failure. States travel on CTRL
# as a single *signed* byte, so these arrive as 0xff, 0xfe.
SERVER_TERMINATE = 11
ACCESS_DENIED = -1
SERVER_ERROR = -2

# A $-string template for the parameters that will be sent
# to the server before the test during negotiation.
PARAM_TEMPLATE = string.Template('{"tcp":true,"omit":0,"time":$duration,"num":0,' + \
                                 '"blockcount":0,"parallel":1,"len":131072,' + \
                                 '"pacing_timer":1000,"client_version":"3.16"}')

# Column order for samples.csv (see docs/csv-format.md)
SAMPLE_COLUMNS = ["run_id", "t_s", "bytes_acked", "goodput_mbps", "cwnd_segs",
                  "srtt_ms", "rttvar_ms", "min_rtt_ms", "total_retrans",
                  "retrans_delta", "pacing_rate_mbps"]


# Type of exception raised when iperf3 server does
# something bad
class IperfError(Exception):
    pass


# Receive an exact number of bytes from the socket.
#
# Blocks until the desired length is met by incoming
# data. When the data is available, it is returned as
# as a bytes object.
#
# If the connection is closed before n bytes can be
# received, an IperfError exception is raised.
def recv_exact(sock, n):
    # recv() may return fewer bytes than asked for, and returns
    # b"" once the peer has closed the connection
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise IperfError("Server closed the connection prematurely")
        buf += chunk
    return bytes(buf)

# Get a state update from the server (blocking).
#
# If the server sets the state to the expected state,
# nothing happens, and None is returned.
#
# If the server encounters an errorr or unilaterally
# terminates the test, an IperfError is raised.
#
# An IperfError is also raised if the state update
# is valid, but does not match expected.
def recv_state(sock, expected):
    # Signed byte, so ACCESS_DENIED arrives as -1 (not 255)
    state = struct.unpack("b", recv_exact(sock, 1))[0]

    print(f"Got: {state & 0xff:#x}")

    if state == expected:
        return

    if state == ACCESS_DENIED:
        # Sent by servers that are busy running another test
        raise IperfError("Server denied access (busy or rate-limited)")

    if state == SERVER_ERROR:
        # Followed by the server's iperf3 error code and errno,
        # each a 4-byte big-endian int
        i_errno, err = struct.unpack("!ii", recv_exact(sock, 8))
        raise IperfError(f"Server reported an error "
                         f"(i_errno={i_errno}, errno={err})")

    if state == SERVER_TERMINATE:
        raise IperfError("Server terminated the test")

    raise IperfError(f"Server said something invalid "
                     f"(got {state & 0xff:#x}, expected {expected:#x})")

# Send a bytestring of variable length by first sending an
# integer specifying the length followed by the encoded
# string itself.
#
# The argument should be a bytes object (not a
# conventional Python string)
def send_varbytes(sock, bytestring):
    # Length indicator is 32-bit (4 bytes) and
    # must be big-endian
    length = len(bytestring)
    length_bstr = length.to_bytes(4, "big")
    
    sock.sendall(length_bstr)
    sock.sendall(bytestring)

# Send an ordinary Python string of variable length by
# first sending an integer specifying the length followed
# by the UTF-8 encoding of the string.
#
# For dumb bytes or strings of different encodings, see
# send_varbytes() above.
def send_varstr(sock, pystring):
    send_varbytes(sock, pystring.encode())

# Open a TCP socket and connect to address and port.
# A timeout is used for the connection. See the constant
# CONNECT_TIMEOUT above.
#
# If algo is given, it is set before connecting.
#
# Exceptions from socket module are allowed to propagate
# on failure.
def connect(addr, port, algo=None):
    last_err = None
    # Try each address the name resolves to (like create_connection)
    for family, socktype, proto, _, sockaddr in \
            socket.getaddrinfo(addr, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, socktype, proto)
        try:
            if algo:
                try:
                    tcpinfo.set_congestion(sock, algo)
                except OSError as e:
                    # Local setup problem, not the server's fault
                    raise IperfError(f"Can't select congestion control "
                                     f"'{algo}' on this host: {e}") from e
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(sockaddr)
            sock.settimeout(IO_TIMEOUT)
            return sock
        except IperfError:
            sock.close()
            raise
        except OSError as e:
            sock.close()
            last_err = e
    raise last_err if last_err else OSError(f"No addresses for {addr}")

# Shut down a TCP socket for writing and then wait
# for the other end to hang up before destroying the
# socket.
def sync_close(sock):
    sock.shutdown(socket.SHUT_WR)
    
    while sock.recv(128):
        pass
    
    sock.close()

# Client results JSON, shaped like the official iperf3 client's.
def build_results(bytes_written, elapsed, algo_used, retransmits,
                  cpu_user, cpu_system):
    return json.dumps({
        "cpu_util_total": cpu_user + cpu_system,
        "cpu_util_user": cpu_user,
        "cpu_util_system": cpu_system,
        "sender_has_retransmits": 1,
        "congestion_used": algo_used,
        "streams": [{
            "id": 1,
            "bytes": bytes_written,
            "retransmits": retransmits,
            "jitter": 0,
            "errors": 0,
            "omitted_errors": 0,
            "packets": 0,
            "omitted_packets": 0,
            "start_time": 0,
            "end_time": elapsed,
        }],
    }, separators=(",", ":"))

# Samples TCP_INFO and writes samples.csv rows. Goodput comes
# from bytes_acked, not bytes handed to send().
class Sampler:
    def __init__(self, sock, run_id, writer):
        self.sock = sock
        self.run_id = run_id
        self.writer = writer
        self.start = None
        self.prev_t = None
        self.prev_acked = None
        self.prev_retrans = None
        self.last = None   # most recent tcp_info dict

    # Record the baseline at the moment data starts flowing
    def begin(self, now):
        info = tcpinfo.read_tcp_info(self.sock)
        self.start = now
        self.prev_t = now
        self.prev_acked = info["bytes_acked"]
        self.prev_retrans = info["total_retrans"]
        self.last = info

    # Take one sample and write it as a CSV row
    def sample(self, now):
        info = tcpinfo.read_tcp_info(self.sock)
        dt = now - self.prev_t
        if dt <= 0:
            return
        acked = info["bytes_acked"]
        goodput_mbps = (acked - self.prev_acked) * 8 / dt / 1e6

        if self.writer is not None:
            self.writer.writerow([
                self.run_id,
                f"{now - self.start:.3f}",
                acked,
                f"{goodput_mbps:.3f}",
                info["snd_cwnd"],
                f"{info['rtt'] / 1000:.3f}",
                f"{info['rttvar'] / 1000:.3f}",
                f"{info['min_rtt'] / 1000:.3f}",
                info["total_retrans"],
                info["total_retrans"] - self.prev_retrans,
                f"{info['pacing_rate'] * 8 / 1e6:.3f}",
            ])

        self.prev_t = now
        self.prev_acked = acked
        self.prev_retrans = info["total_retrans"]
        self.last = info


# Run one iperf3 test. algo=None uses the kernel default;
# samples_writer=None skips sample logging. Returns a dict with
# status ("ok"/"failed"), error, algo_actual, bytes_written and
# server_reported_bytes. Network/server errors don't raise.
def run_test(serv_addr, serv_port, duration, algo=None, interval=0.2,
             run_id="", samples_writer=None):
    # Fill in the parameter template with our test duration
    params = PARAM_TEMPLATE.substitute(duration=duration)

    # Generate a token that identifies our iperf3 session
    # (and encode it as a UTF-8 bytestring)
    sid = cookie.generate_cookie().encode()

    # Create some garbage data to send
    data = random.randbytes(DATASIZE)

    result = {
        "status": "failed",
        "error": None,
        "algo_actual": None,
        "bytes_written": 0,
        "server_reported_bytes": None,
    }

    # Sockets for control and data streams
    ctrlsock = None
    datasock = None

    try:
        # Check the algorithm works locally before contacting the server
        if algo:
            with socket.socket() as probe:
                try:
                    tcpinfo.set_congestion(probe, algo)
                except OSError as e:
                    raise IperfError(f"Can't select congestion control "
                                     f"'{algo}' on this host: {e}") from e

        # Create control (CTRL) socket and connect
        ctrlsock = connect(serv_addr, serv_port)
        
        # Eliminate unnecessary buffering on CTRL connection
        ctrlsock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Send the coookie to the server over the CTRL connection
        ctrlsock.sendall(sid)

        # Receive a state update from the server
        recv_state(ctrlsock, 0x09)
        print("Ready to exchange parameters")
        
        # Send the parameters as JSON
        send_varstr(ctrlsock, params)

        # Receive another state update (a server that rejects
        # our parameters answers with SERVER_ERROR or hangs up)
        recv_state(ctrlsock, 0x0a)
        print("Ready to create streams")

        # Create data (DATA) socket and connect
        # (with the requested congestion control algorithm)
        datasock = connect(serv_addr, serv_port, algo)
        result["algo_actual"] = tcpinfo.get_congestion(datasock)
        if algo and result["algo_actual"] != algo:
            raise IperfError(f"Asked for '{algo}' but the data socket "
                             f"uses '{result['algo_actual']}'")

        # Send the same cookie as before to identify our session
        datasock.sendall(sid)

        # Receive a third state update on CTRL (!)
        recv_state(ctrlsock, 0x01)
        print("Server wants to start test")

        # Receive a fourth state update on CTRL
        recv_state(ctrlsock, 0x02)
        print("Server considers test to be running")

        # Send all data

        # select() waits for buffer space or the next sample, so
        # samples stay on schedule even when sending is blocked
        print("About to send data blocks...")
        
        sampler = Sampler(datasock, run_id, samples_writer)
        cpu_start = os.times()
        launch_time = time.monotonic()
        done_at = launch_time + duration
        next_sample = launch_time + interval
        last_progress = launch_time
        sampler.begin(launch_time)
        
        offset = 0          # position within the data block
        bytes_written = 0   # total bytes handed to send()
        
        while True:
            now = time.monotonic()
        
            if now >= next_sample:
                sampler.sample(now)
                # Skip missed slots instead of bursting to catch up
                while next_sample <= now:
                    next_sample += interval
        
            if now >= done_at:
                break

            if now - last_progress > IO_TIMEOUT:
                raise TimeoutError

            wait = min(next_sample, done_at) - now
            _, writable, _ = select.select([], [datasock], [], wait)
            if writable:
                n = datasock.send(data[offset:])
                offset = (offset + n) % len(data)
                bytes_written += n
                last_progress = time.monotonic()

        end_time = time.monotonic()
        # Final sample, unless the loop just took one
        if end_time - sampler.prev_t > interval / 4:
            sampler.sample(end_time)
        elapsed = end_time - launch_time
        cpu_end = os.times()
        result["bytes_written"] = bytes_written
        
        print("Done sending data")
        
        # Seeks to avoid a race condition where the data is in
        # flight when the test terminates.
        # TODO: make sure this doesn't impact result accuracy
        time.sleep(0.5)

        # Send a state update on CTRL ending the test
        print("Sending end-of-test")
        ctrlsock.sendall((0x04).to_bytes())

        # Receive a fifth state update on CTRL
        recv_state(ctrlsock, 0x0d)
        print("Server is ready to exchange results")
        
        # Send the JSON result string to the server
        cpu_user = (cpu_end.user - cpu_start.user) / elapsed * 100
        cpu_system = (cpu_end.system - cpu_start.system) / elapsed * 100
        send_varstr(ctrlsock, build_results(
            bytes_written, round(elapsed, 6), result["algo_actual"],
            sampler.last["total_retrans"], cpu_user, cpu_system))
        
        # Get JSON results from the server
        reslen = struct.unpack("!i", recv_exact(ctrlsock, 4))[0]
        resjson = recv_exact(ctrlsock, reslen).decode()
        try:
            result["server_reported_bytes"] = \
                json.loads(resjson)["streams"][0]["bytes"]
        except (ValueError, KeyError, IndexError, TypeError):
            pass  # unexpected shape; not needed for our measurements

        # Receive a sixth state update on CTRL
        recv_state(ctrlsock, 0x0e)
        print("Server is ready for us to display results")
        
        # Close DATA connection
        datasock.close()
        datasock = None
        
        # Inform server of state transition to IPERF_DONE
        ctrlsock.sendall((0x10).to_bytes())
        
        # Close CTRL connection
        sync_close(ctrlsock)
        ctrlsock = None
        
        # Output
        print("Got results:")
        print(resjson)

        result["status"] = "ok"

    except IperfError as e:
        result["error"] = str(e)
    except socket.gaierror as e:
        result["error"] = f"Couldn't resolve {serv_addr}: {e}"
    except TimeoutError:
        result["error"] = "Timed out waiting on the server"
    except ConnectionError as e:
        # Refused, reset, or broken pipe mid-test
        result["error"] = f"Connection failed: {e}"
    except OSError as e:
        result["error"] = f"Network error: {e}"
    finally:
        # Clean up
        if datasock:
            datasock.close()
        if ctrlsock:
            ctrlsock.close()
    
    if result["error"]:
        print(result["error"])
    return result

if __name__ == "__main__":
    import csv
    import sys

    DEMO_ADDR = "speedtest.chi11.us.leaseweb.net"
    DEMO_PORT = 5201
    
    DEMO_DURATION = 2
    
    # Usage: python3 iperftest.py [host] [port] [algo]
    addr = sys.argv[1] if len(sys.argv) > 1 else DEMO_ADDR
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEMO_PORT
    algo = sys.argv[3] if len(sys.argv) > 3 else None

    # Samples go to stdout so you can see what gets logged
    writer = csv.writer(sys.stdout)
    writer.writerow(SAMPLE_COLUMNS)
    print(run_test(addr, port, DEMO_DURATION, algo, 0.2, "demo", writer))