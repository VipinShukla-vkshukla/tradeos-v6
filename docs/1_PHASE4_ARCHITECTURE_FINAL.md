# TradeOS v7 — Final Phase 4 Architecture

**Status: FROZEN.** This document supersedes all prior architecture notes in this
conversation. Every decision recorded here was accepted through architectural
review, red-team review, and investment-committee review. No further
architectural redesign is authorised. Changes require a written contradiction,
not a preference.

**Scope note.** "Phase 4" is *allocated autonomy*. Phase 5 is described in a
separate reference document and is explicitly out of implementation scope.

---

## 1. Vision

TradeOS is a single-operator autonomous trading system for NSE cash equities
that survives long enough to learn the truth about its own edge.

Its defining asset is not signal quality. It is **measurement**: the system
records every proposal it rejected and resolves the outcome of every detection
it never traded. With two entries taken against fifty-six daily candidates,
more than ninety percent of the system's information lives in the trades it
did not take. No other property of this system matters as much, and almost no
retail-scale system has it.

Phase 4 completes that asset and puts one decision on top of it: **which
proposal deserves scarce capital, across both books, on one measured scale,
with the option to decline everything on the grounds that something better is
likely to arrive.**

---

## 2. Design philosophy

**Arithmetic before intelligence.** The largest available improvements are unit
economics and exit capture, not prediction. A system that cannot clear its own
transaction costs does not have a signal problem.

**Measurement before adaptation.** Adaptation faster than information arrival
is decay wearing the costume of learning. Sample rate governs clock speed.

**Deletion is a positive act.** Sixteen engines expressing one bet are one
engine with fifteen aliases. Consolidation raises per-engine sample rates and
lowers maintenance without costing expectancy.

**Separation where scars exist, unification only where proven necessary.** The
two books share a *currency* and an *allocator*. They do not share exits. A
fifteen-session time stop and a 15:20 square-off cannot inhabit one policy
object without one being wrong.

**Every consequential component is switchable and defaults off.** Rollback is a
toggle, not a git operation performed under stress.

**Nothing is deleted; things are quarantined.** A zero-reference search means
the search missed something.

---

## 3. Core principles

| # | Principle | Consequence in design |
|---|---|---|
| P1 | Verify, never assert | Every acceptance criterion is a command with expected output |
| P2 | A check that cannot fail is not a check | Every guard must be demonstrated failing |
| P3 | Propose, never auto-apply | One governance door; auto-apply authority removed entirely |
| P4 | Unbiased denominators | Priors come from the full proposal field, never from executed trades |
| P5 | Money moves behind two switches | Product-level and framework-level, both explicit |
| P6 | The hot path does no blocking I/O | Neither tick handling nor the decision loop writes synchronously |
| P7 | Costs are charged per product, always | CNC and MIS friction differ ~5×; gross R:R is not comparable |
| P8 | Evolution over replacement | The existing repository is preserved wherever practical |

---

## 4. Functional requirements

**FR1 — Universe and signal production.** Nightly ingestion, indicator
computation, regime classification, screening, and plan construction producing
an immutable daily plan set with point-in-time features captured at signal time.

**FR2 — Two books, one currency.** A swing book (CNC, multi-week) and an
intraday book (MIS, same-session) each produce proposals expressed in a common
scoring unit.

**FR3 — Allocation.** A single component decides, per cycle, which proposals
receive capital: TAKE, DEFER, or DECLINE. All three are recorded.

**FR4 — Book-appropriate allocation policy.** Swing uses an *assignment* policy
(the full field is known at open). Intraday uses a *stopping* policy (arrivals
are unseen). They share the scoring currency, not the mechanism.

**FR5 — No bypass.** Every entry path either routes through the allocator or is
counted by it as an exogenous slot consumer.

**FR6 — Exit management.** Book-specific exit engines with runner conversion on
the swing side and mandatory square-off on the intraday side.

**FR7 — Outcome resolution.** Every detection and every daily plan resolves to a
forward outcome regardless of whether it was traded.

**FR8 — Learning.** Quarterly-cadence review producing proposals that a human
approves. No automatic application.

**FR9 — Operator surface.** A dashboard from which allocation decisions,
storage headroom, daemon vitals, and shadow performance are readable without a
terminal.

**FR10 — Survival.** Storage roll-off, backup, and one-action rollback.

---

## 5. Non-functional requirements

| Class | Requirement | Bound |
|---|---|---|
| Latency | Decision cycle | 15 s nominal; no synchronous DB write inside it |
| Latency | Tick handler | Pure; no I/O, no logging per tick |
| Latency | Slow timer | 300 s; carries priors refresh, context refresh, buffered writes |
| Storage | Supabase database | Hard ceiling 500 MB; health FAILS at 80% |
| Storage | Bulk/derived data | Oracle block volume; never Supabase |
| Egress | Supabase | ~5 GB/month; no `select("*")` on timers or dashboard paths |
| Availability | Daemon | Single ACTIVE holder via lease; others STANDBY |
| Durability | Backups | Weekly dump off-platform (free plan provides none) |
| Capacity | Universe | ~500 symbols nightly; ~95 intraday watch |
| Capacity | Capital | Designed for lakhs; explicitly not crores |

---

## 6. Complete system architecture

Nine bands, ordered by when they run. Bands 00–07 exist today and are preserved.
Band 08 is formalised in Phase 4.

```
BAND 00  ORCHESTRATION & ACCESS
         evening orchestrator · daemon entry · readiness checks · broker
         client · token lifecycle · scheduled workflows

BAND 01  INGESTION                          (nightly, ~22:00 IST)
         universe · prices · institutional flow · events · global cues ·
         macro · news · surveillance lists · sector map · holiday calendar
                              │
BAND 02  COMPUTE                            (nightly, in-pipeline)
         indicators · sector/industry strength · regime · quality gate
                              │
BAND 03  SELECTION                          (nightly, in-pipeline)
         screening engines → shortlist → entry zones/stops/targets →
         signal records · sizing · portfolio constraints · liquidity gate
                              │
BAND 04  ENRICHMENT                         (nightly, steps 17–20.5)
         market intelligence · conviction annotation · provider chain ·
         ML win-probability (persisted) · immutable daily plan set
                              │
BAND 05  MARKET HOURS                       (15 s loop / 300 s slow timer)
         ┌─ PROPOSALS ────────────────────────────────────────────┐
         │ swing: plan monitoring → decision → Proposal            │
         │ intraday: 2 engine families → gates → Proposal          │
         └──────────────────────┬─────────────────────────────────┘
                                ▼
         ┌─ ALLOCATION LAYER (new) ───────────────────────────────┐
         │ score()   → expected net R per rupee-day, empirical    │
         │ hurdle()  → opportunity cost, regime-conditioned       │
         │ policy    → assignment (swing) | stopping (intraday)   │
         │ select()  → TAKE / DEFER / DECLINE + basket recheck    │
         │ buffer    → verdicts flushed on the 300 s timer        │
         └──────────────────────┬─────────────────────────────────┘
                                ▼
         EXECUTION (unchanged)      EXITS (unchanged, separate)
         CNC live + broker stops    swing lifecycle + runner rules
         MIS paper                  intraday policy + square-off
                                │
BAND 06  LEARNING                           (quarterly freeze cadence)
         outcome resolution · review · discovery · brain analyses →
         proposals → human approval → applied
                                │
BAND 07  OPERATOR SURFACE
         dashboard · control · health · CLI tools · messaging · kill switch

BAND 08  STORAGE & RETENTION                (nightly / weekly)
         roll-off · archive · storage health FAIL · off-platform backup
```

---

## 7. Module hierarchy

Preserved from the existing repository. **One new package.** No renames of live
modules.

```
backend/
  swing/
    ingestion/      unchanged
    compute/        unchanged
    signals/        engine set consolidated; two new engines added
    brain/          unchanged; cadence changed by governance, not code shape
  intraday/
    strategies/     engine set consolidated to two families
    engine.py       gains allocator call-site; load_state narrowed
    price_feed.py   MODE_QUOTE; handler remains pure
    exit_policy.py  unchanged
    cost_model.py   gains full friction ledger incl. flat charges
  analysis/         decision · ranking · readiness · structure ·
                    constraints (basket recheck) · risk model (vol scaling)
  control/          lifecycle · exit rules · monitors · messaging
  execution/        gates · order manager · paper broker · broker stops
  ai/               router · providers · fallback · enrichment ·
                    post-trade · ML support (artifact transport resolved)
  allocation/       NEW — proposal · scoring · hurdle · policies · allocator
  tools/            health · simulate · review · discovery · rollback (new) ·
                    expectancy ledger (new) · allocator report (new)
  db/migrations/    additive only
  kite/             unchanged
frontend/           four new views; existing components preserved
```

---

## 8. Component responsibilities

**Allocation layer (new, five modules).**

| Module | Sole responsibility | Must not |
|---|---|---|
| `proposal` | Define the common shape both books emit; adapt existing outputs | Form any new opinion; promote a refused proposal |
| `scoring` | Convert a proposal to expected net R per rupee-day using empirical R distributions and product-specific friction | Use a binary win/loss model; omit `product=` |
| `hurdle` | Return the bar a proposal must clear now, given slots, time, and regime bucket | Pool regimes into one curve |
| `policies` | Swing assignment policy; intraday stopping policy | Share a mechanism between books |
| `allocator` | Select, basket-recheck constraints, buffer verdicts | Write synchronously; place an order |

**Modified existing components.**

- `cost_model` — becomes the authoritative friction ledger, including flat
  per-scrip depository charges, per product, at realistic clip sizes.
- `portfolio_constraints` — gains a basket-level recheck across the allocator's
  own simultaneous selections, not only against held positions.
- `price_feed` — quote-mode fields; handler purity preserved.
- `entry_ranking` — demoted from final arbiter to one scoring input.
- `risk_model` — gains volatility-regime exposure scaling at book level.
- `ml_support` / providers — win probability persisted; model artifact given a
  defined transport.
- `engine.py` — allocator call-site; narrowed state read.

**Unchanged and load-bearing.** Regime gate, quality gate, event monitor,
surveillance screening, broker-side stops, lease, kill switch, outcome
resolution, point-in-time feature capture.

---

## 9. Alpha engine architecture

**Consolidated set: five engines.** The prior sixteen expressed one or two bets.

| Book | Engine | Basis | Absorbed |
|---|---|---|---|
| Swing | **Continuation** | Breakout structure, trend alignment, volume expansion | Seven prior screeners, thresholds unified |
| Swing | **Accumulation-confirmed** | Delivery-% persistence as primary sort; structure as confirmation only | New (inverted burden of proof) |
| Intraday | **ORB-family** | Opening-auction information release | GAP, PDL as day-type conditions |
| Intraday | **VWR-family** | Institutional benchmark defence | PBK as a condition |
| Swing | **Post-earnings drift** | Slow information diffusion, delivery-confirmed | New |

Retired to quarantine: VCE, RNG, and seven residual screeners. **Retirement is
shadowed for one quarter; the burden of proof is on retention, not removal.**

**Structural overlays** (not signal generators): expiry day-type conditioning;
volatility-regime exposure scaling; liquidity and circuit-band avoidance.

---

## 10. Trading lifecycle

```
T-1 22:00  Ingest → compute → screen → size → constrain → enrich →
           immutable plan set written with point-in-time features
T-1 22:40  Storage roll-off (final non-fatal step) → alerts dispatched

T   09:15  Daemon acquires lease · loads plans and positions (narrow read)
           Swing assignment policy computes P(trigger today) per plan and
           reserves slots for high-edge/high-probability plans
T   09:15+ 15 s cycle:
             evaluate exits on open positions          [always first]
             collect swing proposals (zone touches)
             collect intraday proposals (2 families)
             score() → hurdle() → policy → select()
             basket recheck of portfolio constraints
             TAKE → execution; all verdicts → buffer
T   +300 s slow timer:
             refresh contexts · refresh empirical priors ·
             flush verdict buffer · refresh annotations (2×/day)
T   15:15  Intraday square-off
T   15:30  Outcome resolution: every detection and every plan scored
```

---

## 11. Research and signal-generation pipelines

**Research (offline, laptop).** Model training, hurdle-distribution fitting,
engine consolidation analysis, and microstructure study run on the operator's
machine. Pattern: **heavy computation on the laptop, small artifact into the
database.** Nothing in the live path depends on the laptop being available.

**Signal generation (nightly, scheduled).** Deterministic, dependency-ordered,
gated on data quality. Enrichment annotates; it does not generate. The plan set
is immutable once written — this is the anchor of all point-in-time integrity.

---

## 12. Portfolio construction framework

Single account, two books, shared capital. Construction is enforced at three
points:

1. **Per proposal** — sizing, risk-per-trade, liquidity and band eligibility.
2. **Per basket** — the allocator's simultaneous selections rechecked against
   sector and industry caps *and against each other*.
3. **Per book** — daily entry caps, open-risk budget, combined notional guard.

Diversification is measured by **correlation of engine outcomes**, not by engine
count. Five engines with distinct burdens of proof replace sixteen with one.

---

## 13. Capital allocation framework

**Common currency.** For every proposal, from either book:

```
R_target        = (target − entry) / (entry − stop)
risk_pct        = (entry − stop) / entry
cost_R          = full_round_trip(product, entry, qty) / risk_pct
                  ← includes flat per-scrip charges
E[R]            = expectation over the EMPIRICAL R distribution of this
                  proposal's class, minus cost_R
edge            = E[R] / expected_hold_days
```

**Empirical, not binary.** `E[R]` is taken from the resolved outcome
distribution of the class — which preserves the right tail that runner
conversion exists to create. A binary target/stop model systematically
undervalues swing runners and is prohibited.

**Priors.** Sourced from the **full proposal field**: every daily plan's forward
path and every intraday detection's resolution. Never from executed trades.
Minimum sample floors apply; below the floor the prior is neutral and flagged.

**Hurdle.** The bar a proposal must clear now, rising with time remaining and
with slot scarcity, fitted separately for two regime buckets.

**Policies.**
- *Swing — assignment.* The field is known at open. Estimate P(trigger today)
  per plan; reserve slots for high-edge/high-probability plans; release
  reservations as trigger probability decays through the session.
- *Intraday — stopping.* Arrivals are unseen. Compare against the hurdle;
  take, defer, or decline.

**DEFER semantics** (defined, not implicit): a deferred proposal re-arms on a
fixed cadence, is invalidated by a bounded price drift from its recorded level,
and expires at a stated time. Counterfactual scoring of deferred proposals is
haircut by measured execution shortfall.

**Recording.** Every verdict, including DECLINE, with its inputs, the hurdle it
faced, and the outcome once resolved. Writes are buffered and flushed on the
slow timer.

---

## 14. Risk management framework

Layered, outermost first:

| Layer | Control |
|---|---|
| Existential | Kill switch; storage ceiling health FAIL; lease preventing duplicate actors |
| Book | Daily entry caps; open-risk budget; combined notional guard; volatility-regime exposure scaling |
| Basket | Sector and industry caps rechecked across simultaneous selections |
| Position | Risk-per-trade sizing; broker-side stops on CNC; mandatory intraday square-off |
| Instrument | Surveillance-list exclusion; circuit-band eligibility; liquidity sufficiency for planned exit |
| Event | Scheduled-event gating; held-position event monitor; reduced size across binary events |

**Explicit acknowledgement.** A stop is a plan for continuous prices. Band-limited
and gap-through outcomes are handled by *eligibility and sizing*, not by the
stop.

---

## 15. Position sizing framework

Fixed-fractional risk per trade, **subject to a minimum-viable-trade threshold**
below which no trade is taken. The threshold exists because flat per-scrip
charges make small clips structurally unprofitable: fewer, larger positions
within an unchanged total risk budget. Concentration created by this policy is
capped explicitly per name.

Book-level exposure is scaled inversely to the volatility regime.

Edge-proportional sizing is **Phase 5 and out of scope.**

---

## 16. Market regime framework

Breadth-driven classification with an ML classifier gated behind autonomy phase.
Regime is consumed at three points: entry permission, book-level exposure
scaling, and hurdle bucketing (two buckets — full per-regime fitting is Phase 5).

---

## 17. Statistical framework

**Denominators.** Unbiased by construction: all priors from the full field.
Executed trades are used to score the allocator, never to feed it.

**Sample floors.** Every estimate carries a stated `n` and standard error.
Below floor, neutral prior plus flag. No estimate is used without its `n`.

**Multiple testing.** Any relationship discovered by search requires
out-of-sample confirmation in a subsequent window before it becomes actionable.

**Cadence.** Parameters freeze per component for one quarter. Proposals
accumulate and are decided in batch. This is the primary defence against
fitting noise at a 5–10 observation/week arrival rate.

**Prohibited.** Fitting on 114 features at n≈70. Weekly parameter movement.
Auto-application of any discovered relationship.

---

## 18. Validation and walk-forward methodology

**Detection-level validation is primary.** Every detection resolves, giving
20–50× the sample of closed trades. Engine verdicts are rendered on detections.

**Walk-forward, quarterly.** Fit on quarter N, confirm on N+1, act in N+2.
Nothing is promoted on in-sample evidence.

**Allocator promotion gate — denominated in disagreements, not sessions.**
Minimum **30 disagreements** with the greedy baseline, with counterfactual R
difference and its dispersion reported. Expect months. Sessions of agreement
carry zero discriminating information.

**Engine retirement gate.** One quarter of shadow. Retention requires evidence
of independent edge.

**Conviction-layer gate.** Tier-by-tier forward returns from the unbiased
record. Until produced, the conviction layer is **removed from ranking** and
retained as annotation only.

---

## 19. Explainability framework

Every allocation decision is reconstructable from its record: the proposal
snapshot, the score and its components, the hurdle and the inputs to it, the
verdict, the reason in words, and the resolved outcome. **A decision that
cannot be re-derived is a defect.**

Enrichment output is annotation, carried alongside decisions and auditable
against outcomes — never an unaudited input to ranking.

---

## 20. Monitoring, observability and alerting

**Dashboard (four required views).** Today's allocation ledger ordered by edge;
the live hurdle with today's proposals plotted against it; storage headroom with
projected ceiling date; shadow comparison of allocator versus greedy.

**Plus, independent of Phase 4.** Pipeline freshness per step with data age;
daemon vitals (lease holder, heartbeat, feed source, blind cycles); switch board
showing live/paper/off for both books; open proposal count; positions with R and
peak excursion.

**Alerting.** De-duplicated, keyed per symbol and kind, with re-arm and restate
intervals. Alert volume is a health metric: a system alerting continuously is
failing to decide.

**Constraints.** No polling. Query views, not raw tables. All dashboard reads
column-narrowed.

---

## 21. Governance

**One door.** Every parameter change enters through the proposal queue and is
approved by the operator in batch at quarter boundaries. Auto-apply authority is
removed entirely — the prior per-type policy is retired to eliminate governance
drift.

**Two switches for money.** No live order path opens on a single toggle.

**Quarantine, never delete.** Removal candidates are documented with the search
performed and await operator decision.

**Retention windows are operator decisions.** They determine what the system can
still learn from.

---

## 22. Failure handling

**Failure-domain matrix — a required deliverable, not an aspiration.** For each
of database / daemon host / operator machine / scheduler / broker, the system's
behaviour when it fails during market hours must be written down and verified.

Established positions:

| Failure | Behaviour |
|---|---|
| Database unreachable | Open positions protected by broker-side stops. Kill-switch state read from cached config. No new entries. Lease cannot renew; no promotion of standby |
| Daemon host lost | Broker-side stops remain. Standby may promote once the lease expires |
| Scheduler delayed | Evening pipeline is externally triggered for this reason; roll-off is a pipeline step, not a separate schedule |
| Broker session invalid | No orders; alert; positions retain broker-side stops |
| Feed stale or undead | Staleness guard at every consumer; fallback to slower source; no decision on data of unknown age |
| Storage ceiling breached | Health FAILS at 80% with projected date; read-only mode would halt writes entirely |

---

## 23. Operational safeguards

Shadow before live. Additive migrations only. Config-first rollback, exercised
before it is needed. Provisional TAKEs until confirmed by state reload. Order
path re-validates caps at placement. Buffered writes off the hot path.
Handler purity in the tick path.

---

## 24. Performance objectives

| Objective | Target |
|---|---|
| Median capture ratio | 3% → **30%+** |
| Friction per swing round trip | Modelled within **10%** of realised |
| Net expectancy per trade | Positive after full friction — the precondition for allocation work |
| Priors sample size | Stated on every estimate; floors enforced |
| Allocator evidence | ≥30 disagreements before promotion |
| Storage headroom | Ceiling date monotonically receding |

---

## 25. Scalability considerations

Designed for lakhs, not crores; capacity is bounded by cash-market liquidity in
the mid/small-cap universe and by flat-charge economics at small clips. Scale-up
levers, in order: clip size (already the largest lever), universe breadth,
intraday mix. Institutional scale is not a design goal and pursuing it would
invalidate the sizing framework.

---

## 26. Deployment architecture

Three hosts, three roles, fixed:

| Host | Property | Owns |
|---|---|---|
| Managed database | Small, authoritative, capped | System of record: decisions, outcomes, config. Never bulk data |
| Always-free VM | Always on, modest CPU, large disk, static address | The daemon; anything that must not miss a tick |
| Operator machine | Most capable, intermittent | Research, model training, distribution fitting. Never in the live path |
| Scheduler | Unreliable timing | Nightly batch (externally triggered), weekly maintenance |

**One ACTIVE daemon.** Others STANDBY via lease. Duplicate actors are the
failure mode this arbitration exists to prevent.

---

## 27. Dependency map

```
Ingestion ─────────► Compute ─────────► Selection ─────────► Enrichment
                        │                   │                     │
                        └──── regime ───────┴──── constraints ────┤
                                                                  ▼
                                                          immutable plan set
                                                                  │
                    live quote feed ──► contexts ──────────┐      │
                                                           ▼      ▼
                                                    proposals (2 books)
                                                           │
                     empirical priors ◄── outcome ─────────┤
                     (full field)         resolution       ▼
                                                    ALLOCATION LAYER
                                                           │
                                          ┌────────────────┴──────────────┐
                                          ▼                               ▼
                                    execution                     allocation record
                                          │                               │
                                        exits                             │
                                          │                               │
                                          └────────► outcomes ◄───────────┘
                                                        │
                                                        ▼
                                              quarterly review → proposals
                                                        │
                                                        ▼
                                              operator approval → applied
```

**Critical path for Phase 4 value:** friction ledger → positive per-trade
expectancy → meaningful R → capture-ratio measurement → unbiased priors →
scoring → allocation. Each link is required by the next. The allocator is last
because it optimises a quantity that must first be positive.

---

## 28. Decision flow — allocation, textually

```
For each 15-second cycle:
  1. Evaluate exits on open positions.                    [always first]
  2. Collect proposals from both books.
  3. If none → end cycle.
  4. For each proposal:
       reject if instrument ineligible (band, liquidity, surveillance)
       score() → edge, with product-specific friction and empirical E[R]
       flag if prior below sample floor → neutral prior
  5. Fetch hurdle(regime_bucket, slots_left, minutes_left) per book.
  6. Apply book policy:
       swing    → assignment: compare against reserved slots and
                  P(trigger today) of unfilled higher-edge plans
       intraday → stopping: compare edge against hurdle
  7. Basket recheck: do the selected proposals, taken together, breach
     any sector, industry, or exposure constraint? Drop the weakest until
     they do not.
  8. Emit verdicts. Buffer all. TAKE proceeds to execution as provisional
     until confirmed by the next state reload.
  9. Slow timer flushes the buffer.
```

---

## 29. What this architecture deliberately excludes

Recorded so the exclusions are decisions rather than oversights.

- Full unification of the two books' exit policies — rejected; scar documented.
- An LLM inside the decision loop — rejected on latency and on task fit.
- Depth-book capture — cut; permanence thesis contradicted by storage reality
  and by absent calibration sample.
- Feature attribution across 114 columns at n≈70 — cut as a multiple-testing
  machine.
- Edge-proportional sizing and capital recycling — Phase 5, gated on capital.
- Per-regime hurdle fitting beyond two buckets — Phase 5, gated on sample.
- Institutional-scale capacity — out of mandate.
