# TradeOS v7 — Master Implementation Specification

**Audience: Claude Code, working in the `tradeos-v6` repository.**

This is the single implementation document. There are no separate work-package
prompts. Together with `CLAUDE.md` and the repository itself, this is sufficient
to implement the complete frozen Phase 4 architecture.

**Where this document and `CLAUDE.md` disagree, `CLAUDE.md` wins.** It encodes
failures that have already cost money.

---

## 0. Architectural constraints — non-negotiable

**0.1 Architectural redesign is prohibited.** The architecture is frozen. You
may not restructure packages, invert dependencies, introduce frameworks, rename
live modules, or replace a working component with a better one. If you believe
the architecture is wrong, **stop and report it**; do not implement your
opinion.

**0.2 Branch discipline.** All work on `phase4-allocator`. Never commit to,
merge to, or open a PR against `main` without being told in words. Tag the
pre-merge commit on `main` as the restore point.

**0.3 Nothing is deleted.** No file removal, no `DROP`, no unqualified `DELETE`,
for any reason — including a search returning zero callers. A zero-reference
search means the search missed something: callers live in workflows, the
launcher, remote crontabs, scheduled database functions, and runtime-built
strings. Removal candidates go to `QUARANTINE.md` with the search performed and
what breaks if it was wrong.

**0.4 Propose, never auto-apply.** Learning output enters a proposal queue and
changes nothing. Retire the existing per-type auto-apply policy; it is the
second governance door and it closes.

**0.5 Verify, never assert.** Every acceptance criterion below is a command with
expected output. Run it, paste what it printed, then state what happened.
"It should work" has been wrong repeatedly in this repository.

**0.6 A check that cannot fail is not a check.** For every guard you write,
demonstrate it failing when it should before trusting it green.

**0.7 Money moves behind two switches.** Never widen an existing gate to make
new work function.

**0.8 Everything new defaults off.** Rollback is a toggle.

**0.9 This document is a hypothesis; the running system is the truth.** It was
written without the live database, a running daemon, or a single log line. You
have all of those. **Where a measurement contradicts a claim here, the
measurement wins** — say which claim, what you measured, and what you did
instead. If a section's premise fails verification, stop and report; a section
whose honest outcome is "the premise was wrong" is a successful section.

**0.10 Work cheaply.** Grep to locate, then read the range. The intraday engine
and the shortlist computation are very large files; never read them whole. Cap
all command output.

---

## 1. Coding standards and contracts

**Style.** Match the surrounding file. This codebase has a consistent voice —
explicit names, prose comments explaining *why*, dated notes on landmines.
Preserve it.

**Comment discipline.** When you change behaviour that a prior comment
describes, update the comment. When you fix something that bit the system
before, record the date and the cause as the existing code does.

**Error handling contract.**

| Context | Contract |
|---|---|
| Tick handler | No I/O. No per-tick logging. Update in-memory state and return |
| 15-second cycle | No synchronous database writes. Exceptions in new components are caught, logged at debug, and the cycle continues |
| 300-second timer | Buffered writes flush here. Failures are logged loudly, not swallowed |
| Nightly pipeline | Fatal on data-integrity failure; non-fatal on maintenance failure |
| Order path | Never silently degraded. A failure to place is an alert |

**Database contract.** Additive migrations only. Never `select("*")` on a timer
path or a dashboard path. Confirm a column exists before adding it to an update
payload — an unknown column fails the whole statement and silently loses every
other field.

**Configuration contract.** Every new behaviour has a key with a default
reproducing current behaviour. Keys are readable from the operator control
surface.

---

## 2. Implementation sequence

Ten stages, strictly ordered. **Each gates the next.** After each stage, stop
and report; do not begin the next without being asked.

The ordering is load-bearing and reflects a committee decision: the allocator is
last because it optimises a quantity — per-trade expectancy — that must first be
made positive. Building it earlier produces an efficient allocation of
unprofitable trades.

---

### STAGE 1 — Survival

**Objective.** Prevent the storage ceiling from halting the system; establish
one-action rollback.

**Why first.** The database ceiling puts the project into read-only mode. Writes
fail, the nightly pipeline produces no signals, on an ordinary Tuesday, with no
other warning. This is the only failure in the document that is a total loss.

**Inputs.** Existing storage usage view; existing roll-off function; existing
archive table.

**Deliverables.**
1. Measure current usage and report the total, the top twelve tables by size,
   and the projected ceiling date at measured growth. **If already above ~80%,
   stop and report before running anything else** — a migration against a
   near-full database can itself fail.
2. Invoke the existing roll-off function as the **final non-fatal step of the
   nightly pipeline**. It is already written, transactional, archive-first and
   re-runnable. It has never been called. Read it before scheduling it; confirm
   the archive write precedes the delete rather than assuming it.
3. Storage check in the health tool that **FAILS** above 80% of ceiling, with
   the projected breach date.
4. Weekly database dump to off-platform storage. The platform provides no
   backups; this is the difference between losing a week and losing everything.
5. `tools/rollback` with `--status`, `--allocator-off`, `--all-off`. Writes only
   to configuration. `--all-off` returns every new switch to its pre-Phase-4
   value and prints what it changed.
6. Retention windows for other append-only tables: **measure growth, propose
   windows, do not implement.** A retention window decides what the system can
   still learn from; that is the operator's call.

**Acceptance.** Usage and ceiling date reported · roll-off executed once with
counts logged · health demonstrated FAILING when the threshold is lowered ·
backup produced and verified restorable · rollback tool reports status ·
retention proposals written and awaiting decision.

**Rollback.** Entirely additive.

**Time.** ~1 week.

---

### STAGE 2 — Economics

**Objective.** Make the unit economics of a single trade positive. **Largest
single lever in the programme.**

**Why here.** At current clip sizes, flat per-scrip depository charges plus
statutory friction consume a large fraction of risk per round trip. Every
downstream measurement is denominated in R; if R is not net of true friction,
every number the system produces afterwards is fiction.

**Deliverables.**
1. Complete the friction ledger in the existing cost model: **flat per-scrip
   charges**, statutory components, per product, at realistic clip sizes.
   Interface preserved; internals expanded. The existing `product=` parameter is
   mandatory at every call site — defaulting it understates swing cost roughly
   fivefold.
2. `tools/expectancy_ledger`: net R per trade by product and clip size, applied
   retrospectively to every closed trade.
3. Minimum-viable-trade threshold below which no trade is taken.
4. Revised sizing policy: fewer, larger positions within an **unchanged total
   risk budget**, with explicit per-name concentration caps to bound what that
   creates.

**Validation.** Reconcile modelled friction against actual charges on **≥20 real
round trips. Modelled must match realised within 10%, or the ledger is wrong and
must be corrected before proceeding.**

**Acceptance.** A written expectancy ledger exists. No such document exists in
the repository today, and its absence is why the cost problem went unnoticed.

**Rollback.** Sizing policy is configuration.

**Time.** ~2 weeks.

---

### STAGE 3 — Exits

**Objective.** Convert momentum entries into momentum exits.

**Why here.** The exit module's own docstring records median capture of 3% of
favourable excursion across 70 trades. A momentum system that surrenders its
right tail has no economic thesis. R must be net (Stage 2) before capture can be
measured meaningfully.

**Deliverables.**
1. Instrument the runner decision at **every** target touch: record the evidence
   count, the verdict, whether evidence was sufficient, and the resulting
   action. This distinguishes the two silent failure modes — *the logic cannot
   fire because its inputs are empty* versus *it fires and correctly declines*.
   **If it fires and declines correctly, report that and change nothing.**
2. Re-base exits on the **empirical R distribution** per class rather than a
   fixed target multiple.
3. Record maximum favourable excursion on every position from entry.

**Acceptance.** Median capture ratio measured before and after across ≥30 closed
positions. **Target: 3% → 30%+.**

**Rollback.** Runner behaviour behind a switch defaulting to prior behaviour.

**Time.** ~3 weeks.

---

### STAGE 4 — Measurement spine

**Objective.** Make every decision and non-decision scoreable, on unbiased data.

**Why here.** Everything downstream needs priors. Priors built from executed
trades inherit the old policy's selection and are applied to a region that
policy never sampled.

**Deliverables.**
1. Carry entry rank and R through from open to closed records. Confirm the
   column exists before adding it to any update payload.
2. **Allocation record table** — verdict, score components, hurdle inputs,
   proposal snapshot, reason, resolved outcome, shadow flag defaulting true.
3. **Priors from the full field.** This is the single most important instruction
   in this document. Priors come from *signal-level forward outcomes of every
   daily plan* and *detection-level resolution of every intraday setup* —
   populations the system already records — **not** from closed trades. This
   also dissolves the sample-size problem: tens of thousands of observations per
   year rather than hundreds.
4. Every estimate carries a stated `n` and standard error. Below the sample
   floor: neutral prior plus a flag. **Never fabricate a prior.**
5. Allocator scoring in the review tool: realised R of taken versus deferred
   versus declined.

**Acceptance.** Ranking effectiveness produces a real verdict for the first time
· every prior traceable to an unbiased denominator with a stated `n`.

**Time.** ~3 weeks.

---

### STAGE 5 — Decision-input integrity

**Objective.** Ensure decisions rest on current, true inputs.

**Why here.** The context refresh pulls bars, benchmark price, day range and
volume on the 300-second timer while the feed carries only last price. A
breakout at 10:47 is evaluated against a range built at 10:45.

**Deliverables.**
1. Quote-mode subscription supplying live volume, exchange benchmark price, day
   range and book totals. **The tick handler must remain free of I/O.**
2. Context assembly prefers live fields, falls back to the slower source when
   absent. **Preserve the index fallback** — indices report zero volume, so a
   volume-weighted average is undefined there, and that fallback's absence once
   pinned a market gate permanently.
3. **Staleness guard at every consumer**, with maximum age and fallback. A dead
   socket with an undead cache turns "live" into confident garbage; the guard is
   the deliverable, not the upgrade.
4. Log both sources for one session and report the divergence.

**Acceptance.** Context age at decision time logged and bounded · no decision
executes on data of unknown age · handler purity verified by inspection.

**Time.** ~2 weeks.

---

### STAGE 6 — Engine consolidation

**Objective.** Sixteen engines to five. Remove correlated duplication; raise
per-engine sample rates.

**Why here.** Nine swing screeners correlated ~0.75–0.95 are one engine with
nine names; seven intraday engines are two families. Consolidation costs no
expectancy, removes two-thirds of the tuning surface, and roughly triples
detections per engine — which is what makes Stage 7's learning statistically
capable of a verdict.

**Deliverables.**
1. Swing: seven residual screeners merge into one **continuation** engine with
   unified thresholds.
2. Intraday: gap and prior-day engines become **day-type and reference
   conditions** within the opening-range family; the pullback engine becomes a
   condition within the benchmark-defence family.
3. Compression and range engines retire to shadow.
4. **All retirees are shadowed for one full quarter.** They continue to be
   evaluated and resolved; they simply receive no capital. **The burden of proof
   is on retention.** If a retiree demonstrates independent edge, it returns.

**Acceptance.** Detections per surviving engine ≥3× prior · no measurable
expectancy loss across the shadow quarter.

**Rollback.** Retirees restorable by configuration.

**Time.** ~4 weeks.

---

### STAGE 7 — Governance and cadence

**Objective.** Stop fitting noise.

**Why here.** Adaptation currently runs roughly an order of magnitude faster
than information arrives. Weekly parameter movement against 5–10 closed
observations per week guarantees chasing noise; the system perpetually fits the
recent past and meets each new regime freshly mis-tuned.

**Deliverables.**
1. **Quarterly freeze per component.** Proposals accumulate; decisions are made
   in batch at quarter boundaries.
2. All learning re-based onto **detection-level and signal-level** outcomes.
3. **Out-of-sample confirmation mandatory**: fit in quarter N, confirm in N+1,
   act in N+2.
4. **Auto-apply authority removed entirely.** One governance door.
5. **Conviction layer removed from ranking** and demoted to annotation until
   tier-by-tier forward returns exist from the unbiased record. An unmeasured
   component at the top of the decision stack is unpriced risk.
6. A written freeze calendar.

**Acceptance.** No live parameter moved inside a freeze window · conviction no
longer appears as a ranking input · auto-apply keys retired.

**Note.** This will feel like inaction during drawdowns. **That is the intended
behaviour** and the hardest discipline in the plan.

**Time.** ~1 week.

---

### STAGE 8 — New alpha

**Objective.** Introduce genuinely decorrelated alpha.

**Deliverables.**
1. **Post-earnings drift engine.** Results-day gap, delivery confirmation, drift
   exit. Every input already flows through ingestion. Its edge is structural:
   large buyers cannot compress a week of accumulation into a minute regardless
   of model quality.
2. **Accumulation-confirmed engine.** Delivery-percentage persistence as the
   *primary sort*, structure as confirmation only — the inversion of the current
   burden of proof. Requires one new ingestion source: public block and bulk
   deal disclosures, following the existing ingestion pattern.

**Validation.** Each engine shadows at detection level for **≥1 quarter** and
shows independent expectancy before receiving any capital.

**Rollback.** Each independently switchable.

**Time.** ~8 weeks.

---

### STAGE 9 — Structural overlays

**Objective.** Cheap structural edge and left-tail removal.

**Deliverables.**
1. **Expiry day-type conditioning** of intraday priors, targets and stops.
   Derivative settlement mechanics recur forever; a continuation engine
   calibrated across all days is mis-calibrated on the fraction where settlement
   flows dominate.
2. **Volatility-regime exposure scaling** at book level. Contributes little
   directly; contributes materially to drawdown by shrinking exposure into
   exactly the regimes where a momentum book bleeds.
3. **Liquidity and circuit-band eligibility gate.** Systematically excludes
   names that cannot be exited at plan. Generates zero gross alpha and
   meaningful net alpha by removing left-tail realisations. A stop is a plan for
   continuous prices.

**Acceptance.** Expiry-day outcomes measurably differ from normal days and are
priced accordingly · realised left-tail events reduced.

**Time.** ~5 weeks.

---

### STAGE 10 — Allocation

**Objective.** Optimise which of several positive-expectancy proposals receives
scarce capital.

**Conditional.** Value here depends entirely on Stages 2 and 3 having made
per-trade expectancy positive.

#### 10.1 Package structure

```
backend/allocation/
  proposal      common shape both books emit; adapters
  scoring       edge = E[R] / expected_hold_days
  hurdle        opportunity cost, two regime buckets
  policies      swing assignment | intraday stopping
  allocator     select, basket recheck, buffered write
```

#### 10.2 `proposal`

Defines the single shape both books emit: symbol, framework, product, entry,
stop, target, quantity, source engine, the book's native confidence or rank, and
a metadata map for the record.

Adapters convert what the decision function and the engines **already returned**.
**They form no new opinion and cannot promote a refused proposal.** A second copy
of decision logic drifts; this repository has already lived through divergent
risk-reward models.

#### 10.3 `scoring`

```
R_target   = (target − entry) / (entry − stop)
risk_pct   = (entry − stop) / entry
cost_R     = full_round_trip(product, entry, qty) / risk_pct
E[R]       = expectation over the EMPIRICAL R distribution of this class
             − cost_R
edge       = E[R] / expected_hold_days
```

Three binding rules:

- **`product` is mandatory.** Omitting it understates swing cost roughly
  fivefold and would systematically over-allocate to swing.
- **Empirical distribution, never binary.** A binary target-or-stop model
  assigns a runner the value of its target and nothing more, systematically
  undervaluing exactly the trade class Stage 3 exists to create.
- **`expected_hold_days` is measured**, per book, from closed records. Do not
  hardcode. Report the measured redeployment rate alongside it, because the
  normalisation assumes freed capital redeploys at comparable edge and at two
  slots it frequently idles.

#### 10.4 `hurdle`

Returns the bar a proposal must clear now, given slots remaining, minutes
remaining, and regime bucket.

- **Rises with time remaining** — better is probably still coming.
- **Rises as slots run out** — the last slot is worth more than the first.
- **Two regime buckets**, from the already-computed market state. A pooled curve
  is too high in weak regimes and too low in strong ones — wrong on both tails.

Built from the arrival distributions the system already stores: every intraday
detection with its verdict, and every daily plan against what actually
triggered.

**Cold start:** with insufficient history, return today's effective thresholds so
shadow verdicts initially match current behaviour. **A shadow allocator that
agrees with the live system on day one is the correct starting point** — it
proves the plumbing before it changes any opinion.

#### 10.5 `policies`

**The two books do not share a mechanism.**

- **Swing — assignment.** The full field is known at the open. Estimate
  P(trigger today) per plan from history; reserve slots for high-edge,
  high-probability plans; release reservations as trigger probability decays.
  Forcing this into a stopping framework discards the swing book's single
  greatest informational advantage.
- **Intraday — stopping.** Arrivals are unseen. Compare edge against hurdle.

#### 10.6 `allocator`

Returns TAKE, DEFER or DECLINE per proposal.

- **Basket recheck.** The existing constraint checker compares one proposal
  against held positions. Nothing compares two simultaneous selections against
  each other. `select()` must recheck sector and industry caps across its own
  chosen basket and drop the weakest until they hold.
- **DEFER semantics, defined.** Re-arm cadence, bounded price-drift
  invalidation, and expiry time. Not implicit.
- **Every verdict recorded**, including DECLINE. This is what makes Phase 4
  different in kind from Phase 3.
- **Writes are buffered and flushed on the 300-second timer.** A synchronous
  write inside the decision loop delays exit evaluation on live positions behind
  a network round trip. The catch-and-continue wrapper protects against failure;
  it does nothing against latency.
- **TAKE is provisional** until the position appears in the next state reload.

#### 10.7 Call-site and the no-bypass requirement

Wire into the cycle after both books produce proposals and **before** the
existing action calls. Read-only with respect to everything the live path
subsequently reads. Wrapped so it cannot break the cycle.

**No bypass.** Two paths currently reach entry without passing the allocator:
the scheduled zone-touch watcher, and the approval-driven order path. Each must
either route through the allocator or be **counted by it as an exogenous slot
consumer**. Human override may legitimately bypass the allocator's *judgement*;
it must never bypass its *accounting*. An uncounted fill makes the slot count
wrong and puts a cap breach one cycle away.

#### 10.8 Promotion

**Denominated in disagreements, not sessions.** Agreement observations carry zero
discriminating information. Minimum **30 disagreements** with the greedy
baseline, with counterfactual R difference and its dispersion reported. Expect
months.

Counterfactual fills are **haircut by measured execution shortfall** — decision
price versus realised fill — because zero-slippage paper counterfactuals
systematically flatter whichever policy defers more, which is the allocator.
Instrument that shortfall; the prices already exist in the order path.

Order: shadow → intraday paper (≥5 sessions clean) → swing, each gated on
evidence the operator has read.

**Time.** ~8 weeks.

---

## 3. Operator surface — implemented alongside Stage 10

**Four required views.** Today's allocation ledger ordered by edge, showing
verdict against hurdle with the reason in words; the live hurdle with today's
proposals plotted against it; the storage gauge with projected ceiling date, red
above 80%; the shadow comparison reducible to one number — is the allocator
ahead or behind.

**Five views worth adding regardless.** Pipeline freshness per step with data
age; daemon vitals; a switch board showing live/paper/off for both books; open
proposal count; positions with R and peak excursion.

**Constraints.** No polling — refresh on load and on explicit action. Query
views, not raw tables. All reads column-narrowed. Read-only; the control page
remains the only writer.

**Acceptance.** With the daemon running, and without opening a terminal, answer:
what did the allocator decline today and why · how full is the database · is the
daemon alive and where · is the allocator ahead of greedy.

---

## 4. Statistical safeguards — binding

1. Priors from the **full field**, never from executed trades.
2. Every estimate carries `n` and standard error; floors enforced; below floor
   means neutral and flagged.
3. Out-of-sample confirmation before any parameter change becomes actionable.
4. Quarterly freeze; no movement inside a window.
5. No fitting across the full feature set at current sample sizes.
6. Engine verdicts rendered on **detections**, not closed trades.
7. Counterfactuals haircut by measured shortfall.
8. Promotion gates denominated in **information units** — disagreements,
   detections — never in elapsed time.

---

## 5. Performance expectations

| Path | Expectation |
|---|---|
| Tick handler | No I/O; in-memory update only |
| Decision cycle | 15 s nominal; no synchronous write |
| Allocator scoring | Microseconds; arithmetic over in-memory data |
| Buffered flush | On the 300 s timer |
| Dashboard | No polling; narrowed reads; views not raw tables |
| Egress | The narrowed state read alone should cut a substantial monthly share |

---

## 6. Monitoring, observability, rollback, deployment

**Monitoring.** Storage headroom with projected date · daemon vitals · pipeline
freshness · allocation ledger completeness (proposals seen versus rows written;
buffered writes can leave silent holes in promotion evidence) · alert volume as
a health metric.

**Observability.** Every allocation decision re-derivable from its record. A
decision that cannot be reconstructed is a defect.

**Rollback — three layers, fastest first.**

| Layer | Action | Deploy |
|---|---|---|
| Configuration | `tools/rollback --all-off` | No |
| Kill switch | Halt both books | No |
| Code | Revert the merge commit, redeploy | Yes |

The configuration layer is the one that matters, and it works identically before
or after a merge. **Exercise it during Stage 10** — turn the allocator off
mid-session and confirm the live path resumes unchanged. A rollback never tested
is one whose shape you discover during an incident.

**Deployment.** Nightly batch externally triggered. Daemon on the always-on host;
one ACTIVE via lease, others STANDBY. Research and model training on the
operator machine, with a **defined transport for any trained artifact** — the
model directory is untracked and no scheduled job builds or fetches it, so a
model trained locally is invisible to the process that needs it. Establish where
it lives and whether the consumer can read it **before** writing code that
assumes so; if there is no transport, either version the artifact or fall back
to the rank-decile prior, and **report which and why.**

---

## 7. Definition of done

The implementation is complete when:

1. Storage ceiling date is known and receding; backup verified restorable.
2. A written expectancy ledger exists, reconciled to realised charges within 10%.
3. Median capture ratio measurably improved on live trades.
4. Every prior traces to an unbiased denominator with a stated `n`.
5. No decision executes on data of unknown age.
6. Five engines run; retirees shadowed a full quarter.
7. A freeze calendar exists and has been honoured through one cycle.
8. Conviction is annotation, not a ranking input, pending validation.
9. Every entry path routes through or is counted by the allocator.
10. The allocator has beaten greedy across ≥30 disagreements, or has been
    retired with the evidence recorded.
11. No live module renamed, no file deleted, no schema object dropped.
12. Every new guard demonstrated failing.

---

## 8. Reporting format

After each stage, report exactly:

1. **What was verified** — command and actual output, not a description.
2. **What changed** — files, and why each change was necessary.
3. **What was NOT done** — and why. "This already works" is a complete result.
4. **What this brief got wrong** — per 0.9.
5. **What it costs if wrong** — the failure mode and how it is detected.
6. **Confirmation:** branch is `phase4-allocator`; `main` untouched.

Then stop. Do not begin the next stage without being asked.
