import argparse
import csv
import random
import time
import datetime as dt
import iperfserver
import iperftest


SERVLIST = "listed_iperf3_servers.json"
# dart is our kernel module (tcp_dart.ko); it must be loaded first
ALGOS = ["cubic", "reno", "bbr", "dart"]

TARGET_NUM = 5
NUM_REPS = 3
NUM_RETRIES = 3
DURATION = 10
INTERVAL = 0.2
MAX_ATTEMPTS = 30   # most servers to try before giving up
PAUSE = 1           # seconds between tests

RUNS_FILE = "runs.csv"
SAMPLES_FILE = "samples.csv"
RUNS_COLUMNS = ["run_id", "server", "port", "algo_requested", "algo_actual",
                "rep", "start_utc", "status", "error"]

# Command-line options override the defaults above
ap = argparse.ArgumentParser(description="Run iperf3 tests against random servers")
ap.add_argument("-n", type=int, default=TARGET_NUM,
                help="number of servers that must complete every test")
ap.add_argument("--duration", type=float, default=DURATION, help="seconds per test")
ap.add_argument("--interval", type=float, default=INTERVAL,
                help="seconds between TCP_INFO samples")
ap.add_argument("--reps", type=int, default=NUM_REPS,
                help="repetitions per server and algorithm")
ap.add_argument("--servers", default=SERVLIST, help="server-list JSON file")
ap.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS,
                help="most servers to try before reporting a shortfall")
ap.add_argument("--runs", default=RUNS_FILE, help="output runs CSV")
ap.add_argument("--samples", default=SAMPLES_FILE, help="output samples CSV")
ap.add_argument("--algos", default=",".join(ALGOS),
                help="comma-separated congestion controls to compare")
args = ap.parse_args()

ALGOS = [a for a in args.algos.split(",") if a]
TARGET_NUM = args.n
NUM_REPS = args.reps
DURATION = args.duration
INTERVAL = args.interval
SERVLIST = args.servers
MAX_ATTEMPTS = args.max_attempts

def parse_port(port_str):
    parts = port_str.split("-")

    if 1 <= len(parts) <= 2:
        return int(parts[0])
    else:
        raise ValueError("Bad port string in JSON server list")

# Run one test and log it: a runs.csv row for every attempt
# (including failures), and samples.csv rows via run_test.
# Never raises, so one bad test can't kill the whole batch.
def logged_test(server, port, algo, rep, run_id):
    start = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    try:
        results = iperftest.run_test(
            server.host,
            port,
            DURATION,
            algo,
            INTERVAL,
            run_id,
            samples_writer
        )
    except Exception as e:
        results = {"status": "failed",
                   "error": f"{type(e).__name__}: {e}",
                   "algo_actual": None}

    runs_writer.writerow([run_id, server.host, port, algo,
                          results["algo_actual"] or "", rep, start,
                          results["status"], results["error"] or ""])

    # Keep data on disk if the batch is interrupted
    runs_file.flush()
    samples_file.flush()

    return results

def robust_repeat(server):
    ever_succeeded = False
    port = parse_port(server.port)

    # Interleave algorithms within each rep, and rotate the starting
    # algorithm every rep (cubic, reno, bbr, dart, then reno, bbr, dart,
    # cubic, ...), so changing network conditions and position in the
    # order hit all algorithms equally
    for rep in range(NUM_REPS):
        shift = rep % len(ALGOS)
        for algo in ALGOS[shift:] + ALGOS[:shift]:
            strikes = 0
    
            while True:
                now_str = dt.datetime.now().isoformat()
                run_id = f"{now_str}-{algo}-r{rep}-s{strikes}"
            
                print("************ " + run_id)
        
                results = logged_test(server, port, algo, rep, run_id)
                time.sleep(PAUSE)
        
                if results["error"] is None:
                    strikes = 0
                
                    ever_succeeded = True
                    break
                else:
                    strikes += 1
                
                    if not ever_succeeded or strikes == NUM_RETRIES:
                        return False
    
    return True


servers = iperfserver.parse_servers(SERVLIST)

runs_file = open(args.runs, "w", newline="")
samples_file = open(args.samples, "w", newline="")
runs_writer = csv.writer(runs_file)
samples_writer = csv.writer(samples_file)
runs_writer.writerow(RUNS_COLUMNS)
samples_writer.writerow(iperftest.SAMPLE_COLUMNS)

successes = 0
failures = 0
accepted = []

print("Algorithms: " + ", ".join(ALGOS) + " (start rotates by one each rep; "
      "runs.csv rows are in execution order)")

while successes < TARGET_NUM:
    serv_count = len(servers)
    
    if serv_count == 0 or successes + failures >= MAX_ATTEMPTS:
        print("\nShortfall occurred!")
        print(f"Managed {successes}/{TARGET_NUM}")
        break
    
    selected_idx = random.randint(0, serv_count - 1)
    selected_server = servers.pop(selected_idx)
    
    print(f"\nChose: {selected_server}")
    
    if robust_repeat(selected_server):
        successes += 1
        accepted.append(selected_server)
        print(f"Accepted: {selected_server}")
    else:
        failures += 1
        print(f"Gave up on: {selected_server}")

runs_file.close()
samples_file.close()

print("\nDone!")
print(f"Successes: {successes}, Failures: {failures}")
print("Servers used: " + ", ".join(f"{s.host}:{parse_port(s.port)}" for s in accepted))