#!/usr/bin/env python3
# SPDX-License-Identifier: ISC
"""Experiment 3 analysis: MRAI wire counts and continuous leaf5 /32 correctness.

Two modes:

    analyze_exp3.py point <trial-dir>
        Inside the container, right after a trial, with the handoff tools
        (analyze_mobility, bgp_pcap) and the Experiment 2 directory on
        PYTHONPATH. Reuses Experiment 2's capture audit (10 links, TCP gaps,
        quiet-window UPDATEs, FRR counter reconciliation) and final-FIB check,
        then adds the Experiment 3 outputs listed below.

    analyze_exp3.py collect <group-dir>
        On the host (standard library only). Tabulates every trial's
        exp3_summary.json into <group>_exp3.csv, checks the MRAI 0 arm
        against Experiment 2's staggered 10 s baseline and copies the compact
        evidence into <group>_evidence/.

Per-trial outputs (point mode):
    session_counts.csv      per directed BGP session: UPDATEs, MRAI bursts,
                            minimum gap between bursts, robot Type-2 NLRIs
    leaf5_move_states.csv   one row per move: T_stale and its class
    leaf5_state_timeline.csv  every 100 ms: robots whose leaf5 /32 is correct,
                            on the old VTEP, on another VTEP, missing, or ECMP
    exp3_summary.json       everything above plus Experiment 2's audit fields

Move classes (T_stale = start of the final interval in which leaf5's installed /32
selects only the new VTEP and keeps doing so until the robot's next move or
the end of the cooldown, minus the attach time; first_correct_s and
reverted_after_correct record earlier, unstable correct states):
    correct             converged with T_stale <= STALE_THRESHOLD_S
    transient_stale     converged with T_stale > STALE_THRESHOLD_S
    censored            the robot's next move started before convergence
    persistently_wrong  the robot's last move had not converged, or had
                        reverted, at the end of the cooldown
                        (correct_after_cooldown_s reports any later recovery
                        the route monitor saw while BGP was going quiet)
STALE_THRESHOLD_S is 1 s, fixed before Experiment 3 from Experiment 2's
staggered 10 s trials (1,152 moves, p50 633 ms, max 700 ms).
"""

import bisect
import csv
import json
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

STALE_THRESHOLD_S = 1.0
BURST_S = 0.05
TIMELINE_STEP_S = 0.1
VTEP = {"leaf1": "10.0.0.1", "leaf2": "10.0.0.2", "leaf3": "10.0.0.3",
        "leaf4": "10.0.0.4", "leaf5": "10.0.0.5"}
DEVICES = ("leaf1", "leaf2", "leaf3", "leaf4", "leaf5", "spine1", "spine2")
CLASSES = ("correct", "transient_stale", "censored", "persistently_wrong")
EVIDENCE_FILES = (
    "exp3_summary.json", "counts.csv", "timeline.csv", "type2_counts.csv",
    "session_counts.csv", "leaf5_move_states.csv", "leaf5_state_timeline.csv",
    "leaf5_final_correctness.csv", "frr_verification.json", "quiet_gate.json",
    "capture_settings.json", "quiescence.json", "truth.csv",
)


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def pct(values, p):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# Wire analysis
# ---------------------------------------------------------------------------


def wire_analysis(point, meta, bgp_pcap):
    """Per-session bursts, robot NLRIs, MAC-mobility sequence skips, flush time."""
    robots = meta["config"]["robots"]
    moves = sum(1 for r in read_csv(point / "truth.csv") if int(r["move"]) > 0)
    robot_macs = {"02:52:00:00:%02x:%02x" % (i >> 8, i & 0xFF) for i in range(robots)}
    t = meta["t"]
    sessions = defaultdict(lambda: {"times": [], "robot_adv": 0, "robot_wd": 0,
                                    "other_type2": 0})
    last_seq = {}
    seq_skips = []
    cooldown_nlris = 0
    last_update_t = None
    robot_nlris = other_nlris = 0
    kinds = Counter()
    windows = Counter()
    # Originations: a leaf advertising a robot route under its own RD
    # (RD "10.0.0.N:x" belongs to leafN). Used to tell a route that was never
    # originated apart from one that was originated but coalesced on the way.
    originated = defaultdict(list)  # (leaf, mac, has_ip) -> [timestamps]
    # MAC-mobility sequence ties: two leaves originating the same robot route
    # type with the same sequence number (the Experiment 4/5 failure mode).
    seq_origins = defaultdict(set)  # (mac, has_ip, seq) -> {leaf}
    for path in sorted((point / "bgp_capture").glob("*.pcap")):
        leaf, spine = path.stem.split("_eth-")
        messages, _ = bgp_pcap.read_messages(path)
        for timestamp, outgoing, raw in messages:
            if not t["moves_start"] <= timestamp < t["end"]:
                continue
            msg = bgp_pcap.parse_message(raw)
            if msg["type"] != "UPDATE":
                continue
            phase = "moving" if timestamp < t["moves_end"] else "cooldown"
            windows[phase, "updates"] += 1
            windows[phase, "bytes"] += msg["len"]
            routes = [(action, r) for action in ("adv", "wd") for r in msg[action]
                      if r.get("family") == "evpn" and r.get("rt") == 2]
            if not routes:
                continue
            sender, receiver = (leaf, spine) if outgoing else (spine, leaf)
            session = sessions[sender, receiver]
            session["times"].append(timestamp)
            last_update_t = max(last_update_t or timestamp, timestamp)
            for action, route in routes:
                windows[phase, "type2_" + action] += 1
                if timestamp >= t["moves_end"]:
                    cooldown_nlris += 1
                if route.get("mac") not in robot_macs:
                    other_nlris += 1
                    session["other_type2"] += 1
                    continue
                robot_nlris += 1
                session["robot_" + action] += 1
                kinds["robot_%s_%s" % ("macip" if route.get("ip") else "mac", action)] += 1
                origin = route.get("rd", "").split(":")[0]
                if (action == "adv" and sender.startswith("leaf")
                        and origin == "10.0.0." + sender[4:]):
                    originated[sender, route["mac"], bool(route.get("ip"))].append(timestamp)
                    if "mm" in msg["attrs"]:
                        seq_origins[route["mac"], bool(route.get("ip")),
                                    msg["attrs"]["mm"]].add(sender)
                if action != "adv" or "mm" not in msg["attrs"]:
                    continue
                key = (sender, receiver, route["mac"], route.get("ip"))
                seq = msg["attrs"]["mm"]
                previous = last_seq.get(key)
                if previous is not None and seq > previous + 1:
                    seq_skips.append(dict(sender=sender, receiver=receiver, mac=route["mac"],
                                          ip=route.get("ip"), from_seq=previous, to_seq=seq,
                                          t=timestamp))
                last_seq[key] = max(seq, previous or seq)
    rows = []
    for (sender, receiver), s in sorted(sessions.items()):
        times = sorted(s["times"])
        bursts = [times[0]]
        for a, b in zip(times, times[1:]):
            if b - a > BURST_S:
                bursts.append(b)
        gaps = [b - a for a, b in zip(bursts, bursts[1:])]
        rows.append(dict(
            sender=sender, receiver=receiver, type2_updates=len(times), bursts=len(bursts),
            min_burst_gap_s=round(min(gaps), 3) if gaps else None,
            median_burst_gap_s=round(statistics.median(gaps), 3) if gaps else None,
            robot_adv=s["robot_adv"], robot_wd=s["robot_wd"], other_type2=s["other_type2"],
        ))
    write_csv(point / "session_counts.csv", rows)
    gaps = [r["min_burst_gap_s"] for r in rows if r["min_burst_gap_s"] is not None]
    # Per move: did the robot's new leaf originate its MAC and MAC/IP routes
    # before the robot moved on (or the measurement ended)?
    truth = sorted((r for r in read_csv(point / "truth.csv") if int(r["move"]) > 0),
                   key=lambda r: (int(r["robot"]), int(r["move"])))
    # A MAC/IP route the leaf did not originate during the visit is split into
    # "after leaving" (first origination within 30 s after the robot's next
    # move; the lag is measured from that next attach), "never", and the
    # robot's last move, which has no next move.
    origin_counts = Counter()
    late_lags = []
    for i, r in enumerate(truth):
        nxt = truth[i + 1] if i + 1 < len(truth) else None
        if nxt and nxt["robot"] != r["robot"]:
            nxt = None
        until = float(nxt["t_start"]) if nxt else t["end"]
        mac = "02:52:00:00:%02x:%02x" % (int(r["robot"]) >> 8, int(r["robot"]) & 0xFF)
        start = float(r["t_start"])
        for has_ip, name in ((False, "mac"), (True, "macip")):
            if any(start <= x < until for x in originated[r["dst"], mac, has_ip]):
                origin_counts[name] += 1
            elif has_ip and not nxt:
                origin_counts["macip_not_during_last_move"] += 1
            elif has_ip:
                after = [x - float(nxt["t_attached"]) for x in originated[r["dst"], mac, True]
                         if until <= x < until + 30]
                if after:
                    origin_counts["macip_only_after_leaving"] += 1
                    late_lags.append(min(after))
                else:
                    origin_counts["macip_never_within_30s"] += 1
    return {
        "sessions_with_type2": len(rows),
        "robot_type2_nlris": robot_nlris,
        "non_robot_type2_nlris": other_nlris,
        "min_session_burst_gap_s": min(gaps) if gaps else None,
        "median_session_min_burst_gap_s": statistics.median(gaps) if gaps else None,
        "mean_bursts_per_session": statistics.mean(r["bursts"] for r in rows) if rows else None,
        **{f"{k}_per_move": kinds[k] / moves for k in (
            "robot_mac_adv", "robot_mac_wd", "robot_macip_adv", "robot_macip_wd")},
        **{f"{phase}_{k}": windows[phase, k] for phase in ("moving", "cooldown")
           for k in ("updates", "bytes", "type2_adv", "type2_wd")},
        "bgp_bytes": windows["moving", "bytes"] + windows["cooldown", "bytes"],
        "mac_mobility_seq_skips": len(seq_skips),
        "mac_seq_skips": sum(1 for s in seq_skips if not s["ip"]),
        "macip_seq_skips": sum(1 for s in seq_skips if s["ip"]),
        "moves_mac_originated": origin_counts["mac"],
        "moves_macip_originated": origin_counts["macip"],
        "moves_macip_originated_pct": 100 * origin_counts["macip"] / moves if moves else None,
        "mac_seq_ties": sum(1 for (_, ip, _), v in seq_origins.items() if not ip and len(v) > 1),
        "macip_seq_ties": sum(1 for (_, ip, _), v in seq_origins.items() if ip and len(v) > 1),
        "moves_macip_only_after_leaving": origin_counts["macip_only_after_leaving"],
        "moves_macip_never_within_30s": origin_counts["macip_never_within_30s"],
        "moves_macip_not_during_last_move": origin_counts["macip_not_during_last_move"],
        "macip_after_leaving_lag_p50_s": (round(statistics.median(late_lags), 3)
                                          if late_lags else None),
        "seq_skip_examples": seq_skips[:10],
        "cooldown_type2_nlris": cooldown_nlris,
        "last_type2_update_in_window_after_moves_end_s": (
            last_update_t - t["moves_end"] if last_update_t else None),
    }


def quiescent_reconciliation(point, meta, bgp_pcap):
    """Every captured UPDATE from the quiet window on vs FRR counters after BGP went quiet."""
    quiet = json.loads((point / "quiescence.json").read_text())
    captured = Counter()
    last_update = None
    for path in sorted((point / "bgp_capture").glob("*.pcap")):
        leaf, spine = path.stem.split("_eth-")
        for timestamp, outgoing, raw in bgp_pcap.read_messages(path)[0]:
            if timestamp < meta["t"]["quiet_start"]:
                continue
            if bgp_pcap.parse_message(raw)["type"] != "UPDATE":
                continue
            last_update = max(last_update or timestamp, timestamp)
            sender, receiver = (leaf, spine) if outgoing else (spine, leaf)
            captured[sender, "tx"] += 1
            captured[receiver, "rx"] += 1
    mismatches = []
    for node in DEVICES:
        for direction in ("rx", "tx"):
            key = f"upd_{direction}"
            frr = quiet["counters"][node][key] - meta["updates_before"][node][key]
            if frr != captured[node, direction]:
                mismatches.append(dict(node=node, direction=direction, frr=frr,
                                       captured=captured[node, direction]))
    quiet["last_captured_update"] = last_update
    quiet["captured_tx_updates"] = sum(v for (_, d), v in captured.items() if d == "tx")
    return quiet, mismatches


# ---------------------------------------------------------------------------
# leaf5 state tracking
# ---------------------------------------------------------------------------


def state_at(times, states, t):
    k = bisect.bisect_right(times, t) - 1
    return states[k] if k >= 0 else frozenset()


def classify_moves(meta, truth, belief):
    t = meta["t"]
    horizon = t["end"]
    observed_until = t["monitors_stopped"]
    by_robot = defaultdict(list)
    for row in truth:
        by_robot[int(row["robot"])].append(row)
    rows = []
    for robot, moves in sorted(by_robot.items()):
        moves.sort(key=lambda r: float(r["t_attached"]))
        ip = meta["robot_ips"][robot]
        events = sorted(belief.get(ip, []))
        times = [e[0] for e in events]
        states = [e[1] for e in events]
        for k, move in enumerate(moves):
            if int(move["move"]) == 0:
                continue
            t_start, t_att = float(move["t_start"]), float(move["t_attached"])
            last = k + 1 == len(moves)
            t_next = horizon if last else float(moves[k + 1]["t_start"])
            want = frozenset([VTEP[move["dst"]]])
            old = frozenset([VTEP[move["src"]]])
            at_start = state_at(times, states, t_start) == want
            t_first = t_start if at_start else next(
                (te for te, s in events if t_start <= te < t_next and s == want), None)
            final = state_at(times, states, t_next - 1e-6)
            # Converged = the last entry into the correct state that persists
            # until the robot's next move (or the end of the cooldown).
            t_correct = None
            if final == want:
                t_correct = t_start if at_start else None
                for te, s in events:
                    if t_start <= te < t_next and s != want:
                        t_correct = None
                    elif t_start <= te < t_next and s == want and t_correct is None:
                        t_correct = te
            spans = Counter()
            cursor, current = t_att, state_at(times, states, t_att)
            for te, s in events + [(t_next, None)]:
                if te <= t_att:
                    continue
                end = min(te, t_next)
                if current == want:
                    spans["correct"] += end - cursor
                elif not current:
                    spans["no_route"] += end - cursor
                elif current == old:
                    spans["old_vtep"] += end - cursor
                else:
                    spans["other_vtep"] += end - cursor
                if te >= t_next:
                    break
                cursor, current = te, s
            reverted = t_first is not None and any(
                t_first < te < t_next and s != want for te, s in events)
            stale = None if t_correct is None else max(0.0, t_correct - t_att)
            late = None
            if last and final != want:
                late = next((te - t_att for te, s in events
                             if horizon <= te < observed_until and s == want), None)
            if t_correct is None:
                cls = "persistently_wrong" if last else "censored"
            elif stale <= STALE_THRESHOLD_S:
                cls = "correct"
            else:
                cls = "transient_stale"
            rows.append(dict(
                robot=robot, move=int(move["move"]), src=move["src"], dst=move["dst"],
                t_attached=round(t_att, 6), window_s=round(t_next - t_att, 3),
                last_move=last, t_stale_s=None if stale is None else round(stale, 4),
                cls=cls, correct_at_attach=at_start,
                first_correct_s=None if t_first is None else round(max(0.0, t_first - t_att), 4),
                reverted_after_correct=reverted,
                old_vtep_s=round(spans["old_vtep"], 4), no_route_s=round(spans["no_route"], 4),
                other_vtep_s=round(spans["other_vtep"], 4),
                correct_s=round(spans["correct"], 4),
                final_state=" ".join(sorted(final)) or "none",
                correct_after_cooldown_s=None if late is None else round(late, 3),
            ))
    rows.sort(key=lambda r: r["t_attached"])
    return rows


def state_timeline(meta, truth, belief):
    t = meta["t"]
    attaches = defaultdict(list)
    for row in truth:
        attaches[int(row["robot"])].append(
            (float(row["t_start"]), row["dst"], row["src"]))
    for values in attaches.values():
        values.sort()
    starts = {robot: [v[0] for v in values] for robot, values in attaches.items()}
    tracks = {}
    for robot in attaches:
        events = sorted(belief.get(meta["robot_ips"][robot], []))
        tracks[robot] = ([e[0] for e in events], [e[1] for e in events])
    rows = []
    origin = t["moves_start"]
    steps = int((t["end"] - t["quiet_start"]) / TIMELINE_STEP_S) + 1
    for n in range(steps):
        now = t["quiet_start"] + n * TIMELINE_STEP_S
        counts = Counter()
        for robot, values in attaches.items():
            k = bisect.bisect_right(starts[robot], now) - 1
            if k < 0:
                continue
            _, dst, src = values[k]
            state = state_at(*tracks[robot], now)
            if state == {VTEP[dst]}:
                counts["correct"] += 1
            elif not state:
                counts["no_route"] += 1
            elif len(state) > 1:
                counts["ecmp_or_mixed"] += 1
            elif src and state == {VTEP[src]}:
                counts["old_vtep"] += 1
            else:
                counts["other_vtep"] += 1
        rows.append(dict(t_rel_moves_start_s=round(now - origin, 3),
                         phase="quiet" if now < t["moves_start"] else
                         "moving" if now < t["moves_end"] else "cooldown",
                         **{key: counts[key] for key in (
                             "correct", "old_vtep", "other_vtep", "no_route", "ecmp_or_mixed")}))
    return rows


def final_monitor_matches_fib(meta, belief, point):
    """The route monitor's last state for each robot must equal the final FIB snapshot."""
    snapshot = json.loads((point / "leaf5_final_fib.json").read_text())
    nexthops = {entry["id"]: entry for entry in snapshot["nexthops"]}

    def gateways(entry):
        if "gateway" in entry:
            return {entry["gateway"]}
        if "nhid" in entry:
            return gateways(nexthops.get(entry["nhid"], {}))
        if "group" in entry:
            return set().union(*(gateways(nexthops.get(m["id"], {})) for m in entry["group"]))
        return set().union(*(gateways(m) for m in entry.get("nexthops", [])))

    fib = {r["dst"].removesuffix("/32"): frozenset(gateways(r)) for r in snapshot["routes"]}
    mismatches = []
    for ip in meta["robot_ips"]:
        events = sorted(belief.get(ip, []))
        monitor = events[-1][1] if events else frozenset()
        if monitor != fib.get(ip, frozenset()):
            mismatches.append(dict(ip=ip, monitor=sorted(monitor), fib=sorted(fib.get(ip, ()))))
    return mismatches


def capture_socket_stats(point):
    """Kernel capture-socket counters, present for trials run with the BPF filter."""
    settings = json.loads((point / "capture_settings.json").read_text())
    files = sorted((point / "bgp_capture").glob("*.pcap.stats.json"))
    if not files:
        return {"capture_kernel_filter": False, "capture_socket_packets": None,
                "capture_socket_drops": None}
    stats = [json.loads(f.read_text()) for f in files]
    return {"capture_kernel_filter": "kernel_filter" in settings,
            "capture_socket_packets": sum(s["tp_packets"] for s in stats),
            "capture_socket_drops": sum(s["tp_drops"] for s in stats)}


def cpu_seconds(point, meta):
    samples = read_csv(point / "health.csv")
    start, end = meta["t"]["moves_start"], meta["t"]["end"]
    totals = {"bgpd": 0.0, "zebra": 0.0}
    prev = float(samples[0]["t"])
    for row in samples[1:]:
        now = float(row["t"])
        overlap = max(0.0, min(now, end) - max(prev, start))
        prev = now
        for daemon in totals:
            totals[daemon] += overlap * sum(
                float(row[f"{d}_{daemon}_cpu_pct"]) for d in DEVICES) / 100
    window = end - start
    result = {f"{daemon}_cpu_seconds": round(v, 3) for daemon, v in totals.items()}
    result.update({f"{daemon}_mean_cpu_pct_one_core": round(100 * v / window, 3)
                   for daemon, v in totals.items()})
    return result


def analyze_point(point):
    import analyze_mobility as mobility_analysis
    import bgp_pcap
    import summarize as exp2_summary

    meta = json.loads((point / "meta.json").read_text())
    config = meta["config"]
    mobility_summary, moves = mobility_analysis.summarize_point(str(point))
    rounds = int(config["duration_s"] / config["dwell_s"])
    per_robot = Counter(row["robot"] for row in moves)
    workload_ok = (set(per_robot) == set(range(config["robots"]))
                   and set(per_robot.values()) == {rounds}
                   and all(r["in_measure"] and r["src"] != r["dst"] for r in moves))
    correct_final, checked_final = exp2_summary.check_final_fib(point)
    wire = exp2_summary.count_type2(point, meta)
    detail = wire_analysis(point, meta, bgp_pcap)
    quiet, quiet_mismatches = quiescent_reconciliation(point, meta, bgp_pcap)

    _, truth, _, belief, _, _ = mobility_analysis.load_point(str(point))
    meta["robot_ips"] = (point / "targets.txt").read_text().split()
    truth = read_csv(point / "truth.csv")
    states = classify_moves(meta, truth, belief)
    write_csv(point / "leaf5_move_states.csv", states)
    timeline = state_timeline(meta, truth, belief)
    write_csv(point / "leaf5_state_timeline.csv", timeline)
    mismatches = final_monitor_matches_fib(meta, belief, point)

    verification = json.loads((point / "frr_verification.json").read_text())
    gate = json.loads((point / "quiet_gate.json").read_text())
    stale = [r["t_stale_s"] for r in states if r["t_stale_s"] is not None]
    classes = Counter(r["cls"] for r in states)
    t = meta["t"]
    at_moves_end = min(timeline, key=lambda r: abs(r["t_rel_moves_start_s"]
                                                     - (t["moves_end"] - t["moves_start"])))
    cooldown = [r for r in timeline if r["phase"] == "cooldown"]
    all_correct = [r["t_rel_moves_start_s"] for r in cooldown
                   if r["correct"] == config["robots"]]
    settle = None
    for r in cooldown:
        if r["correct"] == config["robots"]:
            settle = settle if settle is not None else r["t_rel_moves_start_s"]
        else:
            settle = None
    tx_updates = wire["bgp_updates"]
    n = len(states)
    result = dict(
        adv_interval_s=int(config["adv_interval"]), seed=config["seed"],
        dwell_s=config["dwell_s"], rounds=rounds, sync=config["sync"],
        robot=config["robot"],
        completed_moves=len(moves), workload_ok=workload_ok,
        achieved_moves_per_s=mobility_summary["moves_per_s"],
        adv_interval_config=meta.get("adv_interval_config"),
        config_problems=verification["config_problems_capture_start"]
        + verification["config_problems_end"],
        session_changes=len(verification["session_changes_during_measurement"]),
        connections_dropped=verification["total_connections_dropped"],
        daemon_log_issue_lines=sum(f.get("issue_lines_in_interval", 0)
                                   for f in verification["daemon_logs"]["files"].values()),
        quiet_gate_waited_s=gate["waited_s"],
        **{k: wire[k] for k in ("capture_links", "capture_gaps", "quiet_bgp_updates",
                                "bgp_updates", "type2_bgp_updates", "type2_nlris")},
        **capture_socket_stats(point),
        end_window_counter_mismatches=len(wire["counter_mismatches"]),
        quiescent_counter_mismatches=len(quiet_mismatches),
        last_update_after_moves_end_s=round(quiet["last_captured_update"] - t["moves_end"], 3),
        quiescence_wait_s=quiet["waited_s"],
        # Every UPDATE sent from the quiet window until BGP went quiet, so it
        # includes MRAI-delayed UPDATEs that left after the 20 s cooldown.
        updates_through_quiescence=quiet["captured_tx_updates"],
        updates_through_quiescence_per_move=quiet["captured_tx_updates"] / n if n else None,
        quiescence_timed_out=quiet["timed_out"],
        updates_per_move=tx_updates / n if n else None,
        type2_nlris_per_move=wire["type2_nlris"] / n if n else None,
        robot_type2_nlris_per_move=detail["robot_type2_nlris"] / n if n else None,
        type2_nlris_per_update=wire["type2_nlris_per_type2_update"],
        **{k: v for k, v in detail.items() if k != "seq_skip_examples"},
        t_stale_p50_s=pct(stale, 50), t_stale_p95_s=pct(stale, 95),
        t_stale_max_s=max(stale) if stale else None,
        **{f"moves_{c}": classes[c] for c in CLASSES},
        moves_reverted_after_correct=sum(r["reverted_after_correct"] for r in states),
        persistently_wrong_recovered_later=sum(
            r["correct_after_cooldown_s"] is not None for r in states),
        latest_recovery_after_attach_s=max(
            (r["correct_after_cooldown_s"] for r in states
             if r["correct_after_cooldown_s"] is not None), default=None),
        stale_robot_seconds=round(sum(r["old_vtep_s"] + r["no_route_s"] + r["other_vtep_s"]
                                      for r in states), 3),
        robots_incorrect_at_moves_end=config["robots"] - at_moves_end["correct"],
        cooldown_all_correct_from_s=settle,
        cooldown_all_correct_ever=bool(all_correct),
        robots_incorrect_at_end=config["robots"] - timeline[-1]["correct"],
        monitor_vs_final_fib_mismatches=len(mismatches),
        final_leaf5_correct=correct_final, final_leaf5_checked=checked_final,
        probe_loss_pct=mobility_summary["probe_loss_pct"],
        **cpu_seconds(point, meta),
    )
    result["seq_skip_examples"] = detail["seq_skip_examples"]
    result["end_window_counter_mismatch_detail"] = wire["counter_mismatches"]
    result["quiescent_counter_mismatch_detail"] = quiet_mismatches
    (point / "exp3_summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    failures = [name for name, bad in (
        ("workload", not workload_ok), ("capture gaps", wire["capture_gaps"]),
        ("quiescent counter mismatches", quiet_mismatches),
        ("quiescence timeout", quiet["timed_out"]),
        ("config", result["config_problems"]), ("session changes", result["session_changes"]),
        ("monitor/FIB mismatch", mismatches)) if bad]
    assert not failures, failures


# ---------------------------------------------------------------------------
# Host-side collection
# ---------------------------------------------------------------------------


def exp2_baseline(exp2_root):
    """Experiment 2 staggered 10 s per-move rates from its committed and raw outputs."""
    rows = []
    for row in read_csv(exp2_root / "results_exp2.csv"):
        if row["schedule"] != "staggered" or float(row["dwell_s"]) != 10:
            continue
        moves = int(row["completed_moves"])
        point = exp2_root / "trials/dwell-10s" / f"staggered-seed-{row['seed']}"
        belief = [float(r["belief_ms"]) / 1000 for r in read_csv(point / "moves.csv")
                  if r["belief_ms"] not in ("", "None")] if (point / "moves.csv").exists() else []
        rows.append(dict(
            source=f"exp2 staggered 10s seed {row['seed']}", adv_interval_s="unset (0)",
            moves=moves, updates_per_move=int(row["bgp_updates"]) / moves,
            type2_nlris_per_move=int(row["type2_nlris"]) / moves,
            robot_type2_nlris_per_move=int(row["robot_type2_nlris"]) / moves,
            type2_nlris_per_update=float(row["type2_nlris_per_update"]),
            t_stale_p50_s=pct(belief, 50), t_stale_max_s=max(belief) if belief else None,
            final_leaf5_correct=f"{row['leaf5_final_correct']}/{row['leaf5_final_checked']}",
        ))
    return rows


def collect(group):
    root = Path(__file__).resolve().parent
    group = group.resolve()
    summaries = sorted(group.glob("adv-*s/seed-*/exp3_summary.json"),
                       key=lambda p: (int(p.parent.parent.name[4:-1]), p.parent.name))
    assert summaries, f"No exp3_summary.json under {group}"
    rows, checks = [], []
    evidence = root / f"{group.name}_evidence"
    for path in summaries:
        data = json.loads(path.read_text())
        for key in ("seq_skip_examples", "end_window_counter_mismatch_detail",
                    "quiescent_counter_mismatch_detail"):
            data.pop(key, None)
        data["adv_interval_config"] = json.dumps(data["adv_interval_config"], sort_keys=True)
        data["config_problems"] = len(data["config_problems"])
        rows.append(data)
        target = evidence / f"{path.parent.parent.name}-{path.parent.name}"
        (target / "frr_config").mkdir(parents=True, exist_ok=True)
        for name in EVIDENCE_FILES:
            if (path.parent / name).exists():
                shutil.copy2(path.parent / name, target / name)
        for name in sorted((path.parent / "frr_config").iterdir()):
            shutil.copy2(name, target / "frr_config" / name.name)
        checks.append(dict(
            source=f"exp3 {group.name} adv {data['adv_interval_s']}s seed {data['seed']}",
            adv_interval_s=data["adv_interval_s"], moves=data["completed_moves"],
            updates_per_move=data["updates_per_move"],
            type2_nlris_per_move=data["type2_nlris_per_move"],
            robot_type2_nlris_per_move=data["robot_type2_nlris_per_move"],
            type2_nlris_per_update=data["type2_nlris_per_update"],
            t_stale_p50_s=data["t_stale_p50_s"], t_stale_max_s=data["t_stale_max_s"],
            final_leaf5_correct=f"{data['final_leaf5_correct']}/{data['final_leaf5_checked']}",
        ))
    write_csv(root / f"{group.name}_exp3.csv", rows)
    write_csv(root / f"{group.name}_baseline_check.csv",
              exp2_baseline(root.parent / "experiment2") + checks)
    print(f"Wrote {group.name}_exp3.csv, {group.name}_baseline_check.csv and {evidence.name}/")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "point":
        analyze_point(Path(sys.argv[2]))
    elif len(sys.argv) == 3 and sys.argv[1] == "collect":
        collect(Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
