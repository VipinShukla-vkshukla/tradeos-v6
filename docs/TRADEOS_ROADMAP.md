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
