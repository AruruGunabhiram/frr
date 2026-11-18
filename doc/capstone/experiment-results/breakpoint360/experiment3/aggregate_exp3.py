#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Audit the 18-trial Experiment 3 matrix, write aggregate CSVs and render figures.

Run on the host from this directory (or via ``reproduce.sh aggregate``) with
any Python that has Matplotlib (macOS: ``/usr/bin/python3``):

    python3 aggregate_exp3.py

Inputs: trials/<arm>/adv-<N>s/seed-<S>/{exp3_summary.json,
leaf5_state_timeline.csv} written by ``analyze_exp3.py point``.

Outputs:
    results_exp3.csv   one row per trial (18)
    paired_exp3.csv    MRAI 1 and 5 vs MRAI 0 (same arm, same seed) and
                       arm A vs arm B (same MRAI, same seed)
    summary_exp3.csv   mean / min / max over seeds per arm and MRAI
    graphs/01..05_*.png
"""

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent
TRIALS = ROOT / "trials"
GRAPHS = ROOT / "graphs"
ARMS = ("repeat", "keepalive")
ARM_LABEL = {"repeat": "A · Operational (repeat, 0.75 s)",
             "keepalive": "B · Isolated (keepalive, 24 s)"}
ARM_SHORT = {"repeat": "A operational", "keepalive": "B isolated"}
ADVS = (0, 1, 5)
SEEDS = (1, 2, 3)
ROBOTS = 64
MOVES = 384

# Reference palette (dataviz skill): categorical slots 1-2, light surface, ink.
COLOR = {"repeat": "#2a78d6", "keepalive": "#eb6834"}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
MARKER = {0: "o", 1: "s", 5: "D"}

FIELDS = (
    "completed_moves", "achieved_moves_per_s", "bgp_updates", "moving_updates",
    "cooldown_updates", "type2_bgp_updates", "moving_type2_adv", "moving_type2_wd",
    "cooldown_type2_adv", "cooldown_type2_wd", "type2_nlris", "robot_type2_nlris",
    "non_robot_type2_nlris", "updates_per_move", "type2_nlris_per_move",
    "robot_type2_nlris_per_move", "robot_mac_adv_per_move", "robot_mac_wd_per_move",
    "robot_macip_adv_per_move", "robot_macip_wd_per_move", "type2_nlris_per_update",
    "bgp_bytes", "moving_bytes", "cooldown_bytes", "min_session_burst_gap_s",
    "median_session_min_burst_gap_s", "mac_mobility_seq_skips", "mac_seq_skips",
    "macip_seq_skips", "moves_mac_originated", "moves_macip_originated",
    "moves_macip_originated_pct", "moves_macip_only_after_leaving",
    "moves_macip_never_within_30s", "moves_macip_not_during_last_move",
    "macip_after_leaving_lag_p50_s", "mac_seq_ties", "macip_seq_ties",
    "cooldown_all_correct_from_s",
    "bgpd_cpu_seconds", "zebra_cpu_seconds", "bgpd_mean_cpu_pct_one_core",
    "zebra_mean_cpu_pct_one_core", "t_stale_p50_s", "t_stale_p95_s", "t_stale_max_s",
    "moves_correct", "moves_transient_stale", "moves_censored", "moves_persistently_wrong",
    "moves_reverted_after_correct", "persistently_wrong_recovered_later",
    "stale_robot_seconds", "robots_incorrect_at_moves_end", "robots_incorrect_at_end",
    "final_leaf5_correct", "final_leaf5_checked", "probe_loss_pct",
    "capture_links", "capture_gaps", "quiet_bgp_updates", "quiescent_counter_mismatches",
    "end_window_counter_mismatches", "session_changes", "connections_dropped",
    "daemon_log_issue_lines", "monitor_vs_final_fib_mismatches",
    "last_update_after_moves_end_s", "quiescence_wait_s",
    "capture_kernel_filter", "capture_socket_packets", "capture_socket_drops",
    "updates_through_quiescence", "updates_through_quiescence_per_move",
)


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def load_trial(arm, adv, seed):
    point = TRIALS / arm / f"adv-{adv}s" / f"seed-{seed}"
    s = json.loads((point / "exp3_summary.json").read_text())
    # Acceptance (same rules as analyze_exp3.py point, re-checked here).
    assert s["robot"] == arm and s["adv_interval_s"] == adv and s["seed"] == seed, point
    assert s["workload_ok"] and s["completed_moves"] == MOVES, point
    assert s["capture_links"] == 10 and s["capture_gaps"] == 0, point
    assert s["quiescent_counter_mismatches"] == 0 and not s["quiescence_timed_out"], point
    assert not s["config_problems"] and s["session_changes"] == 0, point
    assert s["monitor_vs_final_fib_mismatches"] == 0, point
    assert set(s["adv_interval_config"].values()) == {adv}, point
    row = {"arm": "A" if arm == "repeat" else "B", "robot": arm,
           "adv_interval_s": adv, "seed": seed}
    row.update({k: s[k] for k in FIELDS})
    row["censored_or_wrong_pct"] = 100 * (s["moves_censored"] + s["moves_persistently_wrong"]) / MOVES
    row["stale_robot_seconds_per_move"] = s["stale_robot_seconds"] / MOVES
    row["bytes_per_move"] = s["bgp_bytes"] / MOVES
    timeline = read_csv(point / "leaf5_state_timeline.csv")
    return row, timeline


def pct_change(new, old):
    return 100 * (new - old) / old if old else None


def reduction(new, old):
    return 100 * (old - new) / old if old else None


def pair_rows(rows):
    by = {(r["robot"], r["adv_interval_s"], r["seed"]): r for r in rows}
    out = []
    for arm in ARMS:
        for adv in (1, 5):
            for seed in SEEDS:
                r, base = by[arm, adv, seed], by[arm, 0, seed]
                out.append(dict(
                    comparison="mrai_vs_0", arm=r["arm"], robot=arm, adv_interval_s=adv,
                    reference="adv 0s same arm", seed=seed,
                    update_reduction_pct=reduction(r["bgp_updates"], base["bgp_updates"]),
                    bytes_reduction_pct=reduction(r["bgp_bytes"], base["bgp_bytes"]),
                    type2_nlri_change_pct=pct_change(r["type2_nlris"], base["type2_nlris"]),
                    robot_macip_adv_change_pct=pct_change(r["robot_macip_adv_per_move"],
                                                          base["robot_macip_adv_per_move"]),
                    bgpd_cpu_reduction_pct=reduction(r["bgpd_cpu_seconds"], base["bgpd_cpu_seconds"]),
                    zebra_cpu_reduction_pct=reduction(r["zebra_cpu_seconds"], base["zebra_cpu_seconds"]),
                    t_stale_p50_delta_s=(r["t_stale_p50_s"] or 0) - (base["t_stale_p50_s"] or 0),
                    stale_robot_seconds_ratio=(r["stale_robot_seconds"] / base["stale_robot_seconds"]
                                               if base["stale_robot_seconds"] else None),
                    censored_or_wrong_pct=r["censored_or_wrong_pct"],
                    final_leaf5_correct=r["final_leaf5_correct"],
                ))
    for adv in ADVS:
        for seed in SEEDS:
            a, b = by["repeat", adv, seed], by["keepalive", adv, seed]
            out.append(dict(
                comparison="arm_A_vs_B", arm="A-B", robot="repeat vs keepalive",
                adv_interval_s=adv, reference="arm B same MRAI", seed=seed,
                update_reduction_pct=reduction(a["bgp_updates"], b["bgp_updates"]),
                bytes_reduction_pct=reduction(a["bgp_bytes"], b["bgp_bytes"]),
                type2_nlri_change_pct=pct_change(a["type2_nlris"], b["type2_nlris"]),
                robot_macip_adv_change_pct=pct_change(a["robot_macip_adv_per_move"],
                                                      b["robot_macip_adv_per_move"]),
                bgpd_cpu_reduction_pct=reduction(a["bgpd_cpu_seconds"], b["bgpd_cpu_seconds"]),
                zebra_cpu_reduction_pct=reduction(a["zebra_cpu_seconds"], b["zebra_cpu_seconds"]),
                t_stale_p50_delta_s=(a["t_stale_p50_s"] or 0) - (b["t_stale_p50_s"] or 0),
                stale_robot_seconds_ratio=(a["stale_robot_seconds"] / b["stale_robot_seconds"]
                                           if b["stale_robot_seconds"] else None),
                censored_or_wrong_pct=a["censored_or_wrong_pct"] - b["censored_or_wrong_pct"],
                final_leaf5_correct=a["final_leaf5_correct"] - b["final_leaf5_correct"],
            ))
    return out


SUMMARY_KEYS = (
    "achieved_moves_per_s", "updates_per_move", "type2_nlris_per_move",
    "robot_type2_nlris_per_move", "robot_mac_adv_per_move", "robot_mac_wd_per_move",
    "robot_macip_adv_per_move", "robot_macip_wd_per_move", "type2_nlris_per_update",
    "mac_seq_skips", "macip_seq_skips", "moves_macip_originated_pct",
    "moves_macip_only_after_leaving", "moves_macip_never_within_30s",
    "moves_macip_not_during_last_move", "macip_after_leaving_lag_p50_s",
    "mac_seq_ties", "macip_seq_ties", "updates_through_quiescence_per_move",
    "bytes_per_move", "cooldown_updates", "min_session_burst_gap_s",
    "bgpd_cpu_seconds", "zebra_cpu_seconds", "bgpd_mean_cpu_pct_one_core",
    "zebra_mean_cpu_pct_one_core", "t_stale_p50_s", "t_stale_max_s",
    "moves_correct", "moves_transient_stale", "moves_censored", "moves_persistently_wrong",
    "censored_or_wrong_pct", "stale_robot_seconds_per_move", "robots_incorrect_at_moves_end",
    "robots_incorrect_at_end", "final_leaf5_correct", "probe_loss_pct",
)


def summary_rows(rows):
    out = []
    for arm in ARMS:
        for adv in ADVS:
            sel = [r for r in rows if r["robot"] == arm and r["adv_interval_s"] == adv]
            row = {"arm": sel[0]["arm"], "robot": arm, "adv_interval_s": adv, "trials": len(sel)}
            for key in SUMMARY_KEYS:
                values = [r[key] for r in sel if r[key] is not None]
                row[f"{key}_mean"] = mean(values) if values else None
                row[f"{key}_min"] = min(values) if values else None
                row[f"{key}_max"] = max(values) if values else None
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def style():
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK_2, "text.color": INK,
        "xtick.color": MUTED, "ytick.color": MUTED, "axes.titlecolor": INK,
        "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
        "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.axisbelow": True, "legend.frameon": False, "legend.fontsize": 9,
        "ytick.left": False,
    })


def by_cell(rows, key):
    cells = defaultdict(list)
    for r in rows:
        if r[key] is not None:
            cells[r["robot"], r["adv_interval_s"]].append(r[key])
    return cells


def grouped_bars(ax, rows, key, fmt="{:.1f}", ylabel=None, title=None, reference=None):
    """Bars = mean of 3 seeds per arm and MRAI; dots = individual seeds."""
    cells = by_cell(rows, key)
    width = 0.36
    for k, arm in enumerate(ARMS):
        xs = [i + (k - 0.5) * width for i in range(len(ADVS))]
        means = [mean(cells[arm, adv]) for adv in ADVS]
        ax.bar(xs, means, width, color=COLOR[arm], edgecolor=SURFACE, linewidth=2,
               label=ARM_LABEL[arm], zorder=2)
        for x, adv, m in zip(xs, ADVS, means):
            seeds = cells[arm, adv]
            ax.scatter([x] * len(seeds), seeds, s=12, color=INK_2, zorder=3, linewidths=0)
            ax.annotate(fmt.format(m), (x, max(seeds + [m])), xytext=(0, 3),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=8.5, color=INK, zorder=4,
                        bbox=dict(boxstyle="square,pad=0.1", fc=SURFACE, ec="none"))
    if reference is not None:
        ax.axhline(reference[0], color=INK_2, linewidth=1, linestyle=(0, (4, 3)), zorder=1)
        ax.annotate(reference[1], (len(ADVS) - 0.5, reference[0]), xytext=(0, 3),
                    textcoords="offset points", ha="right", va="bottom", fontsize=8,
                    color=INK_2)
    ax.set_xticks(range(len(ADVS)))
    ax.set_xticklabels([f"MRAI {adv} s" for adv in ADVS])
    ax.tick_params(axis="x", length=0)
    if ylabel:
        ax.set_ylabel(ylabel)
    if title:
        ax.set_title(title, loc="left")
    ax.set_ylim(bottom=0)
    ax.margins(y=0.12)


def figure_legend(fig, ax, y=0.005):
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=len(labels), bbox_to_anchor=(0.5, y))


def save(fig, name):
    GRAPHS.mkdir(exist_ok=True)
    fig.savefig(GRAPHS / name, dpi=160, bbox_inches="tight")
    plt.close(fig)


def fig_updates(rows):
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    grouped_bars(ax, rows, "updates_per_move", ylabel="BGP UPDATE messages per move",
                 title="BGP UPDATE messages per move fall as MRAI rises, in both arms")
    figure_legend(fig, ax, y=-0.06)
    fig.text(0.0, -0.12, "Bars: mean of 3 seeds (384 moves each). Dots: individual seeds. "
             "UPDATEs counted once per directed session, moves + 20 s cooldown.",
             fontsize=8, color=INK_2)
    save(fig, "01_timer_vs_updates.png")


def fig_nlris(rows):
    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.2), sharey=False)
    grouped_bars(axes[0], rows, "robot_type2_nlris_per_move", ylabel="NLRIs per move",
                 title="All robot Type-2 NLRIs", reference=(80, "80 = full work (Exp 1/2)"))
    grouped_bars(axes[1], rows, "robot_mac_per_move", title="MAC routes (adv + wd)",
                 reference=(40, "40 expected"))
    grouped_bars(axes[2], rows, "robot_macip_per_move", title="MAC/IP routes (adv + wd)",
                 reference=(40, "40 expected"))
    fig.suptitle("Type-2 NLRIs per move: MRAI mostly removes MAC/IP routes; MAC routes barely change",
                 x=0.01, ha="left", fontsize=12)
    figure_legend(fig, axes[0], y=-0.07)
    fig.tight_layout()
    save(fig, "02_timer_vs_nlris.png")


def fig_staleness(rows):
    fig, axes = plt.subplots(1, 3, figsize=(12.5, 4.2))
    grouped_bars(axes[0], rows, "t_stale_p50_s", fmt="{:.2f}", ylabel="seconds",
                 title="T_stale median (converged moves)")
    grouped_bars(axes[1], rows, "t_stale_max_s", fmt="{:.1f}", ylabel="seconds",
                 title="T_stale maximum (converged moves)")
    grouped_bars(axes[2], rows, "censored_or_wrong_pct", fmt="{:.0f}%", ylabel="% of moves",
                 title="Moves never converged (censored + wrong)")
    fig.suptitle("leaf5 staleness grows with MRAI; at 5 s most moves never converge in either arm",
                 x=0.01, ha="left", fontsize=12)
    figure_legend(fig, axes[0], y=-0.07)
    fig.tight_layout()
    save(fig, "03_timer_vs_staleness.png")


def fig_timeline(timelines):
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.0), sharey=True)
    for ax, adv in zip(axes, ADVS):
        for arm in ARMS:
            series = defaultdict(list)
            missing = defaultdict(list)
            for seed in SEEDS:
                for r in timelines[arm, adv, seed]:
                    t = round(float(r["t_rel_moves_start_s"]), 1)
                    series[t].append(ROBOTS - int(r["correct"]))
                    missing[t].append(int(r["no_route"]))
            ts = sorted(t for t in series if len(series[t]) == len(SEEDS))
            ax.plot(ts, [mean(series[t]) for t in ts], color=COLOR[arm], linewidth=2,
                    label=f"{ARM_SHORT[arm]}: wrong or missing /32")
            ax.plot(ts, [mean(missing[t]) for t in ts], color=COLOR[arm], linewidth=1.2,
                    linestyle=(0, (3, 2)), label=f"{ARM_SHORT[arm]}: of which no /32")
        sample = timelines[ARMS[0], adv, SEEDS[0]]
        cool = [float(r["t_rel_moves_start_s"]) for r in sample if r["phase"] == "cooldown"]
        t_last = float(sample[-1]["t_rel_moves_start_s"])
        ax.axvspan(min(cool), max(cool), color=GRID, alpha=0.5, zorder=0, linewidth=0)
        ax.annotate("cooldown", ((min(cool) + max(cool)) / 2, 1), ha="center", va="bottom",
                    fontsize=8, color=INK_2,
                    xycoords=("data", "axes fraction"), xytext=(0, -12),
                    textcoords="offset points")
        ax.set_title(f"MRAI {adv} s", loc="left")
        ax.set_xlabel("seconds since first move")
        ax.set_xlim(-10, t_last)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel(f"robots (of {ROBOTS}) wrong at leaf5")
    fig.suptitle("Robots whose leaf5 /32 is wrong or missing, mean of 3 seeds, 100 ms steps",
                 x=0.01, ha="left", fontsize=12)
    figure_legend(fig, axes[0], y=-0.1)
    fig.tight_layout()
    save(fig, "04_leaf5_wrong_timeline.png")


def fig_tradeoff(rows, pairs):
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2), gridspec_kw={"width_ratios": [1.4, 1, 1]})
    ax = axes[0]
    by = {(r["robot"], r["adv_interval_s"], r["seed"]): r for r in rows}
    placed = defaultdict(list)
    for arm in ARMS:
        for adv in ADVS:
            pts = [(0.0 if adv == 0 else next(
                p["update_reduction_pct"] for p in pairs
                if p["comparison"] == "mrai_vs_0" and p["robot"] == arm
                and p["adv_interval_s"] == adv and p["seed"] == seed),
                by[arm, adv, seed]["stale_robot_seconds_per_move"]) for seed in SEEDS]
            ax.scatter([p[0] for p in pts], [p[1] for p in pts], s=42, color=COLOR[arm],
                       marker=MARKER[adv], edgecolors=SURFACE, linewidths=1.5, zorder=3,
                       label=ARM_LABEL[arm] if adv == 0 else None)
            placed[adv] += pts
    for adv, pts in placed.items():
        # One label per MRAI value, above its highest point (both arms).
        top = max(pts, key=lambda p: p[1])
        ax.annotate(f"MRAI {adv} s", (mean(p[0] for p in pts), top[1]), xytext=(0, 9),
                    textcoords="offset points", ha="center", va="bottom", fontsize=8.5,
                    color=INK)
    ax.set_xlabel("BGP UPDATE reduction vs MRAI 0, same arm and seed (%)")
    ax.set_ylabel("stale robot-seconds per move at leaf5")
    ax.set_title("Wire savings vs stale-state exposure", loc="left")
    ax.grid(axis="x", visible=True)
    grouped_bars(axes[1], rows, "bgpd_cpu_seconds", fmt="{:.2f}", ylabel="CPU seconds",
                 title="bgpd CPU (7 switches)")
    grouped_bars(axes[2], rows, "zebra_cpu_seconds", fmt="{:.2f}", ylabel="CPU seconds",
                 title="zebra CPU (7 switches)")
    fig.suptitle("Fewer UPDATEs and less CPU are bought with longer stale forwarding at leaf5",
                 x=0.01, ha="left", fontsize=12)
    figure_legend(fig, axes[1], y=-0.07)
    fig.text(0.01, -0.13, "Markers: circle 0 s, square 1 s, diamond 5 s; one per seed. CPU: summed over "
             "leaves and spines, first move to end of cooldown, ~1 s sampling on Colima "
             "(hardware-dependent, context only).", fontsize=8, color=INK_2)
    fig.tight_layout()
    save(fig, "05_tradeoff_and_cpu.png")


def main():
    rows, timelines = [], {}
    for arm in ARMS:
        for adv in ADVS:
            for seed in SEEDS:
                row, timeline = load_trial(arm, adv, seed)
                row["robot_mac_per_move"] = row["robot_mac_adv_per_move"] + row["robot_mac_wd_per_move"]
                row["robot_macip_per_move"] = (row["robot_macip_adv_per_move"]
                                               + row["robot_macip_wd_per_move"])
                rows.append(row)
                timelines[arm, adv, seed] = timeline
    pairs = pair_rows(rows)
    write_csv(ROOT / "results_exp3.csv", rows)
    write_csv(ROOT / "paired_exp3.csv", pairs)
    write_csv(ROOT / "summary_exp3.csv", summary_rows(rows))
    style()
    fig_updates(rows)
    fig_nlris(rows)
    fig_staleness(rows)
    fig_timeline(timelines)
    fig_tradeoff(rows, pairs)
    print(f"Audited {len(rows)} trials; wrote results_exp3.csv, paired_exp3.csv, "
          "summary_exp3.csv and graphs/01-05.")


if __name__ == "__main__":
    main()
