# Experiment 3 plan: BGP advertisement interval (MRAI)

## Question

Does raising `neighbor fabric advertisement-interval` (the minimum route
advertisement interval, MRAI) from 0 to 1 to 5 seconds reduce BGP UPDATE
messages, EVPN Type-2 NLRIs, or both? How much longer does leaf5's installed
robot /32 stay stale when it does?

The mentor's question is whether batching fixes the load or only trades it
for staleness. Measure both sides on the same fabric and workload.

## Phase 1: what the timer does in this FRR tree

The tree is FRR 10.8.0-dev (`breakpoint360`). The C source was not modified.

### Three mechanisms

| Mechanism | Where | Scope | Applies to robot moves? |
|---|---|---|---|
| **MRAI** (`neighbor X advertisement-interval N`, 0–600 s) | CLI `bgpd/bgp_vty.c:8685`. Setter `peer_advertise_interval_set`, `bgpd/bgpd.c:7291`. Timer logic `bgp_adjust_routeadv`, `bgpd/bgp_fsm.c:1145`. Gate `bgp_generate_updgrp_packets`, `bgpd/bgp_packet.c:466` | Per peer. Stops a peer from writing new UPDATE packets while its `t_routeadv` timer runs. This includes withdrawals. | **Yes.** This is the experiment's variable. |
| **Route-processing hold** (`process_queue->spec.hold = 50`) | `bgpd/bgp_route.c:5392` | Per BGP instance. The best-path work queue waits 50 ms before running and batches whatever arrived. | Yes, but it is fixed. It is not a knob and is not varied. |
| **Subgroup coalesce time** (`coalesce-time` under `router bgp`, default 1000 ms + 50 ms per peer, max 10 s) | `subgroup_announce_all`, `bgpd/bgp_updgrp_adv.c:1149`. Defaults in `bgpd/bgp_updgrp.h:35` | Per update subgroup. It only delays the **full-table announcement** when a subgroup is created or a peer comes up. After that `v_coalesce` is reset to 0. | **No.** It is out of scope. It affects session bring-up, not incremental moves. |

### How MRAI behaves

- **The 0 s case.** When `v_routeadv == 0`, `bgp_adjust_routeadv` cancels the
  timer and schedules packet generation immediately (`bgp_fsm.c:1153`). The
  datacenter profile already uses 0 for eBGP (`BGP_DEFAULT_EBGP_ROUTEADV 0`,
  `bgpd/bgpd.h:2542`). An explicit `advertisement-interval 0` is therefore
  functionally the same as Experiment 2's unset configuration.
- **When the timer starts.** It starts when a route is queued to a subgroup
  whose advertise or withdraw FIFO was empty (`bgp_updgrp_adv.c:692` and
  `:744`).
  - **Case I:** if the last UPDATE write was more than MRAI ago, the timer
    fires at once. The first change after a quiet period is not delayed.
  - **Case II:** otherwise the timer waits `MRAI − (now − last_update)`.
- **One-second granularity.** `last_update` is `monotime()` in whole seconds,
  set once per write batch (`bgpd/bgp_io.c:501`). The elapsed time is compared
  in integer seconds.
  - With MRAI 1 s, a batch may go out only milliseconds after the previous
    one if a second boundary falls between them.
  - With MRAI 5 s, the spacing between bursts on one session should be about
    4–5 s.
- **Per-hop effect.** In this fabric every leaf and spine transmits on every
  robot move: deliveries, echoes and re-advertisements (Experiment 1). Under
  staggered load at about 6.4 moves/s, every session's timer should stay
  armed. Each hop then waits up to one MRAI.

### Can intermediate Type-2 changes be suppressed?

Yes, by design. Pending UPDATEs live in a per-subgroup adj-out FIFO until
packet generation runs. While MRAI holds generation back:

- **A newer change replaces the queued one.** If a prefix changes again,
  `bgp_adj_out_set_subgroup` removes the older queued advertisement
  (`bgp_updgrp_adv.c:647`) and queues the latest one.
- **A change back to what was already sent is dropped.** If the new
  attributes equal what was last sent, the queued entry is deleted and
  nothing is sent (`:607`–`:622`, "suppress UPDATE" / "delete queued UPDATE").
- **A withdrawal can cancel a queued advertisement.** If the prefix was never
  sent, the queued advertisement is freed and no withdrawal is sent
  (`bgp_adj_out_unset_subgroup`, `:703`–`:760`). If it was sent, the queued
  advertisement is replaced by a withdrawal.

**Prediction for this workload: little or none.** Suppression needs the *same
NLRI* to change twice on one sender while that sender's timer is running.
EVPN Type-2 keys include the origin leaf's RD, so the new leaf's MAC and
MAC/IP routes are different NLRIs from the old leaf's. With 10 s dwell, a given
robot's next change comes about 10 s later, which is ≥ 2 × the largest MRAI.
NLRIs per move should stay close to 80.

**How the measurement detects it:**

- robot Type-2 NLRIs per move against the MRAI 0 arm;
- per directed session robot advertisements and withdrawals
  (`session_counts.csv`);
- **MAC-mobility sequence skips**: an advertised sequence number on a session
  that jumps by more than one, meaning that session never carried an
  intermediate location.

### Configuration application

- **How it is applied.** `MOB_ADV_INTERVAL` is applied by the archived
  `test_mobility_scale.py` through `mobility_scale.set_adv_interval`. It runs
  `neighbor fabric advertisement-interval N` under `router bgp` on all seven
  routers, before access points and robots exist.
- **Effect on peers.** The peer-group setter copies the value to every member
  that does not override it. It then re-announces routes to established
  members (`bgpd.c:7319`–`7331`), so the quiet gate below has to absorb that
  burst.
- **How it is verified, per trial, at capture start and after cooldown:**
  - running-config contains exactly ` neighbor fabric advertisement-interval N`
    on every router;
  - `show bgp l2vpn evpn update-groups` shows `MRAI value (seconds): N`;
  - `show bgp neighbors json` gives `minBtwnAdvertisementRunsTimerMsecs ==
    N × 1000` and `Established` for all 14 fabric peers.

## Phase 2 design

| Fixed | Value |
|---|---|
| Fabric | 5 leaves, 2 spines, fresh topology per trial (`test_evpn_capstone.py`, unchanged) |
| Robots and schedule | 64 robots, `MOB_SYNC=0`, `MOB_DWELL=10`. Experiment 2's seeded staggered planner (`exp2_schedule.py`) is reused unchanged. |
| Endpoint behavior | Break-before-make, `MOB_ROBOT=repeat`, DAD off. Default sender-AS-path behavior: no `sender-as-path-loop-detection`. |
| Capture | `route_counter.py` on all 10 leaf–spine links. Raised receive buffer in every arm. |
| Quiet gate | Capture starts only after every router's UPDATE counters stay unchanged for `max(3, 2 × MRAI + 2)` s. A ≥ 10 s quiet control window follows. |
| Cooldown | 20 s for every arm: two 5 s hops, 1 s rounding per hop and processing, with margin. |
| Matrix | MRAI ∈ {0, 1, 5} s × `MOB_SEED` ∈ {1, 2, 3}, 6 rounds (384 moves) per trial, run seed-major. After the smoke, Option C was chosen: both the `repeat` arm (A) and an isolated 24 s `keepalive` arm (B), 18 trials. See [EXPERIMENT3_FINDINGS.md](EXPERIMENT3_FINDINGS.md). |

### Per-move leaf5 state

- **Source.** leaf5's kernel table 500, followed event by event with
  `ip monitor route` (the installed forwarding state, not the BGP RIB).
- **T_stale.** `T_stale = T_correct − T_attached`, where `T_correct` is the
  start of the final interval in which the robot's /32 selects only the new
  VTEP and keeps doing so until the robot's next move or the end of the
  cooldown. `first_correct_s`, `correct_at_attach` and
  `reverted_after_correct` record earlier, unstable correct states. This
  rule was tightened after the smoke: at MRAI 5, stale state sometimes
  pointed at the robot's new leaf before the move's own update arrived.
- **Classes.** The 1 s threshold was fixed beforehand from Experiment 2's
  1,152 staggered 10 s moves (p50 633 ms, max 700 ms).

| Class | Rule |
|---|---|
| `correct` | Converged before the robot's next move, with T_stale ≤ 1 s |
| `transient_stale` | Converged before the next move, with T_stale > 1 s |
| `censored` | The robot's next move started before leaf5 converged on this one |
| `persistently_wrong` | The last move was not converged, or had reverted, at the end of the cooldown |

**Extra per-move columns:**

- time on the old VTEP, with no /32, and on another VTEP;
- whether the route reverted after first becoming correct.

**Continuous timeline.** Every 100 ms, count robots whose leaf5 /32 is
correct, on the old VTEP, on another VTEP, missing, or ECMP. From this come:

- stale robot-seconds;
- robots wrong when the moves end;
- when the cooldown reaches 64/64 correct and stays there.

**Cross-checks:**

- the monitor's last state must match the final `ip -j route` snapshot for
  every robot;
- Experiment 2's independent final-FIB check.

### Wire outcomes

- UPDATE messages and Type-2 NLRIs, split into robot and non-robot, with
  NLRIs per UPDATE. These use Experiment 2's definitions and reconciliation.
- Per directed session: UPDATE bursts (messages within 50 ms) and the
  minimum and median gap between bursts. This is direct on-the-wire evidence
  that the timer is operating.
- The last Type-2 UPDATE after the moves end, and NLRIs during cooldown, to
  show the cooldown flushed delayed updates.
- bgpd and zebra CPU seconds, summed over the seven switches. These are
  hardware-dependent and reported only as context.

### Trial acceptance

A trial is rejected and rerun on a fresh topology if any of these hold:

- a capture gap;
- an FRR counter mismatch. Before the capture stops, `exp3_schedule.py`
  waits for BGP to go quiet (counters stable for the gate hold). The whole
  capture must then match those counters. A mismatch at the end of the
  cooldown is reported but not used for acceptance, because at MRAI > 0
  UPDATEs can still be in flight then;
- the wrong configured or operational MRAI;
- a BGP session change;
- a mismatch between the monitor and the FIB;
- an incomplete workload.

Daemon-log issue lines in the measured interval are reported for review.

### Predictions from the code (before the matrix)

| MRAI | UPDATEs | Type-2 NLRIs per move | T_stale |
|---:|---|---:|---|
| 0 | About 60 per move (Experiment 2: 59.6) | 80 robot | About 0.63 s |
| 1 | Large drop: each session emits about 1 burst/s with several moves packed | About 80 | Roughly 0.6 s + up to 2 × 1 s |
| 5 | Larger drop | About 80 | Roughly 0.6 s + up to 2 × 5 s. Some moves may be censored by the 10 s dwell. |

## Phase 3 smoke gate

Smoke outcome and the decision it raises: see [README.md](README.md).

- **Runs.** One seed and two rounds (128 moves) per MRAI value.
- **Pass requires:**
  - each interval is visible in config and operational state;
  - zero capture gaps;
  - capture counts reconcile with FRR counters;
  - no session resets or daemon errors;
  - transient stale routes observed at 1 s and 5 s and resolved during
    cooldown;
  - the MRAI 0 arm matches Experiment 2's staggered per-move rates.

The full matrix runs only after review.

## Limits

- **Workload range.** One fabric size (5×2), one dwell (10 s) and one
  staggered schedule.
- **Timings.** T_stale is a wall-clock time on Colima, accepted here only
  because a configured multi-second timer dominates it. The sub-second part
  of T_stale is platform-dependent.
- **Settings not tested.** Spine-only or leaf-only MRAI, `coalesce-time`,
  and MRAI combined with `sender-as-path-loop-detection` are possible
  follow-ups. They are not in this matrix.
