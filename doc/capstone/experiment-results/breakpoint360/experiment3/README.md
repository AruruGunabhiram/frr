# Experiment 3: BGP advertisement interval (MRAI) 0 / 1 / 5 s

Status: **matrix complete.** Option C was run: 18 trials, 2 arms × MRAI
0/1/5 s × seeds 1–3, and all 18 passed acceptance.

- **Results and conclusions:** [EXPERIMENT3_FINDINGS.md](EXPERIMENT3_FINDINGS.md).
- **Design, FRR code-path verification and predictions:**
  [EXPERIMENT3_PLAN.md](EXPERIMENT3_PLAN.md).
- **This file:** setup, how to run it, the Part A smoke and the files.

## Setup

- **Fabric:** 5 leaves × 2 spines, rebuilt fresh for each trial.
- **Workload:** 64 robots on Experiment 2's seeded staggered schedule (10 s
  dwell, `MOB_SYNC=0`).
  - break-before-make roams, `MOB_ROBOT=repeat`, DAD off
  - default sender-AS-path behavior
- **Timer under test:** `neighbor fabric advertisement-interval N` on all
  seven routers.
- **Arms:**
  - **A, operational:** `MOB_ROBOT=repeat`, 4 gateway pings over 0.75 s
    after attach.
  - **B, isolated:** `keepalive`, 4 Hz gateway pings for 24 s after every
    attach.

  The Part A proposal said about 12 s for Arm B. 24 s covers the roughly four
  MRAI-paced hops of the withdrawal path at 5 s.
- **Platform:** Colima `frr-dev` on Apple M4: 6 vCPU, about 8 GiB, Linux
  6.8.0-117, FRR 10.8.0-dev.

## Run

From the repository root on the host. The privileged Colima `frr-dev`
container must have the checkout mounted at `/home/frr/frr`. The handoff
archive must be at `team-handoff/guna/mobility_tools.tar.gz`.

```bash
exp3=doc/capstone/experiment-results/breakpoint360/experiment3
bash "$exp3/reproduce.sh" smoke              # Part A: MRAI 0, 1, 5 s; seed 1; 2 rounds (128 moves) each
bash "$exp3/reproduce.sh" matrix-all         # Part B: 2 arms × 3 MRAI × seeds 1–3, 6 rounds each (detached)
tail -f "$exp3/trials/matrix.log"; cat "$exp3/trials/matrix.exit"
bash "$exp3/reproduce.sh" matrix-resume      # keep ACCEPTED trials, rerun the rest (up to 3 attempts each)
bash "$exp3/reproduce.sh" trial keepalive 5 2   # one matrix trial: ARM ADV SEED
bash "$exp3/reproduce.sh" diag repeat 5 2    # one diagnostic trial into diag/ (outside the matrix)
bash "$exp3/reproduce.sh" reanalyze trials   # re-run analysis on finished trials, no new trials
PYTHON=/usr/bin/python3 bash "$exp3/reproduce.sh" aggregate   # results/paired/summary CSVs + graphs/01-05
```

- **No overwrites.** The runner refuses to overwrite a trial directory. A
  matrix trial that fails acceptance is moved to `rejected/` and kept.
- **Shared working tree.** Do not switch branches in this working tree while
  a matrix runs. A checkout at 07:34 UTC removed `experiment2/` for about
  10 s and failed 30 attempts. The runner now copies its plugins into each
  trial's temp directory.
- **Per-trial steps:**
  1. Start the topology: `test_fabric_baseline` + `test_mobility_experiment`
     from the archived `test_mobility_scale.py`, with plugins `exp2_schedule`
     then `exp3_schedule`.
  2. Analyze in the container: `analyze_exp3.py point`. An `ACCEPTED` marker
     is written only when every acceptance check passes.
  3. Aggregate on the host: `aggregate_exp3.py`. It re-checks acceptance on
     all 18 trials. Smoke collection uses `analyze_exp3.py collect`.

## Part A smoke results (seed 1, 128 moves per MRAI, `repeat` robot)

These numbers come from the Part A review. The matrix supersedes them and
revises finding 2 below: see [EXPERIMENT3_FINDINGS.md](EXPERIMENT3_FINDINGS.md).

| | MRAI 0 s | MRAI 1 s | MRAI 5 s |
|---|---:|---:|---:|
| Configured and operational MRAI, 7 routers / 14 peers | 0 ✓ | 1 ✓ | 5 ✓ |
| Capture gaps · quiet-window UPDATEs | 0 · 0 | 0 · 0 | 0 · 0 |
| Capture vs FRR counters (after BGP quiet) | match | match | match |
| Session drops · daemon-log issue lines | 0 · 0 | 0 · 0 | 0 · 0 |
| Min gap between UPDATE bursts on a session | 0.05 s | 0.05 s | **4.52 s** |
| BGP UPDATEs per move | 59.7 | 22.9 | **6.0** |
| Robot Type-2 NLRIs per move | 80.0 | 75.9 | **50.2** |
| — MAC adv / wd | 20 / 20 | 20 / 20 | 20 / 16.3 |
| — MAC/IP adv / wd | 20 / 20 | 16.6 / 19.3 | **6.1 / 7.8** |
| Type-2 NLRIs per UPDATE | 1.36 | 3.37 | 8.53 |
| MAC-mobility sequence skips on any session | 0 | 0 | 0 |
| T_stale p50 / p95 / max (stable convergence) | 0.63 / 0.69 / 0.75 s | 2.90 / 5.05 / 5.40 s | 7.97 / 17.5 / 20.1 s |
| Moves: correct / transient stale / censored / persistently wrong | 128 / 0 / 0 / 0 | 2 / 62 / 47 / 17 | 15 / 24 / 64 / 25 |
| Stale robot-seconds | 72 | 1,071 | 1,501 |
| Robots wrong at end of moves → end of 20 s cooldown | 2 → 0 | 30 → **17** | 43 → **25** |
| Final leaf5 FIB correct | 64/64 | 48/64 | 39/64 |
| Controller probe loss | 0.18% | 15.8% | 31.0% |

Notes on the table:

- **MRAI 0 matches Experiment 2's staggered 10 s baseline.** Per move:
  - UPDATEs: 59.72 vs 59.57–59.73
  - robot NLRIs: 80.0 vs 80.0
  - NLRIs per UPDATE: 1.36 vs 1.35
  - T_stale p50: 0.632 vs 0.632–0.633 s; max 0.75 vs 0.70 s

  Source: [smoke_baseline_check.csv](smoke_baseline_check.csv).
- **Correct-at-MRAI-5 caveat.** All 15 "correct" moves at MRAI 5 were already
  correct at attach. leaf5 still pointed at that leaf from an older stale
  state.
- **Reverts.** 14 moves at MRAI 5 reverted after first looking correct.
  T_stale and the classes use the final correct interval only.
- **The burst gap verifies the timer on the wire.** At MRAI 5, every session's
  minimum gap between UPDATE bursts is 4.5–4.6 s, which fits FRR's whole-second
  timer arithmetic. At MRAI 1 the minimum stays near 0.05 s, as the code
  predicts: a batch can follow the previous one immediately when a second
  boundary falls between them.

## Part A findings, as reviewed (see the matrix revisions below)

1. **MRAI does pack updates.** Messages per move fall 62% at 1 s and 90% at
   5 s. Routes per UPDATE rise from 1.4 to 8.5.
2. **MRAI did not coalesce Type-2 changes, as predicted.**
   - MAC advertisements are unchanged at 20 per move in every arm.
   - No session skipped a MAC-mobility sequence number.

   Each robot moves about every 10 s, and a prefix keyed by the origin's RD
   does not change twice within one sender's wait.
3. **The NLRI "reduction" is missing work, not saved work.**
   - **Where the drop is.** Almost all of it is in MAC/IP routes: 20 → 16.6 →
     6.1 advertisements per move. The new leaf failed to originate the
     robot's MAC/IP route.
   - **Mechanism, from the captures:**
     1. The new leaf still holds the robot's IP as a remote neighbor until the
        old leaf's MAC/IP withdrawal arrives.
     2. At MRAI 0 that withdrawal reaches the new leaf about 0.21 s after
        attach.
     3. The robot's `repeat` pings (0.75 s) are still running, so the leaf
        relearns the IP 0.1–0.37 s later. That gives the 0.63 s T_stale.
     4. At MRAI 1 the withdrawal arrives at a p50 of 2.2 s, after the pings
        have stopped.
     5. In the first 1 s run, 61 of 128 moves had no new MAC/IP route before
        the robot's next move or the end of the run. leaf5 then removes the
        old /32 and has **no /32 at all**.

     This is the "IP not relearned while held as a remote entry" issue in
     Pradeep's notes, which the handoff asked to keep out of the results.
     MRAI widens its window past what `repeat` covers. All persistently-wrong
     robots end with no /32. Controller traffic then falls back to the /24
     ECMP route, which shows up as the 16–31% probe loss.
4. **Staleness from the timer alone is bounded, but censoring dominates.**
   - **Converged moves.** T_stale tracks about 0.6 s + up to 2 × MRAI
     (1 s: max 5.4 s; 5 s: max 20.1 s, including relearn delays).
   - **Censored moves.** At 10 s dwell, 37% (1 s) and 50% (5 s) of moves were
     overtaken by the robot's next move before leaf5 converged.
5. **It is not a dead fabric.**
   - BGP was quiet almost as soon as the cooldown ended. The last UPDATE came
     2.5 s after it at 1 s and 0.1 s after it at 5 s. Quiet was confirmed
     after waits of 7.2 s and 12.8 s, which include the 4 s and 12 s
     stability holds.
   - No BGP session dropped.
   - The late updates, NLRIs and FIB state all reconcile.

   One robot at 1 s recovered 24.5 s after attach, from a later ARP. The rest
   would need the robot to send another ARP.

**What the matrix revised:**

- **Finding 2, sequence skips.** The smoke's "0 sequence skips" came from
  having only 2 moves per robot, so a skipped location had no earlier
  sequence on the same session to compare against. The current analyzer
  still reports 0 on a copy of the smoke data. With 6 moves per robot, the
  matrix shows over 1,000 MAC/IP skips per trial whenever MRAI > 0 and the
  IP relearn misses. These are missing originations, not coalescing.
- **New at MRAI 5, both arms:** a small MAC withdrawal reduction (about 3.5
  NLRIs per move) and a few MAC sequence ties.
- **Finding 3 is confirmed and quantified.** It holds per move from the wire:
  whether the robot's new leaf originated MAC/IP during the visit, only after
  the robot left, or not at all.

## Decision taken: Option C (both arms)

The question was whether the matrix should keep the handoff's `repeat` robot
as is, or isolate the MRAI propagation delay from the IP-relearn issue.

- **A. As planned (`repeat`).** Measures MRAI together with this robot model.
  Answers "does batching trade load for staleness?" for robots that only
  announce at attach. On the smoke, it makes things much worse.
- **B. Isolated.** Use a robot announce window long enough to cover two MRAI
  hops, for example gateway pings for about 12 s after each attach, in every
  arm. This measures the pure timer cost. It needs a new announce mode in
  `exp3_schedule.py`. The archived tools stay unchanged.
- **C. Both.** 18 trials, about 1 h on Colima.

Option C was chosen and run. Arm A answers the mentor's question for the
current robot model. Arm B separates how much of the staleness is the timer
itself. Arm B uses a 24 s keepalive rather than 12 s, for the reason given in
[Setup](#setup).

## Instrumentation fixes made during the smoke

The first smoke attempt ([smoke-v1/](smoke-v1/), kept locally, not tracked)
exposed two analysis issues. All three arms were then rerun with identical
instrumentation:

- **Counter reconciliation.** At 1 s, routes were still propagating when the
  archived test read FRR's counters at the end of the cooldown. The windowed
  capture and the counters differed by in-flight UPDATEs (±2 per device; the
  sign flipped as the window moved). There were 0 capture gaps.
  `exp3_schedule.py` now waits for BGP to go quiet before stopping the
  capture. The analyzer reconciles the whole capture against those counters.
  This matches exactly in every arm. The end-of-cooldown mismatch is still
  reported (`end_window_counter_mismatches`).
- **Stable convergence.** At 5 s, leaf5 sometimes already pointed at the
  robot's new leaf from stale state, then reverted when delayed updates
  arrived. T_stale now uses the final correct interval that persists until
  the next move. `first_correct_s` and `reverted_after_correct` record the
  earlier state.

## Capture fix made during the matrix

At MRAI 5, 13 attempts failed only the capture-gap check.

- **Cause, from a diagnostic trial.** The diagnostic trial (`diag/`) read the
  kernel's `PACKET_STATISTICS`. The archived recorder's unfiltered
  `ETH_P_ALL` socket also queues all data-plane traffic, 39k–257k frames per
  link. The kernel dropped 2.4k–4.4k frames per busy link at the socket, and
  sometimes a BGP frame was among them.
- **Fix.** `exp3_schedule.py` now attaches a kernel `tcp port 179` BPF filter
  (identical kept frames) and records each socket's packet and drop counters.
- **Which trials used it.** Only the last two trials (`repeat/adv-5s/seed-2`
  and `seed-3`). Each passed first time with 0 socket drops.
- **The other 16 trials are complete as they stand.** They have 0 gaps and
  exact reconciliation with FRR's counters. The `capture_kernel_filter`
  column in `results_exp3.csv` marks the recorder each trial used.

## Files

| Path | Content |
|---|---|
| `EXPERIMENT3_PLAN.md` | Research question, FRR code-path verification, design, predictions, acceptance rules |
| `EXPERIMENT3_FINDINGS.md` | Matrix results for both arms, mechanism, reproduction, run history, limitations |
| `exp3_schedule.py` | pytest plugin: capture buffer and kernel BPF filter, Arm B keepalive, BGP quiet gate, MRAI evidence, quiescent stop, session and log audit, 20 s cooldown |
| `analyze_exp3.py` | Per-trial analysis (`point`) and host-side smoke tabulation (`collect`) |
| `aggregate_exp3.py` | Matrix aggregation: acceptance re-check, CSVs, `graphs/01-05` (needs matplotlib) |
| `reproduce.sh` | `smoke`, `trial ARM ADV SEED`, `diag ARM ADV SEED`, `matrix-all`, `matrix-resume`, `reanalyze`, `aggregate`, `collect` |
| `results_exp3.csv` | One row per matrix trial (18) |
| `summary_exp3.csv` | Mean / min / max per arm × MRAI |
| `paired_exp3.csv` | Same-seed comparisons: MRAI vs 0 within each arm, and Arm A vs Arm B at each MRAI |
| `graphs/01–05_*.png` | UPDATEs, NLRIs, staleness, leaf5 timeline, trade-off and CPU |
| `smoke_exp3.csv` | One row per smoke trial (all summary fields) |
| `smoke_baseline_check.csv` | Per-move rates against Experiment 2 staggered 10 s |
| `smoke_evidence/adv-Ns-seed-1/` | See the list below |
| `smoke/`, `smoke-v1/`, `trials/`, `rejected/`, `diag/`, `smoke_evidence/` | Raw trials and evidence: pcaps, monitors, probes, logs. Local only, via `.git/info/exclude` |

Each `smoke_evidence/adv-Ns-seed-1/` folder holds:

- `frr_config/`: running-config, EVPN update groups and per-peer MRAI and
  session JSON, at capture start and at the end;
- `frr_verification.json`;
- the `route_counter.py` outputs `counts.csv` and `timeline.csv`;
- `type2_counts.csv` and `session_counts.csv`;
- `leaf5_move_states.csv`, one row per move;
- `leaf5_state_timeline.csv`, at 100 ms steps;
- the final FIB check, the quiet-gate and quiescence records, `truth.csv`
  and `exp3_summary.json`.
