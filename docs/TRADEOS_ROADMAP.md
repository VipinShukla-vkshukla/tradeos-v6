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

**Not yet built:** a refresh pipeline for `nifty_total_market` itself
(currently static — confirmed no freshness column, updated only when
someone re-imports NSE's index-constituent CSV by hand). The operator
named `https://www.niftyindices.com/IndexConstituent/ind_niftytotalmarket_
list.csv` as the source; a `WebFetch` against that URL timed out (60s),
consistent with anti-automation protection on that host — a scheduled
refresh will need to handle that (proper headers, retries) and must not
corrupt the `nifty_200`/`nifty_500` boolean columns `compute_indicators.
py::fetch_index_membership()` already depends on. Scoped as a separate,
later piece: Population C (Kite's instrument master) already closes the
IPO gap `nifty_total_market`'s staleness would otherwise leave open, so
this is a freshness improvement to Population B, not a blocking gap.

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
