# TradeOS — Historical Replay Harness: DESIGN

**Status: REVISED 2026-08-16 — depth measured, walk-forward split rebuilt.**

> **Revision 1 (2026-08-16).** The original document was written without
> querying the database and its central parameter was wrong. `stock_data_daily`
> begins **2026-03-06**, not mid-2025, so the 6/3/3-month walk-forward split it
> specified **does not exist and is void**. Rebuilt in **§8** against measured
> counts; the honest limits are in **§8.1** (the holdout is 21 days and the
> swing arm may be uninformative) and **§8.2** (`stock_data_daily` is a hard
> floor for both arms; backfill is out of scope). Two further corrections fell
> out of the measurement: **§4.1** — the table is a ~500-name Chartink-screened
> subset, *not* the full NSE EQ cross-section as originally claimed — and
> **§3.2**, the Kite call budget, resized from 10,086 to 4,182.
Implements `docs/PHASE_E_HISTORICAL_REPLAY.md`. Read that first for *why* this
phase exists; this document is *how it is built* and *how it is proven correct
before it is believed*.

Written 2026-08-16 from read-only inspection of source and schema. Every file
and line reference below was read in this session. Every claim I could not
verify without running something or querying the database is marked
**UNVERIFIED** and appears again in §12.

---

## 1 — The two questions, and what would answer them

The harness exists to answer two questions and is not a general backtester. Both
are stated here as decision rules with thresholds **fixed before the run**,
because a threshold chosen after seeing the number is not a threshold.

### Q1 (primary) — the swing target multiple

> Does raising the swing target from 1.90R toward 2.5R cost more in hit rate
> than it gains in break-even margin?

**Measured:** mean net R per replayed plan, at target multiples
`m ∈ {1.905, 2.1, 2.3, 2.5, 2.7, 3.0}`, holding entry and stop **fixed** at the
values the planner computed. Only the target moves. That isolates the parameter
the question is about — sweeping the stop as well would change R's denominator
and make every column incomparable to every other.

**Construction:** paired. The same replayed entries run at every `m`, so the
comparison is a paired difference `ΔR(m) = netR(m) − netR(1.905)` per plan, and
the standard error is the SE of that paired difference — far tighter than
treating the columns as independent samples, and the only correct construction
when the entries are identical by design.

**Decision rule, pre-registered:**

```
adopt m > 1.905  ONLY IF
   (a) mean ΔR(m) > 0 by >= 2 SE of the paired difference, IN-SAMPLE, and
   (b) the same, on VALIDATION, with the same m winning, and
   (c) mean ΔR(m) > 0 in HOLDOUT — sign only, no threshold, one look.
otherwise: the target stays where it is, and this is a finding, not a failure.
```

**Reported alongside, always, because the question is phrased as a trade-off:**
per `m` — n, hit rate (share of plans whose ladder outcome is a net win), mean
win R, mean loss R, mean cost R, mean net R, max drawdown in R, longest losing
streak.

**The design consequence that decides whether Q1 is even askable.** On the 11
live swing trades, `EXIT_TARGET` fired **zero times** and `EXIT_GIVEBACK` fired
6 of 11 (FINDINGS.md, Stage 2d §2). The give-back rung cuts at a 50%
retracement from any peak ≥ 0.5R (`exit_giveback_min_r` 0.5,
`exit_giveback_pct` 50.0 — `position_lifecycle.py:238-239`), and the highest
peak on the whole live book was PPLPHARMA at 1.34R. **The planned target is
almost a dead parameter under the current ladder.** A sweep of `m` alone will
very likely return "no effect", and the reason would be the ladder, not the
target.

So Q1 is run as a **joint sweep**, `m × giveback_pct`:

```
   m           1.905  2.1  2.3  2.5  2.7  3.0        (6)
   giveback_%  50 (live)  65  80  OFF               (4)
                                                    -> 24 cells
```

`giveback_min_r` stays at 0.5 (it is deliberately the same number as
`stall_peak_r` so the two profit-side rules partition the space with no gap —
see the comment at `position_lifecycle.py:234-238`; moving one without the
other reopens the band that comment exists to close). The `m`-only column is
still reported, as the literal answer to the question as asked. The joint grid
is what makes that answer interpretable.

### Q2 (secondary) — the six intraday engines

> Do any of the six n≥30 intraday engines show positive gross R over the
> replayable history, given all six are negative over 13 sessions?

*(Originally phrased "over a year". The history does not reach a year — 75 clean
trading days, §8. The question is unchanged; its horizon is now stated
honestly.)*

The six, with their complete-population figures from FINDINGS.md
(2026-08-15 re-score, 1102 dedup keys / 14 sessions):

```
  engine    n   target          grossR         costR      netR
  SDN     398   67/398 = 16.8%  -0.077±0.072  +0.052    -0.129
  VWR     307   53/307 = 17.3%  -0.345±0.071  +0.229    -0.575
  VCE     138   25/138 = 18.1%  -0.155±0.118  +0.130    -0.284
  ORB     119   12/119 = 10.1%  -0.241±0.100  +0.136    -0.377
  RNG      60    7/60  = 11.7%  -0.137±0.157  +0.040    -0.177
  PBK      32    4/32  = 12.5%  -0.276±0.244  +0.329    -0.604
```

**Measured:** mean **gross** R per deduplicated detection, per engine, per
window. Gross first and separately, because `cost_pct` is 0 on every bucket
blocked before the cost gate (FINDINGS.md PRE-1) and gross R is the only
quantity both sides of any gate carry. Net R is reported beside it and is the
only column that pays.

**Decision rule, pre-registered:**

```
an engine "shows positive gross R" ONLY IF
   (a) mean gross R > 0 by >= 2 SE, IN-SAMPLE, and
   (b) mean gross R > 0 by >= 2 SE, VALIDATION, and
   (c) mean gross R > 0 in HOLDOUT — sign only, one look, and
   (d) it clears the 95th percentile of the 1000-draw random-entry null (§9).
n < 100 in any window -> INSUFFICIENT, printed as such, never ranked.
```

**Direction is collinear with engine and this governs every reading.** SDN is
100% SHORT; every other engine is 100% LONG (FINDINGS.md §3). Any comparison
*between* SDN and another engine is also a long-vs-short comparison and is not
an engine verdict. The harness prints the direction split beside every engine
row so that this cannot be forgotten by whoever reads the output.

---

## 2 — Independence: what the harness may read, and what it may not

Twelve sessions have shown the analysis readers cannot be trusted. Two examples
that are enough on their own: `engine_scorecard._fetch()` pages with `.range()`
and **no `.order()`** — verified sound on three trials, guarantee absent, and
the identical idiom on the identical table returned 5000 distinct of 8324 one
day earlier; and `check_selects` printed "every SELECT names columns that
exist" over the top of its own error output.

This is a **design requirement, not an optimisation.** The harness recomputes
everything it reports from bars and imported engine code.

**FORBIDDEN reads — the harness must contain no reference to any of these:**

| Forbidden | Why |
|---|---|
| `intraday_setups` | except in the verification module (§10), which compares against it and never computes from it |
| `review_engines`, `engine_scorecard` | the readers under suspicion |
| `tools/weekly_review.py`, `tools/engine_scorecard.py` | same |
| `signal_log`, `signal_output_daily` | the pipeline's own record of what it decided; replaying against it is replaying its conclusions |
| `allocation_decisions`, `brain_proposals` | outputs of the learning loop the replay exists to check |
| `closed_positions`, `open_positions` | live book state; also `exit_reason` is wrong on all 11 (F-3) |

A static check enforces this: **`grep` the harness package for every forbidden
table and module name and fail the build if any appears outside
`verify_known_day.py`.** That check must be demonstrated failing — add a
reference to `intraday_setups` in a scratch copy, confirm the check errors,
remove it.

**PERMITTED reads:**

| Permitted | Used for | Point-in-time? |
|---|---|---|
| `kite.historical_data()` | all bars | yes, by construction |
| `stock_data_daily` | universe reconstruction, prev-day reference levels, daily bars | **yes** — keyed `(symbol, date)`, written from the full NSE EQ bhavcopy |
| `sector_strength` | swing engine sector gates | yes — keyed on `date` |
| `market_regime` | `compute_trade_levels(regime=)` | yes — has `date`; must be read with `.eq("date", D)`, never `order desc limit 1` |
| `fii_dii_flow` | swing screener input | yes — has `date` |
| `nse_holidays` | the trading calendar | yes |
| `system_config` | **read once, frozen to a file, then never read again** (§8) | no — see below |

Every one of these reads is paged through `config.fetch_all` with an explicit
sort key, and every paged read asserts `returned == server_count` before a
single statistic is taken from it — the construction the 2026-08-15 re-score
used and printed.

---

## 3 — Bars: fetching, caching, and the call budget

### 3.1 Source and interval

One call per symbol per day, exactly as the live path does it:

```python
kite.historical_data(instrument_token, day, day, interval)
```

`interval = "minute"` — the default of `intraday_bar_interval`, read at
`engine.py:449` and `outcomes.py:122`. The replay must use the same interval the
live system used or the detections cannot be compared to the ones it recorded.

Rate limit: the repo's own statement is **"Kite allows about three a second"**
(`outcomes.py:57`). The harness throttles to **2 req/s** through a token bucket,
with exponential backoff on any exception and a hard stop after 5 consecutive
failures. It never runs unattended past a hard stop — a fetch that silently
degrades is a gap in the denominator.

### 3.2 Expected call count — RESIZED to the measured window

The 12-month budget this section originally carried (~246 days, 10,086 calls)
was void with the window it assumed. Resized against §8's measurement:

```
replayable trading days                   102   (30 in-sample usable + 18 + 21 + 33)
intraday universe per day                  40   (intraday_max_universe)
  + NIFTY 50 index context                  1
                                        -----
symbol-days                        41 x 102 =  4,182 calls   WORST CASE
at a throttled 2 req/s                   ~35 minutes, ONE TIME
minute bars stored                 ~375 x 4,182 = ~1.6M bars  (~85 MB packed)
```

The shallower history is the one place the depth finding *helps*: the fetch is
now small enough that the chunking optimisation below is not worth taking, and
the design proceeds on the per-day worst case without needing the span limit
verified at all.

**The optimisation, contingent on one verification.** Kite Connect v3 documents
a maximum span per `historical_data` request that is longer than one day for
minute data. If that span is ≥ 60 days for the deployed `kiteconnect` version,
the fetch becomes `U × ceil(246/span)` where `U` is the **union** of daily
universes over the year, not 41 per day. `U` is unknown until step 1 computes it
— but step 1 costs **zero Kite calls** (it reads `stock_data_daily`), so the
fetch is sized exactly before a single bar is requested. For a plausible
`U ≈ 200–400`, that is 1,000–2,000 calls, 8–17 minutes.

**UNVERIFIED:** the per-request span limit. Constraint 5 forbids fetching from
Kite in this session and I did not want to assert a number from memory into a
budget. **The design commits to the 10,086-call worst case** and treats chunking
as an optimisation to be taken only after the limit is confirmed against the
installed library.

**Swing daily bars cost zero Kite calls** — `stock_data_daily` already holds
OHLC for the full NSE EQ cross-section per date, and that is where the live
system reads its reference levels from (`engine.py:471-481`). Subject to §5.1.

### 3.3 Cache

Fetch once, ever. Cache layout:

```
backend/tools/replay/cache/
  bars/minute/<SYMBOL>/<YYYY-MM>.parquet     one file per symbol-month
  manifest.jsonl                             one line per (symbol, date) attempted
```

Every attempt writes a manifest line whether it succeeded or not:

```json
{"symbol":"KIMS","date":"2026-03-04","interval":"minute","rows":375,
 "fetched_at":"...","status":"OK"}
{"symbol":"XYZ","date":"2026-03-04","interval":"minute","rows":0,
 "fetched_at":"...","status":"MISS","reason":"no instrument_token"}
```

**A silent gap is a lie in the denominator.** Every report the harness prints
carries a coverage line — `symbol-days requested / OK / MISS`, with the misses
bucketed by reason — and any run whose coverage is below a configurable floor
(default 98%) prints **INCOMPLETE** in the header rather than a clean number.
The cache is keyed on `(symbol, date, interval)`; a re-run with a different
interval builds a separate tree rather than silently mixing two bar sizes.

---

## 4 — Universe reconstruction

### 4.1 Intraday — reconstructible exactly, and this is the good news

`intraday/scanner.py::build_universe(sb, limit)` is a **pure function of one
date's `stock_data_daily` rows.** It reads `date == d` and filters on
`close`, `value_cr`, `atr_pct`, `delivery_pct`, `asm_flag`, `fo_ban_flag`, then
ranks on `0.55·movement + 0.45·liquidity` (`scanner.py:181-244`). Every input is
stored per `(symbol, date)`.

**CORRECTED 2026-08-16 — `stock_data_daily` is NOT the full NSE EQ
cross-section.** This document previously asserted it was, on the strength of
the `SERIES == 'EQ'` filter at `ingest_bhavcopy.py:184`. That filter governs the
*bhavcopy dataframe*; it does not govern what is stored. The full unfiltered set
goes to **`raw_prices`**, and `stock_data_daily` receives a **Chartink-screened
subset** (`ingest_bhavcopy.py:624-633`):

```python
chartink_syms = fetch_chartink_universe(get_supabase(), trade_date)
sdd_final = sdd_final[sdd_final["symbol"].isin(chartink_syms)].copy()
```

Measured: **499–502 symbols per date**, against 2,463 in `raw_prices` on the
same date. The replay universe is a ~500-name pre-screened list, roughly 20% of
the traded cross-section.

**Is it point-in-time? Yes — with one residual risk that cannot be closed from
the database.** `fetch_chartink_universe` reads `.eq("date", trade_date)`
(`:394`), and the row set for date D was written by D's own evening pipeline, so
no future information enters by construction. The churn measurement supports
this: of the 499 symbols on 2026-03-06, **32 are absent** by 2026-08-14 and 34
are new, so this is not one static list applied backwards — names genuinely
leave.

**But the universe is 93% static and that bounds what the replay can conclude.**
465 symbols are present on all 108 dates, and the day-over-day symmetric
difference has a **median of 0** — most consecutive sessions have an identical
universe. So:

- **Survivorship bias is small but not provably zero.** The residual risk is
  `fetch_chartink_universe`'s fallback: when a date has no `chartink_raw_data`
  rows it silently uses **the most recent available date's** universe (`:398-406`).
  During a normal same-evening run that is a *prior* date and is harmless. If any
  historical date was ever re-ingested later, that date's stored universe is the
  re-run date's list — a genuine lookahead, invisible after the fact because
  nothing records which path was taken. It cannot be ruled out from the database
  alone. The harness reports the per-date symbol count and the day-over-day churn
  in its coverage block so an anomalous date is at least visible.
- **The replay can only ever judge names Chartink surfaced.** An engine that
  would have performed well on a name outside that ~500 is unmeasurable here.
  This is a ceiling on scope, not a bias in the estimate, and it applies equally
  to the live system — which trades the same filtered universe. So the replay
  measures the system as deployed, which is the right target for Q1 and Q2, and
  is *not* a general statement about the engines on Indian equities.

The harness therefore **reimplements nothing** — it imports:

```python
from intraday.scanner import build_universe, UniverseEntry
```

and calls it against a date-scoped row set. Two things must be handled:

1. `build_universe` calls `_latest_date(sb)` internally, which resolves to
   *today's* newest date with a non-null `value_cr`. The harness must supply the
   date rather than let the function resolve it. If the parameter does not
   exist, **the harness passes a pre-fetched row set through a thin injection
   point rather than editing the function** — and if that is not possible
   without a source change, it is recorded as a blocker and the change is
   proposed separately, not made inside the replay.
2. `cfg_*` reads inside it (`intraday_max_universe`, `intraday_min_price`,
   `intraday_min_turnover_cr`, `intraday_min_atr_pct`, `intraday_max_atr_pct`,
   `intraday_min_delivery_pct`, `intraday_skip_flagged`) resolve to **today's**
   config. That is handled by the freeze in §8 — not by hoping they have not
   changed.

**One documented gap.** `scanner.live_rerank(bench, quotes, ...)` promotes names
into the traded 40 *intraday*, from live quotes that were never stored. The
replayed universe is therefore the **morning** universe, and any live promotion
is unreproducible. This is one of the four expected verification mismatches in
§10.2 and its size is measured there rather than assumed small.

### 4.2 Swing — reconstructible, with one input that is not

Same story, one table deeper. `swing/signals/screen_stocks.py::load_data(sb,
today)` assembles the screener's inputs, and **five of its nine reads are not
point-in-time.** This is not a production defect — in production `today` *is*
today — but it makes the function unusable in a replay exactly as written:

| Read | Line | Point-in-time? |
|---|---|---|
| `stock_data_daily` | `:235` `.eq("date", today)` | **yes** |
| `sector_strength` | `:252` `.eq("date", today)` | **yes** |
| `market_regime` | `:270` `.order("date", desc=True).limit(1)` | **NO — latest** |
| `safety_lists` | `:274` no date filter | **NO — and unreconstructible, see below** |
| `event_calendar` | `:280` `.eq("is_active", True)` | **NO — current** |
| `fii_dii_flow` | `:283` `.order("date", desc=True).limit(1)` | **NO — latest** |
| `master_shortlist` (force_include) | `:293` `.eq("date", today)` | yes |
| `open_positions` | `:308` `status=ACTIVE` | **NO — today's book** |

The harness builds the same dict with date-scoped reads: `market_regime` and
`fii_dii_flow` by `.eq("date", D)` (both carry a date), `event_calendar` by
event date rather than `is_active`, `open_positions` as **empty** (a replay owns
its own book, §7.3), `force_include` as **empty** (the `ingest_sheets` merge at
`:298-303` is an operator override, not a signal, and including it would credit
the system for a human's picks).

**`safety_lists` cannot be reconstructed and this must be said plainly.** The
table is keyed `PRIMARY KEY (symbol, list_type)` with **no date column**
(`db/tradeos_schema as of 27 Jun.sql:2786`), and its ingest **deletes and
rewrites** the list each run (`ingest_asm_gsm.py:323`). ASM / GSM / F&O-ban
state is a live snapshot with no history in this database. Consequences:

- The harness runs the intraday universe filter with `skip_flagged=False` and
  the swing screener with empty ASM/F&O sets, and **labels every report with
  that fact.**
- The bias direction is stateable: names under ASM or an F&O ban are *harder*
  and *more expensive* to trade than the replay will model, so replayed results
  on any affected name are **optimistic**. The harness reports how many
  symbol-days are currently flagged as an order-of-magnitude sense of the
  exposure, and cannot do better than that.
- If this matters to a conclusion, NSE publishes historical ASM and F&O ban
  lists. Ingesting them with a date column is a separate piece of work and is
  **out of scope for this harness** — recorded here so it is not rediscovered.

**RESOLVED 2026-08-16 — the depth question is answered and it is the binding
constraint on this whole phase.** `stock_data_daily` holds **55,963 rows across
108 distinct dates, 2026-03-06 to 2026-08-14** — about five and a half months,
not the twelve this design was originally written against. The walk-forward
split has been rebuilt around the measurement in §8; the reasoning for why
backfill is out of scope is in §8.2. The branch that mattered is the second one:

- depth ≥ 12 months → swing arm runs with zero Kite calls for daily bars.
  **Not the case.**
- depth < 12 months → the daily OHLC backfill is cheap (one `interval="day"`
  call per symbol covers a multi-year span), **but the 86 indicator columns
  `compute_indicators` writes are not**, and every one of the nine screener
  engines reads them. **This is the case, and backfill is out of scope (§8.2).**

---

## 5 — Engines: exact imports

**Import them. Do not reimplement them.** A replay of a reimplementation
measures the reimplementation.

### 5.1 Intraday — nine classes, one method

```python
from intraday.strategies.orb                import OpeningRangeBreakout   # ORB
from intraday.strategies.gap_and_go         import GapAndGo               # GAP
from intraday.strategies.prev_day_levels    import PrevDayLevelRetest     # PDL
from intraday.strategies.squeeze            import SqueezeExpansion       # VCE
from intraday.strategies.pullback           import TrendPullback          # PBK
from intraday.strategies.vwap_reclaim       import VwapReclaim            # VWR
from intraday.strategies.range_fade         import RangeFade              # RNG
from intraday.strategies.short_distribution import ShortDistribution      # SDN
from intraday.strategies.gap_down_bounce    import GapDownBounce          # GDB
```

Each satisfies `intraday.strategies.base.IntradayStrategy`:
`evaluate(ctx: SymbolContext, phase: str) -> Setup | None` — pure, no I/O, no
broker, no database. That purity is what makes this whole phase possible; it is
a property to protect, not to spend.

**The harness must NOT call `registry.evaluate_all()`, and the reason is a trap
worth naming.** `evaluate_all` iterates `enabled_engines()`
(`registry.py:152-160`), which reads **live `system_config`** through
`engine_lifecycle()` → `cfg_bool(f"intraday_engine_{key}_enabled")` and
`cfg(f"intraday_engine_{key}_lifecycle")`. An engine RETIRED today would produce
**zero detections across the entire year** — and the replay would report "no
evidence" for precisely the engine it was built to judge. The harness
instantiates all nine directly and records the lifecycle state as a *column*,
never as a filter.

**But there is a seam, and it must not be papered over.** `evaluate_all` also
applies `registry._invalidation_is_reachable(s)` (`:168`) — it discards any
setup whose stop sits beyond its own stated invalidation, because such a trade
has no thesis, only a price limit. Calling `.evaluate()` directly skips that
filter and would record setups the live system refuses. So the harness's
detection loop **imports the filter too**:

```python
from intraday.strategies.registry import _invalidation_is_reachable, family_of
```

and reproduces only the *loop*, with the same `meta` stamping
(`meta["sub_engine"]` = the engine, `meta["family"]` = `family_of(name)`,
`meta["lifecycle"]` = the recorded state). `meta["sub_engine"]` is the key
`_setup_is_new` compares on (`engine.py:3151`), so getting it wrong desynchs
every dedup count from the ledger's.

The seam is stated in the harness docstring. Importing a private helper is
deliberate: the alternative is a second copy of a filter whose absence changes
which trades exist.

### 5.2 Swing — nine functions, one module

```python
from swing.signals.screen_stocks import (
    run_ctl, run_sbs, run_tpo, run_vbd, run_iad, run_rsb,
    run_mom_continuation, run_reversal_setup, run_sector_rotation,
    run_eap_overlay, aggregate_and_rank, resolve_regime, get_event_action,
)
from analysis.risk_model import compute_trade_levels, load_risk_params
```

All nine take `(stock_map, sector_rank, ...)` dicts and return dicts — no I/O.
`compute_trade_levels(entry_price, atr_abs, anchor_price=, structure_stop=,
regime=, params=)` (`risk_model.py:288`) produces the stop and target; the
harness holds its output's stop fixed and overrides only the target for the Q1
sweep, per §1.

---

## 6 — Contexts: assembling a historical `SymbolContext`

The engines read `intraday.strategies.base.SymbolContext`. The harness builds it
field-for-field the way `engine.py:515-530` does, from bars and
`stock_data_daily` instead of from ticks:

| Field | Live source | Replay source |
|---|---|---|
| `bars` | `historical_data(today, today)` | cached bars **truncated at the evaluation timestamp** |
| `ltp` | tick | `bars[-1].close` of the last completed bar |
| `vwap` | computed from today's bars | identical formula, `engine.py:501-503` |
| `day_open/high/low` | today's bars | bars so far — **not the full day** |
| `prev_close/high/low` | `stock_data_daily` prior row | same, `.eq("date", D_prev)` |
| `atr_pct_daily`, `avg_volume_20d`, `value_cr`, `sector` | `stock_data_daily` | same |
| `rs_vs_index_pct` | index context vs prev close | same, from replayed NIFTY 50 bars |
| `as_of` | `datetime.now(IST)` | the evaluation timestamp |
| `session_volume` | exchange cumulative | `sum(b.volume)` — **an approximation, flagged** |

**Truncation is the whole game.** Every context is built from bars strictly
before the evaluation timestamp. One `bars[:i+1]` slice off by one is lookahead
that will make every engine look brilliant, so it gets its own test: assert that
for a context built at time T, `max(b.ts for b in ctx.bars) < T` and that
`ctx.day_high` never equals the *full day's* high unless T is the close.

**Cadence.** The live loop evaluates every 15 s
(`intraday_eval_interval_s`); the replay has minute bars and evaluates **once
per completed bar**, ~375 evaluations per symbol-day. This under-samples the
live system by 4× and is the second of the four expected verification
mismatches (§10.2).

**Phase** comes from `intraday.session.phase_at(now)` — it already accepts an
injected `now` (`session.py:89`), so no seam.

**Market context** comes from
`intraday.market_context.classify(index_ltp, index_vwap, index_prev_close,
index_day_low, index_day_high)` (`:87`) — pure, explicitly documented as
"testable against recorded sessions". Fed from the replayed NIFTY 50 context.
Note its own guard: missing data yields NEUTRAL, not RISK_ON.

**No gates.** The replay records **every detection, ungated** — no cost gate, no
structure gate, no AI veto, no conviction floor, no allocator. Three reasons:
Q2 asks about gross R, which is the only quantity both sides of a gate carry;
the complete-population evidence shows not one engine separates its taken half
from its refused half at 2 SE (FINDINGS.md §4); and every gate reads live
config, which would silently apply today's thresholds to last year's bars. The
gates are a separate question and the harness is not the tool for it.

---

## 7 — The exit ladder

**The harness must model the ladder, not the planned levels.** On the live swing
book, 1 of 10 trades reached its planned target and none reached its planned
stop; the ladder decided 11 of 11 outcomes. Scoring entries against planned
levels would measure almost nothing.

### 7.1 Which functions it imports

```python
from control.position_lifecycle import evaluate_exit, load_exit_policy   # SWING
from intraday.exit_policy import (                                       # INTRADAY
    evaluate_intraday_exit, load_intraday_policy, last_completed_close,
)
```

Both are pure — `evaluate_exit(pos, ltp, sessions_held, policy) -> dict`
(`position_lifecycle.py:281`, "Pure — no I/O, no mutation") and
`evaluate_intraday_exit(pos, ltp, policy, now=, last_close=)`
(`exit_policy.py:203`, "Pure — no I/O"). Both take an injected clock. The
harness supplies both policies from the **frozen** parameter file (§8), never
from `load_*_policy()` at run time.

Swing actions the state machine must handle, from the docstring at
`position_lifecycle.py:283-300`: `HOLD`, `BOOK_PARTIAL`, `TRAIL_SL`,
`EXIT_TARGET`, `EXIT_STOP`, `EXIT_TIME`, `EXIT_GIVEBACK`, `EXIT_STALL`, `RUN`,
`EXIT_DETERIORATION`.

### 7.2 The position state the harness must carry

`evaluate_exit` reads these off the `pos` dict, and the harness owns all of
them: `entry_price`, `planned_stop`, `active_sl`, `planned_target`,
`partial_booked_qty`, `current_qty`, `trail_activated`, `high_water_mark`,
`symbol`, plus `policy["_trend_ctx"][symbol]` for the deterioration branch.

`high_water_mark` is the one that matters. The live daemon updates it from
ticks; the stored value **understates the true peak on 6 of 11** (F-4). The
harness therefore computes HWM from **bar highs**, which is strictly more
accurate than the stored column and is a deliberate divergence from live
behaviour — stated in the output, because it will make give-back fire slightly
earlier in the replay than it did live.

### 7.3 The swing ladder on daily bars — the approximation, and its sign

The live swing ladder runs inside the 15 s daemon loop on live LTP. Replaying
that faithfully needs minute bars for every plan symbol — the measured universe
is ~500 names per date (§4.1), so ≈ 500 × 102 days ≈ **51,000 calls, ~7 hours at
2 req/s**. Smaller than the year-long estimate this section originally carried,
and still not a fetch this project should make for this question.

**Decision: the swing ladder steps on daily bars, with a determinate rule and a
measured error bar.** Per completed daily bar, in this order:

```
1. hard levels first, bad fill assumed:
     lo <= active_sl AND hi >= target  -> STOP at active_sl
     lo <= active_sl                   -> STOP at active_sl
     hi >= target                      -> TARGET at target
2. otherwise update HWM  := max(HWM, bar.high)
3. call evaluate_exit(pos, ltp=bar.close, sessions_held, policy)
4. if a rung fires, fill at:
     the rung's own trigger price, if bar.low <= trigger <= bar.high
     else bar.close
```

**The bias this introduces has a known sign and it is conservative.** A
give-back or trail that would have fired intraday at a trigger price inside the
bar fills at that trigger; one whose trigger sits outside the bar's range fills
at the close, which is at or worse than where the live rung would have acted on
a rising price. The replay will not flatter the ladder.

**And the size of that bias is measured, not asserted.** The 11 live trades are
replayed on **minute** bars (≈ 11 trades × ~7 sessions ≈ **77 extra calls**) and
on daily bars, and the two are compared trade by trade. If daily-bar replay and
minute-bar replay disagree by more than 0.10R mean absolute on those 11, the
daily-bar approximation is **rejected** and Q1 is re-scoped to a smaller symbol
set with minute bars. That threshold is fixed here, before the number exists.

Note `evaluate_exit`'s risk line is long-only —
`risk = entry - stop0 if stop0 and stop0 < entry else None` (`:301`) — unlike
`evaluate_intraday_exit`, which is direction-aware through `intraday.direction`.
The swing book is long-only, so this is correct today; the harness must never
feed it a short, and asserts that.

### 7.4 The intraday ladder

Steps on minute bars, same ordering, using `evaluate_intraday_exit` with
`now` = the bar timestamp and `last_close` from `last_completed_close(bars,
now)`. `EXIT_SQUAREOFF` at the session-end phase is a hard terminal state — no
intraday position survives its own session, by design.

---

## 8 — Freezing parameters: the honesty mechanism

Every function the harness imports reads `system_config` through `cfg()` at call
time. A replay that lets that happen is applying today's switches to last year's
bars, and — worse — is not reproducible tomorrow.

**Step 1 of every run** dumps every config key the harness's imports touch into

```
backend/tools/replay/params/<label>.json
```

with, alongside the values, the **git SHA** of `backend/intraday/strategies/`,
`backend/intraday/exit_policy.py`, `backend/control/position_lifecycle.py`,
`backend/analysis/risk_model.py` and `backend/intraday/scanner.py`. Every
imported function is then called with `params=`/`policy=` from that file. The
config is read **once**, at the top, and never again.

**The walk-forward split — MEASURED, not assumed.**

The original split in this document (12 months from 2025-07-01) was written
before the depth of `stock_data_daily` had been queried. It was **void**: the
table does not go back that far. The measurement below was run on 2026-08-16
against the live database and every figure in it is a count, not an estimate.

```
census of stock_data_daily            55,963 rows / 108 distinct dates
earliest date                         2026-03-06
latest date                           2026-08-14
```

```
                                                  trading   fully
window          from          to                    days    usable
--------------------------------------------------------------------------
IN-SAMPLE       2026-03-06 .. 2026-04-30              36        30
VALIDATION      2026-05-01 .. 2026-05-31              18        18
HOLDOUT         2026-06-01 .. 2026-06-30              21        21
--------------------------------------------------------------------------
clean total                                           75        69
CONTAMINATED    2026-07-01 .. 2026-08-14              33        33
--------------------------------------------------------------------------
all dates present                                    108
```

Actual first/last dates carrying rows, since the window edges are weekends:
validation runs **2026-05-04 .. 2026-05-29**, holdout **2026-06-01 ..
2026-06-30**, in-sample **2026-03-06 .. 2026-04-30**.

**"Fully usable" is a narrower count than "present", and the gap is all in
in-sample.** Six dates carry rows whose `value_cr` is **null on every row**:
2026-03-06, 03-09, 03-10, 03-11, 03-12 and 04-28. `value_cr` is 45% of
`build_universe`'s rank (`scanner.py:181-244`) and one of its hard filters, so
the intraday universe cannot be reconstructed for those six dates at all. They
are **excluded**, not silently ranked on a null. In-sample is therefore **30
usable days, not 36**. Validation and holdout have no such dates.

**2026-04-29 is an anomalous ingest and is handled, not dropped.** It carries
**2,476 rows** where every other date carries 499–502. Of those, 500 have a
non-null `close` and 1,976 do not — they are empty shells carrying `value_cr`
but no price. The harness drops null-`close` rows before ranking, which restores
that date to a normal 500-name universe. Recorded here because a date with five
times the candidate pool would otherwise silently produce a different top-40
than any neighbouring day, for a reason that has nothing to do with the market.

**Why the clean window ends 2026-06-30.** The live book runs from 2026-07-13 and
13–14 sessions of it have been examined in detail across this ledger — engine
splits, gate counterfactuals, exit attribution, all of it. Those sessions
**cannot function as a holdout**: the analyst has already seen the outcomes.
Including them would be the exact failure PHASE_E §"the one rule" warns about,
wearing a respectable date range. The contaminated window is opened at
**2026-07-01** rather than 07-13 so that the boundary is a calendar month rather
than a remembered event — the fortnight before the first live trade was still
examined while forming views. It is replayed anyway, printed under a
**CONTAMINATED** heading, and used only for the §10 fidelity comparison against
the live book. **It may never move a parameter.**

### 8.1 — The holdout is thin, and the swing arm may be uninformative

This is stated here, before the run, so that it is a known property of the
design rather than a disappointment discovered while reading results.

**A 21-day holdout is thin.** It is one calendar month. It contains one
expiry, a handful of macro prints and whatever single sector rotation happened
to dominate June — none of which averages out over 21 sessions. A parameter that
survives it has survived one market, not a range of them. The correct reading of
a holdout pass here is "not contradicted", never "confirmed".

**For the INTRADAY arm (Q2) this is survivable.** The unit of observation is a
deduplicated detection, not a day. The live book records ~85 dedup keys per
session across the nine engines (1,102 keys / 13 sessions, FINDINGS.md
2026-08-15 re-score), so 21 days is on the order of **1,700–1,800 detections**
pooled — and the Q2 rule already refuses to rank any engine under n=100 per
window. The large engines (SDN, VWR, VCE) will clear that; the small ones
(PBK at n=32 over 14 live sessions) will not, and will print INSUFFICIENT in
every window. That is the rule working, not a defect.

**For the SWING arm (Q1) it may genuinely be uninformative, and the reason is
structural rather than a matter of sample size.** Q1's estimator is a *paired*
difference `ΔR(m) = netR(m) − netR(1.905)` over identical entries. For any plan
that never trades above 1.905R, that difference is **exactly zero at every m** —
the target never binds, so moving it changes nothing. Only plans that reach the
lower target contribute a single non-zero observation. So the effective n for
Q1 is not the plan count; it is:

```
n_effective(Q1) = plans whose favourable excursion reaches >= 1.905R
```

On the only direct evidence available — the 11 live swing trades —
`EXIT_TARGET` fired **0 times** and the highest peak on the whole book was
PPLPHARMA at **1.34R** (FINDINGS.md Stage 2d §2). That is `n_effective = 0` on
11 trades. At roughly 60 plans/day from the evening pipeline (CLAUDE.md), a
21-day holdout replays on the order of 1,200 plans, but if the reach-1.905R rate
is in the low single digits the informative subsample is **tens of
observations, possibly single digits** — nowhere near enough for a 2-SE paired
test to resolve anything.

**Consequence, accepted in advance:** the `m`-only sweep will most likely return
"no measurable effect" in every window, and that result will be **correct and
uninformative simultaneously**. It is why §1 sweeps `m × giveback_pct` jointly:
the give-back rung fired 6 of 11 live and is where the outcomes actually get
decided, so the give-back axis has a real n behind it even when the target axis
does not. **The harness prints `n_effective` beside every Q1 cell.** A cell
whose paired difference rests on fewer than 30 non-zero observations prints
`<< n_eff<30 UNINFORMATIVE` and is not ranked — the same guard §11 applies to
means without counts, applied to the one place where a large n hides a tiny one.

If Q1 comes back uninformative, **that is the finding**, and the honest response
is to say the data cannot answer it at this depth — not to widen the window into
contaminated months until a number appears.

### 8.2 — `stock_data_daily` is a hard floor for BOTH arms

**Recorded so this is not rediscovered.** Kite's bar history goes back years and
is not the binding constraint. The binding constraint is that **every screener
input other than raw price lives in `stock_data_daily`, which begins
2026-03-06.** Both arms depend on it and neither can be extended past it:

- **Intraday** — `build_universe` is a pure function of one date's
  `stock_data_daily` rows (`close`, `value_cr`, `atr_pct`, `delivery_pct`,
  `asm_flag`, `fo_ban_flag`). No rows, no universe, and therefore no
  reconstructable set of symbols to have fetched bars for.
- **Swing** — all nine screener engines read the 86 indicator columns
  `compute_indicators` writes onto the same table.

`raw_prices` is not an escape hatch and was checked: it holds the **unfiltered**
cross-section (2,463 symbols on 2026-08-14 against 501 in `stock_data_daily`)
but it starts **2026-04-16**, which is *shallower* than `stock_data_daily`, and
it carries OHLCV and delivery only — none of the 86 indicators.

**Backfill is explicitly OUT OF SCOPE.** Extending the window means re-running
`compute_indicators`, `compute_regime` and `sector_strength` across the
reconstructed history, then reconciling them against a `chartink_raw_data`
universe that does not exist for those dates either. That is a materially larger
project than this harness and must be scoped as one rather than absorbed into
it. **The replay is built against 75 clean trading days and its conclusions are
bounded by that.**

**The freeze protocol, and the checks that can fail:**

1. Tuning happens on IN-SAMPLE only.
2. Candidate parameter sets are scored on VALIDATION. One look per set. The
   number of looks is written to `params/validation_log.jsonl` — an append-only
   count, so "let me just check one more variant" is visible afterwards.
3. The winner is written to `params/frozen.json`, **committed, and tagged.**
4. The holdout runner **refuses to start** unless: the working tree is clean;
   `frozen.json` resolves to a committed git object; and no prior holdout result
   exists for that params SHA. One look, enforced by a file that already exists.
5. The holdout result filename embeds the params SHA, so a second run under
   different parameters is visibly a different artefact and cannot overwrite the
   first.
6. **If the holdout disagrees with validation, the holdout wins and the strategy
   is not confirmed.** No re-tuning. That sentence is in the runner's own
   docstring.

Every one of those refusals gets a test that **demonstrates it refusing** —
dirty tree, uncommitted params, duplicate SHA. A gate that has never been seen
to block is not a gate, and this project has found five of those.

---

## 9 — Outcomes

### 9.1 The bad fill

Ported line-for-line from `intraday/outcomes.py:181-199`, whose reasoning is
already written there: *"Both inside one bar. Assume the bad one — a coarse bar
cannot tell you the sequence, and assuming the good one is how a strategy looks
profitable on paper and loses money live."*

```
per bar, in order:
  hit_stop, hit_tgt  = (hi >= stop, lo <= tgt)  if SHORT
                       (lo <= stop, hi >= tgt)  if LONG
  hit_stop and hit_tgt -> STOP  at stop        <- the bad fill
  hit_stop             -> STOP  at stop
  hit_tgt              -> TARGET at tgt
  fell through         -> TIMEOUT at last close
```

Direction handling comes from `intraday.direction` (`D.normalise`, `D.is_short`,
`D.gain_pct`) — imported, not rewritten. The comment at `outcomes.py:164-171`
explains why: the long form applied to a short resolves STOP on the first bar,
every time, and would retire a working short engine on an arithmetic error.

**Why this is a port and not an import.** The loop is inline inside
`resolve_day`, which does token lookup, Supabase reads and Supabase writes. The
harness cannot call it without touching the database. So the loop is copied,
**and the copy is verified against the original's stored output on a resolved
day** (§10.3) — which is the only construction under which a port is
trustworthy.

### 9.2 Two outcome columns per detection, always

| Column | Rule | Purpose |
|---|---|---|
| `planned_R` | §9.1 against the setup's own stop/target | **reproduces `intraday_setups.outcome`** — the verification anchor |
| `ladder_R` | §7 — the exit ladder, stepped | **the real question** |

Reporting both is what makes a verification failure diagnosable: if `planned_R`
reproduces and `ladder_R` looks strange, the ladder model is wrong; if
`planned_R` does not reproduce, the bars or the detections are wrong. One column
would leave both possibilities open.

### 9.3 Costs

```python
from intraday.cost_model import round_trip
```

`round_trip(entry_price, qty, exit_price=None, product="MIS")`
(`cost_model.py:128`). **`product=` is passed explicitly on every call.** CNC and
MIS are different trades financially — delivery pays zero brokerage but 0.1% STT
on *both* legs, 0.015% stamp and a flat ₹15.04 DP fee per sell, so a ₹2,000 CNC
round trip is ~1.0%, not the 0.21% the intraday model reports. Swing = CNC,
intraday = MIS, and the default is MIS, which means an omitted argument
understates swing friction by ~5×. A test asserts the swing path's mean cost R
is materially above the intraday path's; if they come out similar, an argument
was dropped.

`cost_R = cost_pct / risk_pct` — and note this rises as the stop tightens, which
is why cost R is reported as its own column and never folded into gross.

### 9.4 The null model (PHASE_E §E.4)

Random entry on the same universe, the same session phases, the same stop and
target *distances*, the same costs; **1,000 draws**; each engine reported as a
percentile of that distribution. An engine that does not beat random entry with
the same risk parameters has a stop-and-target geometry, not a signal — and
geometry is free to anyone. This feeds decision rule Q2(d).

---

## 10 — Verification: proving the harness before believing it

**If it cannot reproduce a day you already have, nothing after it is
trustworthy.** This is the gate on the entire phase.

### 10.1 The comparison

Two dates, not one: **2026-08-14** (2,289 rows / 212 dedup keys — the largest
session on record) and one ordinary mid-window session. For each, the harness
replays the day cold and compares its detections to the stored
`intraday_setups` rows.

Dedup on **both** sides by `(trade_date, symbol, engine)`, representing each key
by its **first detection by `ts`** — `tools/weekly_review.py::dedupe_setups`'
rule. The ledger already records that the two available constructions (first
detection vs first TAKEN) disagree on 3 of 1102 keys; using the wrong one
chases a 0.2 pp ghost.

Compared per matched key: `direction` and `strategy` exact; `phase` exact;
`entry`, `stop`, `target` to 2 dp; `rr` to 0.01.

**This is the only module permitted to read `intraday_setups`,** and the static
check of §2 whitelists exactly this file.

### 10.2 The four expected mismatches — enumerated in advance

Enumerated **before** the run so that a mismatch gets diagnosed rather than
explained away afterwards. Every non-reproduced key must be classified into one
of these, with a count:

1. **Tick vs bar.** The daemon evaluates every 15 s on live LTP; the replay
   evaluates once per completed minute bar. Detections triggered by an
   intra-bar price the replay never sees will not reproduce.
2. **Live universe rerank.** `scanner.live_rerank` promoted names intraday from
   quotes that were never stored (§4.1). Detections on promoted names cannot
   reproduce at all.
3. **Config drift.** Values have changed since those sessions. The frozen params
   file is diffed against the config as of the replayed date where a dated
   record exists, and the deltas are printed.
4. **Code drift.** `git log --since` on `backend/intraday/strategies/` between
   the replayed date and today. Any engine changed in that window is reported
   separately and excluded from the pass/fail count.

**Acceptance bar, fixed here:**

```
>= 85% of stored dedup keys reproduced within tolerance, AND
100% of the non-reproduced keys classified into causes 1-4 with counts, AND
zero keys the harness produced that the live system did not (extras are worse
  than misses — an extra detection means the replay is more permissive than
  live, which inflates every downstream n)
```

Below that bar, the harness is not trusted and Q1/Q2 are not run.

### 10.3 The outcome-rule check, independent of detection

Separately and first: take the stored `intraday_setups` rows for a fully
resolved date, feed the harness's **ported** outcome resolver the same
`(entry, stop, target, direction, ts)` and the harness's own bars, and require
it to reproduce `outcome` and `outcome_pct` on **≥ 99%** of rows.

This isolates the outcome rule from the detection path. If detections do not
reproduce but outcomes do, you know which half is broken — and that is the
difference between a debuggable failure and a shrug.

### 10.4 Demonstrating the checks FAIL

Per CLAUDE.md — *a check that cannot fail is not a check*, and its mirror, *a
check that cannot pass is the same defect wearing a different hat*. Before any
of the above is trusted, each is shown failing on deliberately corrupted input
**and** passing on correct input:

| Injected fault | Must be caught by |
|---|---|
| bars shifted one day forward | §10.1 comparison collapses |
| bars shifted one bar forward (lookahead) | the truncation assertion in §6 |
| `direction` flipped on every setup | §10.3 outcome reproduction |
| `prev_close` dropped from contexts | detection counts collapse for PDL/GAP |
| bad-fill rule inverted (target wins ties) | §10.3, and gross R rises visibly |
| swing `product=` left at default MIS | the cost-magnitude assertion in §9.3 |
| a forbidden table referenced in the harness | the §2 static grep |
| holdout run twice on one params SHA | the §8 refusal |
| a coverage gap of 10% of symbol-days | the INCOMPLETE header in §3.3 |

And the passing side, which is the half this project has skipped before: a
correct replay of a known day **must** clear 85%, and a realistic engine on a
realistic session **must** produce detections. A harness that reports zero
everywhere is indistinguishable from a market with no setups.

These belong in `backend/tests/test_replay_harness.py`, registered in
`tools/verify.py::MODULES` — not in a scratch directory. Two defects have
already shipped through that gap.

---

## 11 — Outputs

Per window (in-sample / validation / holdout / contaminated), written as CSV +
a printed summary, never as a database write:

```
coverage      symbol-days requested / OK / MISS, misses bucketed by reason
per engine    n(dedup), gross R ± SE, cost R, net R, hit rate,
              max drawdown (R, consecutive), longest losing streak,
              direction split, lifecycle state
segments      regime_at_detection x session phase x direction
null model    percentile of the 1000-draw random baseline
Q1 grid       24 cells (target multiple x giveback pct): n, hit rate,
              mean win R, mean loss R, cost R, net R, paired dR vs baseline + SE
```

**Never a mean without its count.** Any bucket under n=100 prints
`<< n<100 INSUFFICIENT` and is not ranked — the guard the 2026-08-15 re-score
had to invent mid-analysis after producing "+5.5 SE" out of a single
observation.

Nothing is written to `system_config`, `brain_proposals`, or any live table.
Findings go into `docs/FINDINGS.md` as an appended entry. **A backtest that
modifies the system it is testing has stopped being a measurement.**

---

## 12 — Observed, not fixed

Found during this read-only pass. Nothing was changed. Items 1–3 are replay
blockers; 4–6 are recorded so the next session does not rediscover them.

1. **`screen_stocks.load_data()` is not point-in-time on five of nine reads** —
   `market_regime` (latest), `safety_lists` (no date filter), `event_calendar`
   (`is_active`), `fii_dii_flow` (latest), `open_positions` (today's book). Not
   a production defect: in production `today` is today. It does mean the
   function is unusable in a replay as written, and any future tool that calls
   it with a historical date silently gets today's market state. §4.2.

2. **`safety_lists` has no history and cannot be given one retroactively.**
   `PRIMARY KEY (symbol, list_type)`, no date column, and the ingest deletes and
   rewrites (`ingest_asm_gsm.py:323`). ASM/GSM/F&O-ban state is a live snapshot.
   Every historical universe reconstruction in this repo — this harness or any
   future one — is optimistic on flagged names by exactly this much, and cannot
   quantify it from the database alone. §4.2.

3. **`registry.enabled_engines()` reads live config**, so any tool that
   evaluates engines through `evaluate_all` sees today's lifecycle states. For a
   replay this is inverted: an engine retired today would show zero detections
   across all of history, and the replay would report "no evidence" for the
   engine it exists to judge. §5.1.

4. **The give-back rung makes the planned target nearly inert.** `EXIT_TARGET`
   fired 0 of 11 on the live swing book while `EXIT_GIVEBACK` fired 6 of 11, and
   the highest peak recorded was 1.34R against a 1.905R planned target. Q1 as
   literally asked ("does raising the target help?") will probably answer "no
   effect" — for the wrong reason. That is why §1 sweeps target × give-back
   jointly.

5. **`evaluate_exit` (swing) is long-only** in its risk computation
   (`position_lifecycle.py:301`), while `evaluate_intraday_exit` is
   direction-aware through `intraday.direction`. Correct while the swing book is
   long-only. It is exactly the shape of the direction landmine in CLAUDE.md —
   a function that keeps working right up until someone passes it a short — and
   the harness asserts it is never fed one.

6. **`intraday_setups` has no unique constraint on
   `(trade_date, symbol, strategy)`** (migration 014). Dedup is entirely in code
   (`weekly_review.dedupe_setups`, `engine._setup_is_new`). The harness dedups
   itself and does not inherit the problem, but §10.1's comparison must dedup
   the stored side by the same rule or the counts will not line up.

7. **`fetch_chartink_universe` is an unpaged read**
   (`ingest_bhavcopy.py:393-397`, `:403-407`) — `.select("symbol").eq("date",…)`
   with no paging, so PostgREST caps it at 1000 rows and reports nothing. It
   returns ~500 today so it is not currently truncating, but this is the exact
   F-19 idiom, and the value it returns **decides which symbols are stored in
   `stock_data_daily` at all**. If the Chartink universe ever crosses 1000, the
   stored universe silently becomes an arbitrary 1000-row slice and every
   downstream screener, scanner and replay inherits it with no symptom.
   **Recorded, not fixed** — this is production ingest and out of this stage's
   scope. Found while measuring depth for §8.

**UNVERIFIED in this session, by constraint:**

- ~~Depth of `stock_data_daily`~~ — **RESOLVED**, measured 2026-08-16:
  108 dates, 2026-03-06 .. 2026-08-14. See §8, §8.2, §4.2.
- Kite's per-request span limit for minute data (constraint 5). Decides whether
  the fetch is ~10,086 calls or ~1,300. The design commits to the worst case.
- That the engine and exit functions are importable in isolation without side
  effects at import time — plausible from their docstrings and structure, not
  demonstrated, because demonstrating it means running something (constraint 4).

---

## 13 — What this establishes, and what it does not

**Establishes:** whether these signals had edge over the tested period, at a
sample large enough to mean something, decomposed into signal versus friction,
with the target-multiple question answered as a paired comparison over identical
entries.

**Does not establish:** that the edge persists. Markets change; a signal that
paid through 2025 can stop paying in 2026 and the backtest will never say so.
Walk-forward reduces this. Nothing eliminates it.

**Cannot establish:** anything about a strategy not already coded. The replay
only looks where the engines already look. For the other blind spot —
"moved but unseen", real intraday moves that produced no detection from any
engine — the tool already exists and has never been run:

```bash
cd backend && python -m tools.discover_engines --days 30
```

**Cannot establish:** anything about names under ASM or an F&O ban on the date
in question (§4.2, item 2 above).

---

## 14 — Constraint tensions encountered

Per the brief's rule 7, recorded rather than resolved:

- **The universe injection point (§4.1).** `build_universe` resolves its own
  date internally. Supplying a historical date may require a source change to
  `intraday/scanner.py`. Constraint 2 forbids writing code, so this design
  states the requirement and stops. If no injection point exists, that change is
  a separate, approved piece of work — it is not made inside the replay, and the
  harness must not carry a private copy of the filter instead.
- **`stock_data_daily` depth (§4.2).** ~~The single largest open question in this
  design.~~ **RESOLVED 2026-08-16 by measurement**: 108 dates, 2026-03-06 ..
  2026-08-14. It was indeed the largest open question, and the answer moved the
  design — the 6/3/3-month split it was written around does not exist, the clean
  window is 75 trading days, and the swing arm's holdout is 21 days and may be
  uninformative (§8.1). Recorded rather than smoothed over: **a design written
  against an unmeasured assumption was wrong about its central parameter, and
  the measurement was one query.**
- **The outcome-rule port (§9.1).** Constraint-free but worth flagging: it is
  the one place the harness deliberately copies logic rather than importing it,
  because the original is welded to Supabase I/O. The copy is only defensible
  because §10.3 verifies it against the original's own stored output.

---

**Next step: review and approval. No implementation has begun.**
