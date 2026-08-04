# Retention windows — measured, proposed, DECIDED 04-Aug-2026

> **DECISION (operator, 04-Aug-2026).** Implemented in migration 032 and in the
> evening pipeline's roll-off step.
>
> | Table | Decision | Window |
> |---|---|---|
> | `raw_prices` | **prune** | 120 calendar days |
> | `chartink_raw_data` | **prune** | 120 calendar days |
> | `price_history_yf` | **leave alone** | none |
> | `stock_data_daily` | unchanged | 250 trading days |
> | the outcome record | **never** | none |
>
> **The question that decided it.** The operator asked whether the staging
> tables feed fields in `stock_data_daily` that would be lost. They do feed
> them — and the fields persist. Measured on the live database:
>
> ```
> date          rows   delivery_pct   value_cr   sma_200   ret_12m
> 2026-03-09     400            398          0       400       398
> 2026-05-15     400            399        399       387       379
> 2026-08-03     400            400        400       388       382
> ```
>
> Every derived field is present months after the staging row was written, and
> every reader of either staging table queries `.eq("date", today)` — one day
> deep. So pruning costs no field. It costs only the ability to *re-run*
> `compute_indicators` for a day older than the window, and even that is
> recoverable because the bhavcopy and the screener can both be fetched again.
>
> **Window raised from the proposed 60 days to 120.** The live path needs one
> day; 120 buys four months of re-run headroom for about 25 MB of steady state
> while still removing the full 13.6 MB/month these two tables contribute.
>
> **`price_history_yf` is excluded, and the operator's caution about it was
> right.** It is the one price table read with a *470-calendar-day* lookback
> (`compute_indicators.py:207`, covering `ret_12m` at 240 sessions) rather than
> one day, so it is the only one whose loss would break a computed field. It is
> also the slowest grower at 2.55 MB/month. Most risk, least saving.

The measurements that led to that decision follow, unchanged.

Stage 1 of the Phase 4 plan requires retention windows for append-only tables
to be *proposed* rather than chosen, because a retention window decides what
the system can still learn from, and that is not a decision code should make on
its own. Every number below was measured on **04-Aug-2026** against the live
database.

---

## 1. Where the database actually stands

| | |
|---|---|
| Total | **205.6 MB** of the 500 MB plan ceiling — **41.1%** |
| Measured growth | **30 MB/month** (health check, across 87% of size) |
| Health FAILS (80%, 400 MB) | **14-Feb-2027** |
| Writes fail (100%) | **25-May-2027** |

Migration 016 estimated `stock_data_daily` at "roughly 190 MB" across 48,967
rows. It is **58 MB across 51,462** — the estimate was high by a factor of
three. That is the reason every figure here is measured from rows actually
added in the last thirty days rather than straight-lined from a row count.

---

## 2. The finding that matters most

**The roll-off cannot save the database, because it does not engage until after
the ceiling alarm has already fired.**

`archive_stock_data(250)` deletes nothing until the table holds more than 250
distinct trading dates. It holds **99**. At ~21 trading dates a month:

```
  99 dates today  →  251 dates needed  =  152 dates  ≈  7.2 months
  first effective roll-off             ≈  11-Mar-2027
  health FAILS at 80%                  =  14-Feb-2027   ← four weeks EARLIER
```

Worse, `stock_data_daily` is not near its steady state. At 520 rows per trading
date and 1.16 KB per row, a 250-date window caps it at roughly **151 MB** — it
is at 58 MB, so **another ~93 MB of growth is still to come** before the cap
binds at all.

So the roll-off is correct, worth running, and **not the lever**. The lever is
the three price tables that have no retention of any kind.

---

## 3. What every table is read at its deepest

This is the only question that decides a safe window, so it was answered by
tracing consumers rather than by judgement.

| Table | Size | Growth | Deepest read anywhere in the repo | Where |
|---|---|---|---|---|
| `stock_data_daily` | 58.4 MB | 11.90 MB/mo | 200-day moving average → 250 trading days | `compute_indicators` |
| `price_history_yf` | 46.1 MB | 2.55 MB/mo | `.gte("date", today − 470 days)` | `compute_indicators:207` |
| `raw_prices` | 41.5 MB | 8.05 MB/mo | **`.eq("date", today)` — one day** | `compute_indicators:668` |
| `chartink_raw_data` | 26.3 MB | 5.58 MB/mo | **`.eq("date", today)` — one day** | `compute_indicators:558` |
| `master_shortlist` | 6.1 MB | 1.89 MB/mo | rolling shortlist history | brain / review |
| `signal_log` | 4.6 MB | 1.51 MB/mo | **unbounded — the measurement spine** | priors, outcomes |
| `signal_output_daily` | 2.3 MB | 1.69 MB/mo | **unbounded — the measurement spine** | priors, outcomes |
| `msl_history` | 1.9 MB | 0.56 MB/mo | rolling | brain |

`raw_prices` and `chartink_raw_data` together are **67.8 MB (33% of the
database) growing 13.6 MB/month (45% of all growth)**, and no consumer anywhere
in the repository reads either of them at more than a single date. They are
ingestion staging that was never swept. Searched: `swing/`, `analysis/`, `ai/`,
`tools/`, `control/`, `intraday/`, `execution/` — the only readers are
`compute_indicators` (today only), `data_quality_monitor` (freshness, max date
only), and the ingesters that write them.

---

## 4. Proposed windows

Each keeps a generous multiple of the deepest measured read. **None of these is
implemented.**

| Table | Proposed | Multiple of what is read | Caps at | Frees now | Removes |
|---|---|---|---|---|---|
| `raw_prices` | **60 days** | 60× | ~25 MB | ~16 MB | 8.05 MB/mo |
| `chartink_raw_data` | **60 days** | 60× | ~16 MB | ~10 MB | 5.58 MB/mo |
| `price_history_yf` | **550 calendar days** | 1.17× | ~46 MB | ~0 MB | 2.55 MB/mo |
| `stock_data_daily` | **250 trading days (unchanged)** | 1.25× | ~151 MB | 0 now | 11.90 MB/mo, from Mar-2027 |

**Combined effect if all four are accepted:** growth falls from ~35 MB/month to
**~19 MB/month** and roughly 26 MB is freed immediately. The 80% date moves from
14-Feb-2027 to approximately **Jul-2027**, and once `stock_data_daily` reaches
its own cap in Mar-2027 the curve flattens further.

**Explicitly proposed for NO retention:**

`signal_log`, `signal_output_daily`, `intraday_setups`, `closed_positions`,
`signal_outcomes`. These are the unbiased denominator — the full-field priors
the entire statistical framework rests on, and the architecture's stated
defining asset. They total under 10 MB and grow under 4 MB/month. **Deleting
from them to save single-digit megabytes would destroy the thing the storage is
for.** If the ceiling is ever genuinely threatened, the answer is a larger plan,
not a shorter memory.

---

## 5. Two questions only the operator can answer

1. **`price_history_yf` back to Jan-2025 — is that for backtesting?** The live
   path reads 470 days. If offline research reads further, the 550-day window is
   wrong and the table should be left alone; it is the cheapest of the four to
   keep and the hardest to re-fetch at scale.

2. **60 days on the staging tables — too tight?** The live path needs one day.
   60 is chosen so a re-run of any night in the last two months still finds its
   inputs. If the pipeline is ever backfilled further than that, say so and the
   window goes up; the cost is about 0.42 MB per extra day across both tables.

---

## 6. If accepted, how it should be built

Not with new machinery. `archive_stock_data()` in migration 016 is the pattern
and it is correctly written — archive first, delete second, one transaction,
re-runnable. Staging tables need no archive half at all, only the delete, since
nothing reads them historically and the source files are re-fetchable.

It would be one additive migration and one line each in the existing
`28_storage_rolloff` pipeline step, which already exists and already runs last
and non-fatal.

**Until this document is decided, nothing changes.** The health check will
report the 14-Feb-2027 date on every run until it does.
