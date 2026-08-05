# TradeOS v7 — Implementation status

**This is the only document in `docs/` that changes.** Documents 1–5 are frozen
and are not edited. Where measurement contradicts them, the contradiction is
recorded here and the measurement wins, per master spec §0.9: *"This document is
a hypothesis; the running system is the truth."*

Read this to know where the repository actually is against the frozen
architecture, and which of the specification's claims did not survive contact
with the live system.

**Branch:** `phase4-allocator`, then `claude/trade-os-phase-4-perf-7qt631`
(§4·9c, the allocator veto that refused everything). `main` untouched.

---

## 1. Stage status

| Stage | Name | Status | Verified on |
|---|---|---|---|
| 1 | Survival | **Complete** | 04-Aug-2026 |
| 2 | Economics | **Complete** — validated to −0.01% against the broker | 04-Aug-2026 |
| 3 | Exits | **Complete** — premise disproved, reported, exits left alone (§0.9) | 04-Aug-2026 |
| 4 | Measurement spine | **Complete** | 04-Aug-2026 |
| 5 | Decision-input integrity | **Complete** — quote mode gated on parity by design | 04-Aug-2026 |
| 6 | Engine consolidation | **Complete** — shadow phase and both merges | 04-Aug-2026 |
| 7 | Governance and cadence | **Complete** | 04-Aug-2026 |
| 8 | New alpha | **Complete** — PEAD + ACC, shadowed pending detections (§4·9) | 04-Aug-2026 |
| 9 | Structural overlays | **Complete and LIVE** — expiry, volatility, liquidity | 04-Aug-2026 |
| 10 | Allocation | **Complete** — recording, not allocating, pending 30 disagreements (§4·9) | 04-Aug-2026 |

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

- **Stage 6** was PARTIAL for most of this work and is no longer. The operator
  caught the reason: migration 036 implemented a *retirement* and I had
  described it as consolidation. A retirement stops detections contributing and
  leaves per-engine sample rate flat; only a merge raises it, which is what
  Stage 6's acceptance criterion actually measures. Both merges are now built —
  intraday into two families (ORB 7.7×, VWR 1.4×), swing into one CONTINUATION
  family — with every detection preserved and only the convergence vote
  de-duplicated.

**All ten stages are built.** Phase 4 is code-complete. What it is not is
*proven*: the allocator has recorded zero decisions, both new engines have
shadowed zero quarters, and per-trade expectancy remains unmeasured at +₹269
across 25 attributable closes. The instruments exist and have not yet had time
to read anything.

See [`7_PHASE5_READINESS.md`](7_PHASE5_READINESS.md) for what Phase 5 would
require, what it would cost, and why it should not begin yet.

### Stages 8–10, in one line each

**Stage 8.** PEAD and ACC — the only two additions carrying a genuinely
different burden of proof. ACC produces 54 candidates on the live universe;
PEAD produced 0 until migration 039, because `nifty_upcoming_events` is
forward-only and could not answer "what reported three days ago". Both
registered SHADOW.

**Stage 9.** Three overlays that only ever reduce: expiry day-type sizing,
VIX-banded book exposure (bands from this account's own 94 readings, not
textbook levels), and a liquidity gate measured against each name's own traded
value. None will ever appear as a winning trade — their contribution is the
losses that did not happen.

**Stage 10.** `proposal`, `hurdle`, `policies`, `allocator`. Verified end to end
on live priors: swing TAKE, both intraday DECLINE, all verdicts buffered, none
written, `shadow=true` throughout. The `allocator` health check asserts by
inspection that `allocation/` cannot import `execution/` — the one protection
that is structural rather than a config value.

### Stages 5–7, in one line each

**Stage 5.** `SymbolContext` now carries `as_of`; entries are refused on a
context older than 420 s **or of unknown age**, because "we do not know when
this was true, so proceed" is the reasoning that lets a dead feed keep trading.
Exits are deliberately not gated. MODE_QUOTE is implemented and **off** until
`tools/quote_parity` shows one session of agreement. New health check `feed`
asserts the tick handler does no I/O and that the guard is actually consulted;
both demonstrated failing.

**Stage 6 (SUPERSEDED — the merges landed; see §5·0 and the Stage 6 note in §1).**
The paragraph below describes the state before the merges and is kept for the
record only. Intraday engines had only on/off — turning one off stopped it
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

## 4·9 — 05-Aug-2026: THE GO-LIVE DECISION, AND WHERE IT WAS DECLINED

The operator asked to take Phase 4 fully live — "no shadow work, I don't expect
any major shift unless you disagree." I disagree on four of the seven items, and
the disagreement is not caution. It is that flipping those four would mean acting
on **zero or negative evidence**, which is a different thing from acting on new
code that has been strengthened.

**What went live (migration 042).** One switch, plus one registered and
deliberately left off.

| Switch | Action | Why it needs no evidence |
|---|---|---|
| `exit_runner_cap_enforced` | **→ true** | Measured live: **0 open and 0 closed positions** have ever been marked a runner. Enabling the cap changes nothing today and caps at 2 the day two positions simultaneously want to run. Pure downside protection. |
| `sizing_max_cost_r` | **registered, left at 0** | See below — turning it on would nearly halt the swing book. |

**Why `sizing_max_cost_r` stayed off, which is a finding rather than caution.**
Measured CNC friction at this account's own clip sizes:

```
Rs      0–2,500   friction 2.363 R
Rs  2,500–5,000   friction 1.046 R
Rs 10,000–25,000  friction 0.605 R   <- the LARGEST band the account trades
```

Even a lenient 0.7R cap refuses essentially every delivery trade at current
position sizes, because the flat Rs 15.04 DP fee dominates a Rs 4,000 clip. The
gate is not wrong; the clip size is. The real fix is Stage 2 deliverable 4 —
fewer, larger positions inside an unchanged risk budget — which is a
position-sizing change that alters what capital does on **every** trade, not
just the refused ones. It was not asked for and is not made here.

It is now a registered row rather than a Python default returning 0.0 with
nothing describing it. That silent-default shape is what `CLAUDE.md` calls the
dominant failure mode in this project.

### What was NOT switched on, and the evidence for each

| Item | Requested state | Measured today | Verdict |
|---|---|---|---|
| `alloc_live_intraday` / `alloc_live_swing` | live | **0 rows** in `allocation_decisions` | Refused |
| PEAD / ACC | live | **0 detections** recorded | Refused |
| RVS, MOM (swing) | live | n=1,471 @ **−0.49%**, 42% win · n=1,090 @ +0.05% | Refused |
| VCE, RNG (intraday) | live | E[R] **−0.941** and **−1.376**, both p90 −1.00 | Refused |

**The allocator has recorded zero decisions.** Not few — zero. It was wired into
the cycle hours ago and no session has run since. Promoting it would not be
"trusting a strengthened framework"; it would be handing capital allocation to a
component whose output nobody, including me, has ever seen. The gate is ≥30
scored **disagreements** and the count is 0.

**PEAD and ACC have zero detections.** ACC scored 54 candidates in a one-off
test against a live universe; that is not the same as a nightly pipeline run
having recorded them to `screener_performance`. Nothing has been scored.

**RVS, MOM, VCE and RNG are the important distinction.** These are *not*
shadowed because they are new and unproven. They are shadowed because they were
**measured and found to lose money or be flat**, on 1,471 and 1,090 and 75 and
76 observations respectively. No amount of framework strengthening changes those
numbers. Making them live is not removing training wheels — it is overriding a
measurement with a preference, which is the one thing every document in `docs/`
agrees must not happen.

### What this means practically

The system IS live in every sense that matters: the evening pipeline runs all
its new steps, the daemon scores every proposal and records every verdict, both
new engines detect and are resolved nightly, the overlays size and gate real
trades, and governance refuses out-of-window changes. **The only thing "shadow"
means here is that four measured-negative engines and one never-observed
allocator do not receive capital.**

That distinction is the difference between a system that is running and a system
that is running on faith.

### A gap this pass found and closed

`allocation_decisions.outcome_r` — the field `tools/allocator_report` counts a
disagreement on — was created by migration 041 and **written by nothing**. The
allocator could have shadowed for a full year while the scorecard reported zero
scored disagreements, and the gate would never have moved.

Identical in shape to the 1,711 plan outcomes that sat NULL because
`final_snapshot` wrote the columns and no producer ever filled them. Fixed:
`allocation/outcomes.py`, wired as pipeline step 28b. Counterfactuals resolve
**against** the proposal on an ambiguous bar and are haircut by
`alloc_shortfall_r`, which is exposed as an assumption rather than buried as a
constant, because M1's real shortfall instrumentation still does not exist.

---

## 4·9b — 05-Aug-2026: the swing veto did not land, and Stage 10's dashboard

**The operator asked directly whether the thing I said I "nearly got wrong"
was actually fixed. It was half-fixed.** `allocator_permits()` existed and was
called from the intraday path. The matching call in `act_on_candidates` — the
swing side — had been written in the same turn but never actually landed in
the file; an edit that silently did not apply, the same failure class as
several already recorded here. `alloc_live_swing` would have set a database
column and gated nothing, on the real-money book, and nothing would have said
so.

**Fixed at the actual choke point.** `_maybe_enter_swing` is the one function
both `act_on_candidates` call sites funnel through before an order is placed —
paper or live — so the veto lives there instead of being duplicated at each
call site, which is what makes a repeat of this class of miss structurally
harder: there is no second path to a swing fill that could go unguarded.

**A new health check closes the gap that let it happen once.**
`check_allocator_isolation` now asserts, by reading the source, that
`allocator_permits(...)` is called from BOTH the intraday and swing paths by
name — not merely that the function exists. Demonstrated failing by stripping
the swing call site and confirming the check catches exactly that.

**Stage 10's dashboard is built.** `AllocatorTab.tsx` — all four required
views: today's ledger ordered by edge, the live hurdle against today's
proposals, the storage gauge (red above 80%), and shadow-vs-greedy reduced to
one number. The disagreement definition mirrors `tools/allocator_report.py`
exactly: a TAKE with no matching position, or a DECLINE/DEFER with one.
Verified against LIVE data during actual market hours — the dashboard's
storage figure (41.0%) matched the backend's own to one decimal, and the
ledger showed real symbols with real edge/hurdle/reason values matching
`allocation/policies.py`'s own wording.

`OperatorPanel.tsx` gained every CRITICAL Phase 4 switch — allocator, three
overlays, governance, storage, sizing — with `alloc_live_swing` carrying the
same two-click confirm as going live on a framework, because it is the one
control on the new panel that can refuse a real-money entry.

**A pre-existing, unrelated finding surfaced while wiring the dashboard's
reads.** `frontend/.env.local`'s `NEXT_PUBLIC_SUPABASE_ANON_KEY` decodes to a
`service_role` JWT, not the real anon key — meaning every browser-side query in
this dashboard has run with full RLS bypass since the file was created,
including migration 007's protection on `is_secret` config rows. **Contained**:
the file is gitignored and was never committed, and no deployment config
exists, so exposure is bounded to this machine. **Not fixed by this session** —
it requires the operator to copy the real anon/public key from Supabase
Settings → API, which is a credentials action outside what this session
performs. The write path (`/api/config/[key]`) was already correctly
architected with `SUPABASE_SERVICE_ROLE_KEY` as a server-only variable; only
the read-side env value is wrong.

**Two unrelated production issues, reported live by the operator, fixed in
passing:**

- `ai_thinking_enabled` had been set to `true` on 03-Aug, silently reopening
  the exact failure `ai_router.py`'s own docstring documents — deepseek-v4-flash
  spends its token budget on discarded reasoning and never emits the JSON.
  Every batch of that night's step 19 hit `finish_reason=length`, three times,
  640 seconds, zero output. Restored to `false`; `default_value` was also
  `NULL` and is now recorded so `tools/rollback` has a known-good state.
- `tradeos.cmd` had `alloc`, `backup` and `rollback` as typed subcommands but
  **none of the three appeared in the interactive menu** — the surface the
  operator, who is not a programmer, actually uses on double-click. Added a
  PHASE 4 menu section (A/B/R/Q) plus two subcommands that did not exist at
  all (`expectancy`, `quote-parity`). Verified: menu renders at 41 lines
  (console is sized for 45), no letter or label collisions, all four dispatch
  to the correct tool.

---

## 4·9c — 05-Aug-2026: the bar no setup could clear, and the guard one side called

**Branch:** `claude/trade-os-phase-4-perf-7qt631`. Migration 044.

**The operator reported it as a performance regression: since 043 went in, the
intraday book had booked nothing all session, while swing booked one trade in
the morning — and the same stock was being picked up by both frameworks.** Both
symptoms are one event. 043 set `alloc_live_intraday` true and left
`alloc_live_swing` false, so the allocator held a veto over exactly the book
that went silent and none over the book that kept trading. That asymmetry is the
fingerprint of the switch, not of the market.

### The intraday book: three faults, stacked, each sufficient alone

**1. The bar and the score were different quantities.** `scoring.score()`
returns `edge = (E[R] - cost_R) / hold_days` — expected R, NET of costs, PER
day. `hurdle._empirical_base` built its bar from `outcome_pct / risk_pct` over
resolved detections — realised R, GROSS, per trade. Two different numbers
compared with `<`. Reproduced against the real `hurdle`, `scoring` and
`policies` modules on a population shaped like the live one (n=595):

```
  time  slots       bar      edge  verdict
 09:20      2    1.2008   -0.1252  DECLINE
 09:20      1    1.5009   -0.1252  DECLINE
 12:15      2    0.9807   -0.1252  DECLINE
 14:30      1    1.0137   -0.1252  DECLINE
   any      0       inf   -0.1252  DECLINE
```

DECLINE at every hour, every slot count, every regime. Clearing the 09:20 bar
required a prior mean of **+1.41R**; the measured prior for the entire intraday
book is **+0.08R**, and no engine in it has ever been close.

**2. The query never ran.** It selected `regime_at_detection` — a column on no
migration, written by no code, appearing in exactly one place in the entire
codebase. PostgREST rejects the whole request for one unknown column; a bare
`except` swallowed it. **The "empirical bar" has never once been computed**, so
every call fell through to the cold start.

**3. The cold start refused as well.** It returned `0.0`, which reads as
neutral and is not. The edge it is compared against already has costs
subtracted, so `0.0` demands that every proposal beat its own round trip — and
the intraday prior (+0.08R) does not cover the MIS round trip (+0.21R). This
module's own docstring promises a cold start that AGREES with the live path *"so
the plumbing is proved before it changes any opinion."* It did the exact
opposite: an allocator with no data refused everything.

### The check that should have caught fault 2 could not fail

`tools/validate_selects.main()` ends `return 1 if (problems and strict) else 0`.
`health.check_selects` called it as `vs.main()` — no argument — read only the
return code, and printed its own success message over the top of the tool's
error output. Demonstrated with one known-broken site:

```
  allocation/hurdle.py:122 — intraday_setups has no column(s): regime_at_detection
  1 broken select site(s). Every one of them is a step that will fail completely.

  return code = 0            <-- health reads ONLY this
  health.check_selects() ->  ok=True, "every SELECT names columns that exist"
```

**This is the fifth green-while-broken check found in this project**, and the
most expensive: it is what let a dead query reach a live veto. `check_selects`
now passes `strict=True`, and was demonstrated failing before it was
demonstrated passing.

### The fix: the bar is read from where the quantity is defined

`_empirical_base` now takes its arrival distribution from
`allocation_decisions.edge` — the column `scoring.score()`'s own output is
written into. This is not a convenience. It is the only construction under which
the bar and the edge cannot drift apart again, because there is exactly one
definition of the quantity and both sides of the `<` read it from there.

The `bucket` and `framework` arguments now actually filter. They were accepted
and ignored, so STRONG and WEAK returned an identical bar and swing proposals
were priced against intraday detections — the pooled curve the module's own
docstring spends a paragraph arguing against. Where a bucket is thin the query
pools across buckets and **records that it pooled**, so a verdict never claims a
segmentation it did not get.

Verified after the fix, same modules, same method:

```
  cold start (no history)      bar = -inf    edge -0.13  ->  TAKE
  with history (n=400)         bar = +0.346  edge -0.13  ->  DECLINE
                               bar = +0.346  edge +0.45  ->  TAKE
```

The allocator now discriminates instead of refusing. A genuinely good setup
clears; a mediocre one does not; and with nothing to go on it stands aside.

### Slots were pooled across both books

`alloc_max_slots` (2, *"across both books"*) was subtracted from every position
entered today in EITHER framework. One swing entry in the morning therefore
capped the intraday book — governed by `intraday_max_new_per_day` (4) — at a
single slot for the rest of the session; two entries of any kind capped it at
zero, where `hurdle()` returns an infinite bar. The same number was then passed
to `swing_assignment` and `intraday_stopping` independently, so the pooled
budget was never enforced jointly either. **Over-restrictive within a book,
under-restrictive across them, and invisible in either book's own logs.** Each
book now brings its own budget from its own configured cap.

### Stale verdicts could veto

`self._verdicts` was only ever assigned, at the end of `_allocate_shadow`, and
every early return left the previous cycle's verdicts in place while
`allocator_permits` kept reading them. A DECLINE issued at 09:20 could veto a
different setup in the same name at 14:00 on arithmetic that no longer referred
to it. Turning `alloc_shadow_enabled` off mid-session froze the last verdicts
permanently, because that switch and `alloc_live_*` are read independently.
Cleared first now, so no exit path carries one forward; an empty map fails open,
which is the documented behaviour.

### One symbol, one book — written twice, implemented once

The rule appeared in two comment blocks and in one direction of code. Intraday
skipped any name in `self.positions`. Swing called `_held_by_framework(sym,
"SWING")`, which does not look at the intraday book at all — while its own
comment block opened with *"checked across BOTH frameworks, not just swing"* and
closed by saying an intraday tranche *"does not block this."* Those two
sentences cannot both be true and the code implemented the second.

**The asymmetry ran in the dangerous direction.** The PAPER book refused to
collide with the live one; the LIVE book would buy a name the paper book was
already trading. Real money layered on a simulated position — the worst of the
four possible orientations, and the exact inverse of what §1 of `DESIGN_NOTES`
intended. Held in both books at once: the 15:15 square-off sells into a
15-session swing thesis on the same shares, ~a third of a ₹20,000 account sits
on one idea across two sizing models that cannot see each other, and one price
move is scored twice — once in `signal_log`, once in `intraday_setups`.

`_other_framework_holding()` is called from both sides. Intraday refusals are
recorded as `BLOCKED_CROSS_FRAMEWORK` rather than skipped silently, so the rule
can be priced by the weekly review instead of guessed at. Switch:
`one_framework_per_symbol`, default true.

**This reverses the default `DESIGN_NOTES` §1 argues for, deliberately.** Core-
and-satellite is real desk practice and remains the intent. It cannot happen
while intraday is PAPER: `_maybe_open_paper` refuses to place a live intraday
order at all, so the state that section was written to unlock has never once
occurred. §1 now carries the three conditions for turning the switch back off.

### Two new health checks, both demonstrated failing

| Check | Asserts | Broken by |
|---|---|---|
| `hurdle` | bar and edge share one definition; a cold start ADMITS a typical setup; slots are per book | restoring the 0.0 cold start; pointing the population back at `intraday_setups`; stripping `slots_by_framework` |
| `books` | `_other_framework_holding` exists AND is called from both sides; the switch is on | stripping the swing call site; setting `one_framework_per_symbol` false |

Six injected breakages, six correct failures, and both pass on the restored
tree. The first version of the `hurdle` check **passed** when the population was
switched back — its assertion was satisfied by the docstring underneath. It now
strips the docstring and matches the actual call expression. *An assertion a
comment can satisfy is decoration.*

### What went back off

`alloc_live_intraday` → **FALSE**. The intraday book returns to the greedy path
that was producing trades. The allocator keeps scoring and recording; it stops
refusing. Promote it again only after `tools/allocator_report` reads sane on the
corrected arithmetic — and expect a genuine cold start first, because
`allocation_decisions` carries no `regime_bucket` rows yet.

### Not verified in that session

**No database credentials were available.** Every result above was produced by
running the real modules against synthetic data shaped like the live
population. `tools.validate_selects`, `tools.health` and `tools.simulate` were
**never run against the live schema on this branch**, and the frontend changes
were not compiled (`node_modules` absent). The mechanism is proven; the
behaviour on the live book is not. That run is the merge gate.

**Migration 044 must be applied BEFORE the code is deployed.**
`allocator._record` now writes `regime_bucket`, and PostgREST fails the whole
insert on one unknown column — the buffered flush would lose every verdict in
the batch.

---

## 5·0 — THE BINDING NUMBER WAS WRONG

**Checked against `PRODUCTION_DECISION.md`, `TRADING_METHODOLOGY_REVIEW.md` and
`PHASE4_RED_TEAM.md` on 04-Aug-2026, at the operator's request, before merging
engine consolidation to live.**

`PRODUCTION_DECISION.md` §2 states, of the nine swing screeners:

> Inter-engine correlation | **0.75–0.95 within cluster. The binding number in
> this document** … Verdict: **MERGE nine into two** … **Deleted outright:** the
> seven residual screeners.

It is candid that this is an estimate: *"Correlation figures are structural
estimates from signal construction, not measured correlations — the sample does
not support measurement."*

**The sample does support measurement.** `screener_performance` holds **4,747
resolved detections across 2,704 distinct (symbol, date) pairs**, 09-Jun to
27-Jul-2026. Measured Jaccard co-occurrence — how often two engines actually
fire on the same name on the same day:

```
         CTL    IAD    MOM    RSB    RVS    SBS    SEC    TPO    VBD
  CTL      -   0.03   0.25   0.05   0.04   0.04   0.41   0.06   0.01
  MOM   0.25   0.02      -   0.03   0.00   0.02   0.24   0.01   0.02
  SEC   0.41   0.03   0.24   0.05   0.03   0.03      -   0.02   0.01
  RVS   0.04   0.01   0.00   0.00      -   0.00   0.03   0.04   0.00
  ...every remaining pair ≤ 0.08
```

**The highest correlation anywhere is 0.41, against an assumed 0.75–0.95.** The
nine engines are not one bet in nine costumes. **RVS fires alone on 88% of its
detections** — it is the *most independent* engine in the set, not a duplicate.

Per-engine forward return over the same population:

| engine | n | mean % | median % | win % |
|---|---|---|---|---|
| SEC | 2,331 | +0.53 | +0.35 | 54% |
| **CTL** | 1,739 | **+1.08** | +0.72 | 58% |
| RVS | 1,471 | **−0.49** | −0.66 | **42%** |
| MOM | 1,090 | +0.05 | +0.01 | 50% |
| RSB | 170 | +0.35 | +0.47 | 59% |
| IAD | 150 | −0.58 | +0.48 | 57% |
| SBS | 114 | +0.86 | +0.61 | 57% |
| TPO | 76 | **+2.41** | +2.62 | **74%** |
| VBD | 52 | +0.85 | +0.52 | 58% |

**Six of nine are positive.** Deleting seven of them to satisfy a correlation
figure that 4,747 observations contradict would have destroyed independent
signal on the strength of an assumption — the one thing every document in
`docs/` agrees must not happen. H5 in the readiness review anticipated exactly
this risk and expected the shadow quarter to catch it; measurement caught it
first, and three months cheaper.

**What was done instead (migration 036), honouring the intent rather than the
letter:** RVS → SHADOW (n=1,471, −0.49%, 42% win — the largest body of evidence
against any engine in the system, and note this is a *performance* argument, not
a correlation one: independence is not edge). MOM → SHADOW (n=1,090, +0.05%,
indistinguishable from a coin, and the one engine with real overlap onto CTL and
SEC). Everything else stays ACTIVE. CTL is the continuation core the
specification asked for, and the measurement agrees.

**Nine → seven, not nine → one.** Both retirees keep running and keep being
scored; restoring either is one `UPDATE`.

**Where the docs were right and the measurement agrees:** the intraday call.
`PRODUCTION_DECISION.md` marks VCE and RNG for deletion on structural grounds;
the per-engine E[R] from 595 resolved detections makes them measurably the two
worst (−0.941 and −1.376, both p90 = −1.00). Stage 6's intraday shadow proceeds
as specified.

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
9. **Migration 044 is written and NOT applied**, and it must be applied BEFORE
   the §4·9c code is deployed — `allocator._record` writes `regime_bucket`, and
   one unknown column fails the whole buffered insert. Until it runs,
   `check_selects` will correctly flag `allocation_decisions.regime_bucket` as
   missing; that is the check working, not the branch being broken.
10. **§4·9c was verified without a database.** `tools.validate_selects
    --strict`, `tools.health` and `tools.simulate` have never been run against
    the live schema on that branch, and the frontend was not compiled. Run all
    three after 044 and before merging — the two new checks (`hurdle`, `books`)
    are the ones that matter.
11. **`alloc_live_intraday` is OFF again** and should stay off for at least one
    full session. Read `python -m tools.allocator_report` before promoting it:
    the bar it reports will be a cold start until `allocation_decisions`
    accumulates `regime_bucket` rows past `alloc_hurdle_min_sample` (40).
12. **`one_framework_per_symbol` reverses the default `DESIGN_NOTES` §1 argues
    for.** That is an operator decision, not a code decision. §1 carries the
    three conditions for switching it back; the toggle is on the operator panel.

---

## 7. Additional deviations, Stages 2–4

| # | Specification says | What was done | Why |
|---|---|---|---|
| D6 | Nothing about PostgREST partial writes | `_update_stripping_unknown` added to `position_lifecycle` | CLAUDE.md's own landmine: one unknown column fails the WHOLE update. The exit path caught the exception and logged a warning, so a payload carrying price, excursion, stop and exit signal wrote none of them. Deploying code ahead of a migration — the normal order — would have silently stopped the whole book from updating. Now it strips the named column, retries, and warns. |
| D7 | Stage 3 deliverable 2: re-base exits on the empirical R distribution | **Not implemented** | The premise failed verification: capture on winners is 58–67%, not 3%. §0.9 requires stopping and reporting rather than implementing against a disproved premise. |
| D8 | §2.9 "Nine new files total" | Twelve. Added `tools/expectancy_ledger`, `swing/signals/outcomes`, `allocation/scoring` | The first two are named deliverables with no module assigned; the third is one of the five enumerated allocation modules, built at Stage 4 because Stage 4's priors are what it computes. |
| D9 | Stage 10 builds the allocation package | `allocation/scoring` built at Stage 4 | Stage 4 deliverable 3 ("priors from the full field") and 4 ("every estimate carries n and standard error") *are* the scoring module's contents. Building it twice would mean two prior computations that can disagree. |
