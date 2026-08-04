# TradeOS — Production Decision Document

**Standing assumption:** all prior findings accepted. Cost drag (F1), the 3%
capture ratio (F2), over-clocked adaptation (F3), the unvalidated conviction
layer (F4), and the red-team's Critical set (selection-biased priors, bypassed
entry paths, binary scoring) are treated as established fact and are not
re-argued. This document decides what to build, in what order, and whether to
proceed.

**Standing constraint:** one developer, one year, ₹20,000 of live capital, and
a trade rate that produces roughly 5–10 closed observations per week. Every
estimate below is a structured judgment at that scale, not a computation. All
Sharpe figures are committee estimates for the strategy archetype as
implemented here, net of the friction established in F1 — not backtest output,
which does not exist in usable form.

---

## 1. Executive Summary

The engine review produces an uncomfortable but clarifying result: **sixteen
engines are running and perhaps three carry independent, durable edge.** The
nine swing screeners are near-collinear expressions of one factor — breakout
momentum on a public screener universe — and the seven intraday engines are
seven expressions of intraday continuation. The system does not hold a
portfolio of alphas; it holds one alpha with fifteen aliases and a diversified
naming convention.

That finding drives everything downstream. Consolidating sixteen engines to
five costs nothing in expectancy (the removed ones contribute almost no
independent information), removes roughly two-thirds of the maintenance and
tuning surface, and — critically — raises the per-engine sample rate enough
that the learning apparatus can finally say something statistically. **Engine
deletion is a positive-CAGR act at this scale.** It is the single most
counterintuitive recommendation in this document and the committee holds it
with high confidence.

On the roadmap: of nine Phase 4 work packages, **three should be deleted, two
merged, one postponed to Phase 2, and four retained** — and two entirely new
packages must be inserted ahead of all of them, because F1 (position-size
arithmetic) and F2 (capture ratio) together carry more expected CAGR than the
entire allocator programme they were meant to precede. The allocator survives,
but demoted: it is a capital-efficiency optimiser sitting on top of an
expectancy problem that must be fixed first. Optimising the allocation of
negative-expectancy trades allocates them more efficiently.

The final decision is conditional and stated at the end. The short version: not
as it stands, and five specific changes separate the current system from one
this committee would fund with personal capital.

---

## 2. Engine Assessment — existing

**Method note.** Correlation figures are structural estimates from signal
construction (shared inputs, shared market state monetised), not measured
correlations — the sample does not support measurement. Where two engines
respond to the same market state through overlapping inputs, they are treated
as correlated regardless of formula differences.

### Swing screening engines (nine)

The nine are treated as one cluster because they behave as one. All source from
the same public-screener universe, all key on some combination of breakout
structure, volume expansion, trend alignment and relative strength. Different
thresholds on shared inputs produce different names, not different bets.

| Attribute | Assessment |
|---|---|
| Durable edge | **Weak.** Momentum as a factor is structural and persistent; *this expression* of it is the most widely distributed retail signal in India. The factor survives; the crowded expression of it does not. |
| Structural or temporary | Factor structural, expression temporary |
| Decade decay | **High.** In small/mid caps, crowded breakout levels invert into stop-harvesting pools rather than merely fading |
| Failure modes | Sideways regimes (bleed); crowded-level reversal; gap-through on band-limited names; cost drag exceeding gross edge (F1) |
| Est. Sharpe (cluster, net) | **0.2–0.5** at current position sizes; **0.5–0.8** with F1 and F2 remediated |
| Est. win rate | 35–45%, right-tail dependent — which is precisely what F2 destroys |
| CAGR contribution | Currently near zero net; **8–14% gross-of-nothing potential** post-remediation |
| Inter-engine correlation | **0.75–0.95 within cluster.** The binding number in this document |
| Verdict | **MERGE nine into two** |

**The merge:** one *continuation* engine (breakout/trend-alignment, the current
core, thresholds unified) and one *accumulation-confirmed* engine (delivery-%
persistence as primary sort, structure as confirmation — the F7 inversion).
Two engines, genuinely different burdens of proof, sample rate per engine rises
~4×.

**Deleted outright:** the seven residual screeners whose incremental
contribution is threshold variation. Each currently consumes tuning attention,
generates its own weekly review verdict on ~1 trade of evidence, and creates
the illusion of diversification that F10 identified. Deletion improves the
system.

### Intraday engines (seven: ORB, GAP, PDL, VCE, PBK, VWR, RNG)

| Engine | Edge basis | Structural? | Sharpe (est.) | Win rate | Corr. w/ cluster | Verdict |
|---|---|---|---|---|---|---|
| **ORB** (opening range) | Auction-driven information release; genuine microstructure anchor | **Structural** | 0.6–0.9 | 40–48% | 0.5 | **KEEP — becomes the intraday core** |
| **VWR** (VWAP reclaim) | Institutional execution benchmark defence — real, observable flow | **Structural** | 0.5–0.8 | 45–52% | 0.4 | **KEEP** |
| **GAP** (gap continuation) | Overnight information + underreaction | Semi-structural | 0.4–0.7 | 38–45% | 0.6 w/ ORB | **MERGE into ORB** as a day-type condition |
| **PDL** (prior day levels) | Reference-point anchoring (behavioural) | Semi-structural | 0.3–0.6 | 40–47% | 0.7 w/ ORB | **MERGE into ORB** |
| **PBK** (pullback) | Trend continuation after retrace | Weak | 0.2–0.5 | 42–50% | 0.75 w/ VWR | **MERGE into VWR** |
| **VCE** (volatility compression) | Volatility clustering — real, but slow to express intraday | Semi-structural | 0.2–0.5 | 35–45% | 0.5 | **DELETE** — the phenomenon is real, the intraday expression is thin |
| **RNG** (range) | Intraday mean reversion within range | Weak | 0.1–0.4 | 50–58% | 0.3 | **DELETE** — lowest edge, and its function is better served by the new reversion sleeve |

**Result: seven intraday engines become two** (ORB-family, VWR-family), each
absorbing its correlated cousins as *conditions* rather than separate engines.
Detection rate per engine roughly triples, which is what makes the
detection-level learning loop statistically meaningful.

### Non-engine components carrying alpha function

- **Regime gate** — keep, structural, the primary drawdown control.
- **Sector strength ranking** — keep as filter; its promotion to *traded
  signal* is a new engine (below).
- **AI conviction tier** — **suspend from ranking** pending F4 validation. It is
  not an engine; it is an unpriced overlay on every engine.

---

## 3. Top 10 New Alpha Engines

Constraints applied: exploit structure, behaviour or microstructure; no
indicator stacking; survive AI-driven markets (i.e. edge must rest on flow,
constraint or human/institutional behaviour that persists when everyone has a
model); scalable across NSE cash equities.

**N1 — Post-Earnings Drift, delivery-confirmed.**
Structural: information diffuses slowly through a retail-heavy shareholder
base; institutions accumulate over days, not minutes. Entry on results-day gap
with sustained delivery elevation; exit on drift exhaustion. Survives AI
markets because the constraint is *capacity and mandate*, not information — a
large buyer cannot compress a week of accumulation into a minute regardless of
how good its model is. Est. Sharpe 0.8–1.2. Correlation to existing book: 0.2.
**Highest-conviction new engine.**

**N2 — Institutional Accumulation Footprint.**
Delivery-% persistence across a rolling window, confirmed by block/bulk deal
prints and absence of surveillance flags. Structural: block prints are a
*disclosed* record of size that must transact over subsequent sessions.
Behavioural edge: retail screens price, institutions leave a volume signature.
Est. Sharpe 0.7–1.1. Correlation: 0.25.

**N3 — Expiry-Day Microstructure Specialisation.**
Not a standalone engine so much as a day-type regime under which ORB/VWR
priors, targets and stops are re-fitted. Structural: derivative settlement
mechanics recur monthly and weekly forever; pinning and unwinding are
constraint-driven, not opinion-driven. Est. incremental Sharpe on the intraday
sleeve: +0.2–0.4. **Cheapest structural edge available.**

**N4 — Index Inclusion / Exclusion Flow.**
Announced rebalances force passive and benchmark-tracking flow on known dates.
Structural and permanent — mandate-driven buying that must occur regardless of
price. Low frequency (a handful of events per year), high hit rate. Est. Sharpe
0.9–1.4 on the events it trades; CAGR contribution modest due to frequency.
Survives AI markets absolutely — the flow is compulsory.

**N5 — Large-Cap Oversold Reversion (the decorrelator).**
Liquid large caps only, extreme short-horizon oversold with no fundamental
event and intact longer-term structure. Behavioural: panic overshoot in names
with deep institutional support. Est. Sharpe 0.5–0.8, but **correlation to the
momentum book is negative (−0.2 to −0.4)**, which makes its portfolio
contribution exceed its standalone Sharpe. This is the F10 fix.

**N6 — Gap-Down Reversion vs. Gap-Down Continuation, separated by cause.**
Same price event, opposite trades, distinguished by whether an identifiable
information event exists (earnings, sector news, index move) or not. Behavioural
+ microstructure: uninformed gaps revert, informed gaps drift. Est. Sharpe
0.6–0.9. Requires the event calendar the system already ingests.

**N7 — Sector Rotation as Traded Signal.**
Trade the *transition* — first cohort of leadership change within a sector —
rather than filtering by current rank. Structural: institutional rotation
occurs in weeks and is capacity-constrained. Est. Sharpe 0.6–0.9. Correlation
0.35 (shares momentum DNA, differs in timing).

**N8 — Volatility-Regime Position Scaling (meta-engine).**
Not a signal generator: a book-level overlay scaling all exposure inversely to
realised/implied volatility state. Structural: volatility clusters — the single
most reliable empirical regularity in equities. Contributes little CAGR
directly; contributes **materially to Sharpe and drawdown** by shrinking
exposure into precisely the regimes where the momentum book bleeds. Est. Sharpe
uplift on total book: +0.2–0.3.

**N9 — Circuit-Band and Liquidity-Constraint Avoidance (negative alpha engine).**
Systematically identifies and excludes names where the trade cannot be exited
at plan — band-limited, thin-book, surveillance-adjacent. Structural: exchange
mechanics are permanent. Generates zero gross alpha and meaningful net alpha by
removing left-tail realisations. This is the F9 fix expressed as an engine so
it receives measurement discipline rather than being a footnote.

**N10 — Intraday Short Mirror.**
The existing ORB/VWR logic inverted for the short side under MIS. Structural:
symmetry is available and currently unused; adverse regimes are where the long
book has nothing to do. Est. Sharpe 0.4–0.7 standalone; portfolio value comes
from **being the only component that earns in a falling tape** (F6).

---

## 4. Engine Ranking — by expected long-term edge

Ranked on durability × portfolio contribution, not standalone Sharpe.

| # | Engine | Type | Est. Sharpe | Portfolio role | Status |
|---|---|---|---|---|---|
| 1 | **N1 Post-Earnings Drift** | New | 0.8–1.2 | Primary new alpha | Build |
| 2 | **N2 Accumulation Footprint** | New | 0.7–1.1 | Replaces crowded selection | Build |
| 3 | **N4 Index Flow** | New | 0.9–1.4 | Low freq, near-certain | Build (Phase 2) |
| 4 | **ORB-family** (ORB+GAP+PDL) | Merged existing | 0.6–0.9 | Intraday core | Merge |
| 5 | **N5 Large-Cap Reversion** | New | 0.5–0.8 | **Decorrelator** | Build (Phase 2) |
| 6 | **VWR-family** (VWR+PBK) | Merged existing | 0.5–0.8 | Intraday secondary | Merge |
| 7 | **N8 Vol-Regime Scaling** | New (overlay) | +0.2–0.3 book | Drawdown control | Build |
| 8 | **N3 Expiry Specialisation** | New (condition) | +0.2–0.4 sleeve | Cheap structural | Build |
| 9 | **N9 Liquidity Avoidance** | New (negative) | Left-tail removal | Loss prevention | Build |
| 10 | **N6 Gap Separation** | New | 0.6–0.9 | Intraday breadth | Phase 2 |
| 11 | **Swing Continuation** (merged 9→1) | Merged existing | 0.5–0.8 post-fix | Legacy core | Merge, remediate |
| 12 | **N7 Sector Rotation** | New | 0.6–0.9 | Swing breadth | Phase 2 |
| 13 | **N10 Short Mirror** | New | 0.4–0.7 | Bear-regime earner | Phase 2 |
| 14 | **VCE, RNG** | Existing | 0.1–0.5 | None | **Delete** |
| 15 | **7 residual screeners** | Existing | ~0 incremental | Illusory diversification | **Delete** |

---

## 5. Optimized Roadmap

Applied to the nine existing Phase 4 work packages. One developer, one year.

### Deleted

- **WP2.6 (depth capture)** — already flagged for the axe by the red team;
  confirmed. Its value thesis is permanence, its storage is the least permanent
  surface in the fleet, and at 14 closed intraday trades there is nothing to
  calibrate against. **Effort saved: 3–4 weeks. CAGR forgone: ~0.**
- **WP4 (feature attribution across 114 columns)** — a multiple-testing machine
  at n≈70 that will surface the two luckiest spurious relationships. Its
  legitimate function is absorbed into the engine consolidation, which reduces
  the feature surface by construction. **Effort saved: 1–2 weeks. CAGR: ~0,
  possibly negative.**
- **WP8 (design review)** — a valuable exercise, but this document *is* it.
  Retaining it schedules a second redesign after a design freeze. **Deleted on
  principle: the freeze must mean something.**

### Merged

- **WP1 + WP6** → *Measurement spine.* Both concern the same thing: making
  decisions and outcomes scoreable. Splitting them created a gap in which WP1's
  fix was never validated by WP6's consumer.
- **WP2.5 (live quote fields) + WP2 (runner verification)** → *Decision-input
  integrity.* Both are "is the system deciding on true inputs." Same
  investigation, same session, same developer context.

### Postponed to Phase 2

- **WP3/WP5 (allocator, shadow → live)** — retained but **demoted below the
  expectancy fixes.** Rationale: the allocator optimises *which* of several
  trades to take. Until per-trade expectancy is positive net of friction (F1)
  and the right tail is being captured (F2), it is optimising the allocation of
  a loss. It also requires the unbiased priors (red-team C1) that only the
  measurement spine can produce. Ordering it after the expectancy work is not a
  deferral of value; it is the only ordering in which it *has* value.

### Retained, reordered, and two inserted ahead

Full sequence in §7. The two new packages are **WP-A (position-size and cost
rationalisation)** and **WP-B (capture-ratio remediation)** — F1 and F2
respectively, which between them carry more expected CAGR than every remaining
package combined.

### Expected value table

| Package | Effort | ΔCAGR | ΔDrawdown | Robustness | Maint. | ROI |
|---|---|---|---|---|---|---|
| WP0 Storage/rollback | 1 wk | 0 | — | **Critical** (prevents total stop) | Low | Infinite (survival) |
| **WP-A Cost/sizing** | 2 wk | **+3 to +6pp** | Neutral | Med | **Negative** (simplifies) | **Highest** |
| **WP-B Capture ratio** | 3 wk | **+3 to +5pp** | Slightly worse (wider stops) | Med | Low | **Very high** |
| WP-C Measurement spine (WP1+6) | 3 wk | Enabling | — | High | Low | High (gates all learning) |
| WP-D Input integrity (WP2+2.5) | 2 wk | +0.5–1pp | Small | High | Low | High |
| WP-E Engine consolidation 16→5 | 4 wk | +0.5–1pp | Med | **Very high** | **Strongly negative** | **Very high** |
| WP-F Cadence freeze (F3) | 1 wk | +2–4pp preserved | Med | **Very high** | Negative | **Very high** |
| WP-G Engines N1, N2 | 8 wk | +2–4pp | Med (decorrelated) | High | Med | High |
| WP-H Engines N3, N8, N9 | 5 wk | +0.5–1.5pp | **High** | High | Low | High |
| WP-I Allocator (WP3+5+7 dash) | 8 wk | +0.5–1.5pp | Small | Med | Med | Moderate |
| Phase 2: N4, N5, N6, N7, N10 | Year 2 | +2–4pp | High | High | Med | High |

---

## 6. Five Mandatory Changes Before Production

Would I personally implement this system exactly as it stands? **No.**

Five changes, in strict priority order. Nothing else is listed — everything
below the fifth is not a priority, and the discipline of that exclusion is the
point.

**1. Fix position-size economics so a trade can be profitable (F1).**
At ≤₹4,000 clips, flat depository charges plus statutory friction consume
0.18–0.23R per swing round trip against a ~6% stop. No signal in this document
survives that. Fewer and larger positions within the same total risk budget, or
mix shifted toward the MIS sleeve where flat charges do not apply. **Until this
is done every other improvement is fitting a better engine to a leaking hull.**

**2. Fix the exit so the right tail is actually captured (F2).**
Median capture of 3% of favourable excursion means the system is executing
momentum entries with mean-reversion exits. Verify the runner path fires;
re-base exits on the empirical R distribution rather than a fixed target. A
momentum system that does not capture its right tail has no economic thesis.

**3. Slow adaptation to match information arrival (F3).**
Quarterly parameter freezes per component; all learning re-based onto
detection-level and signal-level outcomes where n is 20–50× larger than the
closed-trade record; no change lands without out-of-sample confirmation. Weekly
tuning at 5–10 observations per week is a decay mechanism wearing the costume
of a learning system.

**4. Validate or suspend the AI conviction layer (F4).**
The apex ranking input has never been tested against a single forward outcome
and could as easily be negative alpha. Produce tier-by-tier forward returns
from the unbiased detection record. **Until that measurement exists, the tier
is removed from ranking** and demoted to annotation. An unmeasured component at
the top of the decision stack is unpriced risk, not intelligence.

**5. Consolidate sixteen engines to five (Engine review, this document).**
Nine screeners correlated 0.75–0.95 are one engine with nine names; seven
intraday engines are two families. Consolidation costs no expectancy, removes
two-thirds of the tuning surface, and roughly triples per-engine sample rate —
which is what finally makes change #3's learning loop capable of a verdict.
**Deleting engines is a positive-CAGR act at this scale**, and the committee
expects this to be the hardest of the five to accept.

---

## 7. Frozen Implementation Plan

Architecture is frozen. What follows is execution sequence for one developer
over twelve months. No package may be reordered; each gates the next.

---

### WP0 — Survival infrastructure
**Objective:** Prevent total-system stop from the storage ceiling; establish
one-action rollback.
**Business value:** Read-only mode halts signal generation entirely — this is
the only package whose absence is a 100% loss.
**Deliverables:** Scheduled roll-off of historical price data; health check
that *fails* at 80% of ceiling with projected breach date; weekly backup;
config-first rollback covering every switch.
**Acceptance:** Current usage reported with projected ceiling date; roll-off
executed once with counts logged; health check demonstrated failing.
**Validation:** Deliberately breach the threshold in test; confirm FAIL.
**Rollback:** Additive only — nothing to roll back.
**Deployment:** Roll-off as final non-fatal step of the evening batch.
**Dependencies:** None.
**Risks:** If already near ceiling, the maintenance itself may fail — measure
first.
**Time:** 1 week. **DoD:** Ceiling date known and receding; backup verified
restorable.

---

### WP-A — Cost and position-size rationalisation
**Objective:** Make the unit economics of a single trade positive.
**Business value:** +3 to +6pp CAGR. Largest single lever in the programme.
**Deliverables:** Full friction ledger per product at realistic clip sizes
including flat charges; revised position-size policy (fewer, larger positions
within unchanged total risk); minimum-viable-trade threshold below which no
trade is taken; product-mix guidance.
**Acceptance:** Every historical closed trade re-scored against true friction;
the resulting expectancy table is the reference document for all later work.
**Validation:** Reconcile modelled friction against actual charges on ≥20 real
round trips. **Modelled must match realised within 10% or the ledger is wrong.**
**Rollback:** Position-size policy is configuration; revert instantly.
**Deployment:** Policy change only, no new signal logic.
**Dependencies:** WP0.
**Risks:** Fewer positions means higher single-name concentration — cap
exposure per name explicitly.
**Time:** 2 weeks. **DoD:** A written expectancy ledger exists showing net R
per trade by product and clip size. No such document exists today.

---

### WP-B — Capture-ratio remediation
**Objective:** Convert momentum entries into momentum exits.
**Business value:** +3 to +5pp CAGR. Second-largest lever.
**Deliverables:** Verified runner-conversion firing (instrumented at every
target touch); exits re-based on empirical R distribution per class; excursion
recorded on every position from entry.
**Acceptance:** Median capture ratio measured before and after across ≥30
closed positions. **Target: 3% → 30%+.**
**Validation:** Instrumentation proves the runner path evaluates on every
target touch and records why it ran or exited.
**Rollback:** Runner logic behind a switch defaulting to prior behaviour.
**Deployment:** Swing book only; intraday square-off constraints unchanged.
**Dependencies:** WP-A (R is meaningless until friction is priced).
**Risks:** Wider trailing increases per-trade variance and shallow drawdowns —
expected and acceptable; the right tail pays for it.
**Time:** 3 weeks. **DoD:** Capture ratio measurably improved on live trades,
not simulation.

---

### WP-C — Measurement spine (merged WP1 + WP6)
**Objective:** Make every decision and every non-decision scoreable, on unbiased
data.
**Business value:** Gates all learning; without it every later verdict is
selection-biased.
**Deliverables:** Entry rank and R carried through to closed records; every
proposal — taken, deferred, declined — recorded; **priors sourced from
signal-level and detection-level forward outcomes, not executed trades**;
review scoring taken vs. rejected.
**Acceptance:** Ranking effectiveness produces a real verdict for the first
time; prior estimates carry stated sample sizes and standard errors.
**Validation:** Confirm priors reproduce from the full field, not the traded
subset — the red-team C1 fix.
**Rollback:** Recording only; nothing to revert.
**Deployment:** Additive.
**Dependencies:** WP0.
**Risks:** Tempting to act on early priors — enforce minimum sample floors.
**Time:** 3 weeks. **DoD:** Every prior in the system traceable to an unbiased
denominator with a stated n.

---

### WP-D — Decision-input integrity (merged WP2 + WP2.5)
**Objective:** Ensure decisions rest on current, true inputs.
**Business value:** +0.5–1pp; prevents confident decisions on stale data.
**Deliverables:** Live quote fields replacing five-minute-stale values;
staleness guard with fallback at every consumer.
**Acceptance:** Context age at decision time logged and bounded.
**Validation:** Simulate a dead feed; confirm fallback engages rather than
silently serving stale values.
**Rollback:** Feed mode is configuration.
**Deployment:** Paper first, one full week.
**Dependencies:** WP-C.
**Risks:** Undead feed serving stale values is the failure mode — the guard is
the deliverable, not the upgrade.
**Time:** 2 weeks. **DoD:** No decision executes on data of unknown age.

---

### WP-E — Engine consolidation, 16 → 5
**Objective:** Remove correlated duplication; raise per-engine sample rate.
**Business value:** +0.5–1pp directly; unlocks WP-F's statistical validity.
**Deliverables:** Nine screeners → two (continuation; accumulation-confirmed).
Seven intraday → two families (ORB absorbing GAP/PDL; VWR absorbing PBK). VCE
and RNG retired. All retirements quarantined, not deleted, pending a quarter of
comparative evidence.
**Acceptance:** Detection rate per surviving engine ≥3× prior; no measurable
expectancy loss over the comparison quarter.
**Validation:** Shadow the retired engines for one quarter; if any shows
independent edge, it returns. **The burden of proof is on retention.**
**Rollback:** Quarantined engines restorable by configuration.
**Deployment:** Consolidate in paper, one book at a time.
**Dependencies:** WP-C.
**Risks:** Loss of a genuinely independent signal — mitigated by the shadow
quarter.
**Time:** 4 weeks. **DoD:** Five engines running; each with enough detections
per month to support a monthly verdict.

---

### WP-F — Adaptation cadence freeze
**Objective:** Stop fitting noise.
**Business value:** +2–4pp preserved (decay prevention).
**Deliverables:** Quarterly freeze per component; all learning on
detection/signal-level outcomes; out-of-sample confirmation window mandatory
before any parameter change; auto-apply authority removed entirely.
**Acceptance:** No parameter changes between freeze dates. Proposals accumulate
and are decided in batch.
**Validation:** Audit that no live parameter moved inside a freeze window.
**Rollback:** Cadence is policy.
**Deployment:** Immediate on adoption.
**Dependencies:** WP-E (consolidation is what makes quarterly samples
sufficient).
**Risks:** Feels like inaction during drawdowns. **This is the intended
behaviour and the hardest discipline in the plan.**
**Time:** 1 week. **DoD:** A written freeze calendar exists and has been
honoured through one full cycle.

---

### WP-G — New alpha: N1 Post-Earnings Drift, N2 Accumulation Footprint
**Objective:** Introduce genuinely decorrelated alpha.
**Business value:** +2–4pp; correlation ~0.2 to the existing book.
**Deliverables:** N1 (results-day gap + delivery confirmation + drift exit);
N2 (delivery persistence + block-deal confirmation as primary sort).
**Acceptance:** Each shows independent expectancy over ≥1 quarter of
detection-level evidence before receiving capital.
**Validation:** Detection-level shadow first; capital only on evidence.
**Rollback:** Each engine independently switchable.
**Deployment:** Paper → smallest viable size → normal.
**Dependencies:** WP-C, WP-E, WP-F.
**Risks:** Block-deal data ingestion is the only new external dependency in the
plan.
**Time:** 8 weeks. **DoD:** Both live with measured, independent expectancy.

---

### WP-H — Structural overlays: N3 expiry, N8 vol-scaling, N9 liquidity avoidance
**Objective:** Cheap structural edge and left-tail removal.
**Business value:** +0.5–1.5pp CAGR, disproportionate drawdown benefit.
**Deliverables:** Day-type conditioning of intraday priors; book-level exposure
scaling inverse to volatility regime; systematic exclusion of names that cannot
be exited at plan.
**Acceptance:** Expiry-day outcomes measurably differ from normal-day and are
priced accordingly; realised left-tail events reduced.
**Validation:** Compare pre/post distribution tails over ≥1 quarter.
**Rollback:** All three are configuration overlays.
**Deployment:** Sequential, one per fortnight.
**Dependencies:** WP-E.
**Risks:** Vol-scaling reduces exposure into recoveries — accept; the mandate
is risk-adjusted return.
**Time:** 5 weeks. **DoD:** All three live; drawdown profile measurably
improved.

---

### WP-I — Allocator and operator dashboard (formerly WP3 + WP5 + WP7)
**Objective:** Optimise which of several positive-expectancy trades receives
scarce capital.
**Business value:** +0.5–1.5pp — **conditional on WP-A and WP-B having made
per-trade expectancy positive.**
**Deliverables:** Common scoring currency using empirical R distributions (not
binary); opportunity-cost hurdle with two-bucket regime conditioning; every
entry path routed through or counted by the allocator; verdicts buffered and
flushed off the hot path; dashboard exposing allocation decisions, hurdle,
storage and shadow comparison.
**Acceptance:** **≥30 disagreements** with the greedy baseline, with
counterfactual R difference and dispersion reported. Not sessions —
disagreements.
**Validation:** Shadow until the disagreement count is met — expect months.
Counterfactual fills haircut by measured execution shortfall.
**Rollback:** Config switch; greedy resumes next cycle.
**Deployment:** Shadow → intraday paper → swing, each gated on evidence.
**Dependencies:** WP-A, WP-B, WP-C, WP-D, WP-E, WP-F.
**Risks:** Promoting on an insufficient disagreement sample — the single
likeliest way this package destroys value.
**Time:** 8 weeks. **DoD:** Allocator demonstrably beats greedy on ≥30
disagreements, or is retired with the evidence recorded.

---

### Phase 2 — Year 2, not scheduled here
N4 index flow · N5 large-cap reversion · N6 gap separation by cause · N7 sector
rotation as signal · N10 intraday short mirror. All are positive expected
value; none competes with the year-one sequence for a single developer's time.

---

## 8. Final Investment Committee Decision

The system as it stands is not fundable, and the reasons are not the ones its
author would expect. The engineering is careful, the risk plumbing is genuinely
strong, and the measurement instincts — recording every rejection, resolving
every detection — are better than most institutional systems this committee has
reviewed. What is missing is not sophistication. It is arithmetic: a trade that
cannot clear its own costs, an exit that surrenders the move it was built to
capture, sixteen engines making one bet, and a learning loop running ten times
faster than it learns.

Every one of those is fixable by a single developer inside twelve months, and
none requires a new idea. That is why this is a conditional decision rather
than a rejection.

**GO** — conditional on the five mandatory changes in §6 completing before any
work begins on the allocator programme.

The condition is not procedural. The five changes are ordered so that each one
makes the next measurable: fixing unit economics makes R meaningful; fixing R
makes exit quality measurable; slowing adaptation makes any measurement
statistically valid; validating the conviction layer removes the one unpriced
component sitting above all the others; consolidating engines raises sample
rates enough that the whole apparatus can finally render verdicts instead of
opinions. Executed in that order, the system moves from an estimated 15–25%
probability of positive net edge to something the committee would place at
35–45% — which, for a single-operator systematic programme in Indian equities,
is a respectable place to stand.

Executed out of order, or with the allocator built first, the outcome is a
highly optimised allocation of unprofitable trades, measured precisely, learned
from weekly, and wrong. The capital at risk in year one is small. The decision
that determines whether this becomes a durable programme or an expensive
hobby is the sequencing, and it is being made now.

The committee's conviction rests on one asset the author may undervalue: this
system already records what it rejected and resolves what it never traded. That
is the foundation every durable quantitative programme is built on, and almost
nobody at this scale has it. Fix the arithmetic, slow the clock, and the
measurement spine will tell you the truth about your own edge within a year —
which is more than most funds can claim.
