# TradeOS — Validation & Hardening Brief

**Put this at `docs/HARDENING_BRIEF.md` in the repo.** It is the task brief for a
sequence of Claude Code sessions. It exists so each session starts from the
decision rather than re-deriving it, and so no session invents scope.

**Origin.** This came out of a scenario-coverage review conducted by reading the
source only — no tools were run, nothing was executed against a database or a
broker. **Every claim below is a HYPOTHESIS until Phase 0 confirms or refutes
it.** Several may be wrong. Treat a refuted claim as a good outcome, record it,
and move on.

---

## Rules every session in this sequence follows

These are the repo's own rules from `CLAUDE.md`, restated because they are the
ones this work is most likely to break.

1. **Verify, never assert.** Run it, paste the actual output, then say what
   happened. Do not describe what a command would print.
2. **Demonstrate every new check FAILING before trusting it to pass.** A check
   that cannot fail is not a check.
3. **Assert a realistic input also PASSES.** A check that cannot pass is the
   same defect wearing a different hat.
4. **Money moves only behind explicit switches.** Every behaviour change ships
   behind a config key. Default OFF unless this brief says otherwise, and where
   it says otherwise it says why.
5. **Never widen a gate to make something work.**
6. **Never `Read` a large file whole.** `Grep -n` to locate, then read the
   range. `position_lifecycle.py` is 21k tokens, `intraday/engine.py` is 22k.
7. **Never reimplement a decision — import it.** `decide()` and
   `evaluate_exit()` have one implementation each. Keep it that way.
8. **Own mistakes plainly.** If a phase breaks something, say what broke, what
   it cost, and what was restored.

**After every code change, without exception:**

```bash
cd backend && python -m tools.verify
```

---

## Branch strategy

One branch per change, all off `main`. They are independent, and some may be
rejected after their shadow period — entangling them would make that
unrecoverable.

```
validation/baseline-audit      Phase 0   findings only, ZERO code changes
fix/session-count-parity       Phase 1
feat/corp-action-guard         Phase 2
feat/held-position-guards      Phase 3
config/arm-dormant-gates       Phase 4   config only, no code
feat/breadth-shadow            Phase 5
feat/swing-entry-parity        Phase 6
```

Nothing merges to `main` until `tools.verify` is green on that branch and the
phase's own acceptance criteria are met.

---

# PHASE 0 — BASELINE AND CLAIM VERIFICATION

**Branch:** `validation/baseline-audit`
**Changes code:** NO. Not one line. If you find a bug, record it — do not fix it.
**Output:** `docs/BASELINE_AUDIT_<date>.md`

## 0.1 Establish the baseline

```bash
cd backend && python -m tools.verify 2>&1 | tail -40
cd backend && python -m tools.health  2>&1 | tail -60
cd backend && python -m tools.simulate 2>&1 | tail -60
```

Record: total checks, pass/fail counts, and **which failures are environmental**
(missing `python-telegram-bot`, no market session) versus real. Do not proceed
past a real failure — fix that first, on its own branch, before any of this.

## 0.2 Verify the eleven claims

Each claim below was derived by reading source. For each: **confirm, refute, or
mark unverifiable**, and paste the evidence. A refuted claim is a useful result.

| # | Claim | How to test it |
|---|---|---|
| C1 | `evaluate_exit()` receives **calendar days** from `intraday/engine.py::evaluate_positions` and **trading sessions** from `position_lifecycle.py::manage_open_positions` — same parameter, two quantities | `grep -n "sessions_held\|_sessions_held" control/position_lifecycle.py intraday/engine.py`. Then compute both for a fixture `entry_date` 15 calendar days back spanning two weekends. If they differ, confirmed |
| C2 | The daemon therefore fires `EXIT_STALL` at ~8 sessions (not 10) and `EXIT_TIME` at ~11 (not 15) | `python -m tools.exit_ladder_replay` over closed SWING positions. Look for EXIT_STALL / EXIT_TIME rows whose true session count is below the configured threshold. **This is the money question** |
| C3 | Nothing anywhere adjusts for bonus / split / rights ex-dates | `grep -rni "bonus\|split\|ex_date\|corporate_action" --include=*.py backend/ \| grep -v "\.split(\|rsplit\|splitlines\|train_test"`. Expect hits only in `position_event_monitor.py`'s label list and `sentiment_scorer.py`'s keyword list |
| C4 | `position_event_monitor.py` writes an alert and **nothing reads it** | Trace every consumer of `data_anomalies` rows written by it. If no consumer gates or modifies a position, confirmed |
| C5 | `sl_monitor.check_circuit_locks()` alerts on a circuit lock and takes no action | Read the function and its caller. Confirm the return value is only ever counted, never acted on |
| C6 | `overlay_liquidity_enabled` is OFF, so `liquidity_ok()` returns `True` unconditionally — the swing liquidity gate is decorative | Read the key's live value from `system_config`. Then confirm the early return in `analysis/overlays.py::liquidity_ok` |
| C7 | Same for `sizing_max_cost_r` (0.0), `overlay_vol_scaling_enabled`, `overlay_expiry_enabled`, `intraday_giveback_pct` (0.0) | Read all five live values. Record them in a table |
| C8 | Swing entry has no event / ASM / F&O-ban gate; intraday has all three | `grep -n "NewsGate\|news_gate\|asm_flag\|fo_ban" intraday/engine.py` and check `_maybe_enter_swing` for the same |
| C9 | `_failed_today()` blocks intraday same-day re-entry; swing has no equivalent | Read `_failed_today` and every call site |
| C10 | The index gate reads price only, never breadth | `grep -n "breadth\|advance\|decline" intraday/market_context.py` |
| C11 | Intraday `max_favorable_excursion` is now written on HOLD, but historic closed rows are NULL | Count `closed_positions` rows where `framework='INTRADAY'` and MFE is NULL vs not. This sets the clock on arming the give-back guard |

## 0.3 Answer the question the review could not

**Are the thresholds right for this book?** Independent of whether the code is
correct.

```bash
cd backend && python -m tools.exit_ladder_replay --help
cd backend && python -m tools.expectancy_ledger
cd backend && python -m tools.engine_scorecard
```

Report, with counts:
- Exit-reason distribution for SWING and INTRADAY separately, with net R per reason
- How many closed swing trades carry MFE (the stall/give-back calibration set —
  the code says 55, mostly from a legacy manually-sized book)
- How many closed intraday trades carry MFE
- Whether `EXIT_STALL` and `EXIT_GIVEBACK` are net-positive decisions in the
  realised book

## Phase 0 acceptance

`docs/BASELINE_AUDIT_<date>.md` exists containing: the three tool outputs, a
confirmed/refuted/unverifiable verdict on all eleven claims **with pasted
evidence**, and the threshold analysis. No source file modified.

**Do not start Phase 1 until this document exists.** Phases 1–6 are all
conditional on it.

---

# PHASE 1 — SESSION-COUNT PARITY

**Branch:** `fix/session-count-parity`
**Precondition:** C1 confirmed in Phase 0.
**New config keys:** NONE. This is a bug fix, not a feature.

## The change

`backend/intraday/engine.py::evaluate_positions` computes `held` as calendar
days and passes it to a parameter named `sessions_held`. Replace with the
existing holiday-aware function.

```python
from control.position_lifecycle import _sessions_held
held = _sessions_held(self.sb, p.get("entry_date"), today_ist().isoformat())
```

**Cache it.** As written this is one Supabase read per swing position per
15-second cycle. Compute the session count once on the slow (300s) timer and
store it on the engine, the same way `_trend_ctx` is already handled. A fix that
adds forty round trips a minute is not a fix.

## The test

`backend/tests/test_sessions_parity.py`, exposing `TESTS = [(name, fn)]` and
registered in `tools/verify.py::MODULES`.

- **Must fail first:** with the current code, assert the two call sites return
  different `held` for an `entry_date` spanning two weekends. Paste the failure.
- **Must pass after:** same fixture, both paths agree.
- **Must also pass:** a same-week entry where calendar and session counts
  coincide — proving the test is not just detecting any difference.

## Acceptance

`tools.verify` green. `tools.simulate` shows no position exiting that was not
already exiting. Report how many currently-open positions change their
`sessions_held` and by how much.

## Rollback

`git revert`. There is no config key to flip — which is the point, and the
reason this phase is small and goes first.

---

# PHASE 2 — CORPORATE-ACTION EX-DATE GUARD

**Branch:** `feat/corp-action-guard`
**Precondition:** C3 confirmed.
**New config key:** `exit_corp_action_guard`, **default TRUE.**

This is the one permissive-by-default change in this brief, and the reason is
asymmetric: an unguarded ex-date is an unbounded loss on a healthy position; a
guarded one is a held position plus a Telegram alert.

## The failure being prevented

A 1:1 bonus halves the quoted price. `evaluate_exit()` rung 1 reads
`ltp <= sl`, returns `EXIT_STOP`, and `swing_auto_exit` market-sells a healthy
position at a fabricated −50% move.

## The change

A guard **above rung 1** in `control/position_lifecycle.py::evaluate_exit`.

**`evaluate_exit` must stay pure.** Read the corp-action map from
`policy["_corp_action"]`, exactly as the trend context is already passed via
`policy["_trend_ctx"]`. No I/O inside the function.

Populate it on the slow timer in **both** callers — `manage_open_positions` and
`IntradayEngine.load_state()` — from `nifty_upcoming_events` where
`event_date == today` and `purpose` matches the existing `LOW_EVENT_TYPES` set
in `swing/ingestion/position_event_monitor.py`. Reuse that set; do not define a
second one.

Return:

```python
{"action": "HOLD", "reason": "CORP_ACTION_EXDATE", "new_sl": None, "book_qty": 0,
 "detail": "<symbol> goes ex-<purpose> today — the quoted price is not "
           "comparable to entry/stop/target. Holding and alerting rather than "
           "reading a mechanical reprice as a stop breach. Re-anchor the levels "
           "by hand before the next session."}
```

Alert at `urgency="CRITICAL"`.

## The test

`backend/tests/test_corp_action_guard.py`, registered in `MODULES`. Use
`cfg_ctx()` — `system_config` is a process-wide global and one test's switches
leak into the next.

Four cases, all required:

1. Position entry 1000, stop 950, ltp 500, ex-bonus today → **HOLD**, not EXIT_STOP
2. Same position, no corp action → **EXIT_STOP** (proves the guard is not just
   suppressing all stops)
3. Same position, ex-date **tomorrow** not today → **EXIT_STOP** (proves the
   date match is exact)
4. `exit_corp_action_guard=false`, ex-bonus today → **EXIT_STOP** (proves the
   switch is read)

Demonstrate case 1 failing before the change lands.

## Explicitly NOT in this phase

Auto-adjusting `entry_price`, `planned_stop`, `planned_target` and
`high_water_mark` by the corporate-action ratio. That rewrites four price
columns on a live position and is a separate, migration-grade change. A guard
that holds is verifiable in one test; an adjuster is not. **Do not bundle them.**

---

# PHASE 3 — GUARDS ON HELD POSITIONS

**Branch:** `feat/held-position-guards`
**Precondition:** C4 and C5 confirmed.

Both items have the same shape and that is why they share a phase: **the
detection already exists and writes an alert, and no code reads it.** This is
the "a check that cannot act" pattern.

## 3.1 — Earnings on a held swing position

`position_event_monitor.py` runs at 08:35 and sends a brief. You then hold a
position with a 5% stop into a print with 12% gap risk.

**Minimal version — do not over-build.** A config key
`swing_event_hold_action` with three values:

| Value | Behaviour |
|---|---|
| `ALERT` | Today's behaviour exactly (**default**) |
| `TIGHTEN` | Raise `active_sl` toward the live price when results are within `swing_event_lead_sessions` (2) |
| `EXIT` | Close the position before the print |

Ship with `ALERT`. The key exists so the decision is yours and reversible; the
code path exists so it is one config edit away.

**`TIGHTEN` must never loosen a stop.** Every rung in both ladders holds that
invariant and this one must too.

## 3.2 — Circuit lock on a held position

`sl_monitor.check_circuit_locks()` already reads the real
`lower_circuit_limit` / `upper_circuit_limit` from the Kite quote payload. It
alerts and nothing acts.

What must change is **not** adding an exit — there is no exit liquidity at a
lower circuit, which is the whole problem. What must change is that the exit
ladder **knows the price is frozen**:

- A `EXIT_STOP` decision on a lower-circuit-locked name must not spam a market
  order every 15 seconds into a book with no bids
- The alert must fire once per lock, not once per cycle
- The position must be marked so the operator can see it is unmanageable rather
  than merely losing

Config key: `exit_respect_circuit_lock`, default TRUE. Suppressing a futile
order is not a behaviour change worth gating conservatively.

## The test

`backend/tests/test_held_position_guards.py`, registered in `MODULES`.
Both sub-features tested independently, each with a fails-first demonstration
and a realistic-input-passes assertion.

---

# PHASE 4 — ARM DORMANT GATES

**Branch:** `config/arm-dormant-gates`
**Precondition:** C6 and C7 confirmed, and Phase 0's threshold analysis exists.
**Code changes:** NONE. This phase edits `system_config` only.

Four gates are fully built, tested and switched off. Arming them changes what
the book does without changing a line of code — which makes this the
highest-leverage and most reversible phase here.

**Arm ONE PER WEEK. Never two at once.** Two simultaneous changes to entry
behaviour make attribution impossible when the entry count moves.

| Order | Key | To | Why now | Watch for |
|---|---|---|---|---|
| 1 | `overlay_liquidity_enabled` | `true` | `_maybe_enter_swing` already calls `liquidity_ok()` with a carefully-built `_stock_row` fallback. With the switch off it returns True unconditionally — a gate that cannot fail | Entries refused for "no traded-value data". If that fires often, the fallback is not working and this must come back off |
| 2 | `sizing_max_cost_r` | value from Phase 0 | The measured evidence is stark: sub-₹2,500 CNC clips run 2.363R friction and −3.180 net R. At ₹20k this may matter more than any engine | Entry count collapsing. If it refuses most of the book, the value is too tight — derive it from your own clip-size distribution, not from the example |
| 3 | `overlay_vol_scaling_enabled` | `true` | Bands already fitted to this account's own 94-observation VIX distribution, not textbook levels | Sizing down in a high-VIX period that also trends — the known cost |
| 4 | `intraday_giveback_pct` | **wait** | Requires ~20 closed intraday positions carrying MFE. C11 tells you how far off that is | Do not arm early. Calibrating on the swing number is calibrating on a different horizon |

`overlay_expiry_enabled` stays OFF. The day-type is inferred from the trading
calendar and is documented as wrong by one to three sessions most months on the
monthly flag. Sizing down the wrong days is worse than not sizing down.

## Acceptance per key

Five trading sessions of `intraday_setups` / entry logs after each flip.
Report entry count, refusal-reason histogram, and whether refused names
subsequently moved. **`tools.health`'s `config` check must still pass** — it
asserts a switch nobody reads is not sitting in `system_config`.

---

# PHASE 5 — BREADTH, IN SHADOW

**Branch:** `feat/breadth-shadow`
**Precondition:** C10 confirmed. Phases 1–4 stable.
**New keys:** `mkt_breadth_enabled` (**default FALSE**), `mkt_breadth_weak`.

The repo's own 12-Aug audit ranks this its #1 strategy gap, with a session's
evidence behind it: 7 of 8 losses `SETUP_INVALIDATED`, all long-side
continuation, breadth 0.67, regime score at its five-session low.

## The change

`intraday/market_context.py::classify` gains an optional parameter:

```python
def classify(index_ltp, index_vwap, index_prev_close,
             index_day_low=None, index_day_high=None,
             breadth: float | None = None) -> MarketContext:
```

`breadth=None` must reproduce today's output **exactly**. That is a test, not an
aspiration.

**The rule only ever demotes:** below `mkt_breadth_weak`, RISK_ON → NEUTRAL and
NEUTRAL → CAUTION. It never promotes, and it never blocks on its own.

**Data source.** If advance/decline is not already ingested, use the cheaper
proxy: percentage of the 40-name universe currently above session VWAP,
computable from `self._contexts` with zero new ingestion. Say which one you used.

## Ship it inert

Record `breadth_at_detection` on every `intraday_setups` row **with the switch
off** for 3–4 weeks. Then ask the data whether the population that would have
been demoted actually underperformed.

This is the same discipline `regime_fit` already ships under — built, keyed,
weight 0.0, pending the data that would justify it. Do not deviate from it here
because the hypothesis feels strong. It felt strong for `regime_fit` too.

## The test

`backend/tests/test_market_breadth.py`, registered in `MODULES`:

1. `breadth=None` → byte-identical to current output across all four states
2. `breadth=0.30` → RISK_ON demotes to NEUTRAL
3. `breadth=0.90` → CAUTION does **not** promote
4. `mkt_breadth_enabled=false` with `breadth=0.30` → no demotion

---

# PHASE 6 — SWING ENTRY PARITY

**Branch:** `feat/swing-entry-parity`
**Precondition:** C8 and C9 confirmed. Phases 1–4 stable.

Three gates exist on intraday and not on swing. In each case **the swing book is
the one holding real money.**

**Three separate commits.** Each changes what the book buys. Bundled, you cannot
tell which one moved the entry count.

| Commit | Change | Key (default) |
|---|---|---|
| 6a | Call the existing `NewsGate.check()` from `_maybe_enter_swing`, after the liquidity gate. Blocks results-today/tomorrow, ASM, F&O ban — all three at once, since `check()` already covers them | `swing_news_gate_enabled` (`false`) |
| 6b | Generalise `_failed_today()` to take a framework argument; call from `_maybe_enter_swing` | `swing_block_reentry_after_loss` (`false`) |
| 6c | Nothing new — 6a covers ASM and F&O ban. **Confirm** this rather than adding a second path | — |

**Reuse, do not reimplement.** `NewsGate` is loaded once per session and cached;
`_failed_today` is cached per session. Neither should acquire a swing-specific
copy. If a swing-specific variant seems necessary, stop and say why before
writing it.

## The test

`backend/tests/test_swing_entry_parity.py`, registered in `MODULES`. For each
commit: fails-first demonstration, realistic-input-passes assertion, and a case
proving the switch is actually read.

---

# NOT IN SCOPE

Named so no session picks them up opportunistically.

| Item | Why not |
|---|---|
| Upper-circuit handling on an **open** short | Real, but moot while intraday is PAPER. Must land before `intraday_live_auto_entry` — which has no implementation, deliberately |
| Corporate-action price **adjustment** (vs the Phase 2 guard) | Migration-grade. Separate proposal |
| Market-wide trading halt | Rare; the staleness guard already blocks new entries when ticks stop |
| Peak-margin penalty | Moot while intraday is paper |
| ORB retest arm | New logic, not a gap-fill. Needs its own backtest |
| Joint cross-book sizing | Already a documented precondition for live intraday |
| Overnight gap-risk sizing | Real and unaddressed, but it is a change to the sizing model, not a guard. Propose separately with evidence |
| Merger / buyback handling | Lower frequency than bonus/split/rights, which Phase 2 covers |
| Slippage feedback loop | Measurement gap, not a loss |

---

# ONE-PAGE SUMMARY

| Phase | Branch | Code? | Default | Gates on |
|---|---|---|---|---|
| 0 | `validation/baseline-audit` | No | — | Nothing. Run this first |
| 1 | `fix/session-count-parity` | Yes | n/a — bug fix | C1 confirmed |
| 2 | `feat/corp-action-guard` | Yes | **ON** | C3 confirmed |
| 3 | `feat/held-position-guards` | Yes | ALERT / ON | C4, C5 confirmed |
| 4 | `config/arm-dormant-gates` | **No** | one per week | Phase 0 thresholds |
| 5 | `feat/breadth-shadow` | Yes | **OFF**, 3–4wk shadow | C10, Phases 1–4 |
| 6 | `feat/swing-entry-parity` | Yes | **OFF** | C8, C9, Phases 1–4 |

**If Phase 0 refutes a claim, delete its phase from this brief and say so in the
audit document.** A plan that survives its own evidence unchanged was not a
plan, it was a preference.
