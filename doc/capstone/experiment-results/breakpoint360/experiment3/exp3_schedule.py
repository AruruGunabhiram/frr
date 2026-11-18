# SPDX-License-Identifier: ISC
"""Experiment 3 instrumentation layered on the Experiment 2 matched workload.

Load after ``exp2_schedule`` (``-p exp2_schedule -p exp3_schedule``). The
Experiment 2 plugin supplies the seeded staggered schedule, the quiet window
of at least one dwell and the final leaf5 FIB snapshot. This plugin adds:

* the same raised capture receive buffer in every MRAI arm;
* a BGP quiet gate before capture starts, so MRAI-delayed warm-up UPDATEs do
  not leak into the quiet control window;
* FRR running-config, per-peer MRAI and update-group evidence at capture
  start and after cooldown;
* a session-reset and daemon-log audit over the measured interval;
* a fixed cooldown for every arm, long enough for two 5 s MRAI hops.

``MOB_ADV_INTERVAL`` itself is applied by the archived
``test_mobility_scale.py`` through ``mobility_scale.set_adv_interval`` on the
``fabric`` peer-group of every router, before the robots are placed.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path

import mobility_scale as ms
import pytest
import route_counter
from lib.topogen import get_topogen

OUT = Path(os.environ["MOB_OUT"])
ADV = int(os.environ["MOB_ADV_INTERVAL"])
COOLDOWN_S = int(os.environ.get("EXP3_COOLDOWN_S", "20"))
GATE_HOLD_S = max(3, 2 * ADV + 2)
GATE_TIMEOUT_S = 120
EVIDENCE = OUT / "frr_config"
DAEMON_LOGS = ("bgpd.log", "bgpd.err", "zebra.log", "zebra.err")
LOG_ISSUE = re.compile(
    r"\b(error|err|crit|critical|assert|backtrace|segfault|signal|notification"
    r"|hold timer|reset|ADJCHANGE)\b", re.I)


# tcpdump -dd "tcp port 179" (Ethernet): IPv6 next-header TCP or unfragmented
# IPv4 TCP, either port 179. Same frames as bgp_pcap._is_bgp keeps.
BGP_BPF = [
    (0x28, 0, 0, 12), (0x15, 0, 6, 0x86DD), (0x30, 0, 0, 20), (0x15, 0, 15, 6),
    (0x28, 0, 0, 54), (0x15, 12, 0, 179), (0x28, 0, 0, 56), (0x15, 10, 11, 179),
    (0x15, 0, 10, 0x800), (0x30, 0, 0, 23), (0x15, 0, 8, 6), (0x28, 0, 0, 20),
    (0x45, 6, 0, 0x1FFF), (0xB1, 0, 0, 14), (0x48, 0, 0, 14), (0x15, 2, 0, 179),
    (0x48, 0, 0, 16), (0x15, 0, 1, 179), (0x06, 0, 0, 262144), (0x06, 0, 0, 0),
]
PCAP_FILTER_PATCH = f"""\
    # exp3 kernel BPF filter
    import ctypes as _ctypes
    _prog = b"".join(struct.pack("HBBI", *i) for i in {BGP_BPF!r})
    _buf = _ctypes.create_string_buffer(_prog)
    sock.setsockopt(socket.SOL_SOCKET, 26, struct.pack(
        "HL", {len(BGP_BPF)}, _ctypes.addressof(_buf)))
"""
PCAP_STATS_MARK = "# exp3 socket statistics"
PCAP_STATS_PATCH = """\
        # exp3 socket statistics
        import json as _json
        _packets, _drops = struct.unpack("II", sock.getsockopt(263, 6, 8))
        _dev = [l.split() for l in open("/proc/self/net/dev") if l.strip().startswith(ifname + ":")]
        with open(path + ".stats.json", "w") as _out:
            _json.dump({"tp_packets": _packets, "tp_drops": _drops,
                        "rcvbuf": sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
                        "proc_net_dev": _dev,
                        "softnet_stat": open("/proc/net/softnet_stat").read().split("\\n")}, _out)
"""


def configure_capture_buffer():
    """Raise the capture socket buffer in every arm (Experiment 2 used it at 30 s)."""
    recorder = Path(route_counter.BGP_PCAP)
    source = recorder.read_text()
    original = "socket.SO_RCVBUF, 4 << 20"
    replaced = 'getattr(socket, "SO_RCVBUFFORCE", 33), 8 << 20'
    if replaced not in source:
        assert source.count(original) == 1, "Unexpected archived capture socket setup"
        source = source.replace(original, replaced)
        recorder.write_text(source)
    if PCAP_STATS_MARK not in source:
        # The archived socket is unfiltered ETH_P_ALL and keeps BGP frames in
        # Python, so it also queues all data-plane traffic on the link. A
        # diagnostic run (diag/) showed 2.4k-4.4k kernel socket drops per busy
        # link at MRAI 5; when a BGP frame is among them the trial fails its
        # capture-gap check. Filtering "tcp port 179" in the kernel keeps
        # exactly the frames the Python check already kept, and the socket's
        # packet and drop counters are written when each capture stops.
        bind = "    sock.bind((ifname, 0))\n"
        anchor = "            f.write(data)\n            f.flush()\n"
        assert source.count(bind) == 1 and source.count(anchor) == 1, \
            "Unexpected archived capture loop"
        source = source.replace(bind, PCAP_FILTER_PATCH + bind)
        recorder.write_text(source.replace(anchor, anchor + PCAP_STATS_PATCH))
    settings = {
        "recorder": "handoff bgp_pcap.py",
        "option": "SO_RCVBUFFORCE",
        "requested_bytes": 8 << 20,
        "kernel_filter": "tcp port 179 (classic BPF, SO_ATTACH_FILTER)",
        "socket_stats": "bgp_capture/*.pcap.stats.json",
        "applied_to": "Experiment 3 trials started after 2026-11-18T09:00Z; earlier "
                      "trials used the userspace-only filter",
    }
    (OUT / "capture_settings.json").write_text(json.dumps(settings, indent=2) + "\n")


configure_capture_buffer()

# Arm B ("isolated") announce profile. The archived "repeat" robot pings its
# gateway 4 times over 0.75 s after a roam. The new leaf only relearns the
# robot's IP from gateway traffic that arrives after the old leaf's MAC/IP
# withdrawal has reached it. That takes about four MRAI-paced hops
# (new leaf -> spine -> old leaf -> spine -> new leaf), up to about 20 s at
# MRAI 5. "keepalive" keeps the same 4 Hz gateway pings going for
# KEEPALIVE_S after every attach, so every arm can relearn the IP and only
# the BGP timer delay remains.
KEEPALIVE_S = 24
KEEPALIVE_CMD = ["ping", "-c", str(KEEPALIVE_S * 4), "-i", "0.25", "-W", "1", "-q", ms.GW]
ms.ANNOUNCE["keepalive"] = KEEPALIVE_CMD
(OUT / "announce.json").write_text(json.dumps({
    "robot": os.environ.get("MOB_ROBOT", "repeat"),
    "repeat": ms.ANNOUNCE["repeat"],
    "keepalive": KEEPALIVE_CMD,
}, indent=2) + "\n")


def _peer_fields(info):
    stats = info.get("messageStats", {})
    return {
        "state": info.get("bgpState"),
        "peer_group": info.get("peerGroup"),
        "mrai_ms": info.get("minBtwnAdvertisementRunsTimerMsecs"),
        "established": info.get("connectionsEstablished"),
        "dropped": info.get("connectionsDropped"),
        "last_reset": info.get("lastResetDueTo"),
        "updates_sent": stats.get("updatesSent"),
        "updates_recv": stats.get("updatesRecv"),
    }


def bgp_snapshot(label):
    """Save running configs and per-peer MRAI/session state for every router."""
    tgen = get_topogen()
    EVIDENCE.mkdir(exist_ok=True)
    snapshot = {"label": label, "t": time.time(), "expected_adv_interval_s": ADV, "routers": {}}
    for router in ms.ROUTERS:
        gear = tgen.gears[router]
        config = gear.vtysh_cmd("show running-config")
        (EVIDENCE / f"{router}_{label}_running.conf").write_text(config)
        groups = gear.vtysh_cmd("show bgp l2vpn evpn update-groups")
        (EVIDENCE / f"{router}_{label}_evpn_update_groups.txt").write_text(groups)
        neighbors = gear.vtysh_cmd("show bgp neighbors json", isjson=True)
        snapshot["routers"][router] = {
            "config_lines": re.findall(r"^ *neighbor \S+ advertisement-interval \d+$",
                                       config, re.M),
            "update_group_mrai": re.findall(r"MRAI value \(seconds\): (\d+)", groups),
            "peers": {name: _peer_fields(info) for name, info in sorted(neighbors.items())
                      if isinstance(info, dict) and "bgpState" in info},
        }
    (EVIDENCE / f"bgp_{label}.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    return snapshot


def log_offsets():
    tgen = get_topogen()
    offsets = {}
    for router in ms.ROUTERS:
        logdir = Path(tgen.gears[router].gearlogdir)
        for name in DAEMON_LOGS:
            path = logdir / name
            offsets[f"{router}/{name}"] = path.stat().st_size if path.exists() else 0
    return offsets


def log_audit(start_offsets):
    """Daemon log lines written during the measured interval that look like problems."""
    tgen = get_topogen()
    result = {"files": {}, "issue_lines": []}
    for router in ms.ROUTERS:
        logdir = Path(tgen.gears[router].gearlogdir)
        for name in DAEMON_LOGS:
            key = f"{router}/{name}"
            path = logdir / name
            if not path.exists():
                result["files"][key] = {"present": False}
                continue
            whole = path.read_text(errors="replace")
            segment = path.read_bytes()[start_offsets.get(key, 0):].decode(errors="replace")
            issues = [line for line in segment.splitlines() if LOG_ISSUE.search(line)]
            result["files"][key] = {
                "present": True,
                "bytes_total": len(whole),
                "bytes_in_interval": len(segment),
                "issue_lines_in_interval": len(issues),
                "adjchange_down_whole_run": len(re.findall(r"ADJCHANGE.*\bDown\b", whole)),
            }
            result["issue_lines"] += [f"{key}: {line}" for line in issues[:200]]
    return result


_original_start = route_counter.start
STATE = {}


def gated_start(tgen, outdir):
    """Wait until no router's UPDATE counters move for GATE_HOLD_S, then capture."""
    began = time.time()
    last = ms.update_counters()
    stable_since = time.time()
    changes = 0
    while time.time() - stable_since < GATE_HOLD_S:
        assert time.time() - began < GATE_TIMEOUT_S, "BGP UPDATE counters never settled"
        time.sleep(0.5)
        current = ms.update_counters()
        if current != last:
            changes += 1
            last, stable_since = current, time.time()
    gate = {"hold_s": GATE_HOLD_S, "waited_s": round(time.time() - began, 3),
            "counter_changes_seen": changes}
    (OUT / "quiet_gate.json").write_text(json.dumps(gate, indent=2) + "\n")
    STATE["start_snapshot"] = bgp_snapshot("capture_start")
    STATE["log_offsets"] = log_offsets()
    return _original_start(tgen, outdir)


route_counter.start = gated_start

_original_stop = route_counter.stop
QUIESCE_TIMEOUT_S = 120


def quiescent_stop(cap):
    """Before the capture stops, wait for BGP to go quiet and record the counters.

    The archived test reads FRR's UPDATE counters right at the end of the
    cooldown. If routes are still propagating then, the counters and the
    windowed capture disagree by whatever is in flight. Waiting here (capture
    and leaf5 route monitor still running) gives a counter snapshot that the
    whole capture must match exactly, and the time BGP actually went quiet.
    """
    began = time.time()
    last = ms.update_counters()
    stable_since = time.time()
    changes = 0
    timed_out = False
    while time.time() - stable_since < GATE_HOLD_S:
        if time.time() - began > QUIESCE_TIMEOUT_S:
            timed_out = True
            break
        time.sleep(0.5)
        current = ms.update_counters()
        if current != last:
            changes += 1
            last, stable_since = current, time.time()
    record = {"hold_s": GATE_HOLD_S, "began": began, "last_change_seen": stable_since,
              "waited_s": round(time.time() - began, 3), "counter_changes_seen": changes,
              "timed_out": timed_out, "counters": last}
    (OUT / "quiescence.json").write_text(json.dumps(record, indent=2) + "\n")
    return _original_stop(cap)


route_counter.stop = quiescent_stop


def _check(snapshot):
    """Verify configured and operational MRAI on every router and fabric peer."""
    problems = []
    for router, data in snapshot["routers"].items():
        if data["config_lines"] != [f" neighbor fabric advertisement-interval {ADV}"]:
            problems.append(f"{router}: running-config {data['config_lines']}")
        if not data["update_group_mrai"] or set(data["update_group_mrai"]) != {str(ADV)}:
            problems.append(f"{router}: update-group MRAI {data['update_group_mrai']}")
        for peer, fields in data["peers"].items():
            if fields["state"] != "Established" or fields["mrai_ms"] != ADV * 1000:
                problems.append(f"{router} {peer}: {fields['state']} mrai_ms={fields['mrai_ms']}")
    return problems


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    is_experiment = item.name == "test_mobility_experiment"
    if is_experiment:
        item.module.COOLDOWN_S = COOLDOWN_S
    result = yield
    if is_experiment:
        # Stop leftover keepalive pings before the namespaces are deleted.
        subprocess.run(["pkill", "-f", " ".join(KEEPALIVE_CMD)], check=False)
    if not is_experiment or result.excinfo:
        return
    end = bgp_snapshot("end")
    start = STATE["start_snapshot"]
    resets = []
    for router, data in end["routers"].items():
        for peer, fields in data["peers"].items():
            before = start["routers"][router]["peers"].get(peer, {})
            if (fields["established"], fields["dropped"]) != (
                    before.get("established"), before.get("dropped")):
                resets.append(dict(router=router, peer=peer, before=before, after=fields))
    verification = {
        "adv_interval_s": ADV,
        "cooldown_s": COOLDOWN_S,
        "routers": len(end["routers"]),
        "fabric_peers": sum(len(d["peers"]) for d in end["routers"].values()),
        "config_problems_capture_start": _check(start),
        "config_problems_end": _check(end),
        "session_changes_during_measurement": resets,
        "max_connections_established": max(
            f["established"] for d in end["routers"].values() for f in d["peers"].values()),
        "total_connections_dropped": sum(
            f["dropped"] for d in end["routers"].values() for f in d["peers"].values()),
        "daemon_logs": log_audit(STATE["log_offsets"]),
    }
    (OUT / "frr_verification.json").write_text(json.dumps(verification, indent=2) + "\n")
