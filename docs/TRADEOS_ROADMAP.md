# TradeOS — Roadmap

**Single source of truth for sequence and protocol. Every Claude Code session
reads this first and writes to `docs/FINDINGS.md` last.**

Supersedes the phase numbering inside the three spec documents. Those remain
authoritative for *how* each stage is done; this document is authoritative for
*which stage we are in and whether we may move on*.

| Stage | Spec | Changes code? |
|---|---|---|
| 0 Setup | this document | no |
| 1 Population | `EDGE_DIAGNOSTIC.md` Phase A | no |
| 1b Data repair | conditional — see Gate 1 | **yes** |
| 2 Decomposition | `EDGE_DIAGNOSTIC.md` Phases B, C | no |
| 3 Replay | `PHASE_E_HISTORICAL_REPLAY.md` | new tool only |
| 4 Subtract | `EDGE_DIAGNOSTIC.md` Phase D | config + retirements |
| 5 Observe | this document | no |
| 6 Harden | `HARDENING_BRIEF.md` Phases 0–6 | yes |
| 7 Cadence | this document | ongoing |

---

# PROTOCOL

## The five rules

**1. The ledger is the memory.** `docs/FINDINGS.md` is append-only. Every session
starts by reading it and ends by appending to it. Never edit or delete a prior
entry — a finding later proved wrong gets a new entry saying so, not a rewrite.

**2. Every number must be traceable.** A finding states the command that produced
it and pastes the raw output. A number without a command behind it does not go
in the ledger. If a tool fails, record the failure — do not work around it and
report success.

**3. "I could not compute this" is a required finding.** Missing columns,
insufficient sample, a tool that errors — all of these are results. Reporting an
estimate in their place is the single most damaging thing a session can do here,
because everything downstream inherits it silently.

**4. One stage per session. Stop at the gate.** A session told to diagnose does
not fix. A session told to fix one thing does not fix two. If a session finds
something urgent outside its stage, it records it and stops.

**5. The human decides at every gate.** Claude Code produces evidence and a
recommendation. Approving a retirement, a config flip or a merge is Vipin's call,
made against the ledger — never automatic.

## What keeps running throughout

- **Swing live book: unchanged.** Keep trading it. Nothing modifies it before
  Gate 3
- **Intraday paper: unchanged, and this matters.** It is free sample generation,
  and `intraday_setups` records the refused population too. Every session it runs
  produces more evidence than its four trades suggest. Do not pause it to "stop
  the losses" — paper losses cost nothing and the data is the point
- **The evening pipeline: unchanged**

## Session handoff format

Every session opens with:

```
Read docs/TRADEOS_ROADMAP.md and docs/FINDINGS.md.
Confirm which stage we are in and what the last session concluded.
Then execute STAGE <n> only. Stop at its gate.
```

Every session closes by appending:

```markdown
## <date> — Stage <n> — <one-line result>

**Ran:** <exact commands>
**Raw output:** <pasted, or path to the file it was written to>
**Found:** <what the numbers say>
**Could not determine:** <what failed, what was missing, what n was too small>
**Recommends:** <action, or "no action">
**Gate:** PASS / BLOCKED / NEEDS DECISION
```

---

# STAGE 0 — SETUP

```bash
cd /path/to/tradeos-v6
git checkout main && git pull
git checkout -b diagnostic/stage-1-population
mkdir -p docs
cp ~/Downloads/{TRADEOS_ROADMAP,EDGE_DIAGNOSTIC,PHASE_E_HISTORICAL_REPLAY,HARDENING_BRIEF}.md docs/
printf '# TradeOS Findings Ledger\n\nAppend-only. Never edit a prior entry.\n\n' > docs/FINDINGS.md
git add docs/ && git commit -m "docs: roadmap, specs, findings ledger"
```

Then confirm the environment before trusting anything it produces:

```bash
cd backend && python -m tools.verify 2>&1 | tail -20
cd backend && python -c "from config import get_supabase; print(get_supabase().table('system_config').select('key').limit(1).execute())"
```

Record in the ledger: verify pass/fail counts, and which failures are
environmental (`python-telegram-bot` missing, no market session) versus real.

**Gate 0:** Supabase reachable, `tools.verify` green or its failures explained.

---

# STAGE 1 — WHICH NUMBER IS TRUE

**Spec:** `EDGE_DIAGNOSTIC.md` Phase A. **Read-only.**

Three tools disagree about whether the intraday book has edge. Establish which
population is the real closed book, and check whether the data supports any
analysis at all.

**Also run, in this stage, because it is one command and has never been run:**

```bash
cd backend && python -m tools.discover_engines --days 30
```

Pass B sweeps for real intraday moves that produced no detection from any
engine — winners that never happened, invisible in every P&L. Record what it
finds as hypotheses. Act on none of them yet.

## Gate 1 — the fork

| `planned_stop_at_entry` coverage | Next |
|---|---|
| Above ~60% | → Stage 2 |
| **Below ~60%** | → **Stage 1b first.** Stages 2 and 3 are not meaningful without it |

If coverage is bad, everything computed downstream — engine scores, the
conviction floor, the priors — rests on a quarter of the book. That is a
plumbing problem outranking every strategy question in these documents.

---

# STAGE 1b — DATA REPAIR (conditional)

**Branch:** `fix/stop-at-entry-coverage`. Only if Gate 1 says so.

Ensure `planned_stop_at_entry` and `charges` are written at entry for every new
position, both frameworks. Historic rows stay as they are — backfilling an
invented stop is worse than a null.

Test proves the column is populated on a simulated entry, and fails first
without the fix. `tools.verify` green before merge.

**Gate 1b:** a new paper entry writes both columns. Verified by query, not by
reading the code.

---

# STAGE 2 — SIGNAL OR FRICTION

**Spec:** `EDGE_DIAGNOSTIC.md` Phases B and C. **Read-only.**

The classification that determines everything after it:

| Reading | Diagnosis | Direction |
|---|---|---|
| gross > 0, net < 0 | **friction** | wider stops, larger clips, fewer trades. **Do not add filters** |
| gross < 0 | **signal** | retire engines |
| both > 0 | edge exists | variance or sizing |

Plus B.3 — the counterfactual. Group `intraday_setups` by `cost_verdict` and
compare `TAKEN` against each `BLOCKED_*` reason separately. If refused setups
beat taken ones, the gates are inverted and the fix is deletion.

## Gate 2

Ledger states: the diagnosis, per-engine gross/cost/net R with n, and whether
any gate is inverted. **No retirements yet** — Stage 3 gives them a real sample
first.

---

# STAGE 3 — REPLAY

**Spec:** `PHASE_E_HISTORICAL_REPLAY.md`. **Branch:** `feat/replay-harness`.

The only stage that manufactures a sample large enough to answer the question.
Builds a new tool; changes no live behaviour.

Non-negotiables from the spec, restated because they are what makes this
evidence rather than a story:

- **Walk-forward.** Parameters frozen and committed to git before the holdout is
  touched. One look, ever
- **Universe as it was on each date.** Today's liquid names run backwards is
  survivorship bias
- **Assume the bad fill** when a bar contains both stop and target
- **Beat random entry**, not zero, with the same stop/target geometry and costs
- **Reproduce a known day first.** If the harness cannot regenerate detections
  already in `intraday_setups`, nothing after it is trustworthy

## Gate 3 — the decision that matters most

Per engine, with n: keep, retire, or insufficient evidence.

Retire only on n ≥ 30 deduplicated observations **and** negative gross R **and**
failure to beat the random baseline. Two of three is not enough.

**This gate needs Vipin's explicit approval per engine.** Do not batch it.

---

# STAGE 4 — SUBTRACT

**Branch:** `config/stage-4-subtraction`.

The first stage that changes what the book does. In strict order, **one change
per week**, ledger entry after each:

1. **Retire** engines that failed Gate 3 — set to SHADOW, not deleted. They keep
   recording and can be revived on evidence
2. **Fix friction** if Stage 2 said friction — `sizing_max_cost_r`, clip-size
   floor, stop width. Config, not code
3. **Remove inverted gates** found in B.3
4. **Arm `overlay_liquidity_enabled`** — currently a gate that cannot fail

Nothing is added in this stage. Addition comes after Stage 5 or not at all.

**Gate 4:** each change has a before/after ledger entry with entry count,
refusal histogram, and net R.

---

# STAGE 5 — OBSERVE

**No branch. No changes. Four to six weeks.**

The hardest stage and the one most likely to be skipped.

Run the reduced engine set. Let `intraday_setups` accumulate. Let
`planned_stop_at_entry` coverage climb on new rows. Let the intraday MFE
population reach the ~20 closes that `intraday_giveback_pct` needs.

Weekly, one command, one ledger line:

```bash
cd backend && python -m tools.weekly_review
```

**Change nothing during this stage** unless something breaks. The temptation to
adjust is the thing this stage exists to resist — every mid-flight change resets
the measurement clock and you never find out whether Stage 4 worked.

**Gate 5:** four to six weeks elapsed, and the reduced set measured against the
pre-Stage-4 baseline.

---

# STAGE 6 — HARDEN

**Spec:** `HARDENING_BRIEF.md`, its phases in order.

Now correctly timed: loss-prevention on a book whose edge is understood.

Two amendments from what has been learned:

- **Its Phase 0 is largely redundant** — Stages 1–3 have done that verification.
  Keep only the claim checks not already answered in the ledger
- **Its Phase 5 (breadth) is conditional** on Stage 3's regime segmentation
  supporting it across 200+ observations. If the replay does not support it,
  delete that phase and record why

Phase 2 (corporate-action guard) may be pulled forward at any time — it is
protection against an unbounded loss on a live position and does not depend on
anything in Stages 1–5.

---

# STAGE 7 — CADENCE

Steady state. Nothing here is a project.

| When | What |
|---|---|
| Weekly | `tools.weekly_review`, one ledger line |
| Monthly | `tools.engine_scorecard` both books; `tools.discover_engines` |
| Quarterly | Re-run the replay on the newest quarter as fresh holdout |
| Before any config change | ledger entry stating expected effect, measured after |

**The quarterly re-run is what catches decay.** A signal that paid through 2025
can stop paying, and only a fresh holdout says so.

---

# ANTI-PATTERNS

Named so no session drifts into them.

| Do not | Because |
|---|---|
| Add an engine before Stage 5 completes | More signals on unmeasured signals |
| Tune against the holdout | It stops being a holdout at the first look |
| Pause paper trading to stop losses | Paper losses cost nothing; the data is the product |
| Batch config changes | Attribution becomes impossible |
| Skip Stage 5 | It is the only stage that tells you whether Stage 4 worked |
| Report a number without its command | The ledger becomes fiction |
| Let one session span two stages | Context exhaustion, scope creep, or both |
| Retire an engine on fewer than 30 observations | That is noise, not a verdict |

---

# FIRST SESSION

```
Read docs/TRADEOS_ROADMAP.md, then execute STAGE 0 and STAGE 1 only.

Stage 1 is READ-ONLY. Do not modify a source file. If you find a bug,
record it in the ledger and stop.

Also run tools.discover_engines --days 30 and record its findings as
hypotheses only.

Append to docs/FINDINGS.md in the handoff format. Every number needs its
command pasted. "Could not determine" is a required section — use it.

End by stating which side of Gate 1 we are on: is planned_stop_at_entry
coverage above or below 60%?
```

---

# SWING TRACK — AI CHASE CEILING (separate from Stages 0–7 above)

**Added 2026-08-18.** Not part of the intraday-diagnostic sequence above and
does not renumber it. A session should only pick this track up when told to by
name — "confirm which stage we are in" above still means Stages 0–7.

**Conflict to resolve before Stage C1 starts:** "What keeps running throughout"
says *"Swing live book: unchanged. Keep trading it. Nothing modifies it before
Gate 3."* That line was written for the current intraday-diagnostic effort,
and swing and intraday are separate books on separate code paths — a
swing-only change should not corrupt anything Stage 1–3 is measuring on
`intraday_setups`. But that is a read, not a ruling. **Vipin decides** whether
this track runs in parallel now or waits for Gate 3.

## Why this exists

Evening-alert review, 18-Aug-2026. Full trace in that session's transcript;
restated here so a later session does not have to re-derive it.

`ai_decision_engine` (step 19) already computes `ai_max_chase_pct` /
`ai_zone_high_extended` every evening — an AI-approved entry ceiling above the
mechanical zone, for a stock trending well enough that its pullback zone may
never get touched. The evening alert already shows this to the operator as a
manual "chase" GTT suggestion.

`analysis/trade_decision.py`'s `decide()` — the one function both the alert
and the live auto-entry engine call (`intraday/engine.py`, confirmed firing
live: AARTIIND and TATATECH both auto-entered for real money on 2026-08-17) —
never reads either field. Its only ceiling is `max_entry_for_rr()`
(`trade_decision.py:89`), purely mechanical. So the AI's chase clearance is
informational-only for a human with a manual GTT; the automated path cannot
act on it and silently passes on any name that runs away without a pullback.

The idea: let `max_entry_for_rr`'s result be raised — never lowered — by
`ai_zone_high_extended` when `ai_max_chase_pct` is set, so the live engine can
capture what the pipeline already told a human was worth chasing.

## Non-negotiables

- **No live behaviour change until it is earned.** For every row where
  `ai_max_chase_pct` / `ai_zone_high_extended` are null — which is every
  historical row, and every future row the AI didn't chase-clear — `decide()`
  must be provably byte-identical to today. This is the whole of what "does
  not break what already works" means here; prove it with a check, not a
  reading of the diff.
- **New switch, default OFF** (`swing_chase_ceiling_enabled`), same pattern as
  `overlay_liquidity_enabled` in Stage 4 — the feature ships inert until
  deliberately armed.
- **Shadow before live**, mirroring Stage 5's discipline: log what it would
  have taken, take nothing, for a stated minimum period, before Stage C3.
  Rollback is flipping the switch back — no schema change, no migration.
- **Every number sourced from a real command**, pasted into the ledger, same
  as the rules governing Stages 0–7.

## Stage C1 — Quantify (read-only, no branch)

Before writing any code: how often would this actually have mattered, and
would the trades it would have taken have been any good?

- Count candidates with non-null `ai_max_chase_pct` where `decide()` declined
  solely because price never re-entered `entry_zone_low`–`entry_zone_high`.
- For those symbols, what did price do afterward — would the AI-cleared entry
  have beaten the plan's `planned_stop`/`planned_target`, or lost?

**Gate C1:** a real count and a real "what would it have been worth" number,
both from a query, in the ledger. If this rarely happens, or the skipped
trades mostly would have lost, **stop here — no code follows.** That is a
valid, complete outcome for this stage, not a failure of it.

## Stage C2 — Build behind the switch, shadow-mode only

**Branch:** `feat/swing-chase-ceiling`.

- `swing_chase_ceiling_enabled` (default `False`) gates the whole feature.
- In `trade_decision.py`, when armed AND the row carries a non-null
  `ai_max_chase_pct`: raise (never lower) `max_entry_for_rr`'s ceiling toward
  `ai_zone_high_extended`. Every other path is untouched.
- `tools.verify` check: with the flag off, or with the AI fields null,
  `decide()`'s output is identical to current behaviour on the same fixture
  rows. **Demonstrate this check failing first** — feed it a build without the
  guard — before trusting it to pass, per this project's standing rule that a
  check that cannot fail is not a check.
- Armed but log-only: record what it would have entered, place nothing live.

**Gate C2:** `tools.verify` green including the new inert-by-default check,
plus a stated minimum of shadow-log entries in the ledger with how those
symbols actually resolved.

## Stage C3 — Arm it live

Only after Stage C2's shadow log shows the trades it would have taken were
net positive. **Needs Vipin's explicit sign-off, logged in the ledger** — same
as Gate 3 above, this is not automatic.

Consider a cap on live chase-ceiling entries for the first weeks (e.g. one at
a time) so a bad first read is cheap to reverse. Rollback is the switch, not a
revert.

**Gate C3:** sign-off recorded, plus a stated review date (2–3 weeks out) to
check armed behaviour against what the shadow log predicted.

---

# TRACK D — INTRADAY EVOLUTION

Agreed 23-Aug-2026, after a full trader-lens review of the intraday book
(F-39 through F-53) surfaced five real gaps: a universe pool that only
re-qualifies once a day from stale numbers, no execution-quality signal at
entry, no multivariate view of what actually separates winners from
losers, no same-day self-awareness, and a discovery pipeline that stops at
a text proposal instead of a testable strategy.

**Scope, agreed explicitly:** intraday only. Swing's live book, the
evening pipeline, and every swing-specific file stay untouched by this
track — see "The swing boundary" below for exactly what that does and does
not mean, because a hard "zero shared code" line is not achievable given
today's architecture and pretending otherwise would be the lie, not the
plan.

**Order is fixed. Stages run D2 → D3 → D4 → D5 → D6, in that sequence,
because each later stage either depends on an earlier one being stable
(D6 needs D2–D5 to template off of) or because it is the cheapest,
most self-contained win and should land first (D2).** D1 is this
document.

## The swing boundary — read before any Stage D work

Traced, not assumed (23-Aug-2026 session): the only things intraday and
swing genuinely share are (a) the one live ticker connection — one process
necessarily serving both books' real-time needs, (b) `IntradayEngine`'s
shared position-evaluation loop, which branches by `framework` at
dispatch but shares its surrounding timer machinery, (c)
`position_lifecycle.py`'s write functions (`_upsert_position`, `close()`),
used by both frameworks' entries so a position is never recorded two
different ways, and (d) the allocator, which must see both books at once
by design to enforce one-symbol-one-book and cross-book slot budgets.
Everything else — detection engines, the intraday exit ladder, the cost
model's application, universe building — already touches nothing swing
depends on.

The one already-correct precedent for crossing this boundary safely:
`control/exit_rules.py` receives `dist_vwap_live` (a live tick VWAP
`IntradayEngine.refresh_trend_context()` computes) as a plain, OPTIONAL
value with graceful fallback to yesterday's data when absent — it never
calls into intraday's code. Every Stage D component that produces
anything swing could plausibly use follows this exact shape: a value
handed across, never a function called across.

**Rule for this whole track:** any change touching (b), (c) or (d) above
— however small — runs the FULL `tools.verify` suite plus the
swing-specific health checks (`books`, `broker`, `stops`, `qty_fields`)
before being considered safe, shown in the ledger, every time. "No swing
code changed" is not always achievable; "no swing behaviour changed,
proven" is the actual commitment.

**Prerequisite, before D2 or D3 start:** verify Kite's actual per-connection
websocket subscription ceiling against swing's ~15–20 symbols plus
whatever intraday's expanded footprint would add. This is not optional
and not assumed — a wider intraday universe competes for the same
subscription budget the live-VWAP handoff above depends on.

**Consolidated, 24-Aug-2026.** D2 through D5 were each built and verified
independently, all four branched off the same `main` commit and unaware
of one another by construction — the operator's own explicit plan
("complete all the Ds then do a holistic check before merging"). Merged,
in stage order, into `feat/intraday-evolution` (off `main`): 8 real
source-file conflicts across `overlays.py`, `price_feed.py`, `verify.py`
and `FINDINGS.md` (every branch's own F-number sequence independently
claimed F-54 from wherever `main` sat when it branched, exactly as every
affected entry's own text anticipated — renumbered into one true F-54–64
sequence at merge time, header and collision-note paragraphs only, no
entry content altered). `engine.py`, `run.py`, `base.py`, `scoring.py`
auto-merged clean; verified by inspection, not assumed, that both sides'
additions actually coexist (`SymbolContext.depth` alongside
`universe_population`, the sizing pipeline's `liquidity_capped_budget()`
→ `BLOCKED_LIQUIDITY` → `BLOCKED_DEPTH` gates in the right order,
`Prior.hit_rate` alongside the established/admitted prior split). `tools.
verify`: 930/930 across 90 modules — exactly the sum of `main`'s own 763
plus each stage's own delta (D2 +89, D3 +26, D4 +27, D5 +25). `tools.
health`: 24/24. `tools.simulate`: clean end-to-end run through the full
combined pipeline with real live positions, "nothing was written." F-65
(docs/FINDINGS.md) has the full detail.

**Every stage still ships exactly as it did on its own branch** — every
switch this track has introduced (`intraday_event_core_enabled`,
`intraday_depth_mode_enabled`, `overlay_depth_enabled`,
`intraday_same_day_fit_weight`, both live-requalify switches) remains at
its shipped-off default. The merge is source consolidation, not arming;
Gate D2 through Gate D5 remain individually unproven and are still
deferred to real elapsed market time, per the operator's own plan — this
pass is what makes that final holistic evaluation possible from ONE
branch instead of four, not a substitute for it.

## Stage D2 — Universe live-qualification

**Branch:** `feat/intraday-live-universe`.

Corrected twice, 23-Aug-2026 — first wrongly, then right, both caught by
the operator, not found internally. First pass claimed `stock_data_daily`
held the full bhavcopy (~1,800–2,000 EQ symbols); that was wrong —
`ingest_bhavcopy.py` only ENRICHES rows already in `stock_data_daily`
(`value_cr`/`delivery_pct`/`delivery_qty`) for whichever symbols are
already there. `stock_data_daily` itself is swing's own sheet-baseline
table (`compute_indicators.py`'s own docstring: "sheet baseline"),
confirmed 499 rows on 21-Aug — genuinely ~Nifty 500, the operator's
original claim. The real bhavcopy ingest, `raw_prices`, carries 2,633
rows the same date and is used only to feed those three columns back,
never to create new `stock_data_daily` rows.

`build_universe()`'s ~120-name daily bench is built ONCE per day from
YESTERDAY's turnover/ATR/delivery%, and `live_rerank()` can only reorder
names already in that pool — it cannot add one that didn't qualify
yesterday but is moving hard today. Three candidate populations follow:

- **Population A** — `stock_data_daily` names that failed ONLY
  yesterday's ATR band. Real historical ATR exists; admission is TODAY's
  own move%/turnover-cr clearing the same relative floor
  (`intraday_min_atr_pct`/`intraday_min_turnover_cr`'s live-quote
  equivalents). `scanner.movement_rejected_candidates()` +
  `live_requalify()`. Switch: `intraday_live_requalify_enabled`
  (migration 097).
- **Population B** — `nifty_total_market` members (751 rows) NOT in
  `stock_data_daily` (253 of them, confirmed live, 23-Aug) — real,
  known NSE names outside swing's own sheet-baseline universe, not new
  listings. No historical ATR, so admission is ABSOLUTE: today's
  move%/turnover-cr against the same floors, PLUS a live-quote
  `min_price` check (`intraday_min_price`) that Population A gets for
  free from `_qualifies()` but this population never ran through at
  all — the one gate a live quote can stand in for. Delivery% and
  ASM/F&O-ban have no live-quote equivalent and stay unchecked for this
  population, named not assumed covered.
- **Population C** — the genuine new-listing/IPO case: names in
  NEITHER `nifty_total_market` nor `stock_data_daily`. `nifty_total_
  market` lags an actual listing by NSE's index-reconstitution cycle
  (weeks to months, not days), so this is only visible through Kite's
  own instrument master (`kite_client.fetch_nse_eq_symbols()`,
  `kite.instruments("NSE")`, cached once per day), which is generated
  fresh by the exchange feed and carries a name from its first
  tradeable day. Same absolute admission rule as Population B.

`scanner.unreferenced_candidates()` returns B and C together (`atr_pct
=0.0`, honestly — nothing real to put there); `live_requalify()` handles
all three populations with one shared admission path. Population B/C
ships behind its OWN switch, `intraday_live_requalify_unreferenced_
enabled` (migration 098) — deliberately NOT the same switch as Population
A, because B/C is a materially wider, less-vetted population and folding
it into a switch the operator armed for the narrower one would be exactly
the "silently widen a gate" failure this project's rules forbid.

**Built, 23-Aug-2026 (Stage D2c):** a weekly refresh for `nifty_total_
market` itself — `swing/ingestion/ingest_nifty_total_market.py`, wired
into `brain_sunday_chain.yml`. Correction to this doc's own prior text:
that first pass called the niftyindices.com CSV blocked, based on one
`WebFetch`-tool timeout, and deferred the whole piece on that basis. A
direct `requests.get()` with a plain browser header — no session warmup,
unlike ASM/GSM — returned the real CSV on the first try; the site was
never the obstacle. Fetches all THREE of NSE's own constituent CSVs
(Total Market, Nifty 200, Nifty 500) and recomputes `nifty_200`/
`nifty_500` as an explicit boolean per row from fresh set membership —
not "set true if found, leave stale trues alone" — so a name that
dropped OUT of an index this cycle is correctly cleared, not left
stuck true. Either index CSV failing independently omits that ONE
column from the whole run's payload (Postgres/PostgREST then leaves the
existing value untouched on upsert) rather than writing it wrong from a
failed fetch. Upsert-only, never deletes — a symbol in the table but
absent from a fresh fetch is left exactly as-is and reported as stale in
the log, respecting the swing-boundary caution this table's downstream
reader (`compute_indicators.py`) already earned. First live run: 3 new
symbols, 749 refreshed, 2 pre-existing rows correctly left untouched
(one a hand-inserted test fixture, `DUMMYALCAR`; one, `JBCHEPHARM`, a
real name absent from this week's fresh Total Market list — its stale
`nifty_500=true` from before this run was correctly NOT touched, since
it wasn't in this run's payload at all).

Population C's own Kite-instrument-master filter was also corrected the
same session: `instrument_type == "EQ"` (my first proposal) turned out
to filter NOTHING — verified live, every one of 10,086 `segment=="NSE"`
rows carries that same tag on this endpoint. The real signal is the
tradingsymbol's own suffix — `kite.instruments("NSE")` returns 10,086
rows for `segment=="NSE"`, of which 7,107 carry a `-XX` suffix (bonds/
SDLs/SGBs/T-Bills by far the largest share, plus SME-board and
trade-to-trade names) — real, individually-tradeable NSE instruments,
just not the "is this a new STOCK" question this function exists to
answer. `fetch_nse_eq_symbols()` now filters on that suffix, leaving
2,979 plain mainboard/ETF symbols — checked against a real 2024 rename
(ZOMATO → ETERNAL; the old symbol is correctly absent, the new one
present).

`unreferenced_candidates()` also now excludes any symbol on
`safety_lists` (ASM/GSM/FO_BAN) — that table is keyed on bare symbol
independent of `stock_data_daily`, so it can answer for Population B/C
names `_qualifies()`'s own flagged-check never could (no `asm_flag`
column exists for a name with no `stock_data_daily` row). Reuses
`intraday_skip_flagged`, the same switch `_qualifies()` already reads.

**Population C redefined, 23-Aug-2026 (Stage D2d) — the operator's own
catch.** "Kite-known symbols not in nifty_total_market or stock_data_
daily" measured 2,081 names live — never actually "new listings", almost
entirely small/micro-cap names outside both reference tables' own
coverage. Quoting ~2,100 names every 45s to catch an event that happens
a handful of times a MONTH was never viable, and it wasn't answering the
IPO question either — a name can sit outside both tables for years
without ever being a new listing. Fixed the DEFINITION, not just the
size: `scanner.new_listings()` diffs today's live Kite list against
`kite_symbol_baseline` (migration 100), a table it maintains itself — a
symbol present today but never recorded before is genuinely new, and is
written to the baseline the moment it's found so it is never reported
new again. First run ever (empty baseline) seeds the whole ~2,979-name
universe as already-known and reports ZERO as new — a bootstrap, not
2,979 simultaneous IPOs. Live-verified: Population C went from 2,081 to
0 (this session's bootstrap run); Population A+B (the two populations
that were always bounded and never the actual problem) stayed at 232.
Also added a once-per-day cache (`_ref_cache`) for the three reference
reads Population B needs (`stock_data_daily`, `nifty_total_market`,
`safety_lists`) — none of them change intraday, so re-querying them
every 45s for a full session was pure waste, mirroring `kite_client.py`'s
own `_instr_cache` pattern.

**Stage D2e, 23/24-Aug-2026 — the "Milky Mist" gap, attempted via
`raw_prices`, SCRAPPED by the operator.** Population C's bootstrap
silently absorbs everything Kite currently lists as "already known" (by
design — see Stage D2d above), which means a name that listed shortly
BEFORE this code existed is invisible forever, indistinguishable from a
decades-old stock. A `raw_prices`-based recency check (migration 101)
was built, then live-tested against the FULL Kite universe rather than
trusted: it found real, unfixable coverage-gap false positives (ZEEMEDIA,
a long-listed company, misclassified as a fresh IPO by a genuine ~4-week
gap in `raw_prices`' own coverage) and, separately, a real coverage
DISCONTINUITY in `raw_prices` itself (~170 symbols appeared for the
first time around 17/18-Aug-2026, unrelated to any code change in this
repo) that made 179 names look "recent" against a real IPO rate of a
handful a month. The operator's own call: **"you cannot use raw prices
count to identify the new listings, it has n number of different
records... unnecessarily complicating the things"** — scrapped entirely,
not patched further. Two unrelated real bugs were found and fixed in the
same pass regardless: 343 `INAV` (Indicative NAV — never a tradeable
instrument) symbols were false-positiving through Population C
generally, fixed at the source in `kite_client.py::fetch_nse_eq_
symbols()`; and `new_listings()`'s own read of its 2,979-row baseline
table was silently truncated to 1,000 by PostgREST's cap — caught before
it ever ran.

**Stage D2f, 24-Aug-2026 — replaced with NSE's own confirmed IPO
archive, closes the gap for real.** `https://www.nseindia.com/api/
public-past-issues?index=equity` — verified live, 1,411 real records
back to 2003, every one carrying the ACTUAL NSE symbol directly (no
fuzzy company-name matching needed; `groww.in/ipo`, checked first per
the operator's own suggestion, was found to expose company names only,
no symbol, and only 5 records via a plain fetch against ~100 total). New
table `ipo_listings` (migration 102) + `swing/ingestion/ingest_ipo_
listings.py`, weekly refresh (`brain_sunday_chain.yml`, matching `nifty_
total_market`'s own cadence) — reuses `ingest_asm_gsm.py`'s proven
nseindia.com session-warmup pattern. New `scanner.py::recent_ipo_
candidates()`: mainboard (`security_type=='EQ'`) listings within
`intraday_ipo_recency_days` (45 default) — measured 17 real names live,
24-Aug, matching real IPO cadence, MILKYMIST correctly present (listed
18-Aug-2026). Deliberately INDEPENDENT of `new_listings()`'s Kite-diff,
not a replacement for it — Kite updates daily and can catch a listing
same-day, `ipo_listings` refreshes weekly but is authoritative; either
source alone missing a name does not silently drop it, since `unreferenced_
candidates()` merges and dedupes both. Migration 103 drops the now-dead
`get_raw_prices_first_seen` RPC and its config key — F-59 (docs/
FINDINGS.md) has the full detail.

**Stage D2g, 24-Aug-2026 — the Kite diff was letting ETFs through as
"new listings".** The operator's own follow-up: why keep the Kite diff
at all now that `ipo_listings` exists, and how is it avoiding noise like
ETFs? Checked live rather than assumed: it was NOT avoiding them — Kite's
`instrument_type` field reads `"EQ"` for NIFTYBEES/GOLDBEES exactly as
for RELIANCE/MILKYMIST, and 294 real ETFs sit in the same "plain"
universe real stocks do. This is also the precise, structural reason
`ipo_listings` matters beyond same-day latency: ETFs list via NFO, never
via IPO, so they cannot appear in that archive at all — a guarantee the
Kite diff cannot offer. Fixed with `kite_client.py::is_etf_name()` (the
`name` field, the one signal Kite's data has for this) filtering what
`new_listings()` REPORTS, without changing what it records to `kite_
symbol_baseline` — an ETF is seeded once, same as before, just never
reported as a "new listing". F-60 (docs/FINDINGS.md) has the full detail.

**Stage D2h, 24-Aug-2026 — is the ~270-name universe actually SAFE to
pick from, not just wide?** The operator's own question, dispatched to a
dedicated audit rather than answered from memory. Good news: the 7
engines do not misread Population B/C's missing history as zero —
missing ATR/volume-average fall back to fixed assumptions or disable a
check, never corrupt one, and no engine sizes a stop off ATR at all.
Three real gaps found and closed: **sizing** was flat regardless of
liquidity (`analysis/overlays.py::liquidity_capped_budget()`, reusing
`liquidity_ok()`'s own math to size a thin name down instead of refusing
it outright — which also fixed an incidental finding, `ctx.value_cr`
permanently `None` for Population B/C, meaning `liquidity_ok()` would
have refused every one of them regardless); **paper slippage** was flat
regardless of liquidity (`execution/paper_broker.py::_slippage_pct()`,
optional `value_cr`, 3x default below a 25cr threshold, `None` preserves
every pre-existing call site exactly); and **priors were unsegmented**
— `allocation/scoring.py` now splits established from admitted by `meta.
universe_population` (byte-identical core arithmetic, unchanged, called
once per population), `allocation/allocator.py::_prior_for()` gained a
parallel admitted ladder that never borrows established's numbers,
verified through the real `Allocator` lookup, not the key-builder alone.
19 new tests. F-61 (docs/FINDINGS.md) has the full detail.

**Gate D2:** a live demonstration — a name outside yesterday's bench that
moved hard today gets admitted mid-session, logged with which population
admitted it (A/B/C), and resolved the same way every other detection is.
No capacity, priority, or bar behaviour changes for anything already in
the bench.

## Stage D3 — Event-driven core, in shadow

**Branch:** `feat/intraday-event-core`.

New, parallel orchestration only — reads the same live ticks, calls the
SAME existing detection/scoring/exit functions this book already trusts,
decides nothing that writes anywhere. The reason this is shadow and not
straight to paper, and it is a different reason from a plain safety
instinct: intraday paper is not a sandbox here — it consumes real
capacity slots (`intraday_max_concurrent`, `intraday_max_new_per_day`),
spends real allocator budget, and its outcomes feed the SAME
`intraday_setups`/priors tables F-33, F-39, F-42 and F-53 each spent real
effort de-contaminating this session. A bug in a brand-new orchestration
layer that reaches paper does not cost money — it costs clean data, which
this project has now paid to clean up four separate times.

**Bounded, not indefinite, per the operator's explicit instruction that
there is no scope for error once this is trusted with anything.** Shadow
runs for a STATED, fixed window — proposed: 10 trading sessions or 200
directly-comparable decisions (same symbol, same moment, old core vs new
core), whichever comes first — logging exactly what the new core would
have done and how many seconds sooner than the existing 15s/3s loops.
Move to paper only once that comparison shows the new core's decisions
agree with or improve on the existing loop's, not merely that it ran
without crashing.

**Gate D3:** the stated shadow window complete, a side-by-side comparison
in the ledger (agreement rate, measured latency improvement in seconds,
any case where the two cores would have disagreed and why), operator
sign-off to promote to paper.

**Built, 24-Aug-2026.** `intraday/price_feed.py` gained thread-safe
"dirty symbol" tracking (`drain_dirty()`, fed by the same websocket
thread that already writes prices — no new thread introduced,
deliberately: `engine.py`'s mutable state was never built for concurrent
access, and this stage's whole purpose is measuring a latency
improvement, not chasing the smallest possible one at the cost of a new
class of bug). `intraday/event_core.py::check()` runs from `run.py`'s own
main loop on its own tight timer (2s default, still far tighter than the
15s polling cycle), reuses the SAME `apply_live_quotes()`/`merge_live_
bars()`/`registry.evaluate_all()` the trusted loop already calls, and
writes ONLY to a new, fully separate table (`intraday_event_shadow`,
migration 105) — never `intraday_setups`, never `paper_broker`, never
the allocator. Also fixed a real gap found while building the
comparison tool: `intraday_setups` had no detection timestamp at all
(migration 106, `detected_at`), without which Gate D3's own "measured
latency improvement in seconds" criterion was unmeasurable. `tools/
event_core_compare.py` matches shadow vs. trusted detections by
(symbol, sub_engine) within a window and reports the agreement rate and
latency gap Gate D3 asks for — run live against production, correctly
reports an honest zero (nothing has run in shadow yet). 35 new offline
tests. F-54 (docs/FINDINGS.md, this branch's own sequence) has the full
detail.

**Ships OFF** (`intraday_event_core_enabled=false`). Per the operator's
own stated plan, Gate D3 — like Gate D2 before it — is deferred to a
single holistic pass across every Track D stage once all of them are
built, not cleared stage-by-stage; the mechanism is ready to start
accumulating real shadow evidence the moment it is armed.

## Stage D4 — Execution-quality depth gate

**Branch:** `feat/intraday-depth-gate`.

Agreed 23-Aug-2026, scope as described: switch the relevant websocket
subscriptions to Kite's FULL/depth mode (subject to the subscription-limit
prerequisite above), then a sanity gate at the moment of an
already-decided entry — refuse or flag when the spread is abnormally wide
relative to the stock's own norm, or resting depth cannot absorb the
intended quantity without material slippage. Not prediction, protection —
same shape as the existing `BLOCKED_LIQUIDITY`/`BLOCKED_STRUCTURE` gates,
one more row in the same table.

**Gate D4:** depth data confirmed flowing and logged for the live
universe; a demonstrated refusal on a real thin-book case; no change to
any candidate that already had a healthy spread.

**Built, 24-Aug-2026.** FULL mode scoped to `IntradayEngine.
context_symbols()` (positions ∪ the live universe, ~40-120 names) rather
than the whole ~270-name bench — verified live that Kite's 3,000-
instrument subscription ceiling is uniform across LTP/QUOTE/FULL, so the
reason to stay scoped is bandwidth, not the cap, and only this set can
ever generate an entry decision. `price_feed.py::set_depth_symbols()`
diffs the depth-worthy set and moves symbols into/out of `MODE_FULL`
independently of `resubscribe()`'s own slower cadence; captured depth
reaches the engines via `SymbolContext.depth` (`engine.py::
apply_live_depth()`), the same "carry it on the context" pattern
`apply_live_quotes()` already uses. `analysis/overlays.py::depth_ok()`
— same shape as `liquidity_ok()` — refuses an already-decided entry on
an abnormal spread (`intraday_max_spread_pct`) or insufficient resting
depth on the consuming side of the book
(`intraday_depth_levels_checked`), recording a new `BLOCKED_DEPTH`
verdict through the existing `_record_setup()` path.

Two real bugs caught before either shipped armed: the depth-capture
block in `on_ticks()` sat behind an unrelated quote-capture switch and
would never have stored a FULL-mode tick's depth field; `set_depth_
symbols()` itself had no config check at all, so arming nothing would
still have put live Kite subscriptions into FULL mode the moment the
code ran. Both fixed — the second one specifically by making a disabled
switch revert any symbol already in FULL mode, not merely refuse new
ones. F-54 (docs/FINDINGS.md, this branch's own sequence) has the full
detail, including a mid-session branching correction: this work was
first written on `feat/intraday-event-core` (D3's branch) and was moved
to a fresh `feat/intraday-depth-gate` off `main` before anything was
committed, once the mismatch with this roadmap's own stated branch name
was noticed.

**Ships OFF** (`intraday_depth_mode_enabled=false`,
`overlay_depth_enabled=false`). Per the operator's own stated plan, Gate
D4 — like Gate D2 and Gate D3 before it — is deferred to a single
holistic pass across every Track D stage once all of them are built, not
cleared stage-by-stage.

## Stage D5 — Shadow regression / same-day self-monitor

**Branch:** `feat/intraday-regression-shadow`.

Three stages, not one, because "shadow" means something different here
than in D3 — this is about statistical trust, not code trust:

1. **Calibration only.** The model computes predictions against already-
   resolved history and logs its own predicted-vs-actual accuracy over
   time. Nothing here is visible outside this pipeline yet — this is the
   system convincing itself the model is not just fitting noise before
   anyone else is asked to trust it.
2. **Proposal**, only once (1) shows real, tracked skill. The model's
   output becomes an ordinary `FEATURE_FILTER`-shaped `brain_proposals`
   row, subject to the exact same review `tradeos learn show` already
   surfaces everything else through. No new review mechanism.
3. **Armed**, only on the operator's own decision — unchanged from how
   every other proposal here already works.

**Why not straight to live, concretely, not just "safer":** a model with
too little data per parameter (see the 23-Aug session's own math — roughly
10–20 observations needed per estimated parameter) will confidently
report a pattern that is actually coincidence, and because a regression's
output looks more rigorous than a plain bucket split, a wrong one is
HARDER to catch by eye than the wrong univariate findings F-53 already
found and fixed this session. Stage 1 exists specifically to catch that
before it ever reaches a human's review queue, let alone a live decision.

The same-day self-monitor (does this engine's OWN hit-rate today look
like an outlier against its own history) is a same-day-only, resets-every-
morning dampener on sizing — same slot `regime_fit_multiplier` already
occupies, never a change to the underlying learned prior.

**Gate D5:** Stage 1's calibration log covers a stated minimum window with
a stated accuracy bar met; first proposal reaches `brain_proposals` and is
indistinguishable in the review UI from a hand-built `feature_edge_study`
finding.

**Stage 1 built, 24-Aug-2026 — same-day self-monitor only.** D5 bundles
two mechanisms (a same-day monitor and a general regression model)
without specifying either's shape; asked the operator directly rather
than guessing, and built the well-specified one first — the general
regression stays a separate, later session, once real calibration
evidence can answer its own open design questions (target variable,
feature set) honestly.

`allocation.scoring.same_day_fit_multiplier()`: a one-sided exact binomial
test (`scipy.stats.binomtest`) asking whether one engine's win rate TODAY
is a statistical outlier BELOW its own historical rate — a dampener only,
never a boost for a good day (this project has already been burned once
by treating "looks good on a small same-session sample" as signal —
hurdle.py's STRONG-bucket history). Ships at weight 0.0, an exact no-op,
same precedent `regime_fit_multiplier` already set. `tools/same_day_
calibration.py` walks every resolved trading day walk-forward (historical
prior from strictly earlier days only — Stage 3's own non-negotiable,
applied here) and logs whether the monitor would have flagged each day,
to `intraday_same_day_calibration` (migration 108) — nothing here touches
any live decision; Stage 1 is calibration-only by the roadmap's own words.

Two real bugs caught by actually running the tool against production, not
by inspection: today's win/loss count was first computed from raw rows
without deduplicating a lingering setup's ~15s re-records (this project's
own documented "one setup counted eleven times" landmine, here inflating
one engine to 670 "trades" in a day); and the calibration's first correct
run then reported 0 of 22 days flagged for the OPPOSITE reason — the
live weight is 0.0 by design, so every call hit the no-op guard before
the binomial test ever ran, making the calibration a tautology. Fixed
with an explicit `probe_weight` override parameter, the same "supply the
population instead of fetching it" shape `intraday_priors(sb, rows=...)`
already uses. F-54 (docs/FINDINGS.md, this branch's own sequence) has the
full detail.

**Result, run live against production:** 22 (engine, day) pairs ever
reached the 5-trade same-day floor across 4 engines and 10 days; 0 of 22
were flagged even at full probe weight. The worst pair (ORB 0-for-5
against a 29% historical rate) reached p=0.18 against a 0.05 bar. Not an
absence of a finding — the mechanism runs correctly; the book has not yet
generated a same-day sample extreme enough for it to have anything to
say. Gate D5 itself needs real elapsed sessions accumulating more pairs
than this, the same evidence-accumulation deferral every prior Track D
gate has carried.

**Ships OFF** (`intraday_same_day_fit_weight=0.0`). Called from exactly
one place — the calibration tool — never from `engine.py`'s entry path or
`score()`.

## Stage D6 — Automatic discovery-to-shadow-strategy pipeline

**Branch:** `feat/intraday-auto-strategy`.

**This is a step forward from what exists, not a duplicate of it — worth
being precise about which part is new.** `discover_engines.py` already
runs weekly and already writes real `ENGINE_CANDIDATE` proposals to
`brain_proposals` (verified live, 23-Aug: 8 raised to date, including the
one that became GDB) — that part is not new and every proposal still goes
through the operator's own review, unchanged. What does not exist today:
turning an approved candidate into RUNNABLE code. GDB became a real engine
because a human read the proposal and hand-wrote `gap_down_bounce.py`.
Stage D6 automates that translation — generating a templated candidate
strategy from a discovered pattern, reusing the same structural primitives
every engine already shares (`risk_from_structure`, the `Setup`
dataclass), and running it in shadow (detect and log, write nothing,
consume no capacity).

**Why shadow here even though every proposal already goes through
review:** the review gate that already exists checks whether the FINDING
is worth pursuing. It was never built to review whether AUTO-GENERATED
CODE behaves sensibly — does it crash, does it detect anything reasonable
at all, does its risk construction hold up. That is a different question,
and shadow is how it gets answered before a human is ever asked to spend
review time on a candidate whose code might not even run correctly.

**Gate D6:** an approved `ENGINE_CANDIDATE` proposal produces running
shadow code without a human writing it, a stated minimum of shadow
detections logged, and the operator's own decision — informed by the
shadow log, exactly like every other proposal here — on whether it
graduates toward Stage C-style promotion.

**Built, 24-Aug-2026, on `feat/intraday-evolution` directly (not a fifth
separate branch — built on top of the just-consolidated D2–D5 branch).**
Scope agreed with the operator first: all 11 of `discover_engines.py`'s
Pass B features (not just the 3 gap-based ones with an existing intraday
translation — the operator chose the larger scope), LONG only (the raw
feature name never specifies direction; see gap_down_bounce.py's own
docstring). Every templated candidate reuses ONE fixed, generic shape
rather than inventing mechanism per candidate: the discovered daily-bar
condition as a filter, a single-bar VWAP reclaim (GDB's own reused
mechanism) as the live trigger, a structural stop via
`risk_from_structure()`, a fixed R-multiple target. `"gap down > 1%"` is
explicitly excluded — GDB already covers it.

Two real gaps found and closed before either shipped armed. First:
`brain_proposals.evidence` was JSONB but only ever written a bare string
— fixed by having Pass B write structured evidence (`feature_name`,
`lift`, `avg_move_pct`, `closed_strong_rate`, …) alongside the same
summary sentence, so the template never has to parse prose. Second, more
serious: no approval mechanism existed for `ENGINE_CANDIDATE` proposals at
all (`tools/proposal_backtest.py`'s own docstring: "nothing exists to
replay"), and the FIRST fix — a new `SHADOW_APPROVED` status — was wrong.
Real `brain_proposals` rows showed `status=APPROVED` is already the
precedented human-approval mechanism for this exact type (proposals
#188/#190 became GDB this way), safe specifically because
`ENGINE_CANDIDATE` sits in `REVIEW_ONLY`. Corrected before shipping:
`tools/approve_candidate.py` now calls the EXISTING `approve_proposal()`
rather than inventing a parallel status that would have fragmented one
human decision into two fields nothing kept in sync.

Live-verified, not just offline: `tools.discover_engines --days 30`
raised a genuine fresh candidate (`gap up > 1%`, 1.6x lift, 88% closed
strong); `tools.approve_candidate --id 186 --dry` read that real row and
built a valid candidate end to end. Left `PENDING`, not approved — that
decision is the operator's, even though its only consequence today is a
shadow-only log. F-66 (docs/FINDINGS.md) has the full detail.

**Ships OFF** (`intraday_candidate_shadow_enabled=false`, no candidate
currently `APPROVED`). Gate D6 needs a real armed session with at least
one approved candidate — deferred to the same single holistic pass as
every other Track D gate.

---

# TRACK E — SWING EVOLUTION

Agreed 24-Aug-2026, after a live-trader-lens walk of the swing book (F-43,
F-46, F-67) surfaced two real trades worth naming — HAL (chased 1.2%+
above its own evening-computed zone, entered despite the AI's own logged
lesson about exactly that pattern) and HINDCOPPER (a genuine pending-fill
race that bought the same name twice) — plus, independent of either
trade, six structural gaps found by tracing what the exit ladder and
entry gate actually read versus what a professional desk would check:
`evaluate_exit()` sees only `regime_at_entry`, frozen on the day a
position opened, never the market it is actually sitting in today;
`sector_rank_at_entry` is read in exactly one place (the 3R runner
decision) and nowhere during ordinary holding; `ai_recommended_action`
(`TIGHTEN_SL` etc.) is written and displayed but never executed; swing has
no thesis-invalidation level distinct from its price stop, unlike
intraday; `weekly_structure` is read only post-1R, never at entry; and
`ai/post_trade_analysis.py` — genuinely sophisticated trajectory and
lesson-grading machinery — has exactly one downstream consumer, an AI
prompt whose own ranking weight was correctly cut to zero on 04-Aug once
`ai_tier`/`ai_conviction` were shown not to be predictive. The lesson
engine produces real findings and they currently have nowhere to land.

**Scope, agreed explicitly: swing only, and stricter than Track D's own
boundary.** Track D's boundary section (above) names four points where
intraday and swing genuinely share code — the ticker connection, the
`IntradayEngine` position-evaluation timer, `position_lifecycle.py`'s
write functions, and the allocator. Track E's own prior work (F-43, F-46,
F-67) already lives inside two of those four: `control/position_lifecycle
.py` (swing's exit ladder, but a file both frameworks' close paths write
through) and the swing-only branches of `intraday/engine.py` (the shared
daemon file, never its intraday branches or any file under
`intraday/strategies/`, `intraday/exit_policy.py`, `intraday/
shortability.py`, `intraday/direction.py`, `intraday/market_context.py`).
Track D's own rule for crossing (b)/(c)/(d) applies here with no
exception: **any Track E change touching the shared daemon file or
`position_lifecycle.py` runs the full `tools.verify` suite plus the
swing-specific health checks (`books`, `broker`, `stops`, `qty_fields`)
before being considered safe** — the same discipline F-43/F-46/F-67 used,
made explicit as a standing rule rather than something re-derived every
session. `allocation/*.py` (scoring, hurdle, allocator, outcomes) is
**never edited** by this track, only ever imported from read-only
(`swing_family()`, already the pattern F-46 set) — it is shared
infrastructure Track D also depends on, and the boundary discipline
above ("a value handed across, never a function called across") applies
symmetrically. Nothing under `intraday/` outside the shared daemon file's
own swing branches is touched, read, or depended on by any stage below.

**Order is fixed, and phased differently from Track D's flat D2→D6
sequence** because Track E's stages have a real dependency chain: the
learning core (E6) needs to know, from E2's own numbers, whether there is
enough resolved history per engine to fit anything safely before its
shape is finalized, and position scaling (E7) is deliberately last
because it is the only stage in this track that adds capital risk rather
than sharpening a decision already being made — it should be built once
E6's validated-finding mechanism exists to help decide which winners
actually merit it, not before. E1 is this document.

## Stage E2 — Quantify (read-only, no branch)

Three questions, answered from real data before anything downstream is
designed, matching the discipline that already produced F-43's ladder
reprice and F-46's stall-clock numbers:

1. **Do features separate winners from losers per engine, not just
   book-wide?** Every tercile study run so far (`final_score`,
   `implied_rr`) tested one column at a time and found both flat — real
   evidence about those specific features in isolation, and silent about
   combinations. Mine `signal_log`'s 80+ fields against
   `signal_output_daily`'s resolved outcomes, per engine family
   (CTL/SEC/TPO/SBS/RSB/IAD/VBD pooled as CONTINUATION, MOM and RVS kept
   separate, matching `swing_family()`), for combinations — not single
   columns — that separate TARGET from STOP at a real sample size.
2. **Does the lesson engine's own output correlate with anything?**
   `post_trade_analysis`'s A–F grade and its 20+ rule-based lessons have
   never been checked against realized forward outcome, book-wide. If
   grade doesn't predict result, E6 needs a different foundation than
   "trust the existing grades."
3. **Is there enough resolved history per (engine, regime) cell to fit
   anything safely yet?** A model or even a threshold refinement built on
   40 trades overfits and reports false confidence; the same
   `priors_min_sample_swing`-style floor discipline this project already
   applies to `swing_priors()` needs a real answer here, not an assumed
   one.

**Gate E2:** three real numbers in the ledger — a distribution, a
correlation, a sample-size table by (engine, regime). If any answer is
"not yet, not enough data," that changes E6's shape (a simpler rule-based
refinement rather than a fitted model) but does not block E3–E5, which
depend on none of this.

**Gate E2 — CLOSED, 24-Aug-2026 (F-68, `feat/swing-evolution`).** New
`tools/swing_feature_edge_study.py` — independent of `tools/
feature_edge_study.py` and everything under `intraday/`, the same
proven tercile/bucket-vs-rest method reimplemented rather than imported,
per this track's own non-negotiables. Distribution: CONTINUATION n=427
(20 findings), MOM n=118 (11 findings), RVS n=12 (below the 40-sample
floor). Two land directly on this session's own trades: CONTINUATION's
`sector` split puts metals & mining at 31% win rate against 75% for
every other sector (HINDCOPPER's own sector); MOM's `sector_rank_at_
entry` split shows rank ≤4 at 100% (39/39) against rank ≥10's 82%.
Correlation: **unanswerable** — `post_trade_analysis`'s A–F grade is
computed and used only to word a lesson's prose, never persisted to any
column; Stage E6 needs to capture it before this question has an
answer. Sample-size table: every resolved row currently reads `regime =
'NEUTRAL'` — no regime diversity exists yet to validate E3's own premise
against, which does not block building E3 but means it ships unvalidated
against a regime shift until one actually occurs. 31 `PENDING` findings
written to `brain_proposals`, `target_key` prefixed `SWING/`, nothing
live changed. Full detail: `docs/FINDINGS.md` F-68.

## Stage E3 — Close the "knows but doesn't act" gaps

The smallest, lowest-risk stage, and the one a professional desk would
consider table stakes rather than an enhancement:

- **Regime-aware exit ladder.** `evaluate_exit()` reads
  `regime_at_entry` — frozen on entry day — and never the market's
  current state. Read live regime; in RISK_OFF tighten the giveback
  threshold and shorten the effective stall clock further below E2/F-46's
  own per-family number; in a strong RISK_ON/TRENDING tape, more
  patience. Touches `control/position_lifecycle.py` only.
- **`ai_recommended_action` becomes real.** `TIGHTEN_SL` is written by
  `ai_decision_engine.py`, displayed by `alerts/send_alerts.py`, and
  consumed by nothing else — confirmed by grep, not assumed. HINDCOPPER's
  own tighten-stop recommendation over a live geopolitical risk sat as a
  notification. Execute it as an actual stop adjustment, one-directional
  only (never looser than the current active stop), same asymmetry every
  other rule in this ladder already respects.
- **A standing health check for the pending-fill/reconcile visibility
  class.** F-67 fixed one instance (`_pending_fills` erased by a
  `load_state()` rebuild that hadn't caught up); the class of risk — an
  order placed and not yet reflected as held — deserves a `tools.health`
  check that catches the next instance of this shape before it becomes a
  second real order, not a human reading a position two days later.

**Ships behind switches, shadow-logged before arming** — this stage
changes live exit behavior on a book that is currently working, which is
a different risk profile from a stall-clock number that can only ever
tighten.

**Stage E3 — BUILT, 24-Aug-2026 (F-70, `feat/swing-evolution`).** All
three pieces shipped: the standing health check (armed immediately, it
is read-only diagnostic) caught a SECOND, previously unknown incident of
F-67's own shape on HAL (21-Aug, three days before HINDCOPPER) —
`ai_recommended_action` execution and the regime-aware multiplier both
ship OFF, shadow-logged, exactly as planned. `tools.simulate` confirmed
the AI-tighten shadow log firing correctly against HINDCOPPER's real
position (`sl 503.85 -> 539.00`). HAL's own doubled position (2 shares
instead of the intended 1, ~44% of the portfolio) was left untrimmed —
the operator's call, not this session's to make. Full detail: `docs/
FINDINGS.md` F-70. Arming either switch is a separate decision, not
part of this stage's own completion.

## Stage E4 — In-trade intelligence

Depends on E2's per-engine numbers for calibration, not on E6 being
built.

- **Live sector-rank decay as an ordinary holding check**, not just the
  3R runner gate — and using today's actual rank, not the entry-day
  snapshot `exit_runner_max_sector_rank` currently reads. A sector rotating
  out beneath a held position is invisible to this book below 3R today.
- **A swing-native invalidation level**, computed at entry from
  `assess_trend()`'s own evidence (leading sector, structural setup,
  momentum state) and checked independently of price — so a position that
  hit its stop and one whose thesis broke first are told apart in the
  record, closing the same gap intraday already closed for itself.
- **Day-by-day participation decay.** `delivery_pct`/`volume_trend` are
  already computed every evening; nothing checks whether a held
  position's own participation is fading relative to what it opened on,
  ahead of the fixed stall clock — the swing-cadence version of F-45's
  intraday volume-decay idea.
- **Structural read from day one, not gated at 1R.** `deterioration_check
  ()` — the only thing that reads whether a position's thesis is still
  intact — only ever runs at `gain_r >= exit_deterioration_min_r` (1.0).
  A trade going wrong from day one gets zero structural evidence until
  fastfail (day 4) or the calibrated stall clock (day 6–10) — pure
  price-and-time until then. F-46's telemetry already runs at any gain_r;
  this stage makes a BROKEN read near breakeven actionable early, not
  just recorded.
- **Sector rotation as a book-wide early signal.** `sector_strength` is a
  daily time series the pipeline already builds; a sector trending down
  for 2–3 sessions, before it falls out of the top ranks a single
  position's own check would catch, is visible earlier across the whole
  held book than in any one name's chart.

**Stage E4 — FULLY BUILT, 24-Aug-2026 (F-71 + F-72, `feat/swing-evolution`).**
All four pieces shipped. F-71: structural break checked from day one
(`EXIT_INVALIDATED`, distinct from `EXIT_DETERIORATION`) and live
sector-decay tightening from `sector_strength`'s already-computed state.
F-72: day-by-day participation/delivery decay (`vol_ratio` at entry vs.
latest, tighten-only, 2-session floor) and a book-wide sector-
concentration health check (`tools/health.py::check_sector_concentration_
risk`, flags when >=50% of the open SWING book sits in WEAKENING sectors
— a per-position check cannot see this by construction). All four ship
OFF, shadow-logged. Live proof: two of the book's three open positions
(HINDCOPPER/metals & mining, AARTIIND/chemicals) sit in sectors reading
WEAKENING — both the per-position shadow line AND the new book-wide
health check correctly caught it (67% of the book). Participation decay
found no live signal on today's book and said so honestly (HINDCOPPER
re-entered today with no entry-day baseline yet, AARTIIND's volume rose
rather than decayed, HAL's latest session is still its entry day) —
proven instead by five synthetic tests. Verifying F-71 live surfaced and
fixed a real gap in `tools/simulate.py` — it had been building an
incomplete policy dict since F-46, missing the exact context `evaluate_
exit()` needs; factored into one shared function (`load_live_exit_
context`) both the daemon and the simulator now call, so they cannot
drift apart again — F-72's participation-decay fetch was added to that
same shared function rather than a fourth separate copy. Investigating a
`tools.health` run for F-72 also turned up `pending_dup` flagging
HINDCOPPER again; traced by timestamp before reporting — the incident
predates the F-67 fix commit earlier the same day, not a recurrence, and
is noted as an untested-live fix (fix landed 27 minutes before close) in
F-72 rather than assumed clean.

**F-73 refinement, same day:** the sector-decay multiplier now exempts a
WEAKENING-sector position whose OWN `vol_ratio` is holding or rising
(`swing_sector_decay_strength_exempt_floor`, migration 111) — a group-
level sector read must not override demonstrated stock-level strength.
Live proof: AARTIIND (chemicals, WEAKENING) now reads EXEMPTED, its own
volume up 25% since entry. F-73 also fully traces the HINDCOPPER order
collision to F-67's already-documented outcome — reconcile corrected it
to the true 4-share holding, no double position ever resulted, unlike
HAL's retained real one. Full detail: `docs/FINDINGS.md` F-71, F-72,
F-73.

## Stage E5 — Entry-side intelligence

- **A zone-drift penalty in `score_plan()`.** HAL filled at 5010.20
  against an evening-computed zone of 4794–4949 — a real chase, not a
  clean entry — and nothing in `entry_ranking` penalizes a fill that has
  drifted materially above its own zone. Derate, do not refuse outright;
  the live-price R:R recomputation `decide()` already does is correct in
  principle, this closes the gap where a technically-passing R:R still
  represents a materially worse trade than the same setup at the zone
  price.
- **The AI's own "lessons" as checkable predicates, not prose.** HAL's
  20-Aug note literally read *"avoiding chasing after a sharp rally"* as
  a stated lesson one day before the same AI approved a trade that did
  exactly that. A small, named set of these lessons (overextension from a
  moving average, chase distance, RSI band) become structured checks in
  `entry_refusals()`, the same mechanism `filter_reason` already uses — a
  self-contradiction between a stated lesson and the candidate it is
  attached to becomes a real refusal, not a footnote.
- **Weekly-structure confirmation at entry.** `weekly_structure` is read
  once, inside `assess_trend()`, which only ever runs post-1R — the
  entry gate (`analysis/trade_decision.py`, `entry_ranking.py`) never
  sees it. Pull it into `score_plan()` or `entry_refusals()` if E2's
  numbers support it as a real signal, not decoration.

**Piece 1 — BUILT, 24-Aug-2026 (F-74, `feat/swing-evolution`).** Quantify
first (this project's own "quantify before build" pattern) found the
zone-drift penalty as originally scoped — raw % distance above
`entry_zone_high` — does not cleanly separate outcomes on the available
sample. HAL's own real numbers pointed at the actual mechanism: R:R
RETENTION, not price distance — stop/target stay fixed while the zone
catches up to a running price, so `rr_at_zone_low` (7.63–14.09 across
HAL's three prior snapshots) collapsed to `rr_live` 1.17 at the real
fill. Re-bucketed the same sample by retention: worst-half avg −0.003R
(3 of 4 losses), best-half avg +0.227R (1 loss) — small (n=8/bucket) but
directionally real. Building this surfaced that the mechanism to act on
it already half-existed and was silently dead: `score_plan()`'s own
comment claims its R:R term is "the live figure," but `implied_rr` is
pipeline-only and never refreshed, while `decide()`'s `rr_live` — the
actual live figure — sat unused at both ranking call sites
(`intraday/engine.py::_maybe_enter_swing`, `tools/simulate.py::
simulate_swing_entries`). Fixed by reviving the dead path (new shared
`entry_ranking.live_ranking_input()`) rather than building a second,
parallel penalty next to a mechanism that already existed but didn't
work — smaller change, same effect, no new config switch needed. Full
detail: `docs/FINDINGS.md` F-74.

**Pieces 2–3 — INVESTIGATED, both evidence-based "not yet," 24-Aug-2026
(F-75).** Piece 2: the roadmap's own HAL/20-Aug anecdote doesn't survive
a literal check (the lesson's stated trigger, RSI-W>85, wasn't met —
HAL's real rsi_weekly was 66.2). `ai_max_chase_pct` was null on the
actual entry day (2.0 the day before) but even carrying it forward
wouldn't have stopped this trade (real chase was 0.94%, under any
sensible cap) — R:R retention (piece 1) is the real mechanism, and the
sample (n=16) is too thin to set a hard refusal on it without risking a
threshold no real winner can clear. Piece 3: found first that its own
prerequisite — `assess_trend()`'s EXISTING exit-side use of
`weekly_structure` — was silently dead (wrong vocabulary, same shape as
migration 048's "RISK ON"/"RISK_ON" collision) and, worse, was
deflating the trend score for the majority of positions by inflating
`checks` without ever contributing evidence. Fixed as a direct
correctness fix (not gated — restoring already-live logic, same
precedent as F-67/F-69). Live proof: HINDCOPPER's verdict flipped
INTACT(67%) -> STRONG(78%); AARTIIND/HAL rose in confidence with
unchanged verdicts; nothing on today's book actually changes decision
(no consumer treats STRONG differently from INTACT), so this improves
accuracy for future borderline cases rather than today's book. Pulling
the now-working signal into the entry gate — piece 3's original literal
scope — was not done; same thin-sample reasoning as piece 2. Full
detail: `docs/FINDINGS.md` F-75.

**Pieces 2-3 — SHADOW-BUILT, 24-Aug-2026 (F-76, `feat/swing-evolution`).**
Operator's own correction to F-75: thin evidence justifies staying OFF,
not staying unbuilt — the same participation-decay precedent (Stage E4)
already established. `entry_refusals()` now shadow-logs both: R:R
retention below `entry_rr_retention_floor` (0.20 — chosen so HAL's own
real 0.1533 retention actually lights up the shadow, not 0.15) and a
candidate whose own `assess_trend()` verdict already reads BROKEN
(reusing F-75's fix directly, not a parallel check). Both switches off.
Wiring the shadow logs into `tools.simulate` for visibility surfaced a
real, independent gap: that tool had never called `entry_refusals()` at
all — `entry_respect_filter_reason` is armed live, so the daemon was
already refusing plans on this basis while `tools.simulate` silently
showed a different result (SIEMENS misreported as the top TAKE on
2026-08-21's real plan set). Same shape as F-71 §3's incomplete policy
dict, a second instance in the same tool. Full detail: `docs/FINDINGS.
md` F-76.

**Stage E5 complete.**

## Stage E6 — The learning core

The most valuable stage in this track and the one that gets the most
caution, sequenced after E2–E5 so it can build on real per-engine numbers
rather than a guess:

- **Reconnect the lesson engine on a measured path, not through the AI
  prompt.** `post_trade_analysis`'s grading has real machinery behind it
  and, per its own docstring, is built to catch exactly the kind of gap
  this whole track has been finding — but its only consumer today is
  `ai_decision_engine.py`'s prompt context, downstream of a component
  (`ai_tier`/`ai_conviction`) already shown not to carry ranking signal
  on its own. If E2's correlation check confirms the grades predict
  forward outcome, route validated findings into `score_plan()` directly
  — never back through the AI prompt that was already shown not to be
  the channel.
- **Per-engine feature tuning**, the swing shape of F-44's `feature_edge_
  study.py`: each engine's own trigger sharpened from its own resolved
  winners and losers, not a blanket ranking bonus applied book-wide.
- **A living engine lifecycle.** MOM and RVS were promoted to `ACTIVE` on
  measured-flat-to-negative historical evidence (`docs/6_
  IMPLEMENTATION_STATUS.md`'s own win-rate table) and the promotion has
  sat static since 07-Aug. `ACTIVE`/`SHADOW` becomes a rolling,
  out-of-sample-validated read, continuously re-evaluated as new trades
  resolve — the same discipline as everything else in this stage — not a
  decision made once that quietly keeps governing capital months later.
- **A real anticipatory model, out-of-sample validated** (only once E2
  confirms sample size supports it per engine/regime cell): fit
  probability-of-target and expected-R from the full resolved feature
  set, walk-forward validated, re-fit as more trades close — genuine
  calibrated anticipation from the hundreds of data points already being
  computed, not a threshold nudge and not a claim of certainty. This is
  the direct answer to "make the engines smarter," done the way a real
  desk does it rather than by hand-tuning one more weight.
- **A swing discovery engine**, the swing shape of F-66's discovery-to-
  shadow-strategy pipeline: mine every resolved plan — not only the ones
  the 9 existing engines fired on — for feature combinations with a real
  edge above the book average, template a validated one into a candidate
  engine, gate it behind the same human-approval mechanism (`brain_
  proposals`, `status=APPROVED`) F-66 already proved out, shadow-run
  before it ever touches capital.

**Every finding in this stage validates out-of-sample before it can
influence a live decision, and even then only as a priority tie-break or
a bounded derate — never a new hard gate invented from a small sample —
mirroring F-48/F-50's exact contract.**

**Two of five pieces BUILT, 24-Aug-2026 (F-77, `feat/swing-evolution`).**
The other three (lesson-grade reconnection, the anticipatory model,
per-engine feature tuning / discovery engine) checked against this
session's own F-68/F-69 findings and found genuinely blocked, not merely
uncautioned about: the lesson grade currently predicts BACKWARDS (grade
D averages +0.30R, grade C −0.01R); the anticipatory model has zero
regime diversity to fit against (every resolved row reads `regime=
NEUTRAL`); the feature-edge findings that would feed tuning/discovery
didn't survive F-69's own recency re-check.

Built: (1) `tools/swing_feature_edge_study.py::validate_pending_swing()`
— the recency validator F-68 §4 and F-69 §3 both explicitly asked for,
requiring BOTH a since-creation AND a short recent-window check to
independently confirm before VALIDATED. Live proof: automatically
reproduces F-69's own by-hand conclusion exactly on the two findings
that failed, and independently finds three MORE that pass the recent-
window check alone. (2) `tools/weekly_review.py::
review_swing_engine_lifecycle()` — all 9 swing strategies re-measured
against current evidence instead of a static 25-Jul/07-Aug decision.
Live: CTL/MOM/SEC (only three above the 40-sample floor) all read
healthy and keep ACTIVE; RVS (avg −0.97%) and TPO (avg +0.36%) are the
two names with real cause for concern, both correctly held as too thin
to act on. Full detail: `docs/FINDINGS.md` F-77.

## Stage E7 — Position scaling

Sequenced last, deliberately: the only stage in this whole track that
adds capital risk rather than sharpening a decision already being made,
and the one that most benefits from E6's validated-finding mechanism
existing first to help judge which winners actually merit it.

- Never before the runner line (`gain_r >= giveback_runner_min_r`, the
  same 1.0R the F-43 tiered giveback guard already uses) — adding to a
  position whose original risk is not yet secured is adding to weakness.
- Only on independent trend evidence at the bar the RUN decision already
  requires (`assess_trend()` STRONG with real evidence), ideally once E4/
  E6's telemetry has enough closed trades to have validated that bar
  rather than trusting it on faith.
- The add is a new risk allocation competing for capital like any other
  candidate — its own structural stop, its own `risk_pct_per_trade`
  budget through `check_new_entry()` — never sized off the position's own
  unrealized profit.
- Caps at one add; the combined position's risk is measured from the add
  forward, not blended with the original entry's now-stale number.

## Non-negotiables across the whole track

- No file under `intraday/` outside the shared daemon file's own swing
  branches, and no file under `allocation/`, is ever edited — read-only
  imports only, the exact discipline F-46 already established.
- Every stage that changes live exit or entry behavior ships behind a
  switch, shadow-logged, before arming — the one exception granted this
  session (F-43/F-46's giveback tiering and stall-clock calibration
  shipping straight to live) was the operator's own explicit, stated
  call, not a default this track assumes going forward.
- Any change touching the shared daemon file or `position_lifecycle.py`
  runs the full `tools.verify` suite AND the swing-specific `tools.
  health` checks (`books`, `broker`, `stops`, `qty_fields`) before being
  considered safe — Track D's own rule, adopted here explicitly rather
  than re-derived.
- Every number in every gate comes from a real query against this
  account's own data, the same standard every finding in F-43/F-46/F-67
  was held to — a claim about "probably works" does not clear a gate here.
