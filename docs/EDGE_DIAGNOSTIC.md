# TradeOS — Edge Diagnostic

**Put this at `docs/EDGE_DIAGNOSTIC.md`. Run it BEFORE `HARDENING_BRIEF.md`.**

The hardening brief prevents rare catastrophic losses. It creates no edge. This
document is about the question that actually matters right now: **is TradeOS
selecting trades that pay, and if not, why not.**

## The question is not what it looks like

"Am I picking the right trades" has two possible answers with **opposite fixes**:

| | Signature | Fix |
|---|---|---|
| **Signal problem** | gross R negative | retire engines, filter harder |
| **Friction problem** | gross R positive, net R negative | wider stops, larger clips, fewer trades |

Getting this backwards is expensive. If the problem is friction and you add
filters, you cut the volume and keep the fixed cost — a smaller version of the
same loss. `tools/engine_scorecard.py` separates gross R, cost R and net R for
exactly this reason. **Do not look at net R alone.**

Measured on this account 10-Aug-2026, per that tool's own docstring: the
intraday SHORT book ran gross **+0.141R** against cost **0.293R**. The signal
was real. The stop was too tight to pay for it.

## Rules

Same as the hardening brief. Additionally:

- **Every number carries its n.** An engine's fate is not decided on a mean
  without a count. `engine_scorecard` deduplicates to one observation per
  (symbol, engine, trade_date) because the raw table votes once per 15-second
  cycle — on 10-Aug that inflation *flipped the sign* on the two highest-volume
  engines.
- **Never compare numbers from different populations.** This has already caused
  one wrong conclusion in this repo (see PHASE A).
- **Retire nothing on one session.** 12-Aug was one day.
- **Change nothing in Phases A–C.** They are read-only. Decisions come in D.

---

# PHASE A — WHICH NUMBER IS TRUE

**Branch:** `diagnostic/population-reconciliation`
**Code changes:** NONE.

Three tools currently disagree about whether the intraday book has edge, and the
disagreement is structural, not statistical.

| Source | 10-Aug reading |
|---|---|
| Operator dashboard, 40 closed trades | **+₹141.80, profit factor 1.55** |
| `tools/exit_audit.py` | 14 winners @ +1.327R, 26 losers @ −0.687R → **+0.018R** |
| `tools/engine_scorecard.py`, same window | **−0.138R gross** |

The cause is known: `engine_scorecard` counts every `intraday_setups` row with
`cost_verdict='TAKEN'` — 119 deduplicated rows — but the closed book has 40
trades. **79 "TAKEN" detections never became a position.** `cost_verdict` is
written when a setup clears `is_worth_taking()`, *before* the allocator veto and
`_maybe_open_paper()`'s capacity checks run.

## A.1

```bash
cd backend && python -m tools.taken_reconciliation --days 30
```

Report: how many `TAKEN` rows became real positions, how many did not, and
**what the non-positions resolved to.** If the 79 phantoms resolved worse than
the 40 real trades, `engine_scorecard`'s negative reading is measuring the
capacity gate's refusals and calling them the book's performance.

## A.2

```bash
cd backend && python -m tools.exit_audit --days 30
cd backend && python -m tools.engine_scorecard --book INTRADAY
```

Reconcile against each other and against the dashboard. **State which number is
the real closed book and use only that one from here on.**

## A.3 — The blocker

`tools/expectancy_ledger.py` reports `planned_stop_at_entry` present on **21 of
91** closed rows, `charges` on 21 of 91. R needs the stop planned at entry;
seventy rows cannot produce one.

```bash
cd backend && python -m tools.expectancy_ledger
```

Report exact current coverage of both columns, split SWING / INTRADAY. **If
coverage is still under ~50%, say so loudly.** Every engine score, the
conviction floor and the eventual allocator priors are being computed on a
quarter of the book, and that is a data-completeness problem, not a strategy
problem.

If Stage 4 is already carrying the stop through, coverage improves on its own
and the fix is patience. If it is not, **fixing that write is higher priority
than anything in either brief** — nothing downstream is trustworthy without it.

## Acceptance

`docs/EDGE_DIAGNOSTIC_A_<date>.md`: the three populations reconciled, one
declared authoritative with reasoning, and current column coverage.

---

# PHASE B — WHERE THE MONEY GOES

**Branch:** `diagnostic/edge-decomposition`
**Code changes:** NONE. Use the authoritative population from Phase A only.

## B.1 — Signal or friction?

```bash
cd backend && python -m tools.engine_scorecard --book INTRADAY
cd backend && python -m tools.engine_scorecard --book SWING
```

Produce, per engine family, with n:

| Engine | n | gross R | cost R | net R | verdict |
|---|---|---|---|---|---|

Then classify the **book as a whole**:

- gross > 0, net < 0 → **friction problem.** Do not add filters. Go to B.4
- gross < 0 → **signal problem.** Go to B.2
- both positive → the book has edge; the pain is variance or sizing. Go to B.5

This single classification determines everything after it.

## B.2 — Which engines earn their existence

For each of the nine engines, with n and gross/cost/net R separately.

Expect a small number to carry the book. That is normal and is not a failure.
The question is whether the losers are losing on **signal** (retire) or on
**stop width** (fix the stop).

**Do not retire on fewer than ~30 deduplicated observations.** Record the ones
below that threshold as "insufficient evidence" and let them run.

## B.3 — The counterfactual: are the gates backwards?

**This is the highest-value query in this document and it has never been run.**

`intraday_setups` holds every detection including refused ones, and
`outcomes.py` resolves every row against real bars, net of cost, assuming the
bad fill when one bar contains both stop and target.

Group by `cost_verdict` — `TAKEN` versus each `BLOCKED_*` reason — and report
hit rate and mean `outcome_pct` with n for each.

| Reading | Meaning |
|---|---|
| TAKEN > BLOCKED | gates are working. Leave them alone |
| TAKEN ≈ BLOCKED | gates are noise — they cost opportunity and return nothing |
| **BLOCKED > TAKEN** | **gates are inverted.** The fix is removal, not addition |

Do this per blocking reason, not in aggregate. One inverted gate hidden among
four good ones will not show up in a total.

This is the direct answer to "am I picking the right trades." It measures the
trades you *didn't* take against the ones you did, across hundreds of rows
rather than forty.

## B.4 — If it is friction

```bash
cd backend && python -m tools.expectancy_ledger --by-clip
```

cost R = `cost_pct / risk_pct`. **A tighter stop makes this larger.** So the
intraday framework's tight stops and the 0.21% MIS round trip are in direct
tension, and the swing book's small clips face a worse version: sub-₹2,500 CNC
clips were measured at **2.363R friction, −3.180R net.**

Report net R by clip-size bucket for both books, and identify the clip size
below which no realistic edge survives. **That number is a hard floor on
position size**, and on ₹20,000 of capital it may mean the book supports far
fewer than seven concurrent positions.

If so, say it plainly. Running seven positions an account cannot afford to run
is a structural problem no engine improvement can fix.

## B.5 — Does conviction predict anything?

Bucket `confidence` (0.55–0.65, 0.65–0.75, 0.75–0.85, 0.85+) and report mean
`outcome_pct` and hit rate with n per bucket.

**If outcome does not rise with confidence, the score is noise** — and the
conviction floor that rises 0.55 → 0.80 through the day is filtering on a random
variable while feeling principled. That would be worth knowing before another
line of scoring logic is written.

Do the same for SWING using `final_score` against realised R.

## B.6 — When, not what

Segment the authoritative population by:

```bash
cd backend && python -m tools.engine_scorecard --book INTRADAY --by phase
```

- **`phase`** — is PRIME actually better than DRIFT, or is that an assumption?
- **`regime_at_detection`** — this column exists precisely to accumulate this
  evidence, and `regime_fit_multiplier()` sits at weight 0.0 waiting for it
- **`direction`** — long vs short, separately. They have different cost profiles
- **engine × regime** — the day-type question

12-Aug's 7-of-8 `SETUP_INVALIDATED` losses were all long-side continuation on a
0.67-breadth day. That is a hypothesis with a shape: *continuation engines may
only pay in continuation regimes.* If `regime_at_detection` shows the same
pattern across 30 sessions rather than one, that is the finding that justifies
Phase 5 of the hardening brief — and if it does not, that phase should be
deleted.

## Acceptance

`docs/EDGE_DIAGNOSTIC_B_<date>.md` with every table above carrying its n, and a
one-line answer to: **signal problem, friction problem, or neither.**

---

# PHASE C — SWING

**Branch:** `diagnostic/swing-edge`

Smaller sample, so the questions are cruder.

## C.1

Do the nine screening engines produce differentiated outcomes, or does
everything converge on the same names? Report realised R by originating engine,
with n. **Expect most buckets to be too small to judge — say so rather than
ranking noise.**

## C.2

Does `final_score` correlate with realised R? Same test as B.5. If not, the
ranking layer is decorative and `swing_max_new_per_day=2` is choosing arbitrarily
from the top of a list that does not mean anything.

## C.3

```bash
cd backend && python -m tools.exit_ladder_replay --min-r 0.5
```

Are the exit rungs helping? Specifically:

- Is `EXIT_GIVEBACK` net-positive versus holding to the stop? It was calibrated
  on 24 of 28 losers that had been >0.5% green — but that is the *loser*
  distribution only. **How many winners does it also cut?** That number was never
  measured and it is the one that decides whether the rung pays
- Same for `EXIT_STALL` — how many stalled positions worked in sessions 11–15?
- Note: this reading is contaminated until `fix/session-count-parity` lands, so
  either sequence that phase first or state the contamination

## C.4

"Profits are not significant" on ₹20,000 with 1% risk per trade is **₹200 of
risk per position**. At 2R average on a 40% hit rate, expectancy is roughly
₹40–60 per trade before friction. **That may be working exactly as designed and
still feel like nothing.**

Compute realistic expectancy per trade in rupees at current capital, sizing and
friction. Then state what monthly return the framework produces if every rule
performs to specification.

If that number is small, the problem is capital, not selection, and no amount of
engine work changes it. **That is a finding worth having explicitly rather than
implicitly.**

---

# PHASE D — DECIDE

**Branch:** `proposal/edge-actions`
**Still changes nothing.** Output is a proposal document.

Rank every finding by expected R gained per unit of risk introduced. Then apply,
in strict order:

1. **Fix the data first.** If Phase A.3 shows poor column coverage, that outranks
   everything. Nothing computed on a quarter of the book is trustworthy
2. **Subtract before adding.** Retiring an engine with n≥30 and negative gross R
   is free — no new logic, no new failure mode. This is almost always the largest
   single improvement available and it is the one nobody wants to do
3. **Fix friction before signal**, if B.1 says friction. Stop width and clip size
   are config, not code
4. **Only then consider adding.** Any new filter must beat the counterfactual
   from B.3, not just sound sensible

Then reconcile against `HARDENING_BRIEF.md`:

- Phase 5 (breadth) — **keep only if B.6 supports it across 30 sessions.** One
  session is not evidence
- Phases 1, 2, 3 — unaffected; they are loss-prevention and stand on their own
- Phase 4 (`sizing_max_cost_r`) — **may be promoted to first** if B.4 shows
  friction dominates. It is config-only and reversible

Route through `brain_proposals`. Nothing auto-applies.

---

# SEQUENCING

```
A   population reconciliation      1 session   ← start here
B   edge decomposition             1-2 sessions
C   swing                          1 session
D   proposal                       1 session
    ── then ──
1   session-count parity           from HARDENING_BRIEF
2   corp-action guard
    ...
```

Phases A–D change no code and can run tonight, in parallel with the market being
closed. **Nothing in the hardening brief should start until Phase D exists**,
with the exception of Phase A.3's data fix if coverage turns out to be bad —
that one is urgent regardless.

---

# WHAT THIS MIGHT FIND

Named in advance so no session softens them:

- **The book already has edge and the recent pain is variance.** Forty trades is
  not a sample. PF 1.55 was the last real measurement
- **Several engines have never paid** and turning them off is the entire fix
- **The edge is real and the account is too small to express it.** Sub-₹2,500
  clips at 2.363R friction is not a strategy problem
- **The confidence score does not predict anything**, making the ranking and the
  floor decorative
- **The gates are net-negative** — refusing better trades than they permit
- **There is no edge yet and the sample cannot tell you**, in which case the
  correct action is to keep running small and collect, not to keep building

The last one is the hardest to accept and the most common. Guard against the
temptation to add logic in place of an answer.
