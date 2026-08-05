# Terminology — every "regime" or "state" word in this system, and who owns it

**Grep this file, don't read it whole if you already know which word you're
chasing.** It exists because "RISK OFF" and words like it are used by at least
four independent classifiers in this codebase, three of which overlap in
vocabulary while meaning different things, computed on different clocks, from
different inputs, stored in different places. A person reading two log lines
five minutes apart can reasonably believe they're reading about the same thing
when they are not.

One live bug was found and fixed by writing this document: `allocation/
hurdle.py::regime_bucket()` was written against system #1's vocabulary below,
but its only real caller has always fed it system #2's. See migration 047 and
§1/§2 for the detail. Everything else here is confirmed correct as of
05-Aug-2026, or flagged as a naming collision without a behavioural bug behind
it — read the "risk" column in the summary table before assuming a hit needs
fixing.

---

## Summary table

| # | System | Format | Cadence | Values | Storage | Consumers |
|---|---|---|---|---|---|---|
| 1 | [Swing regime](#1-swing-regime) | `RISK ON` (space) | once/day | 5 states | `market_regime` table | `generate_signals.py`, `send_alerts.py`, `trade_decision.py`, `allocation/hurdle.py` |
| 2 | [Intraday market context](#2-intraday-market-context) | `RISK_ON` (underscore) | every 15s | 4 states | not persisted, in-memory only | `intraday/engine.py`, `allocation/hurdle.py` (via `mc.state`) |
| 3 | [Sector event bias](#3-sector-event-bias) | `RISK_OFF` (underscore) | per event | open-ended | `sector_event_bias` column, per candidate | `intraday/news_gate.py`, `position_event_monitor.py` |
| 4 | [Market structure](#4-market-structure) | `UPTREND` etc | pivot-driven | 6 states | not persisted, computed on demand | `analysis/market_structure.py`, both frameworks |
| 5 | [Allocator regime bucket](#5-allocator-regime-bucket) | `STRONG` / `WEAK` | derived | 2 states | written to `allocation_decisions.regime_bucket` | `allocation/hurdle.py`, `allocation/allocator.py` |
| 6 | [Legacy / dead labels](#6-legacy--dead-labels) | mixed | — | `BULLISH`, `BEARISH`, old `CAUTION` | none — pre-hysteresis artifacts | should not appear in any live comparison |
| 7 | [Day type](#7-day-type-unambiguous) | `EXPIRY` etc | daily | calendar-only | not a regime at all | `analysis/overlays.py` only |
| 8 | [Free-text `weekly_structure`](#8-free-text-weekly_structure) | prose | per signal | not an enum | `signal_output_daily.weekly_structure` | `control/exit_rules.py` grading only |

---

## 1. Swing regime

**Defined:** `swing/compute/compute_regime.py::apply_hysteresis()`
**Written to:** `market_regime` table, column `regime` (and `predicted_regime`
from the ML classifier — see §6)
**Computed:** once per session, pipeline step [10.4], after `compute_indicators`
and before `screen_stocks`
**Format:** SPACE-separated, five states, in ascending order of favourability:

```
RISK OFF   RECOVERING   NEUTRAL   RISK ON   TRENDING
```

Built from a weighted score (price structure, breadth, momentum, VIX, FII/DII
flow, USD/INR direction, S&P 500 correlation) with a hysteresis engine so the
regime doesn't flap on a single noisy day — a downgrade to `RISK OFF` requires
an extreme score OR two consecutive weak days; recovery out of `RISK OFF`
requires the score to clear `NEUTRAL` while showing a genuine uptrend, not one
green candle.

**Who reads it, and why that's correct:**
- `swing/signals/generate_signals.py` — sizes the minimum R:R bar per regime
  (`min_rr_to_enter_TRENDING` through `min_rr_to_enter_RISK_OFF`), and can hard-
  block new buys in `RISK OFF` via `block_buys_risk_off`. Looked up dynamically
  as `T.get(f"min_rr_{regime_name}", ...)` — confirmed consistent, the dict keys
  and the f-string both use the space-separated form.
- `analysis/trade_decision.py` (via `regime=` parameter) and `alerts/
  send_alerts.py::decision_line()` — same string, passed through unmodified.
- `allocation/hurdle.py::regime_bucket()` — **this is where the bug was.** The
  function's matching logic was written for this vocabulary, but see §2 for
  what it actually receives.

**This is a daily, WHOLE-MARKET judgment.** It does not know what any one stock
is doing and it does not update intraday — a session that opens `RISK ON` stays
`RISK ON` until the next evening run, even if the index reverses hard by noon.
That gap is exactly why system #2 exists.

---

## 2. Intraday market context

**Defined:** `intraday/market_context.py::classify()`
**Written to:** nowhere — an in-memory `MarketContext` dataclass, rebuilt every
cycle. There is no historical table of intraday market states.
**Computed:** every 15 seconds, from the index alone (NIFTY 50 LTP vs its own
VWAP, previous close, and today's range)
**Format:** UNDERSCORE-separated, four states:

```
RISK_OFF   CAUTION   NEUTRAL   RISK_ON
```

Answers one question only: *is the index itself confirming strength or
weakness RIGHT NOW*, so that six intraday engines evaluating six different
stocks don't each independently discover the same market-wide move and call it
six unrelated "signals" — the correlation-in-selloffs problem the module's own
docstring names directly.

**Who reads it:**
- `intraday/engine.py::evaluate_intraday_setups()` — via `mc.allow_longs` /
  `mc.allow_shorts` (short side added on `claude/intraday-short-selling`) and
  `mc.size_multiplier`, gating and sizing every setup this cycle.
- `allocation/hurdle.py::regime_bucket()` — via `mc.state`, passed as
  `regime=mc.state` from `_allocate_shadow` (`intraday/engine.py:2453`). **This
  is the only live caller of `regime_bucket()` in the entire codebase.**

**The bug, precisely:** `regime_bucket()` checked `"RISK ON" in r.upper()`. The
string this system's only caller has ever supplied is `"RISK_ON"` — an
underscore, not a space. `"RISK ON" in "RISK_ON"` is `False` in Python (the
substring must match character-for-character; a space and an underscore are
different characters). So the `STRONG` branch was unreachable in every session
this system has ever run in production. Migration 044's regime segmentation for
the allocator's hurdle — the entire reason a bucket function exists — was
silently collapsing to one bucket. Fixed in migration 047: `regime_bucket()`
now names both vocabularies' values explicitly rather than pattern-matching a
substring across an assumed-shared format. Verified:

```python
>>> from allocation.hurdle import regime_bucket
>>> regime_bucket("RISK_ON")   # the actual production input
'STRONG'
>>> regime_bucket("TRENDING")  # swing's own strongest state
'STRONG'
```

`tools/health.py::check_allocator_hurdle` now asserts both of those directly
and was demonstrated failing against the pre-fix function.

---

## 3. Sector event bias

**Defined:** `ai/ai_decision_engine.py` (writes `sector_event_bias` from an
`event_bias` field produced by the events/news classification pipeline)
**Written to:** `sector_event_bias`, a per-candidate field carried on swing
signals and candidates — NOT a market-wide table
**Computed:** per event, per sector, whenever the news/events ingestion runs
**Format:** free-ish string, observed values include `NEGATIVE`, `RISK_OFF`,
`BEARISH`, `POSITIVE` — no single enum is enforced at the schema level

**Who reads it:**
- `intraday/news_gate.py::check()` — `if bias in ("NEGATIVE", "RISK_OFF",
  "BEARISH")`, treats any of the three as a WARN-level flag on entries into
  that sector.
- `control/position_event_monitor.py` — surfaces sector events onto open swing
  positions via the same field.

**Not a bug, but the collision is real.** This describes ONE SECTOR's news
posture, not the whole market's technical state. A pharma-sector name can show
`sector_event_bias: RISK_OFF` (a bad earnings pre-announcement, say) on a
session where both system #1 and system #2 above read `RISK ON` / `RISK_ON`.
Seeing "RISK_OFF" in a log line without checking which field it came from is
how a reader concludes the whole market turned when one sector's news did.
Confirmed this field never feeds `regime_bucket()`, `market_context.classify()`,
or `compute_regime.py` — the three systems stay isolated from each other in
code, even though a person reading logs has to do that separation manually.

---

## 4. Market structure

**Defined:** `analysis/market_structure.py::classify()`
**Written to:** nowhere persisted — computed fresh from a bar list every time
it's needed, for either a daily (swing) or intraday bar series
**Computed:** on demand, from fractal pivots (a high/low with `k` bars
confirming it on both sides — default `k=2`)
**Format:** six states, describing the SEQUENCE of highs and lows, not the
market's mood:

```
UNKNOWN   RANGE   DOWNTREND   REVERSAL_UP   CONFIRMED_UP   UPTREND
```

This is deliberately a DIFFERENT axis from regimes #1–#3. A regime says
"is the environment favourable"; structure says "is THIS chart's own sequence
of highs and lows currently bullish, bearish, or turning" — one stock's
five-minute chart and the Nifty's daily chart can both be classified this way,
independently, because pivots don't care about timeframe.

**Direction-aware since `claude/intraday-short-selling`:**
`gate_for_framework(framework, highs, lows, direction=)` routes to
`gate_long()` or `gate_short()`. `gate_short` is not a naive mirror —
`CONFIRMED_UP` and `REVERSAL_UP` both block a short outright with no config to
override, because standing in front of a confirming reversal is a squeeze
candidate, not a trade, whereas the long side's equivalent
(`structure_allow_reversal`) is a config-gated *entry timing* choice, not a
symmetric risk.

**Who reads it:** `control/exit_rules.py` (imports the real constants —
`UPTREND, CONFIRMED_UP, DOWNTREND` — and compares `st.state` directly; clean,
no string drift) and `intraday/engine.py::evaluate_intraday_setups()` (the
`intraday_structure_gate` switch).

**Config-scoped correctly:** tolerance and pivot-confirmation parameters are
prefixed `structure_swing_*` / `structure_intraday_*`
(`analysis/market_structure.py::for_framework`) — the one place in this survey
where a shared concept was deliberately given framework-scoped config from the
start, and it stayed that way.

---

## 5. Allocator regime bucket

**Defined:** `allocation/hurdle.py::regime_bucket()`
**Written to:** `allocation_decisions.regime_bucket` (migration 044)
**Computed:** per allocator cycle, DERIVED from #1 or #2 — never an
independent classification of its own
**Format:** two states only:

```
WEAK   STRONG
```

Exists to segment the arrival distribution the allocator's hurdle is built
from (§ see `allocation/hurdle.py::_empirical_base`) — a pooled curve is wrong
in both regimes, so the bar is drawn separately for weak and strong sessions.
This is the function that had the bug described in §1/§2. After migration 047
it explicitly names every value both upstream vocabularies can produce, rather
than pattern-matching a substring:

- Swing `TRENDING`, `RISK ON`, `RECOVERING` → `STRONG`; `NEUTRAL`, `RISK OFF` →
  `WEAK`
- Intraday `RISK_ON` → `STRONG`; `NEUTRAL`, `CAUTION`, `RISK_OFF` → `WEAK`
- Anything unrecognised (including `None`, `""`) → `WEAK` — an unknown market
  is not evidence of strength, the same reasoning `market_context.classify()`
  applies to its own missing-data case.

**If you add a new regime value anywhere upstream, this function will silently
route it to `WEAK` unless you also add it here.** That fallback is intentional
and safe (weak is the conservative default), but it means a new state added to
`compute_regime.py` or `market_context.py` without a matching entry here is
invisible rather than broken — worth a grep of this file when either upstream
system changes.

---

## 6. Legacy / dead labels

Three strings appear in comparison logic across this codebase but **cannot be
produced by any live classifier**:

| Label | Where it still appears | What it actually was |
|---|---|---|
| `BULLISH` | `allocation/hurdle.py` (pre-047), `alerts/send_alerts.py` (pre-047), `ai/post_trade_analysis.py:417` | Pre-hysteresis experimental regime label |
| `BEARISH` | `ai/providers/ml_regime_classifier.py`'s own `legacy_map` (normalises it away) | Same era, same fate |
| `CAUTION` (as a **swing** regime value) | `alerts/send_alerts.py` (pre-047) | Pre-hysteresis "bear-watch" state — note `CAUTION` **is** a live value in system #2 (intraday), so this label isn't dead everywhere, only dead as a *swing* regime |

`ai/providers/ml_regime_classifier.py` already has the correct answer, in a
`legacy_map` used before training data is built:

```python
legacy_map = {
    "CAUTION": "NEUTRAL",     # pre-hysteresis bear-watch → closest modern state
    "BULLISH": "RISK ON",     # early experimental label
    "BEARISH": "RISK OFF",    # early experimental label
}
```

Nothing outside that one function performs this normalisation, which is why
these three strings kept reappearing in comparison logic elsewhere — each
place that checks for `"BULLISH"` was, in effect, re-deriving a mapping that
already exists once, correctly, and not importing it. `regime_bucket()` and
`regime_icon()` are now consistent with this table as of migration 047.
`ai/post_trade_analysis.py:417` was not touched — it's a grading tool
(assigns a letter grade to a closed trade after the fact) rather than a
live gate, so a dead comparison there costs a slightly-wrong grade on
historical rows, not a live decision. Flagged here rather than fixed, since
grading logic changes should be a deliberate, separate decision.

---

## 7. Day type (unambiguous)

**Defined:** `analysis/overlays.py::day_type()`
**Computed:** daily, from the trading calendar (expiry Thursdays, month-end,
etc.) — no price or sentiment input at all
**Format:** calendar labels (`EXPIRY`, `NORMAL`, and similar)

Included in this survey only to rule it out. It answers "what KIND of session
mechanically is today" (settlement-dominated, thin, etc.), never "is the
market favourable" — no overlap with §1–§5 in either vocabulary or purpose, and
no consumer was found treating it as a regime.

---

## 8. Free-text `weekly_structure`

**Read at:** `control/exit_rules.py:115` — `ws = (sig.get("weekly_structure") or
"").upper()`, then keyword-matched: `any(k in ws for k in ("HIGHER", "UPTREND",
"BULLISH", "HH"))`

**This is NOT `analysis/market_structure.py`'s `Structure` enum.**
`weekly_structure` is a free-text descriptive field (written elsewhere as
human-readable prose, e.g. "uptrend, higher highs confirmed") that this one
function keyword-scans for a soft for/against tally when grading a position —
it is not a hard gate, and a false match here shifts a descriptive score, not
an order. The word overlap with §4's real enum (`UPTREND`, and `BULLISH` from
§6's dead vocabulary) is coincidental prose, not a shared data contract.
Flagged so a future reader doesn't assume it reads the same states §4 defines.

---

## What to do when you add a new regime-shaped concept

1. **Give it a format nothing else uses.** Two of the four real systems above
   ended up as near-homophones (`RISK ON` / `RISK_ON`) purely by historical
   accident, and that's what let #1's matching logic silently fail against #2
   for as long as it existed. A visibly different vocabulary (or a shared
   Python `Enum` imported by both sides) makes this class of bug impossible
   rather than merely unlikely.
2. **If it derives from an existing system, name the source in the docstring**
   the way §5 now does, and grep for every place that FUNCTION is called
   before trusting the docstring's assumed caller — `regime_bucket()`'s
   docstring was correct about what it was matching; it was wrong about what
   it would be given.
3. **Add it to this file's summary table.** The whole point of this document
   existing is that the next person asking "what does X mean here" finds one
   place with the real answer instead of five files with five plausible ones.
