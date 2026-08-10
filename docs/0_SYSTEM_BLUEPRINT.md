# TradeOS — system blueprint

**Read this first.** One page (a dense one) for the whole machine: what runs,
what it is guarded by, and how the pieces are wired to each other. Depth lives
elsewhere and this document points to it rather than repeating it —
`CLAUDE.md` for the hard-earned rules, `USER_GUIDE.md` for day-to-day
behaviour, `DESIGN_NOTES.md` for what is decided but unbuilt,
`TERMINOLOGY.md` for every regime/state word, `6_IMPLEMENTATION_STATUS.md`
for the append-only history of every defect found and fixed.

---

## 1. What this is, in one paragraph

A single-operator system trading NSE cash equities on one account, split into
two frameworks that share nothing except the ticker feed and a few whole-
account safety limits. Its defining asset is not signal quality — it is
**measurement**: every proposal a framework rejects is recorded and its
outcome resolved anyway, so the system knows what it *didn't* do as precisely
as what it did. Almost no retail-scale system has that property, and most of
this codebase's discipline exists to protect it.

---

## 2. The two frameworks, side by side

| | Swing | Intraday |
|---|---|---|
| Horizon | 1–3 weeks | flat by 15:15 IST |
| Mode | **LIVE** — real orders | **PAPER** — simulated fills, real decision logic |
| Product | CNC (never MIS) | MIS |
| Capital sleeve | `swing_capital` (`config.capital_for("SWING")`) | `intraday_capital` (`config.capital_for("INTRADAY")`) |
| Universe | ~60 plans from the evening pipeline | own 40-symbol scanner, own criteria |
| Entry decided by | `analysis/trade_decision.py::decide()` | `intraday/engine.py::evaluate_intraday_setups()` |
| Exit decided by | `control/position_lifecycle.py` | `intraday/exit_policy.py` |
| Direction | LONG only (cash delivery) | LONG and SHORT |
| Daily entry cap | `swing_max_new_per_day` (2) | `intraday_max_new_per_day` (4) |

They are deliberately **not unified**. One exit policy applied to both is how
a 15-session time stop once ended up on a trade that had to be flat by 15:20.
`analysis/market_structure.py` is the one piece of logic genuinely shared
between them, because a pivot sequence means the same thing on a daily chart
and a 5-minute one — everything else that looks similar (regime vocabulary,
structure states, cost models) is deliberately kept separate. See
`TERMINOLOGY.md` for the four vocabularies that use overlapping words for
different things.

**One symbol, one book.** Whichever framework reaches a name first owns it
until it closes (`_other_framework_holding`, both directions, switch
`one_framework_per_symbol`). Migration 028 made two rows for one symbol
*storable* — that is a schema guarantee, not a trading policy — and holding
both would mean an intraday square-off selling into a multi-week swing thesis.

---

## 3. Process flow

```
EVENING  (GitHub Actions, 27 steps, run_pipeline.py)
  ingest bhavcopy / chartink / FII-DII / events
    → compute_indicators (86 cols on stock_data_daily)
    → sector_strength → compute_regime (5-state swing regime, hysteresis)
    → screen_stocks    (9 engines + PEAD/ACC → master_shortlist)
    → compute_msl      (entry zones, final_score)
    → quality_gate → signals → ai_decision_engine (tier, conviction)
    → signal_output_daily   ← 114 columns, the day's plans, IMMUTABLE

MARKET HOURS  (intraday/run.py, one process, BOTH books)
  KiteTicker websocket, MODE_LTP/QUOTE, ~95 symbols
    every 15s:  evaluate_positions  → route by framework → exit ladder
                evaluate_candidates → decide() → swing entry if ranked
                evaluate_intraday_setups → 8 engines → gates → allocator
                                          → paper entry (long or short)
    every 300s: gtt_manager.sync (CNC only) · context refresh · allocator flush
    at close:   square_off_paper (covers shorts with a BUY) · outcomes.resolve_day

WEEKLY  (brain_sunday_chain.yml)
  weekly_review    engines, gates, ranking → brain_proposals
  discover_engines refused-but-right + moved-but-unseen → brain_proposals
```

**Decision reuse is load-bearing, not a style choice.** `decide()` and
`evaluate_exit()` are each called from the pipeline, the dashboard, Telegram
*and* the daemon. A second implementation anywhere is how this project once
had three divergent R:R models give three different answers for one stock on
one day. Never reimplement a decision function — import it.

---

## 4. Every engine, and the family it belongs to

Engines are graded and retired on evidence, not deleted — a `SHADOW` engine
still evaluates and records, it just receives no capital. Families exist
because thirty detections a quarter cannot settle whether an engine has an
edge and 230 can; the sub-engine that actually fired is kept in `meta` so a
family can be split again if the evidence ever demands it.

**Swing — `swing/signals/screen_stocks.py`, family `CONTINUATION` unless noted:**

| Engine | What it looks for |
|---|---|
| CTL | Core Trend Leaders — consolidation/trend-alignment breakout |
| SEC | Sector-led continuation |
| TPO | Trend Pullback Opportunities |
| SBS | Structural Breakout Swing — BB squeeze, delivery trend |
| VBD | Velocity Burst Detector — single-day 3–6% move, institutional confirm |
| RSB | Relative Strength Breakout — RS leader coiling near resistance |
| IAD | Institutional Accumulation — quiet delivery + RS + volume expansion |
| MOM | Momentum Continuation — own family, **ACTIVE** (promoted 07-Aug-2026) |
| RVS | Reversal Setup — SMA50 bounce, RSI turning up — own family, **ACTIVE** (promoted 07-Aug-2026) |
| PEAD | Post-earnings drift, delivery-confirmed — Stage 8, **ACTIVE** |
| ACC | Accumulation via delivery-% persistence — Stage 8, **ACTIVE** |

**MOM/RVS promotion, 07-Aug-2026 — read before trusting either at face value.**
Both were demoted to SHADOW on measured underperformance (n=4,747 detections,
`docs/6_IMPLEMENTATION_STATUS.md`'s win-rate table: RVS 42% win / **−0.49%**
average forward return — negative; MOM 50% / +0.05% — barely positive,
against TPO's 74%/+2.41%). The demotion never actually took effect: `strategy_
config` carries the lifecycle in TWO places — an outer column and a nested
`params.lifecycle` — and only the outer one was updated on 04-Aug.
`engine_registry.load_registry()` reads the nested one, so both engines had
been functionally ACTIVE the entire time regardless of the documented SHADOW
status. Promoted here to make that state deliberate and consistent on both
fields rather than silently accidental — an operator decision made WITH the
negative RVS evidence in view, not a data gap being closed. See
`engine_lifecycle_log` for the audit trail and `knowledge_base/KNOWLEDGE_
BASE.md` for the watch item.

**Intraday — `intraday/strategies/registry.py`:**

| Engine | Family | What it looks for |
|---|---|---|
| ORB | ORB | Opening-range breakout, first 15 min |
| GAP | ORB | Overnight repricing that holds |
| PDL | ORB | Prior-day level, break and retest |
| VCE | VCE | Compression releasing — **ACTIVE** (promoted 07-Aug-2026, paper-only) |
| PBK | VWR | First pullback in a trend day |
| VWR | VWR | Fade and reclaim of the day's VWAP |
| RNG | RNG | Low of a proven range — **ACTIVE** (promoted 07-Aug-2026, paper-only) |
| **SDN** | **SDN** | **SHORT-only.** VWAP rejection, the failed-breakout trap, range breakdown — `intraday_engine_sdn_lifecycle`, currently `SHADOW`/`ACTIVE` per the operator's own switch |

**VCE/RNG promotion, 07-Aug-2026.** Also measured-negative (E[R] −0.94% and
−1.38% respectively) — promoted anyway, deliberately, on the grounds that
intraday is paper-only (`intraday_live_auto_entry` has no implementation, so
zero real-capital risk) and that `alloc_live_intraday` is now live, so a weak
individual VCE/RNG candidate still has to clear the same cost-netted edge
hurdle as everything else rather than being taken on lifecycle alone.
Single-source config keys (`intraday_engine_vce_lifecycle`,
`intraday_engine_rng_lifecycle`) — no dual-field issue like MOM/RVS had.

Structural overlays (`analysis/overlays.py`) sit above all of these and can
only ever shrink or refuse a trade, never invent one: expiry day-type sizing,
VIX-based exposure scaling, and the liquidity/circuit-band gate.

---

## 5. The direction spine — why shorting isn't "one more engine"

`intraday/direction.py` is the single definition of what LONG and SHORT mean
arithmetically (`sign()`, `risk_per_share()`, `gain_r()`, `is_better_price()`).
Every module that touches a position's P&L imports it rather than
re-deriving the sign — the exit ladder, the cost model, the allocator's
scorer and prior lookup, the outcome resolver, excursion tracking, and the
paper broker's entry/cover legs. `direction` defaults to `LONG` everywhere so
every pre-shorting caller keeps compiling — which is also *why* three
separate call sites were found forgetting to pass it explicitly, each one
silently mis-scoring a short as a long rather than crashing. `tools.health`'s
`shorts` check now greps the call sites, not just the function definitions,
and one assertion is a live schema probe rather than text matching.

**What has no long equivalent:** `intraday/shortability.py` is a *solvency*
filter, not a quality one — it asks whether a short can be **covered**, not
whether it is good. An upper-circuit lock has no price at which to buy back,
and an uncovered short goes to the exchange's auction at a penalty around 20%
of the trade value. Shorts therefore cover `intraday_short_cover_lead_min`
minutes before a long would exit, and `analysis/market_structure.py`'s
`gate_short` blocks a confirmed uptrend outright with no config override — a
short's version of a squeeze, not a trade.

---

## 6. The allocator — an opportunity-cost layer, not a safety gate

`allocation/allocator.py::select()` scores every proposal both books produce
this cycle on one common scale (`edge` = net-of-cost expected R per rupee-day,
`allocation/scoring.py`) and buffers a TAKE/DEFER/DECLINE verdict for each —
including the ones it would refuse, because "the allocator beat greedy" is a
claim about the trades it did *not* take. It **cannot import `execution`** —
enforced by `tools.health`'s `allocator` check via source inspection — so a
wrong verdict can be arbitrarily bad and it still cannot place an order.

`allocator_permits()` in `intraday/engine.py` is the one consumer that makes
`alloc_live_{intraday,swing}` mean what it says; both default `false`
(shadow-only). It **fails open**: a proposal the allocator never scored is
allowed through, because the allocator is an opportunity-cost optimiser on
top of gates that already said yes, not a safety control.

The bar it compares against — `allocation/hurdle.py::hurdle()` — rises with
time remaining and with slots running out, is segmented `STRONG`/`WEAK` by
regime (`regime_bucket()`, now direction- and vocabulary-correct after the
05-Aug fix — see `TERMINOLOGY.md`), and is permissive rather than punitive on
a cold start: an allocator with no history must be indistinguishable from no
allocator at all.

**It is also floored, since 10-Aug-2026 (migration 057).** A percentile is a
relative answer, and a percentile of an all-negative arrival population is
still negative — so the bar admitted proposals the scorer had itself measured
as losing, and got *more* permissive as the session ran out because the bar
decays toward its base. `alloc_edge_absolute_floor` (0.0) clamps the final bar
so a proposal whose expected R does not cover its own round trip can never
clear it, whatever else is arriving. Cold starts stay exempt: "no opinion" is
not the same claim as "measured bad". Two paired fixes landed with it — the
intraday prior is now built from gate-passed detections rather than from every
detection including the refused ones (`priors_intraday_taken_only`), and the
per-engine priors are reachable by the allocator for the first time (the dict
was keyed `"ORB"` while the lookup asked for `"INTRADAY/ORB"`, so every
proposal had been scored off one pooled book distribution). See `CLAUDE.md`'s
landmine list for the measured evidence behind each.

---

## 7. The guardrails

**Money moves only behind explicit switches, always two of them:**
`{framework}_auto_entry` (entries happen at all) **and**
`{framework}_live_auto_entry` (they may spend real money). Intraday live
auto-entry has never been implemented — the code path logs a warning and
stays manual, on principle, not by oversight. Shorting adds a third door on
top of the usual two: `intraday_allow_shorts` plus the engine's own
`intraday_engine_sdn_lifecycle`.

**`tools/health.py` — every check, one command, run before trusting a
session (`tradeos health`):**

| Check | Catches |
|---|---|
| config | a risk number contradicts another, or a switch nothing reads |
| shorts | a short taken while some module still does long-only arithmetic |
| governance | a parameter changing itself, or an unmeasured layer ranking trades |
| allocator | the allocator reaching an order path despite its switches |
| hurdle | a bar that can never be cleared, so the book goes quiet |
| books | one symbol held by both frameworks with contradictory exits |
| storage | the database refusing writes, pipeline going silent |
| feed | decisions on data of unknown age, or late ticks |
| exits | an exit that can sell without alerting, or fires from one caller only |
| costs | charges priced off a stale or wrong-product rate |
| selects | a query naming a column the schema no longer has |
| kite | no broker session, or the IP not allowlisted |
| data | decisions running on stale inputs |
| broker | resting orders that don't match the positions they protect |
| daemon | nothing currently watching open positions |
| pending | an entry order that never filled, tracked as a real position |
| learning | engines judged on evidence that was never collected |
| simulate | a pipeline stage completing while producing nothing (slow — skipped by `--quick`) |

The project's own rule, earned repeatedly: **a check that cannot fail is not
a check, and a check that cannot pass is the same defect wearing a different
hat.** Every check above has been demonstrated failing on a deliberately
broken input before being trusted to pass on a correct one — `check_selects`
is the clearest example, having once returned green while logging its own
failure underneath.

**Structural invariants, enforced in code rather than by convention:**
`open_positions` keyed on `(symbol, product)` since migration 028, never
`symbol` alone · reconcile reads day positions, not only holdings, and skips
PAPER rows · a fill is written back immediately, before the next cycle can
re-derive and re-place the same order · `config._force_ipv4()`, because
Kite's order allowlist is IPv4-only and a dual-stack host resolves IPv6 first
· PostgREST fails an entire write on one unknown column, so writers strip and
retry rather than lose a whole payload over one bad field.

---

## 8. Key tables — the system of record

| Table | What it is |
|---|---|
| `signal_output_daily` | the evening pipeline's immutable daily plans (114 cols) |
| `market_regime` | the swing regime, once/day, with hysteresis |
| `open_positions` | both books, keyed `(symbol, product)`, `direction` column since the shorting work |
| `closed_positions` | gross `realized_pnl` plus separate `charges` |
| `intraday_setups` | every detection, taken or not, with its verdict and resolved outcome |
| `allocation_decisions` | every allocator verdict including DECLINE, with `direction` and `regime_bucket` |
| `system_config` | every switch and tunable; `cfg()` is the only reader that matters |
| `brain_proposals` | weekly learning output — **written, never auto-applied** |

---

## 9. What is deliberately not built

`intraday_live_auto_entry` has no implementation behind it — committing real
capital on a single tick is the highest-variance action this system could
take, and that is not a decision to make by flipping a switch.
`alloc_live_swing` stays off until intraday's promotion has run clean for
several sessions — the allocator earns the real-money book last, not first.
Phase 5 (adaptive position sizing, recyclable capital) is scoped in
`5_PHASE5_VISION.md` and `7_PHASE5_READINESS.md` and explicitly **not**
started — anticipatory architecture for it is called out as the most likely
way the current phase fails, because a hook built for a future capability is
complexity paid for with no measurable edge today.

---

## 10. Where to go deeper

| Question | Document |
|---|---|
| "How do I run/operate this day to day?" | `USER_GUIDE.md` |
| "What's decided but not yet built?" | `DESIGN_NOTES.md` |
| "Does 'RISK OFF' here mean the same thing as over there?" | `TERMINOLOGY.md` |
| "What broke, and what was learned?" | `6_IMPLEMENTATION_STATUS.md` |
| "What was the original Phase 4 contract?" | `1_PHASE4_ARCHITECTURE.md` (frozen) |
| "What would Phase 5 add, and is it worth it?" | `5_PHASE5_VISION.md`, `7_PHASE5_READINESS.md` |
| "Session rules for working in this repo" | `../CLAUDE.md` |
