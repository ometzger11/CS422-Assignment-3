"""
Generate report plots and summary tables from runs.csv + samples.csv
(format: docs/csv-format.md).

Outputs (all in --out):
  timeseries_<server>.pdf   one page per repetition: goodput, cwnd, RTT,
                            retransmits vs time, one line per algorithm
  scatter_<server>.pdf      cwnd / RTT / retransmits vs goodput, all reps pooled,
                            one row per algorithm
  algo_comparison.pdf       mean goodput of every run, per server and algorithm
  summary_goodput.csv/.md   min / median / mean / p95 goodput per server x algorithm
  summary_rtt_retrans.csv/.md  RTT, queueing delay (srtt - min RTT) and
                            retransmits per server x algorithm
  (both summaries end with ALL rows that pool every server)

Only servers where every algorithm x rep has status == "ok" are plotted
(a server dropped partway through is skipped, and listed on stdout).

Usage:
  python3 plots.py --runs runs.csv --samples samples.csv --out plots/
"""

import argparse
import csv
import os
import re
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")  # no display needed (works over SSH / in Docker)
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

# Fixed color + marker per algorithm, so the same algorithm looks the same in
# every figure. Markers give a second cue for grayscale printing / colorblindness.
ALGO_STYLE = {
    "cubic": {"color": "#2a78d6", "marker": "o"},
    "reno":  {"color": "#eb6834", "marker": "s"},
    "bbr":   {"color": "#1baf7a", "marker": "^"},
    "dart":  {"color": "#eda100", "marker": "D"},
}
ALGO_ORDER = ["cubic", "reno", "bbr", "dart"]
OTHER_STYLE = {"color": "#6b6a66", "marker": "X"}

SAMPLE_FLOAT_COLS = ["t_s", "goodput_mbps", "cwnd_segs", "srtt_ms", "rttvar_ms",
                     "min_rtt_ms", "total_retrans", "retrans_delta",
                     "pacing_rate_mbps", "bytes_acked"]

plt.rcParams.update({
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.edgecolor": "#8a8984",
    "axes.labelcolor": "#2b2b29",
    "xtick.color": "#52514e",
    "ytick.color": "#52514e",
    "axes.grid": True,
    "grid.color": "#e4e3df",
    "grid.linewidth": 0.6,
    "lines.linewidth": 1.5,
    "legend.frameon": False,
    "pdf.fonttype": 42,  # embed real fonts so text stays selectable in the PDF
})


def style(algo):
    return ALGO_STYLE.get(algo, OTHER_STYLE)


def algo_sort_key(algo):
    return ALGO_ORDER.index(algo) if algo in ALGO_ORDER else len(ALGO_ORDER)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def load(runs_path, samples_path):
    """Return {run_id: run_dict} for ok runs, each with run["samples"] as
    a dict of numpy arrays sorted by time."""
    runs = {}
    expected = set()            # every (algo, rep) attempted in this batch
    done = defaultdict(set)     # server label -> (algo, rep) pairs that succeeded
    with open(runs_path, newline="") as f:
        for row in csv.DictReader(f):
            rep = int(row.get("rep") or 0)
            label = f'{row["server"]}:{row["port"]}'
            expected.add((row.get("algo_requested"), rep))
            if row.get("status", "ok") != "ok":
                continue
            done[label].add((row.get("algo_requested"), rep))
            row["algo"] = row.get("algo_actual") or row.get("algo_requested") or "?"
            row["rep"] = rep
            row["label"] = label
            runs[row["run_id"]] = row

    # Keep only servers that completed every algorithm x rep, so all
    # algorithms are compared on the same destinations
    incomplete = {label for label, pairs in done.items() if pairs != expected}
    for label in sorted(incomplete):
        print(f"Skipping {label}: did not complete every algorithm/rep")
    runs = {rid: r for rid, r in runs.items() if r["label"] not in incomplete}

    cols = defaultdict(lambda: defaultdict(list))
    with open(samples_path, newline="") as f:
        for row in csv.DictReader(f):
            rid = row["run_id"]
            if rid not in runs:
                continue  # failed run, or no runs.csv entry
            for c in SAMPLE_FLOAT_COLS:
                v = row.get(c, "")
                cols[rid][c].append(float(v) if v not in ("", None) else np.nan)

    for rid, run in list(runs.items()):
        if rid not in cols:
            del runs[rid]  # ok in runs.csv but no samples: nothing to plot
            continue
        arrs = {c: np.asarray(v) for c, v in cols[rid].items()}
        order = np.argsort(arrs["t_s"])
        run["samples"] = {c: a[order] for c, a in arrs.items()}
    return runs


def by_server(runs):
    groups = defaultdict(list)
    for r in runs.values():
        groups[r["label"]].append(r)
    return dict(sorted(groups.items()))


def plot_timeseries(server, server_runs, out_dir):
    """One page per repetition, four stacked panels sharing the time axis.
    All pages for a server share y-limits so reps are directly comparable."""
    panels = [
        ("goodput_mbps", "Goodput (Mbit/s)"),
        ("cwnd_segs", "cwnd (segments)"),
        ("srtt_ms", "Smoothed RTT (ms)"),
        ("retrans_delta", "Retransmits / interval"),
    ]
    # Shared y-limits across every rep and algorithm for this server
    ylims = {}
    for col, _ in panels:
        vals = np.concatenate([r["samples"][col] for r in server_runs])
        top = np.nanmax(vals) if np.isfinite(vals).any() else 1
        ylims[col] = (0, top * 1.08 if top > 0 else 1)
    tmax = max(np.nanmax(r["samples"]["t_s"]) for r in server_runs)

    reps = sorted({r["rep"] for r in server_runs})
    path = os.path.join(out_dir, f"timeseries_{safe_name(server)}.pdf")
    with PdfPages(path) as pdf:
        for rep in reps:
            rep_runs = sorted((r for r in server_runs if r["rep"] == rep),
                              key=lambda r: algo_sort_key(r["algo"]))
            fig, axes = plt.subplots(len(panels), 1, figsize=(7, 8.5), sharex=True)
            for ax, (col, ylabel) in zip(axes, panels):
                for r in rep_runs:
                    s = r["samples"]
                    st = style(r["algo"])
                    if col == "retrans_delta":
                        ax.step(s["t_s"], s[col], where="post", color=st["color"],
                                linewidth=1.0, label=r["algo"])
                    else:
                        ax.plot(s["t_s"], s[col], color=st["color"], label=r["algo"])
                    if col == "srtt_ms":
                        # Base RTT: the gap above it is queueing delay
                        ax.plot(s["t_s"], s["min_rtt_ms"], color=st["color"],
                                linestyle=(0, (2, 2)), linewidth=1.0)
                ax.set_ylabel(ylabel)
                if col == "retrans_delta":
                    # Mostly zeros plus rare bursts: symlog keeps small counts visible
                    ax.set_yscale("symlog", linthresh=1)
                    ax.set_ylim(0, max(ylims[col][1] * 1.5, 10))
                else:
                    ax.set_ylim(*ylims[col])
            axes[-1].set_xlim(0, tmax)
            axes[-1].set_xlabel("Time since data start (s)")
            fig.legend(*axes[0].get_legend_handles_labels(), ncol=len(rep_runs),
                       loc="upper center", bbox_to_anchor=(0.5, 0.965))
            axes[2].text(0.995, 0.97, "dashed = min RTT", transform=axes[2].transAxes,
                         ha="right", va="top", color="#52514e", fontsize=8)
            fig.suptitle(f"{server} — repetition {rep + 1}", y=0.995)
            fig.tight_layout(rect=(0, 0, 1, 0.95))
            pdf.savefig(fig)
            plt.close(fig)
    return path


def plot_scatter(server, server_runs, out_dir):
    """cwnd / RTT / retransmits vs goodput, every sample from every rep.
    One row per algorithm with shared axes: four overlaid point clouds are
    too close in color to tell apart, so each algorithm gets its own row."""
    xs = [
        ("cwnd_segs", "cwnd (segments)"),
        ("srtt_ms", "Smoothed RTT (ms)"),
        ("retrans_delta", "Retransmits / interval"),
    ]
    algos = sorted({r["algo"] for r in server_runs}, key=algo_sort_key)
    fig, axes = plt.subplots(len(algos), len(xs), figsize=(10, 1.9 * len(algos) + 1.0),
                             sharex="col", sharey=True, squeeze=False)
    for row, algo in enumerate(algos):
        st = style(algo)
        rs = [r for r in server_runs if r["algo"] == algo]
        y = np.concatenate([r["samples"]["goodput_mbps"] for r in rs])
        for ax, (col, xlabel) in zip(axes[row], xs):
            x = np.concatenate([r["samples"][col] for r in rs])
            ax.scatter(x, y, s=14, color=st["color"], marker=st["marker"],
                       alpha=0.55, linewidths=0)
            if col == "retrans_delta":
                # Mostly zeros plus rare bursts: symlog shows both
                ax.set_xscale("symlog", linthresh=1)
            elif col != "srtt_ms":  # RTT never reaches 0; let it zoom to the data
                ax.set_xlim(left=0)
            if row == len(algos) - 1:
                ax.set_xlabel(xlabel)
        axes[row][0].set_ylabel(f"{algo}\nGoodput (Mbit/s)")
    # Shared retransmit axis: keep a readable range even with no retransmits
    retrans_ax = axes[0][[c for c, _ in xs].index("retrans_delta")]
    retrans_ax.set_xlim(-0.5, max(10, retrans_ax.get_xlim()[1]))
    axes[0][0].set_ylim(bottom=0)
    fig.suptitle(f"{server} — per-sample metrics vs goodput (all repetitions)")
    fig.tight_layout()
    path = os.path.join(out_dir, f"scatter_{safe_name(server)}.pdf")
    fig.savefig(path)
    plt.close(fig)
    return path


def finite(a):
    return a[np.isfinite(a)]


def summary_row(server, algo, rs):
    """Stats over all samples of runs rs, plus the spread of per-run mean
    goodput across repetitions (run-to-run variance)."""
    def pooled(col):
        return finite(np.concatenate([r["samples"][col] for r in rs]))

    g = pooled("goodput_mbps")
    srtt = pooled("srtt_ms")
    # Queueing delay estimate: smoothed RTT above the connection's min RTT
    queue = finite(np.concatenate([r["samples"]["srtt_ms"] - r["samples"]["min_rtt_ms"]
                                   for r in rs]))
    run_means = [np.nanmean(r["samples"]["goodput_mbps"]) for r in rs]
    run_retrans = [np.nansum(r["samples"]["retrans_delta"]) for r in rs]
    return {
        "server": server,
        "algo": algo,
        "runs": len(rs),
        "samples": len(g),
        "min_mbps": np.min(g),
        "median_mbps": np.median(g),
        "mean_mbps": np.mean(g),
        "p95_mbps": np.percentile(g, 95),
        "run_mean_std_mbps": np.std(run_means, ddof=1) if len(rs) > 1 else np.nan,
        "median_srtt_ms": np.median(srtt),
        "p95_srtt_ms": np.percentile(srtt, 95),
        "median_queue_ms": np.median(queue),
        "p95_queue_ms": np.percentile(queue, 95),
        "mean_retrans_per_run": np.mean(run_retrans),
    }


def summarize(groups):
    """One row per server x algorithm, then ALL rows pooling every server
    (every server ran every algorithm the same number of times)."""
    rows = []
    for server, server_runs in groups.items():
        for algo in sorted({r["algo"] for r in server_runs}, key=algo_sort_key):
            rows.append(summary_row(server, algo,
                                    [r for r in server_runs if r["algo"] == algo]))
    if len(groups) > 1:
        every = [r for rs in groups.values() for r in rs]
        for algo in sorted({r["algo"] for r in every}, key=algo_sort_key):
            rows.append(summary_row("ALL", algo, [r for r in every if r["algo"] == algo]))
    return rows


def fmt(v):
    if isinstance(v, (float, np.floating)):
        return "" if np.isnan(v) else f"{v:.2f}"
    return str(v)


def write_rtt_summary(rows, out_dir):
    cols = ["server", "algo", "runs", "median_srtt_ms", "p95_srtt_ms",
            "median_queue_ms", "p95_queue_ms", "mean_retrans_per_run"]
    csv_path = os.path.join(out_dir, "summary_rtt_retrans.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r[c]) for c in cols])

    md_path = os.path.join(out_dir, "summary_rtt_retrans.md")
    with open(md_path, "w") as f:
        f.write("| Server | Algo | Runs | Median sRTT | p95 sRTT | Median queue "
                "| p95 queue | Retransmits / run |\n")
        f.write("|---|---|---:|---:|---:|---:|---:|---:|\n")
        for r in rows:
            f.write(f"| {r['server']} | {r['algo']} | {r['runs']} | "
                    + " | ".join(fmt(r[c]) for c in cols[3:]) + " |\n")
        f.write("\nRTTs in ms over all samples. Queue = smoothed RTT minus the "
                "connection's min RTT (an estimate of queueing delay). "
                "Retransmits are segments, from tcp_info total_retrans.\n")
    return csv_path, md_path


def write_summary(rows, out_dir):
    cols = ["server", "algo", "runs", "samples", "min_mbps", "median_mbps",
            "mean_mbps", "p95_mbps", "run_mean_std_mbps"]

    csv_path = os.path.join(out_dir, "summary_goodput.csv")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([fmt(r[c]) for c in cols])

    md_path = os.path.join(out_dir, "summary_goodput.md")
    with open(md_path, "w") as f:
        f.write("| Server | Algo | Runs | Min | Median | Mean | p95 | Std of run means |\n")
        f.write("|---|---|---:|---:|---:|---:|---:|---:|\n")
        for r in rows:
            f.write(f"| {r['server']} | {r['algo']} | {r['runs']} | "
                    + " | ".join(fmt(r[c]) for c in cols[4:]) + " |\n")
        f.write("\nGoodput in Mbit/s, computed over all samples from all runs. "
                "Std of run means = spread of per-run mean goodput across repetitions.\n")
    return csv_path, md_path


def plot_comparison(groups, out_dir):
    """Mean goodput of every run: one row per server, algorithms side by side,
    one dot per repetition so run-to-run variance is visible."""
    servers = list(groups)
    algos = sorted({r["algo"] for rs in groups.values() for r in rs}, key=algo_sort_key)
    fig, ax = plt.subplots(figsize=(7, 0.55 * len(servers) + 1.4))
    offsets = np.linspace(-0.22, 0.22, len(algos)) if len(algos) > 1 else [0]
    xmax = 0
    for i, server in enumerate(servers):
        for off, algo in zip(offsets, algos):
            st = style(algo)
            means = [np.nanmean(r["samples"]["goodput_mbps"])
                     for r in groups[server] if r["algo"] == algo]
            if not means:
                continue
            xmax = max(xmax, max(means))
            y = np.full(len(means), i + off)
            ax.scatter(means, y, s=30, color=st["color"], marker=st["marker"],
                       edgecolors="white", linewidths=0.8, zorder=3,
                       label=algo if i == 0 else None)
            ax.plot([min(means), max(means)], [i + off] * 2, color=st["color"],
                    linewidth=1.0, alpha=0.6, zorder=2)
    ax.set_yticks(range(len(servers)))
    ax.set_yticklabels(servers)
    ax.set_ylim(len(servers) - 0.5, -0.5)  # padding above/below, first server on top
    ax.set_xlim(0, xmax * 1.08 if xmax > 0 else 1)  # room so the largest marker isn't clipped
    ax.set_xlabel("Mean goodput per run (Mbit/s)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=len(algos))
    # Title centered on the whole figure, so long server names can't push it off the edge
    fig.suptitle("Mean goodput by server and algorithm (one marker per repetition)")
    fig.tight_layout()
    path = os.path.join(out_dir, "algo_comparison.pdf")
    fig.savefig(path)
    plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs.csv")
    ap.add_argument("--samples", default="samples.csv")
    ap.add_argument("--out", default="plots")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    runs = load(args.runs, args.samples)
    if not runs:
        raise SystemExit("No successful runs with samples found.")
    groups = by_server(runs)

    for server, server_runs in groups.items():
        print(plot_timeseries(server, server_runs, args.out))
        print(plot_scatter(server, server_runs, args.out))
    print(plot_comparison(groups, args.out))
    rows = summarize(groups)
    for p in write_summary(rows, args.out) + write_rtt_summary(rows, args.out):
        print(p)


if __name__ == "__main__":
    main()