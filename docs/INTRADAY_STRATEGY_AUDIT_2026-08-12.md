# TradeOS — Intraday Strategy Authenticity Audit

**Scope:** the intraday framework only (candidate generation → engines → gates →
scoring → allocator → entry readiness → risk/exits). Swing is out of scope
except where it touches intraday. **Date:** 12-Aug-2026. **Method:** read the
implementation (not only the docs), cross-check against the observed logs /
daily reviews, and run the project's own offline logic suite
(`python -m tools.verify`) as the baseline.

**Baseline established:** `tools.verify` runs **422 checks, all intraday-logic
checks green** (the only 3 failures are environmental — `python-telegram-bot`
absent in this container — and unrelated to strategy). The intraday code is in a
verified state, and a great deal of it was hardened *today* (12-Aug) in a sweep
of one-sided-bound / entry-inside-invalidation defects.

---

## 1. Executive Verdict

> **TradeOS's intraday framework is strategy-driven, not indicator/score-driven —
> decisively so.** It is closer to a disciplined discretionary intraday trader
> encoded in rules than to an indicator-confluence scorer, and it is unusually
> well instrumented for one.

The evidence for that verdict, concretely:

- **Every engine is a *structural* setup, not an indicator blend.** They reason
  about levels the market actually defends — the opening range, the previous-day
  high, VWAP, a proven range's edges, a volatility coil — with volume as
  confirmation. There is no RSI/MACD/stochastic stack anywhere in the intraday
  path. "Indicator soup" (§10 of the brief) simply is not the failure mode here.
- **Hard requirements are hard, and the soft score cannot buy past them.**
  Setup validity, shortability (solvency), cross-framework exclusivity, event
  block, market-structure sequence, cost/keep-ratio and liquidity are all
  `return None` / `continue` gates. The engine's `confidence` only affects
  *ranking*, corroboration and the conviction floor — it can never rescue a
  failed hard gate (§11, the brief's highest-priority risk, is not present).
- **Setup quality and entry quality are separated inside every engine** — and
  the separation was the subject of today's fixes (confirmation-floor lower
  bounds, chase caps, entry-inside-own-invalidation checks). See §6.
- **The allocator is an opportunity-cost layer, not a validity manufacturer.**
  It cannot import `execution` (enforced by `tools.health`), fails open, and runs
  *after* every safety gate. It cannot make a bad setup tradeable (§15).
- **Regime and time-of-day are first-class**, via `market_context` (index gate,
  per-direction) and `session` phases + a budget-aware conviction floor (§7, §8).

The one honest caveat: several engines are **paper-only and measured-negative**
(VCE, RNG; and MOM/RVS on the swing side) and were promoted *deliberately* on a
paper book rather than on a positive expectancy. That is a known, tracked bet —
not a hidden weakness — and the allocator's cost-netted hurdle still has to be
cleared per-candidate.

---

## 2. Existing Intraday Strategy Inventory

Nine engines, grouped into six scored **families** (`registry.FAMILIES`). Merging
counts correlated detections under one identity without deleting any — the
sub-engine that fired is retained in `meta.sub_engine`.

| Family | Engines | Direction | Lifecycle (per blueprint) |
|---|---|---|---|
| ORB | ORB, GAP, PDL | LONG | ACTIVE |
| VWR | VWR, PBK | LONG | ACTIVE |
| VCE | VCE | LONG | ACTIVE (paper, promoted on paper) |
| RNG | RNG | LONG | ACTIVE (paper, promoted on paper) |
| SDN | SDN (VWR-reject / TRP / ORB-breakdown) | SHORT | SHADOW/ACTIVE per operator switch |
| GDB | GDB | LONG | SHADOW (from `brain_proposals#190`) |

Coverage is deliberate: **three engines that pay when a level breaks, one when a
trend continues, two when nothing breaks at all, one short family, one
recovery.** The "nothing breaks" pair (RNG, VWR) matters — most sessions do not
trend, and a book of only breakout engines is idle on the majority of days.

---

## 3. Engine → Practical Strategy Mapping

Each engine translated into a concrete, recognisable trading hypothesis (not a
generic label):

| Engine | The actual strategy it implements |
|---|---|
| **ORB** | Opening-range breakout continuation in PRIME, gated on volume expansion, range-sanity vs ATR, a **confirmed** break (min-break floor), a not-yet-chased entry, and above-PDH (not into overhead supply). |
| **GAP** | Overnight repricing that *holds* — gap sized between noise and exhaustion (ATR-scaled), above the opening-range low, not given back and **not already extended past the open**, on volume. |
| **PDL** | Previous-day-high **break-and-retest** — old resistance must become support and hold, with entry lifted enough off the level that the thesis has more room than the round trip costs. |
| **VCE** | Volatility contraction → expansion (a squeeze) — recent range compressed vs the prior window, then a **confirmed** release above the coil, on volume, targeting the measured move. |
| **PBK** | First controlled pullback to VWAP in a proven trend day, touch-counted (refuses the 3rd+ test), higher-low structure required; stop = losing VWAP = thesis death. |
| **VWR** | VWAP reclaim after a genuine flush below it — single-bar crossing, in a stock that is strong on the day; fade-and-reclaim, the drift-window mean-reversion trade. |
| **RNG** | Buy the low of a *proven* range (both edges rejected ≥2×), wide enough to clear costs edge-to-edge, on quiet volume; the complement to the breakout engines. |
| **SDN** | Short distribution in three shapes — VWAP rejection (institutional sell-programme footprint), the failed-breakout **trap** (forced sellers, no long equivalent), and opening-range breakdown; all require price under VWAP and prev close, and being *early* to the failed level. |
| **GDB** | Gap-down **bounce** (mean-reversion LONG) — reclaim of VWAP off a panic gap-down open; found by `discover_engines` as an uncovered population VWR explicitly refuses. |

---

## 4. Strategy Authenticity Assessment

Classification per the brief's rubric (A genuine / B incomplete / C
indicator-driven / D redundant / E incorrectly implemented). Each engine was run
through the authenticity checklist (regime, setup, bias, trigger, confirmation,
timing, invalidation, stop, target, R:R, time logic, exit, no-trade).

| Engine | Class | Notes |
|---|---|---|
| ORB | **A — Genuine** | All 13 components present. The retest *arm* of "retest OR strength" is explicitly unbuilt (documented), but the strength arm (min-break floor) now enforces confirmation, so the engine is complete, not incomplete. |
| GAP | **A — Genuine** | Both-sided gap sizing, hold test, extension guard added today. |
| PDL | **A — Genuine** | The rare retail engine that trades the *retest*, not the break; cost-aware room requirement is a genuine edge-preservation rule. |
| VCE | **A — Genuine** (paper) | Coherent squeeze logic; measured-negative so paper-only, but that is a lifecycle decision, not a logic defect. |
| PBK | **A — Genuine** | Trend-day signature + touch counting is textbook-correct; invalidation level now declared (was prose-only until today). |
| VWR | **A — Genuine** | Single-bar-crossing freshness + strength filter; the drift-window workhorse. |
| RNG | **A — Genuine** (paper) | Two-sided touch confirmation is what stops it buying a stepwise decline; invalidation end-of-range fixed today (it had never completed a trade before). |
| SDN | **A — Genuine** | The trap condition genuinely has no long mirror; "being early is the edge" is correctly enforced by the chase cap. |
| GDB | **A — Genuine** (shadow) | Evidence-derived, reuses VWR's calibrated reclaim logic pointed at the population VWR refuses. |

**No engine is C (indicator-driven), D (redundant) or E (incorrectly
implemented).** Redundancy that *did* exist — ORB/GAP/PDL treated as three
independent votes — was already neutralised: they are merged into one family and
corroboration is **cross-family only**, so three readings of one opening
structure no longer manufacture conviction (`registry.evaluate_all`).

---

## 5. Allocator Assessment

**Verdict: the allocator selects genuine strategy-qualified trades; it does not
take the highest score regardless of setup.**

- It runs **after** every hard gate and **fails open** (`allocator_permits`) — it
  is an opportunity-cost optimiser on top of gates that already said yes, and its
  absence can never *create* a trade.
- It **cannot import `execution`** (structural prohibition, asserted by
  `tools.health`), so a wrong verdict is inert.
- Proposals are scored on one cost-netted `edge` (`scoring.score`), priors are
  **per-engine, gross, gate-passed (TAKEN-only), direction-segmented and
  de-duplicated** — every one of those a fix for a specific way the prior
  otherwise inverts the learning loop (all documented, all with verify tests).
- The bar (`hurdle`) is a **percentile of the live arrival population**,
  segmented STRONG/WEAK by regime, rising with time and scarcity, permissive on a
  cold start, and — since migration 057 — **floored at 0 expected R** so it can
  never admit a proposal whose expected R does not cover its own round trip.
- **Slots are per-book**, a `_basket_recheck` enforces the sector cap on
  simultaneous selections, and DEFER has a real lifecycle.

The allocator is, if anything, the most-audited component in the system.

---

## 6. Entry-Quality Assessment (setup quality vs entry quality)

**The distinction is present and was the explicit target of today's sweep.** For
every engine, "is there a valid setup?" is separated from "is *this price/moment*
a good entry?":

- **Overextension / late entry** — every breakout engine caps the chase
  (`orb_max_chase_pct`, `vce_max_chase_pct`, `gap_max_extension_pct`,
  `intraday_short_max_chase_pct`); reclaim engines cap extension past VWAP.
- **Under-confirmation** — the mirror bound was missing and is now enforced:
  `base.confirmation_pct()` gives every level-break a **minimum** break distance
  derived from the *same* config key the exit reads (`intraday_invalidation_
  buffer_pct`), so an entry can no longer open inside its own kill zone. This
  closed a real bleed: on 12-Aug, breaks of 0.02–0.19% over a level were entered
  and cut `SETUP_INVALIDATED` within minutes.
- **Entry vs its own invalidation** — `registry._invalidation_is_reachable()`
  refuses any setup whose stop sits beyond the level it calls its own thesis
  death (NATIONALUM: a stop 5% away from a "gap given back" invalidation).
- **Distance from anchor / off-high** — PBK requires being genuinely *off* the
  high and near VWAP; PDL requires the retest to be near the level; RNG requires
  price *at* the low.

**Setup quality and entry quality are sufficiently separated.** This is a
strength, not a gap.

---

## 7. Market-Regime Assessment

- **`market_context.classify()`** gates the whole book on the index alone,
  **per direction**: RISK_ON (longs, full/neutral size), NEUTRAL (longs reduced),
  CAUTION (minimal, both sides small), RISK_OFF (**no new longs**, shorts with
  the tape). Missing index data → NEUTRAL, never RISK_ON. Shorts default to
  *refuse* unless weakness is actively confirmed — asymmetric on purpose (upper-
  circuit lock has no cover price).
- **`market_structure.gate_for_framework()`** adds a pivot-sequence gate so an
  engine reasoning about a single static level cannot buy a lower high in a
  downtrend.
- **Per-engine regime *fit*** (`regime_fit_multiplier`) exists and correctly
  classifies engines momentum vs mean-reversion — but is **shipped inert**
  (weight 0.0) pending `regime_at_detection` data, mirroring the swing side's
  `rank_weight_tier` precedent. This is a deliberate, well-reasoned deferral, not
  a gap. **(One completeness defect found and fixed here — see §11.)**

Regime handling is appropriate and layered. The one thing the index gate does
**not** yet read is **market breadth** (advance/decline) — flagged in §10/§11 as
a candidate, not implemented.

---

## 8. Time-of-Day Assessment

- **Session phases are structural** (`session.py`): OPENING is input-only, PRIME
  is the quality window, DRIFT switches breakout engines off (only VCE and mean-
  reversion run), AFTERNOON, then no new entries near the close. Engines **declare
  their valid phases** and the registry enforces them (SDN's phase tuple was
  corrected today to name real phases and is now enforced like the other eight).
- **Runway floors** — no entry with < `intraday_min_runway_min` (45) to square-
  off; shorts additionally gated on a **cover deadline** read from the *same* keys
  the exit uses, and tightened as that deadline approaches.
- **Budget-aware conviction floor** — the confidence floor rises from
  `intraday_min_confidence` (0.55) to `_scarce` (0.80) as the day's entry budget
  is spent, so "good enough at 09:20" is not "good enough at 14:30". (The floor's
  denominator bug — scaling against the broker order cap instead of the entry cap
  — was fixed today.)

Time-of-day is handled well and was actively corrected today.

---

## 9. Loss / Failure-Mode Classification (observed evidence)

From the **12-Aug daily review** (intraday's worst session of five: 20% win
rate, −₹580 net, 10 trades):

- **7 of 8 losses exited `SETUP_INVALIDATED`** across four LONG-side
  continuation/breakout engines (VWR, ORB×3, VCE×2). **None broke its risk plan**
  — all were small, plan-sized losses (−0.15R to −0.72R), i.e. the invalidation
  cut working *as designed*, cutting before the stop.
- Classification (per the brief's taxonomy): **wrong-regime**, not bad-strategy /
  bad-entry / execution. The governing regime score was at its lowest of five
  sessions (48), breadth 0.67 (decliners leading), Nifty −0.46%. Long
  continuation theses break shortly after entry on a weak-breadth down day
  *without* tripping a hard stop — which matches the loss shape precisely.
- **Do not over-fit (brief §23).** One session's cluster, fully explained by
  regime, is **not** grounds to touch any engine's invalidation logic. The daily
  review reached the same conclusion independently. The structural observation it
  raises — the index gate reads price but not breadth — is logged as a
  *hypothesis to measure*, not a change to ship.

**SDN** produced 91 setups and 0 trades, dominated by `BLOCKED_SHORTS_MARKET`
(the regime gate). Verified as **correct rejection**: a Nifty-weak day is not
automatically name-by-name weak enough to clear the short engine's own gates, and
the market context did not confirm intraday weakness (index mostly above its own
VWAP / prev close during the session).

---

## 10. Top Strategy Gaps (ranked)

1. **[MEDIUM/HIGH — flagged, not implemented] Index gate is price-only, not
   breadth-aware.** The clearest recurring theme in the reviews. Adding advance/
   decline (or % of universe above VWAP) to `market_context` would let the book
   stand down long-continuation engines on weak-breadth down days. Requires a
   breadth data source and calibration → **needs approval + evidence.**
2. **[MEDIUM — flagged] `regime_fit` nudge table is long-momentum-shaped; SDN
   (short) gets inverted nudges.** SDN is classified MOMENTUM and would be
   *penalised* in NEUTRAL/CAUTION and given *zero* help in RISK_OFF — the exact
   regimes it is built for. Correct handling needs the nudge to be
   direction-aware. The feature is inert, so this is latent; fixing it properly
   is a design change to an unvalidated feature → **flag, do not ship on
   cleverness** (the module's own stated discipline).
3. **[MEDIUM — flagged] ORB retest arm unbuilt.** ORB documents "retest OR
   strength"; only the strength arm exists. A genuine second entry path, but a
   new-logic addition → propose separately.
4. **[LOW — IMPLEMENTED] GDB missing from the engine-archetype map.** Fixed here
   (see §11).

No gap rises to "add a new strategy family" — the brief's §7 warning against
adding strategies for completeness is respected. Coverage is already broad
(break / trend / range / short / recovery).

---

## 11. Minimal Change Plan — and what was implemented

### IMPLEMENTED (LOW)

**File:** `backend/allocation/scoring.py` (`ENGINE_ARCHETYPE`) and
`backend/tests/test_regime_fit.py`.

```
PROBLEM
  regime_fit_multiplier() is keyed by p.source, and from_intraday() sets
  p.source to the engine FAMILY. The families that actually reach the lookup are
  {ORB, VWR, VCE, RNG, SDN, GDB}. ENGINE_ARCHETYPE classified the eight SUB-ENGINE
  names but was missing GDB — the one family with no archetype. (GAP/PDL/PBK in
  the map are sub-engines merged into other families and are never looked up.)

EVIDENCE
  A direct probe: set(FAMILIES.values()) − ENGINE_ARCHETYPE.keys() == {'GDB'}.
  regime_fit_multiplier('GDB', ...) returns (1.0, 'GDB unclassified — no opinion').

CURRENT BEHAVIOUR
  Inert today (weight 0.0, and GDB is SHADOW so no GDB proposal reaches the
  allocator). But the day GDB is promoted AND the weight is raised, GDB — a
  mean-reversion bounce — would silently score as "no opinion" instead of getting
  the mean-reversion nudge, i.e. it would be treated as favourable in RISK_ON,
  the opposite of the truth. Exactly the "future regime_fit_report() run silently
  drops them" failure this module's own test docstring already names.

REAL-WORLD PRINCIPLE
  A gap-down bounce is mean-reversion (buy the recovery, not the gap) — its own
  module docstring says so explicitly. Its regime preference is the opposite of a
  breakout's.

PROPOSED MINIMAL CHANGE
  Add one row: "GDB": MEAN_REVERSION. Strengthen the test to assert through the
  CONSUMER'S lookup (every registry.FAMILIES value must be classified), which is
  the drift-proof check that would have caught GDB and will catch the next family.

WHY SUFFICIENT
  GDB is a single-engine family with no archetype ambiguity (unlike the pooled
  VWR family). One row closes the gap completely.

EXPECTED BENEFIT
  Correct regime handling for GDB the moment it is promoted / the weight raised.
  Zero behaviour change today (proven byte-identical at weight 0).

SIDE EFFECTS
  None today. Verified: score() output is byte-identical with/without
  engine_family='GDB' at weight 0.

VALIDATION
  python -m tools.verify --module regime_fit → all 18 checks pass (was 16).
  The new drift-proof guard demonstrably FAILS when the GDB row is removed
  (proved). Full suite: 422 checks, only the 3 pre-existing telegram-env
  failures remain — no regressions.

CHANGE LEVEL: LOW
```

### FLAGGED — NOT implemented (require approval / evidence)

| Gap | Level | Why not now |
|---|---|---|
| Breadth in `market_context` | MEDIUM/HIGH | New data source + calibration; brief §23 forbids over-fitting to one weak-breadth session |
| Direction-aware `regime_fit` nudges for SDN | MEDIUM | Redesign of an inert, unvalidated feature — "do not ship on cleverness" |
| ORB retest entry arm | MEDIUM | New logic path; propose separately |

---

## 12. Architecture Preservation Confirmation

> **The change implemented can be, and was, made within the existing TradeOS
> architecture.** It is a single data-row addition to an existing classification
> table plus two test assertions. No engine framework, allocator framework,
> interface, schema, config contract, data flow, scheduler behaviour or logging
> contract was changed. No new module, subsystem or abstraction was introduced.
> No swing behaviour was touched.
>
> **No architectural change was required or made.** The larger opportunities (§10)
> are listed separately for approval and were **not** implemented.

---

## Final Implementation Report (brief §27)

- **Changes made:** `ENGINE_ARCHETYPE` gains `"GDB": MEAN_REVERSION`
  (`allocation/scoring.py`); `test_regime_fit.py` gains a drift-proof
  "every family that reaches the lookup is classified" guard and a GDB-specific
  assertion, both registered in the module's `TESTS`.
- **Strategy improvement:** the regime-fit layer now classifies **every** engine
  family that can reach it, so GDB will be treated as the mean-reversion engine it
  is once promoted — not silently as "no opinion."
- **Allocator improvement:** none needed — the allocator already selects genuine
  strategy-qualified trades and cannot manufacture validity.
- **Entry improvement:** none in this change; the entry-quality bounds were
  already corrected in today's engine sweep.
- **Risk improvement:** removes a latent regime/engine mismatch that would have
  surfaced on GDB's promotion.
- **Tests:** `tools.verify --module regime_fit` → 18/18. Full suite → 422 checks,
  only 3 pre-existing environmental (telegram) failures. New guard proven able to
  fail.
- **Validation:** behaviour proven byte-identical today (weight 0, GDB shadow).
- **Remaining gaps:** breadth-aware regime, direction-aware regime-fit for
  shorts, ORB retest arm — all flagged for approval, none implemented.
- **Architecture:** unchanged.

### Final question (brief)

> *"Within TradeOS's existing architecture, have we made the engines and
> allocators behave more like practical, disciplined intraday traders?"*

The engines and allocator **already** behave like a disciplined intraday trader —
coherent structural setups, real market context, confirmed entries that cannot
open inside their own invalidation, sensible rejection conditions, and
strategy-consistent risk. This audit **confirms** that (the honest and most
important finding), corrects **one latent completeness gap** without changing any
behaviour today, and hands the operator a ranked, evidence-gated list of the
genuinely larger opportunities rather than shipping any of them on cleverness.
