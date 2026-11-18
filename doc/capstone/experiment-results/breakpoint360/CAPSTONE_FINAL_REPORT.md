# Cisco Capstone final report: EVPN mobility for warehouse robots

*Five experiments plus a baseline root-cause study on an FRR EVPN leaf–spine
fabric (FRR 10.8.0-dev, eBGP, 2 spines × 5 leaves unless stated). This
report is the synthesis. Each experiment's own findings document holds the
full tables, method and audit (see the [evidence index](#11-evidence-index)).*

## 1. Executive summary

**The question.** Can an EVPN fabric keep track of thousands of mobile robots
that roam between access points on different leaves? And what limits it:
BGP load, timers, or protocol correctness?

**The answer in one line.** The fabric's control-plane load is predictable
and moderate. What breaks robot reachability is not load. It is any
condition in which **a robot moves, or needs to be relearned, faster than the
fabric converges**.

The main findings:

1. **Route work per roam is exact and linear.**
   - Every roam costs **8 × leaves × spines** EVPN route transmissions
     fabric-wide: 80 on 5×2, 544 on 17×4.
   - Each spine receives 4 × leaves routes per roam and each leaf
     4 × spines.
   - Half of all transmissions are echoes and re-advertisements that
     receivers discard (Exp 1).
2. **Packing updates is free when it comes from coincidence.** Robots moving
   at the same time share BGP messages: 75–79% fewer UPDATEs, 70–87% less
   bgpd CPU, the same route volume, and every robot correct (Exp 2).
3. **Packing updates by delaying them is not free.** Raising the BGP
   advertisement interval (MRAI) to 1 s or 5 s cuts UPDATE messages 57–88%,
   but (Exp 3):
   - **Isolated arm** (robots keep sending traffic): convergence rises from
     about 0.5 s to about 5.4 s at 1 s.
   - **Operational arm** (the existing robot model): 47% of moves never
     converge at 1 s, and about 78% never converge at 5 s in both arms.
   - **The route-count reduction is mostly routes that were never
     originated.** It is not saved work.
4. **Overload causes permanent errors, through a sequence-number tie, not
   through lost messages.**
   - Above about 4,000 moves/s on a laptop, half the robots sit on the wrong
     leaf. After the load stops, 7,195–9,327 of 16,000 robots stay wrong.
   - The cause: a robot moves again before its last move has spread, two
     leaves claim the same MAC-mobility sequence, and the lower IP wins
     (Exp 4).
   - A one-shot burst of 16,000 arrivals recovers in 4.7 s with nothing
     stuck (Exp 5).
5. **Underneath everything is a deterministic IP-relearn gap.**
   - After a roam, the new leaf holds the robot's IP as a remote NOARP
     entry. The Linux kernel rejects the robot's ARPs until the old leaf's
     withdrawal arrives (124 of 124 such ARPs rejected).
   - A robot that does not send traffic after that point stays unreachable
     from the controller (baseline roadmap360 study).
   - Exp 3 shows MRAI widens this gap. Exp 4 notes it blocks healing after a
     tie.

**What we recommend to Cisco** (details in [section 8](#8-recommendations-for-cisco)):

- **Keep MRAI at 0** on EVPN fabrics that carry mobile endpoints.
- **Require endpoints to keep sending traffic for a few seconds after every
  roam.**
- **Pursue two protocol and implementation fixes:**
  - relearn the IP when the remote entry is withdrawn;
  - break sequence ties toward the endpoint's actual location.
- **Size spines from the measured 4 × leaves formula.**
- **Test `sender-as-path-loop-detection`,** which targets the 50% of
  redundant route transmissions.

## 2. The program at a glance

| Study | Question | Load and scale | Platform | Trials | Status |
|---|---|---|---|---:|---|
| Baseline: roadmap360 (NOARP) | Why does the controller lose a robot's /32 after a roam? | 1 robot plus fleet variants (64–1,000 robots, up to 25 moves/s) | Emulated laptop VM, 4 vCPU | 120 | Complete |
| Exp 1: amplification | How much BGP work does one roam create, and how does it scale with fabric size? | 1 robot, settled roams; 6 topologies from 5×2 to 17×4 | Colima on Apple M4, 6 vCPU | 18 | Complete, committed |
| Exp 2: synchronization | Does moving robots together or staggered change the load? | 64 robots, dwell 10 s and 30 s, 6.4 moves/s | Colima on Apple M4 | 12 | Complete, committed |
| Exp 3: MRAI timer | Does the BGP advertisement interval reduce load, and what does it cost? | 64 robots, dwell 10 s, MRAI 0/1/5 s, 2 robot behaviours | Colima on Apple M4 | 18 | Complete, committed |
| Exp 4: overload ramp (Pradeep) | At what move rate does the fabric break, and how? | 16,000 injected robots, 500 → 8,000 moves/s | Laptop, Intel i5-10310U, bare metal | 2 ramps | Complete (handoff notes) |
| Exp 5: arrival burst (Pradeep) | Can the fabric absorb a mass arrival at one leaf? | 2,000 / 8,000 / 16,000 robots to one leaf within 1 s | Same laptop | 3 floods (plus the Exp 4 race test, 70 trials) | Complete (handoff notes) |

**Common ground across Exp 1–3:**

- the unchanged capstone topology (`test_evpn_capstone.py`);
- break-before-make roams;
- DAD off (Exp 1–3; the roadmap Workstream D runs left it at the default);
- the robot's /32 in leaf5's (the controller's leaf's) kernel FIB as the
  correctness signal;
- packet captures on every leaf–spine link, reconciled message-for-message
  with FRR's own counters.

## 3. Baseline mechanism: the NOARP IP-relearn gap (roadmap360)

*Source: [REPORT_360_FINDINGS.md](../roadmap360/REPORT_360_FINDINGS.md),
120 trials.*

**What happens after a robot roams from leaf A to leaf B:**

1. **Leaf B learns the robot's MAC at once,** from its first frame.
2. **Leaf B still holds the robot's IP from leaf A's MAC/IP route.** It is a
   remote, extern-learned neighbour in the `NOARP` state.
3. **The kernel refuses to overwrite that entry with the robot's ARP.**
   - All 124 robot ARPs that arrived while the NOARP entry existed were
     rejected by `__neigh_update()` (`-EPERM`).
   - None was ever accepted.
4. **Leaf B can learn the IP locally only after leaf A's MAC/IP withdrawal
   arrives.**
   - At idle with MRAI 0 that takes about 75 ms: two 50 ms bgpd work-queue
     holds plus a spine relay.
   - The IP must then be relearned from an ARP that arrives *after* the
     withdrawal (45 of 50 ordering runs; 0 contradicted this).
5. **Until then leaf5 has no /32 for the robot.** Controller traffic follows
   the /24 ECMP route to an arbitrary leaf. That leaf ARPs for the robot,
   and the robot's reply goes to the shared anycast gateway MAC, so it lands
   on its own leaf's SVI and is ignored. **The fallback path is a black
   hole.**

**Consequences:**

- **Robots that keep transmitting recover.** Burst, delayed and ping
  patterns recovered 30/30.
- **Robots that go quiet after their first ARP can stay unreachable for tens
  of seconds.**
  - Fleet roams without re-ARP: 124 of 320 robot-moves were unrecovered at
    window end.
  - Make-before-break with a 100 ms overlap: 10/10 runs unrecovered.
- **No FRR defect is claimed.** The gap is the interaction of the kernel
  NOARP guard, FRR's deliberate retention of remote neighbours, the anycast
  gateway and bgpd batching.

This mechanism reappears in Exp 3 and Exp 4.

## 4. Experiment results

### 4.1 Exp 1: route amplification per roam

*Source: [experiment1/EXPERIMENT1_FINDINGS.md](experiment1/EXPERIMENT1_FINDINGS.md).*

**Setup.** 18 fresh trials and 108 single-robot roams, each fully settled
before the next. 0 capture gaps; counts reconciled with FRR's counters.

| Leaves × spines | Leaf RX per roam | Spine RX per roam | Fabric NLRI transmissions per roam | UPDATEs per roam |
|---|---:|---:|---:|---:|
| 5 × 2 | 8 | 20 | 80 | 60 |
| 9 × 2 | 8 | 36 | 144 | 108 |
| 17 × 2 | 8 | 68 | 272 | 204 |
| 5 × 4 | 16 | 20 | 160 | 120 |
| 9 × 4 | 16 | 36 | 288 | 216 |
| 17 × 4 | 16 | 68 | 544 | 408 |

**Findings:**

- **Each roam is four route changes,** each sent on every attached session:
  MAC and MAC/IP advertised by the new leaf, and the same two withdrawn by
  the old leaf. Every seed gave identical counts (sample standard deviation
  0).
- **Spine load grows with leaves, and leaf load grows with spines.**
- **Of all robot route transmissions, 50% are spine echoes back to the
  origin and leaf re-advertisements toward the other spine.** The receiver
  discards these.
- **Projection.** At 32 × 4 the formula gives 1,024 transmissions per roam.
  This is extrapolated route work, not a capacity limit.

### 4.2 Exp 2: synchronized vs staggered mobility

*Source: [experiment2/EXPERIMENT2_FINDINGS.md](experiment2/EXPERIMENT2_FINDINGS.md).*

**Setup.** 64 robots, 384 roams per trial, 12 trials, 4,608 roams. Each
synchronized trial is paired with a staggered trial on the same seed and
schedule.

| Dwell | UPDATEs (sync / staggered) | Type-2 NLRIs | NLRIs per UPDATE | UPDATE reduction | bgpd CPU reduction |
|---|---|---|---|---:|---:|
| 10 s | 4,939 / 22,901 | 30,880 both | 6.26 / 1.35 | 77–79% | 70–78% |
| 30 s | 5,251 / 23,091 | 30,880 / 30,827 | 5.91 / 1.34 | 75–79% | 74–87% |

leaf5 was correct after all 4,608 roams across the 12 trials, and the final
FIB was correct for 768/768 robot endpoints.

**Findings:**

- **Same routes, fewer messages.** Robot routes stay at 80 per roam in both
  schedules. Simultaneous moves share messages, because their route changes
  meet inside bgpd's existing 50 ms processing hold.
- **No cost in correctness or staleness.**

### 4.3 Exp 3: BGP advertisement interval (MRAI)

*Source: [experiment3/EXPERIMENT3_FINDINGS.md](experiment3/EXPERIMENT3_FINDINGS.md).*

**Setup.**

- 64 robots on Exp 2's staggered 10 s schedule, 18 trials (2 arms × 3 MRAI
  values × 3 seeds), all accepted.
- Arm A uses the existing robot model: 4 gateway pings in 0.75 s after a
  roam.
- Arm B isolates the timer: the robot keeps pinging its gateway at 4 Hz for
  24 s after each roam.

| | Arm A, MRAI 0 / 1 / 5 s | Arm B, MRAI 0 / 1 / 5 s |
|---|---|---|
| UPDATEs per roam | 59.4 / 25.5 / 7.2 | 59.6 / 23.2 / 7.7 |
| UPDATE reduction vs 0 | – / 57% / 88% | – / 61% / 87% |
| Robot Type-2 NLRIs per roam | 80 / 78.4 / 59.9 | 80 / 80 / 60.9 |
| bgpd CPU seconds (7 switches) | 4.62 / 2.03 / 1.09 | 3.59 / 2.23 / 1.27 |
| leaf5 T_stale, median of converged roams | 0.63 / 3.3 / 7.2 s | 0.49 / 5.4 / 8.7 s |
| Roams that never converged | 0% / 47% / 79% | 0% / 0% / 77% |
| Robots correct at leaf5 at the end | 64 / 36–45 / 31–42 (of 64) | 64 / 64 / 64 |
| MAC-mobility sequence ties per trial | 0 / 0 / 2–7 | 0 / 0 / 0–12 |

**Findings:**

- **MRAI also delays withdrawals.** The withdrawal the new leaf waits for
  (section 3) now crosses about four timer-paced hops. The relearned route
  then needs two more to reach leaf5.
- **Arm B shows the pure timer cost:** every roam converges, but in about
  5.4 s instead of 0.5 s.
- **Arm A shows the operational cost.** The robot's pings end before the
  withdrawal arrives, so the IP is never relearned while the robot is there
  (181 of 384 roams at 1 s). The robots left without a correct /32 at the end
  match the robots whose last roam never got a MAC/IP route.
- **There is almost no genuine coalescing.** EVPN keys routes by origin, and
  each robot changes only every 10 s. The NLRI reduction at 5 s is mostly
  MAC/IP routes that were never originated.

### 4.4 Exp 4: overload ramp (Pradeep)

*Source: [reference/nov_17_pradeep_RESULTS.md](reference/nov_17_pradeep_RESULTS.md)
(verbatim copy of the team handoff notes).*

**Setup.** Injected robots: the tool writes each MAC and IP binding straight
into the new leaf's kernel tables. This bypasses the NOARP gap, and the
robots never send traffic afterwards. 16,000 robots, ramp 500 → 8,000
moves/s.

| Moves/s | Robots wrong at leaf5 (mean) | leaf5 convergence | Routes per roam (spine / leaf) |
|---:|---:|---:|---|
| 500 | about 90 | 0.2 s | 20 / 8 |
| 2,000 | about 560 | 0.3 s | 20 / 8 |
| 4,000 | about 8,200 | 2.0 s | 12.3 / 5.0 |
| about 7,150 | 10,300–12,000 | 1.4–1.7 s | 6.2 / 2.6 |

**Findings:**

- **The Exp 1 formula holds up to 2,000 moves/s.** Above that, per-roam
  route counts fall because outdated changes are superseded before they are
  sent.
- **The errors persist after the load stops.** 7,195 and 9,327 of 16,000
  robots stay wrong after all queues drain.
- **Sequence ties explain most stuck robots.** For robots stuck on their
  previous leaf, that leaf had the lower IP in 84% of cases (chance would be
  50%).
- **Proven at zero load.** A second move within 100 ms, with the true leaf
  on the higher IP, left 20/20 trials stuck.
- **Nothing was lost on the wire.** No session reset and no daemon failure
  occurred.

### 4.5 Exp 5: one-shot arrival burst (Pradeep)

| Robots arriving at one leaf in 1 s | Peak wrong at leaf5 | All correct again after | Wrong 120 s later |
|---:|---:|---:|---:|
| 2,000 | (sampling missed the peak) | 2.0 s | 0 |
| 8,000 | about 4,000 | 3.4 s | 0 |
| 16,000 | about 12,400 | 4.7 s | 0 |

**Finding.** Volume alone does not break the fabric. Each robot moves once,
so no tie can form.

## 5. Cross-experiment synthesis

### 5.1 One rule explains every failure we saw

> **A robot is at risk whenever it moves again, or must be relearned, before
> the fabric has converged on its previous state.**

| What lengthened convergence relative to the robot's behaviour | Where | Result |
|---|---|---|
| Kernel NOARP guard plus a robot that goes quiet after its first ARP | Baseline; roadmap fleet roams | Controller /32 missing for tens of seconds (124/320 fleet roams unrecovered at window end) |
| Make-before-break overlap delays the old leaf's withdrawal | Baseline, Workstream D | MBB 100 ms: 10/10 unrecovered; BBM: 0/10 |
| MRAI timer delays the withdrawal past the robot's announce window | Exp 3, Arm A | 47% (1 s) to 79% (5 s) of roams never converge |
| MRAI timer makes convergence longer than the 10 s dwell | Exp 3, both arms, 5 s | About 78% of roams censored; sequence ties appear at only 6.4 moves/s |
| CPU backlog makes convergence longer than the time between a robot's moves | Exp 4, 4,000 moves/s and above | Sequence ties; 45–58% of robots permanently wrong |
| Large backlog, but each robot moves only once | Exp 5 | Fully recovered in 2–5 s |
| Robots move together; nothing is delayed | Exp 2 | Fewer messages, every roam correct |

### 5.2 Ways to reduce BGP load, and what each one costs

| Mechanism | UPDATE messages | Route volume (NLRIs) | CPU | Correctness cost | Verdict |
|---|---|---|---|---|---|
| Coincident moves (synchronized schedule), Exp 2 | −75 to −79% | 0% | bgpd −70 to −87% | None | Benign; comes from existing 50 ms batching |
| MRAI 1 s, Exp 3 | −57 to −61% | 0 to −2% | bgpd −26 to −76% | Convergence about 10× slower; 47% of roams lost for robots that only announce at attach | Not recommended |
| MRAI 5 s, Exp 3 | −86 to −88% | About −25% (mostly routes never originated) | bgpd −60 to −80% | About 78% of roams never converge; sequence ties | Not recommended |
| Overload supersession, Exp 4 | Falls | −38% (spine) at 4,000/s | Saturated | Permanent wrong-leaf state | A failure symptom, not a lever |
| `sender-as-path-loop-detection` (proposed) | Expected to fall | Targets the 50% echo/re-advertisement share (Exp 1) | Expected to fall | Not measured | **Next experiment** |

**A falling route count is a warning sign, not a win.** In Exp 1 and Exp 2,
every healthy roam costs exactly 80 robot route transmissions on 5×2. The
only times the count fell (Exp 3 at MRAI 5, Exp 4 above 2,000 moves/s), the
fabric was losing or superseding robot locations. **A 5×2 fabric whose
per-roam route count falls below 80 should be treated as degrading.**

### 5.3 Quantities that held across experiments

| Quantity | Value | Seen in |
|---|---|---|
| Robot Type-2 transmissions per healthy roam, 5×2 | 80 | Exp 1, Exp 2, Exp 3 at MRAI 0, Exp 4 at ≤2,000/s (20 per spine, 8 per leaf) |
| BGP UPDATEs per staggered roam, 5×2 | 59.4–60 | Exp 1, Exp 2 (staggered), Exp 3 at MRAI 0 |
| leaf5 /32 convergence per roam, unloaded, MRAI 0 | About 0.5–0.63 s (Colima); about 0.2 s (Exp 4 laptop) | Exp 2, Exp 3, Exp 4 |
| Withdrawal gap holding the NOARP entry, MRAI 0 | About 75 ms idle; about 0.2 s under fleet load | Baseline, Exp 3 smoke |
| bgpd batching hold | 50 ms per hop (`bgpd/bgp_route.c`) | Baseline decomposition, Exp 3 code review |

### 5.4 How the NOARP gap and the sequence tie combine

Exp 4 left open whether a robot that loses a tie heals on its own. Its
injected robots never sent traffic, and the true leaf would hold the IP as a
remote entry. Exp 3 at MRAI 5 produced real ties with real robots:

- **Arm B: every tie healed** (final FIB 64/64). Continued robot traffic let
  the true leaf re-announce with a higher sequence once the remote entry was
  gone. That is the healing path Pradeep predicted.
- **Arm A: robots stayed wrong.** The robots left wrong match roams whose
  MAC/IP route was never originated, which is the NOARP gap, not ties.
  Without traffic after the gap closes, nothing triggers a relearn.

**Conclusion.** The two failure modes are linked through endpoint behaviour.
**Post-roam endpoint traffic is the single most effective lever available
without changing FRR or the kernel.**

## 6. What transfers to production, and what does not

| Result | Transfers? | Why |
|---|---|---|
| 8 × leaves × spines route transmissions per roam, and the per-role formulas | **Yes**, for the same propagation policy | Protocol and topology logic; exact and seed-independent |
| 50% of transmissions are echoes and re-advertisements | Yes, for eBGP leaf–spine without sender-side loop detection | Policy-driven |
| Coincident moves pack into fewer messages | Yes, qualitatively | Any BGP implementation that batches; ratios depend on timing |
| MRAI delays withdrawals and multiplies convergence by the hop count | **Yes** | Protocol behaviour (per-peer timer gates all UPDATEs) |
| NOARP rejection of ARP against extern-learned neighbours | Yes, on Linux-based EVPN (FRR + kernel) | Kernel guard; other NOS implementations may differ |
| Sequence tie and the lower-IP tie-break | **Yes** | RFC 7432 tie-break logic |
| Absolute times (0.5 s convergence, the 4,000 moves/s break point, CPU seconds) | **No** | Emulated switches on laptops; production ASIC/CPU platforms will differ |
| The shape (fine, then a sudden failure once convergence exceeds the robot's dwell) | Yes | Follows from section 5.1 |

## 7. Limitations

- **Emulation, not hardware.** Every switch is an FRR instance in a network
  namespace: on a Colima VM (Exp 1–3, roadmap) or one laptop (Exp 4–5). CPU,
  timing and break-point numbers describe these hosts.
- **One topology for the dynamic studies.** Exp 2–5 and the baseline used
  5×2 only. Exp 1 scaled the topology but used settled single-robot roams.
- **Small samples per condition.** 3 seeds per cell (Exp 1–3), 2 ramp runs
  (Exp 4), 5 trials per race point. Percentiles are descriptive.
- **Robot models are simplified.** Robots are namespaces with pre-wired
  radios. Exp 4–5 robots are injected, bypass the NOARP gap and never send
  traffic. The Exp 3 Arm B keepalive is a modelling tool, not a proposed
  robot design.
- **The two halves were measured on different machines and harnesses.**
  Exp 1–3 and Exp 4–5 differ in harness, robot model and host. Cross-links
  in this report use protocol-level quantities (route counts, mechanisms),
  not absolute times.
- **Some explanations are inferred, not traced:**
  - Exp 3: the trigger of late stale MAC/IP originations, and the cause of
    the MAC withdrawal reduction;
  - Exp 4: the robots stuck on an older leaf or with no /32.
- **Not tested:**
  - `sender-as-path-loop-detection` under mobility;
  - spine-only or leaf-only MRAI;
  - `coalesce-time` (it affects only full-table announcement);
  - DAD under high mobility (off in Exp 1–3);
  - topologies larger than 17×4.
- **Measurement tooling had to be hardened during the program.** Capture
  buffers were raised in Exp 2, and Exp 3 added a kernel BPF filter after
  diagnosing socket drops. Every accepted trial reconciles exactly with
  FRR's counters, and rejected attempts are kept locally.

## 8. Recommendations for Cisco

Ordered by expected impact and readiness.

1. **Keep BGP MRAI at 0 on EVPN fabrics with mobile endpoints.**
   - FRR's datacenter profile already does this.
   - **Why:** MRAI's message savings come almost entirely from delaying
     withdrawals, which turns a 0.5 s convergence into 5 s or more and
     strands robots (Exp 3).
   - **To cut message count, rely on coincident batching instead.** It is
     already free (Exp 2).
2. **Specify endpoint behaviour after a roam.**
   - Robots should keep sending gateway traffic (ARP or ping) for a short
     window after each re-association, long enough to outlast the fabric's
     withdrawal path. That is under a second at MRAI 0 in this emulation;
     size it on production hardware.
   - **Evidence:**
     - burst and ping patterns recovered 30/30 (baseline);
     - Exp 2's 4-ping robots converged 4,608/4,608;
     - Exp 3's Arm B healed even sequence ties.
   - **This is the only fix that needs no changes to the network.**
3. **Prefer break-before-make handoff at the access layer.** The
   make-before-break overlap delayed the old leaf's withdrawal and left 10/10
   runs (100 ms overlap) unrecovered (baseline, Workstream D).
4. **Raise two implementation and protocol items with the FRR and NOS
   teams.** These are candidate directions, not validated fixes:
   - **IP relearn on withdrawal.** When a remote extern-learned neighbour is
     removed while the MAC is local, actively probe the IP, so the leaf does
     not wait for the endpoint to ARP again. This closes the NOARP gap
     independently of robot behaviour.
   - **Sequence-tie handling.** A leaf that has the MAC locally and loses a
     MAC-mobility tie could re-advertise with a higher sequence instead of
     accepting the lower-IP winner. This touches RFC 7432 behaviour and
     needs standards-aware review (Exp 4, section 7).
5. **Use the measured formulas for capacity planning.**
   - Per roam:
     - each spine receives 4 × leaves routes;
     - each leaf receives 4 × spines;
     - the fabric carries 8 × leaves × spines.
   - Multiply by the fleet's peak roam rate to size spine control-plane
     headroom.
   - Keep peak roam rate well below the point where convergence exceeds the
     minimum time between a robot's moves (Exp 4 shape).
6. **Evaluate `sender-as-path-loop-detection` next.** Half of all route
   transmissions are echoes and re-advertisements (Exp 1). Removing them
   should raise the overload threshold without delaying anything, unlike
   MRAI. It delays the Exp 4 failure point; it does not remove it.
7. **Monitor what matters in operation.**
   - **Alert** when the controller-leaf /32 disagrees with the access-layer
     location.
   - **Treat as degradation signals:**
     - a fall in route transmissions per roam below the formula;
     - the same MAC advertised with equal sequence numbers from two leaves.

## 9. Proposed next steps

| Priority | Work | Answers |
|---|---|---|
| 1 | `sender-as-path-loop-detection` on the Exp 2/3 workload and the Exp 4 ramp | Real load reduction without delay? How far does it push the break point? |
| 2 | Real robots (not injected) under the Exp 4 ramp, with and without post-roam traffic | Measures the combined NOARP and tie failure directly |
| 3 | Prototype the IP-relearn-on-withdrawal behaviour in zebra | Closes the NOARP gap for silent endpoints |
| 4 | Repeat Exp 1 and Exp 4 on switch hardware | Production timing, break point and CPU headroom |
| 5 | Larger fabrics (32×4) and DAD enabled under mobility | Validates the projections and DAD interaction |

## 10. Reproducibility and data integrity

- **Committed.** Exp 1–3 are committed on branch `breakpoint360` with
  runners (`reproduce.sh`), analysis code, per-trial CSVs, graphs and
  findings.
- **Local only.** Raw captures and trial folders stay local (excluded from
  git). Exp 1's raw archive is checksummed (`SHA256SUMS`).
- **Acceptance gates in Exp 1–3:**
  - 0 capture gaps;
  - exact reconciliation of every captured UPDATE with FRR's per-router
    counters;
  - no BGP session changes;
  - the configured timers verified in running and operational state;
  - the final FIB checked independently of the route monitor.
- **Rejected attempts** are retained locally with their reason.
- **Exp 4–5 and the baseline** are documented in the handoff notes and the
  roadmap360 report, with their own run folders.

## 11. Evidence index

| Document | Content |
|---|---|
| [roadmap360/REPORT_360_FINDINGS.md](../roadmap360/REPORT_360_FINDINGS.md) | Baseline NOARP root cause, 120 trials |
| [experiment1/EXPERIMENT1_FINDINGS.md](experiment1/EXPERIMENT1_FINDINGS.md) | Amplification per roam, 6 topologies |
| [experiment2/EXPERIMENT2_FINDINGS.md](experiment2/EXPERIMENT2_FINDINGS.md) | Synchronized vs staggered mobility |
| [experiment3/EXPERIMENT3_FINDINGS.md](experiment3/EXPERIMENT3_FINDINGS.md) | MRAI 0/1/5 s, two robot behaviours |
| [reference/nov_17_pradeep_RESULTS.md](reference/nov_17_pradeep_RESULTS.md) | Exp 4 overload ramp, Exp 5 burst, sequence-tie race test (verbatim copy of the team handoff, with its figures) |
| [experiment3/EXPERIMENT3_PLAN.md](experiment3/EXPERIMENT3_PLAN.md) | FRR code-path verification of MRAI, adj-out coalescing and timers |
