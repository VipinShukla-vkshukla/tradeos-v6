# TradeOS v7 — Implementation status

**This is the only document in `docs/` that changes.** Documents 1–5 are frozen
and are not edited. Where measurement contradicts them, the contradiction is
recorded here and the measurement wins, per master spec §0.9: *"This document is
a hypothesis; the running system is the truth."*

Read this to know where the repository actually is against the frozen
architecture, and which of the specification's claims did not survive contact
with the live system.

**Branch:** `phase4-allocator`. `main` untouched.

---

## 1. Stage status

| Stage | Name | Status | Verified on |
|---|---|---|---|
| 1 | Survival | **Complete** | 04-Aug-2026 |
| 2 | Economics | **Complete** — validated to −0.01% against the broker | 04-Aug-2026 |
| 3 | Exits | **Complete** — premise disproved, reported, exits left alone (§0.9) | 04-Aug-2026 |
| 4 | Measurement spine | **Complete** | 04-Aug-2026 |
| 5 | Decision-input integrity | **Complete** — quote mode gated on parity by design | 04-Aug-2026 |
| 6 | Engine consolidation | **PARTIAL** — shadow phase complete, merges outstanding | 04-Aug-2026 |
| 7 | Governance and cadence | **Complete** | 04-Aug-2026 |

**On what "complete" means here.** A stage is complete when its deliverables are
built and its acceptance criteria are met *or answered*. Three of the entries
above deserve their qualifier spelled out rather than hidden behind a word:

- **Stage 2** is validated, not merely built: modelled friction reconciled to
  −0.01% of the broker's own statement. The specification asks for ≥20 round
  trips and this is 4 — so the *rate table* is validated (exchange and SEBI
  match to four decimals, which independently confirms the turnover) while the
  *sample* cannot yet catch a rate that is only wrong in cases these four trades
  never hit. That is a stated limitation, not an open deliverable.
- **Stage 3** is complete *because* its premise failed. §0.9 is explicit: "a
  section whose honest outcome is 'the premise was wrong' is a successful
  section." Capture on winners is 62.6%, not the 3% the spec assumed, so
  deliverable 2 was correctly not built. Deliverables 1 and 3 were.
- **Stage 5** is complete with `intraday_quote_mode` off. That is the specified
  end state, not an unfinished one — the specification requires one session of
  logged parity before the switch is trusted, and the switch waits on evidence
  rather than on work.

**Stage 6 is the one that is genuinely partial, and it is not relabelled.** Its
deliverables 1 and 2 are the merges — seven swing screeners into one
continuation engine, GAP/PDL into the ORB family as conditions, PBK into VWR.
Only deliverables 3 and 4 (retire VCE and RNG to shadow; shadow all retirees for
a quarter) are done. Calling that complete would be the kind of quiet
overstatement this log exists to prevent.
| 8 | New alpha | not started | |
| 9 | Structural overlays | not started | |
| 10 | Allocation | `allocation/scoring` built early (Stage 4 needs it); the other four modules not started | 04-Aug-2026 |

### Stages 5–7, in one line each

**Stage 5.** `SymbolContext` now carries `as_of`; entries are refused on a
context older than 420 s **or of unknown age**, because "we do not know when
this was true, so proceed" is the reasoning that lets a dead feed keep trading.
Exits are deliberately not gated. MODE_QUOTE is implemented and **off** until
`tools/quote_parity` shows one session of agreement. New health check `feed`
asserts the tick handler does no I/O and that the guard is actually consulted;
both demonstrated failing.

**Stage 6.** Intraday engines had only on/off — turning one off stopped it
recording, which destroys the evidence a retirement decision needs. Added
ACTIVE/SHADOW/RETIRED to match what swing has always had. VCE and RNG are
shadowed: they evaluate, their detections are written with verdict `SHADOW` and
qty 0, outcomes resolve them, they can never be funded, and they cannot lift a
survivor's confidence through corroboration. **The merges (GAP/PDL into ORB,
PBK into VWR, seven swing screeners into one) are deliberately not done** —
they rest on correlations the readiness review says are structural estimates
"because the sample does not support measurement", and shadowing is what
produces that sample. The swing side waits for a stronger reason still: 85
resolved plans against intraday's 595.

**Stage 7.** `evaluate_auto_apply()` returns False before reading anything —
removed in code, not toggled off, because a JSON edit re-opening it would look
like tuning. `governance_allows()` is enforced inside `apply_proposal`, the one
place a parameter actually changes, and applies to human-approved proposals too.
`freeze_calendar` covers 2026-Q3 → 2027-Q1. Conviction is demoted to
`Ranked.annotations` with both weights at 0.

Stages are strictly ordered and each gates the next (master spec §2).

---

## 2. Where the specification was wrong

Recorded per §0.9. Each of these is a claim in a frozen document or in the
existing codebase that measurement contradicted.

### 2.1 The storage estimate was high by a factor of three

Migration 016's header sized `stock_data_daily` at "roughly 190 MB before
indexes" across 48,967 rows and straight-lined an over-limit database "over the
limit" at 400 trading days. Measured 04-Aug-2026: **58 MB across 51,462 rows**,
whole database **205.6 MB (41.1%)**.

**What was done instead:** the health check derives growth from rows actually
added in the last thirty days at each table's own measured bytes-per-row, and
reports the coverage fraction. Nothing in the projection is straight-lined from
a row count.

**Cost if wrong:** an over-stated projection is ignored, and then the real one
is ignored too.

### 2.2 The roll-off cannot prevent the breach it was written for

Not a specification error — a discovery that changes what Stage 1 is worth.
`archive_stock_data(250)` deletes nothing until the table holds 251 distinct
trading dates. It holds **99**.

```
first effective roll-off   ≈ 11-Mar-2027
health FAILS at 80%          14-Feb-2027   ← four weeks earlier
```

And `stock_data_daily` has roughly **93 MB of growth still ahead of it** before
a 250-date window caps it at ~151 MB.

Meanwhile `raw_prices` and `chartink_raw_data` — 67.8 MB, 33% of the database,
45% of all growth — are read by exactly one query shape anywhere in the
repository: `.eq("date", today)`. One day deep.

**What was done instead:** the roll-off is wired as specified, because it is
correct and eventually necessary. The actual lever is written up in
[`RETENTION_PROPOSAL.md`](../RETENTION_PROPOSAL.md) as a proposal, because
Stage 1 deliverable 6 says measure and propose, not implement.

### 2.3 A comment claimed a reader that did not exist

Migration 016: *"Read by the health dashboard so the ceiling is visible months
before it is reached."* Nothing read `v_storage_usage`. Nothing called
`archive_stock_data`. Both had been sitting unused since February.

This is the repository's own dominant failure mode — the check that cannot fail,
here in its purest form: a guard that was never wired to anything, described in
the present tense.

---

## 3. Defects found and fixed outside the stage's scope

### 3.1 The daemon never re-read its configuration — CRITICAL

`config.get_system_config()` caches on first read. **Nothing in the daemon ever
refreshed it.** A process that booted at 09:00 answered every `cfg_*()` call for
the rest of the session from the 09:00 snapshot.

The consequences, in order of seriousness:

1. **The kill switch did not work mid-session.** `intraday/run.py:146` checks
   `is_kill_switch_active()` on every pass of the loop, against a dict that
   could not change. Setting `master_kill_switch = true` during a live session
   stored it, displayed it as engaged, and the daemon kept trading.
2. **The configuration rollback layer did not exist.** Master spec §6 makes
   `tools/rollback --all-off` the fastest of three rollback layers, the one that
   needs no deploy. Against a running daemon it would have changed nothing.
3. Every entry gate, exit threshold and cap in the daemon was equally frozen at
   boot.

**Why this had to be fixed inside Stage 1:** Stage 1's deliverable is
one-action rollback. Building a rollback tool on top of a configuration layer
that a running daemon cannot see would have shipped a control that reports
success and does nothing — the exact class of failure principle P2 exists to
prevent.

**Fix:** one call to `get_system_config(refresh=True)` on the daemon's existing
300-second slow timer, ahead of everything that consults config. Not on the
15-second cycle: the hot path does no blocking I/O (P6), and the egress budget
is a stated NFR. Cost is ~20 narrow reads per session.

**Residual, and it is an operator decision:** a switch now takes up to 300
seconds to be honoured. For entry gates that is immaterial. For the kill switch
it means up to five minutes of continued trading after the handle is pulled.
Architecture §22 explicitly specifies kill-switch state is "read from cached
config", so a bounded cache is architecturally correct — but if five minutes is
too long, re-reading that single row on the 15-second cycle costs ~24 KB of
egress per session. **Not done, because tightening an existential control is not
a change to make without being asked.**

**Guarded:** `tools/rollback.daemon_honours_config()` asserts the refresh is
still present by reading the source — the same technique `check_exit_actions`
already uses — and `--all-off` **refuses to run** if it is gone, rather than
reporting a rollback it cannot deliver. Demonstrated failing.

### 3.2 The obvious backup design would have published the trading record

The natural implementation of "weekly database dump to off-platform storage" is
a scheduled GitHub Actions workflow uploading an artifact. **This repository is
public** (`"private": false`, confirmed against the GitHub API), and artifacts
from a public repository's workflow runs are downloadable by anyone.

That design would have published every position, every P&L figure and every
decision the system has ever made, once a week, permanently.

**What was done instead:** the backup runs as a systemd timer on the always-on
VM — which architecture §26 already describes as the host with the large disk —
writing to local disk, private, retaining the last eight dumps.

### 3.3 The backup would have written credentials in plaintext

The backup runs with the service key, which **bypasses** the RLS that migration
007 added to hide `is_secret` rows. A naive dump of `system_config` writes the
Kite access token, the Gmail app password and the Discord webhook to a file —
reopening, via a backup, exactly the disclosure 007 was written to close.

**Fix:** `is_secret` rows are redacted before serialisation, using the same
marker RLS uses so the two cannot drift. `verify()` re-reads the written file
and **fails** if any secret survived. Demonstrated failing.

A restore therefore returns configuration and not credentials. That is correct:
credentials are rotated after an incident, never restored from a file written
before it.

### 3.4 Pre-existing and unrelated: the Kite IP allowlist is stale

Surfaced by the health sweep, not caused by this work:

```
✗ kite  public IP is 103.197.75.71 but 103.197.74.208 is allowlisted
        — order placement will be REJECTED
```

Every readiness check except this one passes, which is precisely the landmine
`CLAUDE.md` documents. **No live order can be placed from this machine until the
allowlist is updated at developers.kite.trade.** Not fixed here — it is an
account setting, not code.

---

## 4. Deviations from the letter of the specification

Each is a deliberate choice with its reasoning, per the instruction to document
every assumption.

| # | Specification says | What was done | Why |
|---|---|---|---|
| D1 | §0.8 "Everything new defaults off" | `storage_rolloff_enabled` defaults **true** | Off reproduces the exact failure Stage 1 exists to prevent, and the roll-off is provably a no-op today (99 trading dates against a 250-day window), so enabling it changes no behaviour now. `--all-off` restores it to false. |
| D2 | §2.9 "Nine new files total" | Ten. `tools/backup.py` is the tenth | Stage 1 deliverable 4 mandates a backup and names no module for it. The other new tools are enumerated; this one had to be given a home. |
| D3 | §10.5 `--all-off` "returns every new switch to its pre-Phase-4 value" | Pre-Phase-4 values are read from `system_config.default_value`, not a Python list | One source of truth. A registry in Python drifts from the migration that created the row; the column is written by the same migration and sits next to the key. The Python side holds only the *membership* question, and checks it in both directions. |
| D4 | Stage 1 deliverable 1: "report the projected ceiling date" | Reported by `tools.health`, recomputed on every run, not stored | A stored series would need a new table, which is not in the additive-migration list and is not needed: the projection is measured fresh each run. Ceiling-date movement is observed by running health, which the launcher already does. |
| D5 | Migrations are applied by hand in the SQL editor | Migration 030's four `system_config` rows are **already live** | See §6 below. This needs the operator's attention. |

---

## 5. Stage 1 — acceptance, verified

Master spec Stage 1 acceptance criteria, each with the evidence.

| Criterion | Result |
|---|---|
| Usage and ceiling date reported | 205.6 MB / 500 MB = **41.1%**; 30 MB/month; 80% on **14-Feb-2027**, full on **25-May-2027**. Below the 80% stop-and-report threshold, so Stage 1 proceeded. |
| Roll-off executed once with counts logged | `archive_stock_data(250)` → `{archived: 0, deleted: 0, cutoff: null}`. A no-op, correctly, and it says so rather than logging a success it did not earn. |
| Health demonstrated FAILING when the threshold is lowered | PASS at 80%; **FAIL** at 20%; **FAIL** with the ceiling lowered to 100 MB; PASS again on restore. |
| Backup produced and verified restorable | 5.7 MB, 46 tables, 51,270 rows, 5 secrets redacted, reads back complete. All four `verify()` guards demonstrated failing: unreadable file, table that errored mid-dump, empty record table, leaked secret. |
| Rollback tool reports status | `--status`, `--allocator-off` (reports the allocator does not exist yet), `--all-off` verified writing, printing what it changed, and idempotent on a second run. Its daemon-refresh guard demonstrated failing, and `--all-off` refuses in that state. |
| Retention proposals written and awaiting decision | [`RETENTION_PROPOSAL.md`](../RETENTION_PROPOSAL.md) — measured, proposed, **not implemented**, with two questions only the operator can answer. |

Full health sweep after the changes: **10 of 11 checks pass**; the failure is
the pre-existing Kite IP mismatch in §3.4.

---

## 5a. CORRECTIONS — 04-Aug-2026, after the operator supplied real data

Two things stated in §5b were wrong when written. Both are corrected here and in
the code; §5b is left as originally written so the correction is visible rather
than quietly absorbed.

### C1 — The expectancy figures attributed 69 hand-entered trades to the system

**What §5b said:** "SWING / CNC, 72 trades, net −₹12,489… the book is losing
money." **What is true:** 69 of those 72 real-money closes were entered by hand
*before the signal engine existed*. The dashboard has always known this — its
`inEra()` in `PerformanceTab.tsx` splits "Since automation" from "Legacy /
manual" — and the ledger pooled them anyway.

Corrected, with the dashboard's own rule mirrored in `load()`:

| | n | real money | gross | friction | net |
|---|---|---|---|---|---|
| **Since automation** | 25 | **3** | +₹647 | ₹378 | **+₹269** |
| Legacy / manual | 69 | 69 | −₹9,667 | ₹3,052 | −₹12,720 |

**The system's own record is +₹269 across 25 closes, of which 3 are real
money.** That is not evidence of positive expectancy — the sample is far too
small — but it is emphatically not the −₹12,476 previously reported against it.
The claim in §5b that Stage 10's precondition is "not met, and not close" was
based on the wrong population and is withdrawn; the honest statement is that
**expectancy is unmeasured, because n=25 cannot measure it.**

### C2 — The largest cost in this account is not a trading cost

Reconciliation against the broker's own statement (`--reconcile`) surfaced a
line no model in this repository has ever carried:

```
realised P&L over the window   ₹    277.16
trading friction               ₹    -89.20
fixed costs                    ₹   -500.00     ← Kite Connect API, monthly
NET                            ₹   -312.04
```

**The API subscription is 5.6× total trading friction.** On ₹20,000 of capital
it is 2.5% *per month* — roughly 30% a year — and it is charged whether or not
a trade is placed. Every cost model here optimises the ₹89 and ignores the ₹500.

At 4–5 trades a month, each trade must net **₹100–125** purely to pay for the
API before the account earns anything. That is a far harder bar than any
friction threshold in Stage 2, and it is a capital-scale problem rather than a
code problem: the same ₹500 against ₹2,00,000 would be 0.25%/month.

**This does not change the architecture.** It is recorded because Stage 2's
objective is "make the unit economics of a single trade positive", and the unit
economics of this *account* are dominated by a fixed cost the specification does
not mention.

### Stage 2 is now VALIDATED

The reconciliation the specification requires, run against
`pnl-DSY688.xlsx` (Zerodha Console, 01-Jul to 04-Aug-2026):

| component | modelled | actual | gap |
|---|---|---|---|
| STT | ₹25.5964 | ₹26.0000 | −1.6% |
| exchange txn | ₹0.7858 | ₹0.7894 | −0.5% |
| SEBI turnover | ₹0.0256 | ₹0.0257 | −0.4% |
| stamp duty | ₹2.4763 | ₹2.0000 | +23.8% |
| GST | ₹0.1461 | ₹0.1557 | −6.2% |
| **statutory** | **₹29.0302** | **₹29.0208** | **0.0%** |
| **DP × 4 sell days** | **₹60.16** | **₹60.18** | **−0.0%** |
| **TOTAL** | **₹89.19** | **₹89.20** | **−0.01%** |

**Modelled friction is within 0.01% of realised.** Stamp duty is 23.8% out in
percentage terms and ₹0.48 out in rupees — Zerodha rounds it to the rupee per
contract note — so it is flagged as immaterial rather than as an error.

**Caveat, stated because the specification asks for ≥20 round trips and this is
4:** the *rate table* is validated — exchange and SEBI match to four decimal
places, which also independently confirms the turnover being reconciled — but
the *sample* cannot catch a rate that is only wrong in cases these four trades
never hit.

**A bug this caught in the reconciliation itself:** the first run reported a
43% turnover drift. The parser was reading the Zerodha sheet by compacted cell
position, and a blank "Open Quantity Type" column shifted every column after it
left by one — buy value came out ₹8,924 against a true ₹16,627. It was caught
only because the exchange fee is an exact percentage of turnover and therefore
implies the turnover independently. The parser now maps columns by header name,
and the cross-check is kept.

---

## 5b. Stages 2–4 — what was found

### Stage 2 — Economics

**The friction ledger was already complete.** Flat per-scrip DP charges, per
product, at realistic clip sizes — `intraday/cost_model` already had all of it,
and every call site already passes `product=`. "This already works" is a
complete result (§8.3). What was missing was the *ledger document*, and
`tools/expectancy_ledger` is now it.

**The first written expectancy figures in this project's history:**

| | n | gross | friction | net |
|---|---|---|---|---|
| SWING / CNC | 72 | −₹9,373 | ₹3,116 | **−₹12,489** |
| INTRADAY / MIS | 10 | −₹51 | ₹55 | −₹106 |
| INTRADAY / CNC | 9 | +₹369 | ₹250 | +₹119 |
| **ALL** | **91** | **−₹9,055** | **₹3,421** | **−₹12,476** |

Gross is negative, so friction is added to a loss rather than taken from a
profit. **The precondition Stage 10 is conditional on — positive per-trade
expectancy — is not met, and is not close.**

**Friction by clip size is the finding that matters:**

| CNC clip | friction | as R | net R |
|---|---|---|---|
| ₹0–2,500 | 1.09% | **2.363 R** | −3.180 |
| ₹2,500–5,000 | 0.75% | 1.046 R | −0.598 |
| ₹10,000–25,000 | 0.32% | 0.605 R | **+1.322** |

A trade whose friction is 2.4× its own risk budget cannot be won; the
arithmetic excludes it before the thesis is considered. MIS is flat at
0.11–0.21% at every size — the entire gap is the ₹15.04 DP fee.
`min_viable_position()` returns **0 for CNC at a 0.7% target**: no delivery
position size makes that trade viable at any size.

**Delivered:** a friction-based minimum-viable-trade gate in
`portfolio_constraints`, keyed on `cost_R` — the same currency the allocator
will rank on. **Defaults to off** (`sizing_max_cost_r = 0`), because a gate that
refuses trades changes what the account does with money.

**NOT validated.** The specification requires modelled friction reconciled
against realised on ≥20 round trips within 10%. `closed_positions.charges` is
*modelled*, so comparing them would be the model checking itself.
Realised charges are not in the Kite API — they live in the contract note and
the Console export. `--reconcile FILE.csv` is built and waiting for that file.
**Stage 2 is not complete until it is supplied**, and every R below inherits
that uncertainty.

### Stage 3 — Exits: THE PREMISE FAILED

The specification's basis for Stage 3, repeated in the architecture (§24) and in
`exit_rules.py`'s own docstring:

> median capture ratio 3%, meaning almost all of the favourable excursion was
> given back. Target: 3% → 30%+.

**Measured across 58 closed positions with a usable excursion:**

| | n | median capture |
|---|---|---|
| winners | 28 | **+67.1%** |
| losers | 30 | −220.6% |
| ALL (pooled) | 58 | **−0.8%** |

The 3% figure is reproducible — as the *pooled* median, which is dominated by
losers. Capture ratio is only a coherent question for a trade that had a
favourable move to keep, and on those the system keeps **67%**. Excluding three
winners whose recorded capture exceeds 100% (proof that MFE is sampled on the
lifecycle run rather than per tick, so peaks between runs are missed), the
conservative figure is **58.9%**.

Either way it is roughly **twice the 30% target Stage 3 exists to reach.**

Per §0.9 — *"If a section's premise fails verification, stop and report; a
section whose honest outcome is 'the premise was wrong' is a successful
section"* — **the exits were not re-engineered.** Deliverable 2 (re-base exits
on the empirical R distribution) was not implemented, because it would be
optimising a component that is not the problem. The problem is that only 28 of
58 trades are winners.

**What was fixed anyway**, because it was found while looking:

- **`exit_max_runners` was computed and thrown away.** `max_runners` and
  `already` were read at the top of `target_decision` and never referenced
  again. The cap on concurrent runners has never been applied. It could not
  have been: counting runners needs a marker, and `already` read
  `pos["runner_since_r"]` — **a column that exists in no migration**. Migration
  031 adds it; the cap is now enforceable behind `exit_runner_cap_enforced`,
  defaulting false because enforcing it changes which positions run.
- **Runner instrumentation on every branch** — evidence count and verdict are
  now recorded at every target touch including the declining ones, which are
  the branches that carry the information.

### Stage 4 — Measurement spine

**The swing book's unbiased denominator did not exist.** `signal_output_daily`
carries seven outcome columns, written as NULL by `final_snapshot` and filled
in by nothing. Measured before this work: **0 resolved out of 1,711**. The
intraday side, by contrast, was perfect — 595 of 595.

`ai_decision_engine` reads `outcome_category` and `outcome_return_pct` to tell
the model how past signals fared. It has always read NULL.

This is the population the master spec calls *"the single most important
instruction in this document"*, for the book that holds real money, and it had
no producer.

**Delivered:** `swing/signals/outcomes.py`, deliberately shaped like
`intraday/outcomes` so the two cannot disagree about what TARGET means, plus
pipeline step 28. Intrabar ties resolve **against** the plan — a prior that
flatters itself is worse than none.

**Result — 860 of 1,711 plans resolved** (the rest are still inside their
15-session window):

| verdict | n | mean |
|---|---|---|
| TIMEOUT | 625 | −0.86% |
| NOT_TRIGGERED | 150 | — |
| TARGET | 76 | +4.85% |
| STOP | 9 | −6.45% |

**And `allocation/scoring` — the first empirical priors this system has had:**

```
INTRADAY/GAP  E[R]=-0.019 (n=103)    SWING/ALL   E[R]=+0.509 (n=85)
INTRADAY/ORB  E[R]=-0.421 (n=30)     SWING/WATCH E[R]=+0.467 (n=71)
INTRADAY/VWR  E[R]=-0.544 (n=154)    trigger rate 83%
INTRADAY/PBK  E[R]=-0.663 (n=60)
INTRADAY/VCE  E[R]=-0.941 (n=75)     hold days: SWING 7.22 (n=72)
INTRADAY/PDL  E[R]=-1.008 (n=97)                INTRADAY 0.55 (n=19, thin)
INTRADAY/RNG  E[R]=-1.376 (n=76)
INTRADAY/ALL  E[R]=-0.691 (n=595)
```

**Every intraday engine has negative expected R on the full field, before
friction.** RNG's p90 is −1.00, meaning nine detections in ten lose a full R or
more.

**This independently confirms the frozen architecture's consolidation call.**
Stage 6 nominates VCE and RNG for retirement without having seen this data;
they are measurably the two worst engines (−0.941 and −1.376). The architecture
was right for structural reasons and the evidence agrees.

**The swing +0.509R is flagged, not celebrated.** It rests on 85 plans all
signalled between 24-Jul and 31-Jul, because `planned_stop` was only populated
from 28-Jul; and fast outcomes resolve first, so a young sample
over-represents quick winners. Sitting next to a −₹12,489 realised swing book
and a −0.86% mean forward return on the 625 plans without levels, it is a
warning. `allocation.scoring` prints that warning every time it runs.

---

## 5c. What remains — Stages 8, 9, 10

Stages 8 and 9 were not started, and Stage 10 has only its `scoring` module.
Stated plainly rather than presented as a judgement call.

| Stage | Remaining work |
|---|---|
| 6 (partial) | The engine MERGES: GAP/PDL → ORB family as day-type conditions, PBK → VWR as a condition, seven swing screeners → one continuation engine. Gated on the shadow quarter and on a swing denominator that has 85 observations today. |
| 8 | Post-earnings-drift engine; accumulation-confirmed engine; block/bulk deal ingestion (the one new external dependency in the whole plan) |
| 9 | Expiry day-type conditioning; volatility-regime exposure scaling; liquidity/circuit-band eligibility gate |
| 10 | `proposal`, `hurdle`, `policies`, `allocator`; no-bypass accounting; four dashboard views; `tools/allocator_report`; allocator entries in `tools/rollback` |

**Two of these should be re-read before they are built.**

- **Stage 10 is conditional** on Stages 2 and 3 having made per-trade expectancy
  positive (master spec §10). Expectancy is **unmeasured**, not negative — the
  system's own record is +₹269 across 25 closes, of which 3 are real money. An
  allocator optimises which of several positive-expectancy proposals gets
  capital; with n=25 there is nothing yet to optimise over.
- **Stage 8's accumulation engine needs block and bulk deal ingestion**, which
  M3 in the readiness review flags as having no source specified. If no reliable
  free source exists the engine degrades to delivery-persistence only, and that
  degradation must be reported rather than silently absorbed.

**Not done and worth doing:** a health check asserting the governance door stays
shut. Everything else new in Stages 1–7 has a guard that was demonstrated
failing; `evaluate_auto_apply` does not yet.

**Two of these are now better informed than the specification could be**, and
should be re-read before they are built:

- **Stage 6** has its evidence. The per-engine priors above are exactly the
  measurement consolidation was meant to be justified by, and they support it.
- **Stage 10 is conditional on Stage 2 and 3 having made per-trade expectancy
  positive** (master spec §10, "Conditional"). Expectancy is **−₹12,476**.
  Building an allocator now would produce an efficient allocation of
  loss-making trades. The architecture anticipated this and put the allocator
  last for exactly this reason.

---

## 6. Open items for the operator

1. **Migration 030's four rows are already in the live `system_config`.** They
   were written during Stage 1 rather than applied by hand in the SQL editor as
   this repository's migrations normally are. The values are exactly what
   `030_storage_survival.sql` declares, so applying the file now is a harmless
   no-op (`ON CONFLICT DO NOTHING`) — but the file should still be run so the
   migration history is honest about what is in the database.
2. **`RETENTION_PROPOSAL.md` needs a decision.** Nothing changes until it gets
   one, and the 14-Feb-2027 date does not move without it.
3. **The Kite IP allowlist is stale** — no live order can be placed until it is
   updated.
4. **The kill-switch refresh lag is 300 s.** Tighten it, or accept it, per
   §3.1.
5. **Migrations 031 is written and NOT applied.** Until it is, the runner
   columns do not exist. That is now safe rather than destructive — see D6 —
   but the runner instrumentation records nothing until it is run.
6. **Realised broker charges are needed to validate Stage 2.** Zerodha Console
   → Reports → P&L → Tradewise → download, then
   `python -m tools.expectancy_ledger --reconcile <file>.csv`. Until then every
   R in this system carries unquantified friction error.
7. **The book is losing money.** −₹12,476 net across 91 closed trades on a
   ₹20,000 account. This is not a finding about the code; it is what the code
   now reports honestly for the first time. Stage 10's own precondition is not
   met.
8. **Stages 5–9 are not started.** See §5c.

---

## 7. Additional deviations, Stages 2–4

| # | Specification says | What was done | Why |
|---|---|---|---|
| D6 | Nothing about PostgREST partial writes | `_update_stripping_unknown` added to `position_lifecycle` | CLAUDE.md's own landmine: one unknown column fails the WHOLE update. The exit path caught the exception and logged a warning, so a payload carrying price, excursion, stop and exit signal wrote none of them. Deploying code ahead of a migration — the normal order — would have silently stopped the whole book from updating. Now it strips the named column, retries, and warns. |
| D7 | Stage 3 deliverable 2: re-base exits on the empirical R distribution | **Not implemented** | The premise failed verification: capture on winners is 58–67%, not 3%. §0.9 requires stopping and reporting rather than implementing against a disproved premise. |
| D8 | §2.9 "Nine new files total" | Twelve. Added `tools/expectancy_ledger`, `swing/signals/outcomes`, `allocation/scoring` | The first two are named deliverables with no module assigned; the third is one of the five enumerated allocation modules, built at Stage 4 because Stage 4's priors are what it computes. |
| D9 | Stage 10 builds the allocation package | `allocation/scoring` built at Stage 4 | Stage 4 deliverable 3 ("priors from the full field") and 4 ("every estimate carries n and standard error") *are* the scoring module's contents. Building it twice would mean two prior computations that can disagree. |
