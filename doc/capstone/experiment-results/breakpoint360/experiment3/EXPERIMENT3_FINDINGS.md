# Experiment 3 findings: BGP advertisement interval (MRAI) 0 / 1 / 5 s

**Question.** Does raising `neighbor fabric advertisement-interval` from 0 to
1 to 5 s reduce BGP load, and what does it cost in staleness of the
controller-side leaf (leaf5) robot /32?

**Short answer:**

- **Messages drop a lot.** MRAI batches UPDATE messages: 57–61% fewer at 1 s
  and 86–88% fewer at 5 s. bgpd and zebra CPU fall with them.
- **Changes barely drop at 1 s, and the drop at 5 s is mostly lost work.**
  At 1 s, robot Type-2 changes fall by at most 2%. At 5 s about a quarter are
  missing, but almost all of them are MAC/IP routes that the robot's new leaf
  **never originated** before the robot moved on. Very little is genuine
  coalescing.
- **The cost is large and grows with MRAI.**
  - **MRAI 1.** With the operational robot model (Arm A), 47% of moves never
    converge at leaf5, and 21–35 robots end with no correct /32. With robots
    that keep talking (Arm B), every move converges, but each takes about
    5.4 s instead of about 0.5 s.
  - **MRAI 5.** About 77–79% of moves never converge in either arm.
  - **New failure.** MAC-mobility sequence ties appear (Experiment 4/5's
    failure mode) with no overload at all.
- **The mechanism.** MRAI also delays withdrawals. The new leaf can only
  relearn the robot's IP after the old leaf's MAC/IP withdrawal arrives, which
  is the NOARP behaviour measured in the roadmap360 study. A 10 s dwell then
  becomes "the robot moved again before the previous move spread".

Raw numbers: [results_exp3.csv](results_exp3.csv) (one row per trial),
[summary_exp3.csv](summary_exp3.csv) (mean / min / max per cell),
[paired_exp3.csv](paired_exp3.csv) (same-seed comparisons). Code-path
verification and design: [EXPERIMENT3_PLAN.md](EXPERIMENT3_PLAN.md).

## Setup

| | |
|---|---|
| Fabric | 5 leaves × 2 spines, eBGP EVPN, fresh topology per trial. `test_evpn_capstone.py` unchanged. FRR 10.8.0-dev (`breakpoint360`), C source unmodified |
| Workload | 64 robots, Experiment 2's seeded staggered schedule (`exp2_schedule.py`, unchanged): dwell 10 s, 6 rounds = 384 moves, 6.4 moves/s achieved in every trial |
| Endpoint | Break-before-make roams, DAD off, default sender-AS-path behaviour |
| Timer | `neighbor fabric advertisement-interval {0,1,5}` on all 7 routers. Verified per trial in running-config, update groups and per-peer `minBtwnAdvertisementRunsTimerMsecs` on all 14 sessions, at capture start and at the end |
| Seeds | 1, 2, 3 per cell (same robot schedule across arms and MRAI values for a given seed) |
| Windows | Moves (60 s) + 20 s cooldown. Capture then keeps running until every router's UPDATE counters are stable for `max(3, 2·MRAI+2)` s (quiescence) |
| Platform | Colima `frr-dev` on Apple M4: 6 vCPU, about 8 GiB, Linux 6.8.0-117 |

**The two arms differ only in what each robot sends after a roam:**

- **Arm A, operational (`MOB_ROBOT=repeat`).** The archived robot sends 4
  gateway pings over 0.75 s after attach.
- **Arm B, isolated (`keepalive`).** The same robot keeps sending 4 Hz gateway
  pings for **24 s** after every attach (`exp3_schedule.py`). The Part A
  proposal said "about 12 s". 24 s was used because the IP relearn waits for a
  withdrawal that crosses about four MRAI-paced hops (new leaf → spine → old
  leaf → spine → new leaf), up to about 20 s at MRAI 5. 12 s would have cut
  off part of the MRAI 5 arm.

### Acceptance (every one of the 18 trials)

- 384/384 moves completed.
- 0 capture gaps on all 10 leaf–spine links.
- Exact reconciliation: every captured UPDATE matches FRR's per-router tx/rx
  counters at quiescence.
- 0 UPDATEs in the quiet control window.
- MRAI correct in config and operational state.
- 0 session changes or drops, and 0 daemon-log issue lines.
- leaf5 route monitor equals the final FIB for every robot.

Rejected attempts are kept in `rejected/` (see
[Run history](#run-history-and-rejected-attempts)).

## Results: Arm A (operational, `repeat`)

Mean of 3 seeds, [min–max] where the spread matters.

| | MRAI 0 s | MRAI 1 s | MRAI 5 s |
|---|---:|---:|---:|
| **Wire** | | | |
| BGP UPDATEs per move (moves + cooldown) | 59.4 | 25.5 | 7.2 [6.5–7.9] |
| UPDATE reduction vs MRAI 0, same seed | – | 57.0% | 87.9% |
| UPDATEs per move through quiescence (all delayed UPDATEs) | 59.4 | 25.7 | 7.6 [6.8–8.4] |
| BGP bytes per move | 7,180 | 5,100 | 2,850 |
| Byte reduction vs MRAI 0 | – | 29.1% | 60.3% |
| Robot Type-2 NLRIs per move | 80.0 | 78.4 | 59.9 |
| MAC adv / wd per move | 20 / 20 | 20 / 20 | 20 / 16.5 |
| MAC/IP adv / wd per move | 20 / 20 | 18.5 / 19.9 | 11.6 / 11.9 |
| Type-2 NLRIs per Type-2 UPDATE | 1.35 | 3.08 | 8.44 |
| Median per-session minimum gap between UPDATE bursts | 0.05 s | 0.08 s | 4.15 s |
| **Moving vs cooldown windows** | | | |
| UPDATEs during moves / during 20 s cooldown | 22,730 / 76 | 9,376 / 434 | 2,225 / 545 |
| Bytes during moves / during cooldown | 2.75 MB / 10 kB | 1.87 MB / 84 kB | 0.88 MB / 214 kB |
| Last UPDATE after moves end | 0.4 s | 24.5 s [19.1–32.2] | 38.7 s [35.5–41.2] |
| **CPU** (7 switches, moves + cooldown, context only) | | | |
| bgpd / zebra CPU seconds | 4.62 / 2.36 | 2.03 / 1.24 | 1.09 / 0.67 |
| **leaf5 staleness** (384 moves per trial) | | | |
| T_stale p50 / p95 / max (converged moves) | 0.63 / 0.68 / 0.90 s | 3.29 / 5.67 / 25.5 s | 7.22 / 19.9 / 24.2 s |
| Moves: correct / transient stale / censored / persistently wrong | 383 / 0.7 / 0 / 0 | 1.7 / 201 / 155 / 26.7 | 27.7 / 53 / 269 / 34.3 |
| Moves never converged (censored + wrong) | 0% | 47.3% | 79.0% |
| Stale robot-seconds per move | 0.56 | 7.69 | 10.6 |
| Robots wrong at end of moves → end of cooldown | 3.3 → 0 | 42 → 26.7 | 57.7 → 34.3 |
| leaf5 FIB correct after quiescence | 64/64 | 41 [36–45] | 37.3 [31–42] |
| Controller probe loss | 0.50% | 27.4% | 43.8% |
| **Origination** (per trial, of 384 moves) | | | |
| New leaf originated MAC route during the visit | 384 | 384 | 384 |
| New leaf originated MAC/IP route during the visit | 384 | 203 | 152 |
| … only after the robot had left | 0 | 153 | 97 |
| … never (within 30 s), or the robot's last move | 0 | 1.7 + 26.3 | 102 + 33 |
| MAC / MAC/IP sequence skips on a session | 0 / 0 | 0 / 1,084 | 44 / 1,359 |
| MAC / MAC/IP sequence ties (two leaves, same seq) | 0 / 0 | 0 / 0 | 3.0 / 1.0 |

## Results: Arm B (isolated, `keepalive` 24 s)

| | MRAI 0 s | MRAI 1 s | MRAI 5 s |
|---|---:|---:|---:|
| **Wire** | | | |
| BGP UPDATEs per move (moves + cooldown) | 59.6 | 23.2 | 7.7 [6.5–8.3] |
| UPDATE reduction vs MRAI 0, same seed | – | 61.2% | 87.1% |
| UPDATEs per move through quiescence | 59.6 | 23.2 | 8.0 [6.8–8.8] |
| BGP bytes per move | 7,200 | 4,930 | 2,930 |
| Byte reduction vs MRAI 0 | – | 31.6% | 59.3% |
| Robot Type-2 NLRIs per move | 80.0 | 80.0 | 60.9 |
| MAC adv / wd per move | 20 / 20 | 20 / 20 | 20.1 / 16.4 |
| MAC/IP adv / wd per move | 20 / 20 | 20 / 20 | 12.4 / 12.0 |
| Type-2 NLRIs per Type-2 UPDATE | 1.35 | 3.47 | 8.10 |
| Median per-session minimum gap between UPDATE bursts | 0.05 s | 0.06 s | 3.41 s |
| **Moving vs cooldown windows** | | | |
| UPDATEs during moves / during 20 s cooldown | 22,820 / 77 | 8,301 / 594 | 2,201 / 745 |
| Bytes during moves / during cooldown | 2.76 MB / 10 kB | 1.76 MB / 129 kB | 0.83 MB / 292 kB |
| Last UPDATE after moves end | 0.3 s | 6.7 s | 28.4 s [25.1–34.7] |
| **CPU** (7 switches, moves + cooldown, context only) | | | |
| bgpd / zebra CPU seconds | 3.59 / 1.77 | 2.23 / 1.39 | 1.27 / 0.88 |
| **leaf5 staleness** (384 moves per trial) | | | |
| T_stale p50 / p95 / max (converged moves) | 0.49 / 0.64 / 0.72 s | 5.41 / 6.29 / 6.46 s | 8.72 / 20.6 / 22.9 s |
| Moves: correct / transient stale / censored / persistently wrong | 384 / 0 / 0 / 0 | 0 / 384 / 0 / 0 | 25 / 63 / 274 / 22.3 |
| Moves never converged (censored + wrong) | 0% | 0% | 77.1% |
| Stale robot-seconds per move | 0.52 | 4.91 | 10.5 |
| Robots wrong at end of moves → end of cooldown | 3.3 → 0 | 36.7 → 0 | 59 → 22.3 |
| All 64 correct (and stays so) from | 0.2–0.5 s after moves end | 5.5–6.2 s after moves end | not within cooldown |
| leaf5 FIB correct after quiescence | 64/64 | 64/64 | 64/64 |
| Controller probe loss | 0.49% | 5.7% | 45.7% |
| **Origination** (per trial, of 384 moves) | | | |
| New leaf originated MAC/IP route during the visit | 384 | 384 | 195 [167–211] |
| … only after the robot had left | 0 | 0 | 83 |
| … never (within 30 s), or the robot's last move | 0 | 0 | 98 + 7.3 |
| MAC / MAC/IP sequence skips on a session | 0 / 0 | 0 / 0 | 25 / 1,455 |
| MAC / MAC/IP sequence ties | 0 / 0 | 0 / 0 | 3.7 / 1.3 |

Graphs:

| | |
|---|---|
| [01_timer_vs_updates.png](graphs/01_timer_vs_updates.png) | UPDATE messages per move |
| [02_timer_vs_nlris.png](graphs/02_timer_vs_nlris.png) | Robot Type-2 NLRIs per move, split into MAC and MAC/IP |
| [03_timer_vs_staleness.png](graphs/03_timer_vs_staleness.png) | T_stale p50 and max, and moves never converged |
| [04_leaf5_wrong_timeline.png](graphs/04_leaf5_wrong_timeline.png) | Robots wrong or missing at leaf5 over time, at 100 ms steps |
| [05_tradeoff_and_cpu.png](graphs/05_tradeoff_and_cpu.png) | UPDATE savings vs stale robot-seconds, and bgpd/zebra CPU |

## What the numbers say

### 1. MRAI is working, and it batches messages

- **The timer is visible on the wire.** At 5 s the median per-session minimum
  gap between UPDATE bursts is 3.4–4.2 s. At 0 s it is 0.05 s, which is just
  the 50 ms burst definition.
- **At 1 s the gap can be near zero.** Bursts may follow each other almost
  immediately, as the code predicts. `last_update` is kept in whole seconds
  (`bgpd/bgp_io.c:501`), so a batch can go out just after a second boundary.
- **More routes per message.** Type-2 NLRIs per UPDATE rise from 1.35 to about
  3.1–3.5 at 1 s and about 8.1–8.4 at 5 s.
- **The reductions are not an artifact of the window.** UPDATEs and bytes per
  move fall by the amounts in the tables. Counting every delayed UPDATE up to
  quiescence changes the reduction by less than 1 point: 56.7% / 87.2% in
  Arm A and 61.2% / 86.5% in Arm B.
- **Some of the work moves into the cooldown.** At MRAI 5, 20–25% of the
  UPDATEs in the moves + cooldown window are sent during the cooldown, and a
  few more after it. Arm A was still sending UPDATEs 35–41 s
  after the moves ended.
- **CPU falls in step:**
  - **Same-seed reductions.** bgpd falls 26–76% at MRAI 1 and 60–80% at
    MRAI 5. zebra falls 7–69% at MRAI 1 and 47–74% at MRAI 5.
  - **Why the MRAI 1 range is wide.** The MRAI 0 baselines themselves vary
    by up to 1.5× between seeds. CPU on Colima is context, not a result.

### 2. Changes are barely coalesced; most of the "saved" NLRIs are missing routes

The plan predicted little coalescing, because EVPN Type-2 routes are keyed by
the origin RD and each robot changes only every 10 s.

**MAC routes behave as predicted:**

- At MRAI 1 there is no change: 20 advertisements and 20 withdrawals per move,
  and no MAC sequence skips.
- At MRAI 5 there are still 20 MAC advertisements per move, but withdrawals
  fall to 16.4–16.5 per move. MAC sequence skips appear: 2–72 per trial, out
  of about 7,700 MAC advertisements.

The withdrawal drop fits FRR's adj-out coalescing (`bgp_adj_out_set_subgroup`,
`bgpd/bgp_updgrp_adv.c:607`–`647`). The likely case is a queued withdrawal
replaced by a re-advertisement of the same NLRI while a timer runs, but no
per-NLRI trace confirms this. Either way it is small: about 3.5 of 80 NLRIs
per move.

**MAC/IP routes are a different story.** The MAC/IP drop (20 → 11.6–12.4
advertisements per move at 5 s, 20 → 18.5 in Arm A at 1 s) is mostly
**routes that were never originated**. The capture tells, for every move,
whether the robot's new leaf advertised the MAC/IP route under its own RD
while the robot was there:

| New leaf originated the MAC/IP route… | A 0 s | A 1 s | A 5 s | B 0 s | B 1 s | B 5 s |
|---|---:|---:|---:|---:|---:|---:|
| during the visit | 384 | 203 | 152 | 384 | 384 | 195 |
| only after the robot left | 0 | 153 | 97 | 0 | 0 | 83 |
| not at all (never, or last move) | 0 | 28 | 135 | 0 | 0 | 105 |

How to read this:

- **The skips are mostly missing routes.** The MAC/IP sequence skips (over
  1,000 per trial) are this missing work seen from a session's point of view:
  the next MAC/IP route a session carries has jumped past locations that were
  never advertised.
- **Why the Part A smoke showed none.** Its 2 rounds give each robot only 2
  moves, so a skipped location has no earlier sequence on the same session to
  compare against. The current analyzer, re-run on a copy of the smoke's
  MRAI 5 trial, still reports 0.
- **The NLRI reduction is the wrong measure.** At MRAI 5, the 19–20 fewer
  NLRIs per move are mostly a symptom of moves the fabric never learned.
  They are not savings.

### 3. Why MRAI delays the IP relearn past the robot's announce window

**Background (roadmap360 study, 120 trials; see
[REPORT_360_FINDINGS.md](../../roadmap360/REPORT_360_FINDINGS.md)).**

- After a roam, the new leaf still holds the robot's IP as a remote,
  extern-learned **NOARP** neighbour from the old leaf's MAC/IP route.
- The kernel rejects the robot's early ARPs against that entry. Every one of
  the 36 runs in which an ARP arrived before the withdrawal showed the
  rejection.
- The local IP appears only after the old leaf's MAC/IP withdrawal removes the
  NOARP entry (45 of 50 ordering runs; the other 5 never relearned within the
  window).
- At idle with MRAI 0, that withdrawal gap was about 72–74 ms (median per
  ARP pattern).

**Effect of MRAI.** MRAI gates **withdrawals** as well as advertisements: the
per-peer `t_routeadv` gate in `bgp_generate_updgrp_packets`,
`bgpd/bgp_packet.c:466`. The withdrawal the new leaf waits for needs about
four hops, each paced by MRAI:

1. The new leaf's MAC route (higher sequence) goes to a spine.
2. The spine passes it to the old leaf.
3. The old leaf withdraws its MAC/IP route to a spine.
4. The spine passes the withdrawal back to the new leaf.

Only then can a robot ARP be accepted. The relearned MAC/IP route then needs
two more paced hops to reach leaf5 (5. new leaf → spine, 6. spine → leaf5).

- **MRAI 0.** The withdrawal arrives about 0.2 s after attach (Part A smoke,
  under load). That is inside Arm A's 0.75 s ping window, so every move
  relearns. T_stale is 0.63 s in Arm A and 0.49 s in Arm B.
- **MRAI 1, Arm B.** The keepalive pings are still running when the
  withdrawal arrives, so all 384 moves relearn. The six paced hops give a very
  steady T_stale of 5.2–5.6 s (p50), max 6.5 s, compared with about 0.5 s at
  MRAI 0. This is the **pure timer cost**: about 10× slower convergence, and
  every move is affected because under continuous load each session's timer
  stays armed.
- **MRAI 1, Arm A.** The withdrawal arrives after the 4 pings have ended, so
  the robot's IP is never relearned while the robot is there (181 of 384
  moves).
  - **Stale late origination.** In 146–160 moves per trial, the leaf instead
    originated the robot's MAC/IP route **after the robot had left**: median
    0.56–0.73 s after the robot's next attach, about one MRAI slot. It then
    advertises a /32 at a leaf the robot no longer occupies.
  - **Likely trigger (inferred from timing, not traced).** The robot's ARP
    for the anycast gateway at its next leaf is a broadcast. It plausibly
    floods over VXLAN to the leaf it just left. That leaf's NOARP entry is
    gone by then, so it accepts the ARP.
  - **The robots left wrong.** Robots whose last move never originated
    (24 / 35 / 20 for seeds 1–3) match the robots wrong at the end of the
    cooldown (24 / 35 / 21). Those robots stay wrong until they send something new.
    This is the "IP held as a remote entry, not relearned" issue noted in
    `team-handoff/nov_17_pradeep_RESULTS.md` (section 6, a local, untracked
    handoff file). MRAI makes the window in which the robot's own traffic
    misses it much wider.
- **MRAI 5, both arms.** The six-hop path takes up to about 30 s, three times
  the 10 s dwell. 77–79% of moves are overtaken by the robot's next move
  before leaf5 converges (censored). Even with 24 s of keepalive traffic, only
  195 of 384 moves have the MAC/IP route originated during the visit, because
  the robot has usually moved on before the withdrawal arrives. Arm B
  recovers only because the keepalive keeps running after the last move. All
  22 robots still wrong at the end of the cooldown recovered before
  quiescence. Arm A's do not.

**Why converged moves look faster in Arm A at MRAI 1** (p50 3.29 vs 5.41 s):
Arm A's converged moves are a selected subset. Moves whose relearn missed the
ping window mostly drop out as censored or wrong instead of counting as slow.
Compare the never-converged share and stale robot-seconds per move (Arm A
7.7 vs Arm B 4.9), not T_stale alone.

### 4. MRAI 5 recreates Experiment 4/5's sequence-tie precondition without overload

**What Experiments 4/5 found** (`team-handoff/nov_17_pradeep_RESULTS.md`,
sections 1, 4 and 5):

- Robots stay on the wrong leaf through a **MAC-mobility sequence tie**.
- A tie happens when a robot moves again before its previous move has spread
  to every leaf. Two leaves then claim the same sequence number, and "lower
  VTEP IP wins" (`bgpd/bgp_route.c`, the "same MM seq … lower IP" rule).
- There, the precondition came from overload or a deliberately short second
  move: at zero load, a second move within 100 ms (with the true leaf on the
  higher IP) left 20 of 20 trials stuck.

**Here the precondition comes from the timer alone:**

- At 6.4 moves/s with 10 s dwell, CPU is low in every arm. Still, MRAI 5
  makes "spread" take longer than the dwell for most moves, so ties appear:
  - Arm A: 9 MAC ties and 3 MAC/IP ties over 3 seeds;
  - Arm B: 11 MAC ties and 4 MAC/IP ties over 3 seeds;
  - none at MRAI 0 or 1.
- **In Arm B every tie healed.** Final FIB 64/64 in all seeds: the robot's
  continued traffic makes a leaf advertise a higher sequence, as the Exp 4/5
  notes predicted.
- **Arm A's end state is explained by the missing IP relearn, not by ties.**
  The persistently wrong robots match the last moves that never originated a
  MAC/IP route.

Ties are a small effect at 64 robots. The point is that **raising MRAI moves
the fabric toward the Exp 4/5 failure regime by itself**, without any
overload.

### 5. Answer to the mentor's question

**MRAI does not fix the load; it trades it for staleness.**

- **Load.** It cuts UPDATE messages by more than half at 1 s and by about 87%
  at 5 s, and CPU with them.
- **Staleness, robots that keep talking (Arm B).** 1 s already multiplies
  T_stale by about 10, from 0.5 s to 5.4 s, on every move.
- **Staleness, robots that only announce at attach (Arm A, the archived
  model).**
  - **MRAI 1.** Nearly half of the moves never converge, and a third of the
    robots are left with no correct /32 until they send traffic again.
  - **MRAI 5.** About four-fifths of moves never converge in either arm.
- **No real coalescing benefit.** The Type-2 change volume barely drops,
  because EVPN keys routes by origin RD and robots change only every 10 s.
  What looks like an NLRI reduction is mostly routes that were never
  originated.

## Reproduction

From the repository root on the host. Colima `frr-dev` must have the checkout
mounted at `/home/frr/frr`, and `team-handoff/guna/mobility_tools.tar.gz` must
be present.

```bash
exp3=doc/capstone/experiment-results/breakpoint360/experiment3
bash "$exp3/reproduce.sh" matrix-all          # 18 trials, detached in frr-dev (about 1 h)
tail -f "$exp3/trials/matrix.log"; cat "$exp3/trials/matrix.exit"
bash "$exp3/reproduce.sh" matrix-resume       # keep ACCEPTED trials, rerun the rest
bash "$exp3/reproduce.sh" trial keepalive 5 2 # one trial: ARM ADV SEED
bash "$exp3/reproduce.sh" reanalyze trials    # re-run per-trial analysis only (no new trials)
PYTHON=/usr/bin/python3 bash "$exp3/reproduce.sh" aggregate   # CSVs and graphs (needs matplotlib)
```

**How the runner works:**

- **Per trial.** Each trial runs `pytest` in the container on the archived
  `test_mobility_scale.py` (`test_fabric_baseline` and
  `test_mobility_experiment`), with the plugins `exp2_schedule` then
  `exp3_schedule`. Then `analyze_exp3.py point` runs, and an `ACCEPTED` marker
  is written only if every acceptance check passes.
- **Failed attempts.** A failing attempt is moved to
  `rejected/<arm>/adv-Ns/` and rerun on a fresh topology, up to 3 attempts per
  trial.
- **Re-checking.** `aggregate_exp3.py` re-checks acceptance on all 18 trials
  before writing anything.

## Run history and rejected attempts

All rejected attempts are kept locally in `rejected/`, which is not tracked.

1. **External checkout, 07:34 UTC.** The shared working tree was switched to
   another branch and back by something outside this run, between 07:34:25
   and 07:34:35 UTC.
   - **Effect.** For those seconds, `experiment2/` (the `exp2_schedule` and
     `summarize` modules) did not exist. 30 trial attempts failed instantly
     with `ImportError`. They are in `rejected/checkout-interrupt-0734/`.
   - **Fix.** The runner now copies its plugins into the trial's temp
     directory, so a checkout can no longer remove them mid-run.
   - **One trial restored.** For `keepalive/adv-5s/seed-1`, only the analysis
     step had failed; its pytest run had passed. It was restored and
     re-analyzed, and it passes acceptance.
   - **Warning.** Do not switch branches in this working tree while a matrix
     is running.
2. **Capture gaps at MRAI 5 (13 attempts rejected).** Every rejected attempt
   at MRAI 5 failed only the capture-gap check, often with matching counter
   mismatches. FRR state, convergence checks and the FIB were fine.
   - **Diagnosis.** One diagnostic trial (`diag/`, local) read the kernel's
     `PACKET_STATISTICS` from each capture socket.
     - The archived recorder opens an unfiltered `ETH_P_ALL` socket and keeps
       BGP frames in Python. It therefore also receives all data-plane
       traffic: 39k–257k frames per link.
     - On busy links the kernel dropped 2.4k–4.4k frames at the socket.
       Interface counters showed 0 drops.
     - When a BGP frame was among the dropped frames, the trial failed its
       gap check.
     - Why mostly MRAI 5: there, stale /32s at leaf5 send more misdirected
       controller traffic onto the fabric links (an inference).
   - **Fix.** `exp3_schedule.py` now attaches a kernel BPF filter for
     `tcp port 179` (the `tcpdump -dd` program). The frames kept are exactly
     those the Python check already kept. It also records each socket's
     packet and drop counters.
   - **Which trials used it.** Only the last two trials
     (`repeat/adv-5s/seed-2` and `seed-3`), which then passed on their first
     attempt with about 230 BGP frames and **0 socket drops** per link.
   - **Why the earlier trials stand.** The other 16 used the unfiltered
     recorder. They are complete by construction: 0 gaps, and every UPDATE
     reconciles exactly with FRR's counters. The `capture_kernel_filter`
     column in `results_exp3.csv` marks which recorder each trial used.

## Limitations

- **Timings are platform-dependent.** T_stale and CPU are wall-clock and
  process measurements on Colima. Multi-second MRAI effects dominate them, but
  the sub-second parts and the CPU figures will differ on real switches. CPU
  also varies up to 1.5× between seeds at MRAI 0.
- **One configuration.** One fabric size (5×2), one dwell (10 s), one
  staggered schedule, 3 seeds per cell. A longer dwell would reduce
  censoring. More robots would increase the batching gain.
- **Arm B is a model, not a product setting.** Its 24 s keepalive is
  synthetic traffic (4 Hz × 64 robots) that also adds data-plane load. It
  isolates the timer; it is not a recommended robot behaviour.
- **Two claims are inferences, not traces:**
  - the trigger of the late MAC/IP origination after a robot leaves (flooded
    ARP at the next attach);
  - the cause of the MAC withdrawal reduction at MRAI 5 (adj-out
    replacement).

  Both are consistent with the timing and the code. Neither was traced at
  neighbour-table or per-NLRI level.
- **Some delayed UPDATEs fall outside the 20 s cooldown.** At MRAI 5, Arm A
  sent its last UPDATE up to 41 s after the moves ended. Per-move UPDATE
  counts use moves + cooldown. The through-quiescence column shows this
  changes the reduction by less than 1 point.
- **Settings not tested.** Spine-only or leaf-only MRAI, `coalesce-time` (it
  applies only to initial table announcement), and MRAI combined with
  `sender-as-path-loop-detection`.
- **Recorder change.** The capture recorder changed for 2 of 18 trials, as
  described above. Acceptance and reconciliation are identical across all
  trials.
