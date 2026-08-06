# TradeOS v6 — read this before answering anything

Claude Code loads this file automatically at the start of every session in this
repository. It exists so a new conversation starts with the design rather than
re-deriving it, and so advice never contradicts decisions already made and paid
for.

**Read `docs/0_SYSTEM_BLUEPRINT.md` first** — every engine, every guardrail, and
how the pieces connect, in one page. **`USER_GUIDE.md`** for how the system
behaves day to day, **`DESIGN_NOTES.md`** for what is decided but unbuilt.
**`knowledge_base/KNOWLEDGE_BASE.md`** for what trading *evidence* has shown so
far — read it before a daily trading review, update it after (see
`knowledge_base/REVIEW_PROMPT.md`). This file is the operating context around
all four.
`docs/TERMINOLOGY.md` disambiguates every regime/state vocabulary — read it
before writing anything that compares a "regime" string, because at least two
of these near-homophone systems have already collided silently once.

---

## What this is

An Indian equity trading system with **two separate frameworks** sharing one
₹20,000 account:

| | Swing | Intraday |
|---|---|---|
| Horizon | 1–3 weeks | flat by 15:15 IST |
| Mode | **LIVE** — real orders | **PAPER** — simulated fills |
| Product | CNC (never MIS) | MIS |
| Universe | ~60 plans from the evening pipeline | 40 names, own scanner |
| Exits | `control/position_lifecycle.py` | `intraday/exit_policy.py` |

They are deliberately **not** unified. Different horizons need different rules,
and one policy applied to both is how a 15-session time stop ended up on a trade
that must be flat by 15:20.

## The operator

Trades their own capital, is not a programmer, and reads every explanation. They
ask precise questions and have repeatedly been right when they pushed back —
the (symbol, product) key, the alert-noise problem and the ranking gap were all
their calls. Take their domain instinct seriously; they know Indian markets and
Zerodha better than the codebase does.

They want to know **why**, not just what. An answer that skips the reasoning
gets asked for the reasoning.

---

## Rules this project runs on

These are earned, not stylistic. Each one has a failure behind it.

**Verify, never assert.** "It should work" has been wrong repeatedly here. Run
it, read the output, then say what happened. Six exit-path bugs surfaced in one
day in July and every one was found by running something, not by reading it.

**A check that cannot fail is not a check.** Five separate health checks were
found reporting green while the thing they watched was broken — a GTT check that
could not see, a config check that read keys it never fetched, a quality audit
checking its own output, a token check reading a key that did not exist, and
`check_selects`, which called `validate_selects.main()` without `strict=True`,
read the return code it hardcodes to 0, and printed "every SELECT names columns
that exist" over the top of its own error output. That last one is what let a
select on a non-existent column survive into a live veto and empty the intraday
book. Test that a check FAILS when it should.

**A check that cannot PASS is the same defect wearing a different hat.** The
allocator's bar could not be cleared by any setup at any hour, and every refusal
looked exactly like an ordinary market. When you build a threshold, assert that
a realistic input clears it — not only that a bad one does not.

**Silent defaults are the enemy.** A config key nobody reads, a column nobody
writes, a step that completes producing nothing — this is the dominant failure
mode. `tools/validate_config.py` and `tools/health.py` exist because of it.

**Propose, never auto-apply.** Learning tools write to `brain_proposals` and
change nothing. A system that re-tunes itself becomes one nobody can audit.

**Money moves only behind explicit switches.** Entries need two (`*_auto_entry`
AND `*_live_auto_entry`). Never widen a gate to make something work.

**Own mistakes plainly.** Several were made and corrected in-session. Say what
broke, what it cost, and what was restored. Do not bury it.

---

## Working cheaply in this repo

This file costs ~1.8k tokens every session. One careless whole-file `Read` of
`compute_msl.py` costs **29k** — sixteen times this entire file. So the way to
save context is not to shorten this document, it is to stop reading large
things. Context spent re-reading is context unavailable for the problem, and a
session that runs out mid-task rediscovers everything from scratch.

Measured over one long session here: 1,190 Bash calls, ~492k tokens of tool
output. The twenty largest results were 25% of it, and most were whole files.

**Never `Read` a large file whole.** 28 modules exceed 20k chars. Locate, then
read the range:

```bash
# find it, then read only what matters
Grep -n "def evaluate_exit" backend/control/position_lifecycle.py
Read file_path=backend/control/position_lifecycle.py offset=196 limit=60
```

Worst offenders, in tokens for a full read: `compute_msl` 29k · `send_alerts`
29k · `ai_decision_engine` 24k · `intraday/engine` 22k · `screen_stocks` 21k ·
`position_lifecycle` 21k · `generate_signals` 20k · `post_trade_analysis` 17k.

**`USER_GUIDE.md` is 12k tokens. Grep it, never read it whole.**

**Grep in the cheapest mode that answers the question.**
`files_with_matches` to locate · `count` to size · `content` with `-A/-B` only
when the surrounding lines *are* the answer. Cap with `head_limit`.

**Cap every Bash output** — `| tail -20`, `| grep -E "..."`. A pipeline run or
health sweep emits hundreds of lines and three of them matter.

**Prefer structure over pixels.** A browser screenshot of `/control` is ~13k
tokens; `read_page` or a `javascript_tool` geometry check is a few hundred and
is what actually verifies a layout claim.

**Never re-read a file to confirm an edit.** `Edit` fails loudly if it did not
apply. Re-reading buys nothing and costs the whole file.



## Research & Evolution Charter

Treat this repository as an institutional-grade trading framework that should evolve through evidence rather than feature accumulation.

### Trading Knowledge

When recommending strategy improvements:

- Draw upon established concepts from widely respected literature, research and long-standing market practices related to Indian equities.
- Prioritize knowledge that has demonstrated success over multiple market cycles (roughly the past 20–30 years).
- Focus on below but not limited to only these topics:
  - Intraday trading
  - Swing trading
  - Price Action
  - Market Structure
  - Volume Analysis
  - Momentum
  - Trend Following
  - Breakouts / Breakdowns
  - Mean Reversion
  - Multi-timeframe analysis
  - Risk Management
  - Position Sizing
  - Trade Psychology

Always distinguish between:
- proven principles,
- market-specific adaptations,
- and experimental ideas.

### Algorithmic Translation

Do not stop at explaining trading concepts.

Whenever appropriate:

- translate discretionary concepts into objective rules;
- identify measurable signals;
- recommend suitable filters;
- consider execution quality, liquidity, slippage and transaction costs;
- suggest validation through backtesting or forward testing;
- prefer robust and explainable systems over complex ones.

### TradeOS Evolution Principles

Before proposing any enhancement:

1. Understand the existing architecture.
2. Reuse existing components whenever possible.
3. Prefer incremental improvements over redesigns.
4. Minimize code churn.
5. Preserve backward compatibility unless there is compelling evidence otherwise.
6. Explain expected benefits, risks, implementation complexity and measurable success criteria.

Continuously evaluate improvements to:

- signal quality
- stock selection
- entry timing
- exit timing
- market regime detection
- position sizing
- capital allocation
- execution quality
- adaptive learning
- operational controls
- monitoring
- performance attribution

The objective is continuous evolution of TradeOS into a fully autonomous, robust, adaptive, self-evolving, institutional-quality trading framework for Indian equities focused on Intraday and Swing trading while preserving simplicity, maintainability, and measurable performance improvements.


## Before changing anything

```bash
cd backend && python -m tools.health        # to identify if anything broken
cd backend && python -m tools.simulate      # what BOTH books would do, writes nothing
```

After changing anything that touches positions, orders or reconciliation, run
both again. `simulate` is read-only and safe at any time.

## After changing anything — `python -m tools.verify`

```bash
cd backend && python -m tools.verify        # 35 offline logic checks, ~2s
cd backend && python -m tools.verify --module direction_spine
```

**Run this instead of writing a throwaway verification script.** Every check in
`backend/tests/` was once exactly that — written into a scratch directory, run
once, lost at session end, and rewritten by the next session that touched the
same code. Two defects shipped through that gap.

No database, no broker, no network — pure arithmetic over in-memory objects,
which is possible only because `evaluate_exit`, `score`, `is_worth_taking`,
`classify` and the engines are all pure functions. Protect that property; a
test that needs the live book belongs in `tools/health.py` instead.

    health   asks the RUNNING SYSTEM questions   → is TODAY safe?
    verify   asks the LOGIC questions            → is this CHANGE safe?

Adding a check: expose `TESTS = [(name, fn)]` in a `tests/test_*.py` module and
register it in `tools/verify.py::MODULES`. Use `cfg_ctx()` for anything reading
`system_config` — it is a process-wide global and one test's switches will
otherwise leak into the next. **Demonstrate the check FAILING before trusting
it to pass.**

## Landmines, learned the hard way

- **`open_positions` is keyed on `(symbol, product)`** since migration 028. Never
  upsert on `symbol` alone. Use `control.position_lifecycle._upsert_position`.
- **Reconcile must read day positions**, not just holdings. A CNC buy is not in
  `holdings()` until T+1; treating it as sold closes a position bought that
  morning. It must also skip PAPER rows.
- **Record a fill immediately.** An order placed and not written back is
  re-derived next cycle and placed again. PPLPHARMA sold twice this way.
- **Kite's order allowlist is IPv4 only.** `config._force_ipv4()` exists because
  a v6 source address rejects every order while every readiness check passes.
- **PostgREST caps responses at 1000 rows silently.** Page anything larger.
- **PostgREST fails the WHOLE update on one unknown column.** A single missing
  column loses every other field in the payload. Strip and retry, do not
  abandon the row.
- **Migrations run against a live book.** Verify preconditions first.
- **CNC and MIS are different trades financially.** Delivery pays zero
  brokerage but 0.1% STT on *both* legs, 0.015% stamp, and a flat ₹15.04 DP fee
  per sell. `cost_model.round_trip(..., product=)` — default MIS. A ₹2,000 CNC
  round trip is ~1.0%, not the 0.21% the intraday model reports.
- **`signal_log` is the SWING pipeline's table.** Intraday has no row in it;
  its outcomes live in `intraday_setups` and are scored by
  `outcomes.resolve_day`. Writing an intraday result there does not error — it
  lands on whatever swing signal shares the symbol and date and poisons the
  learning loop.
- **`deepseek-v4-flash` is a reasoning model.** `max_tokens` budgets reasoning
  AND output together, reasoning is spent first, and it expands to fill
  whatever it is given (8000→7999, 32000→32000, always `finish_reason=length`).
  Raising the budget never converges; `ai_thinking_enabled` is off for that
  reason. Read `finish_reason`, never infer truncation from a closing brace.
- **A gate and the thing it gates must be the SAME QUANTITY.** `scoring.score()`
  returns edge = net-of-cost expected R **per day**; `hurdle` compared it against
  gross realised R **per trade**. Both are "R", neither is the same number. The
  intraday book took zero trades for a full session and the logs read like a
  quiet market. The bar is now a percentile of `allocation_decisions.edge` — the
  same column the scorer writes — because that is the only construction under
  which the two cannot drift apart again. Migration 044.
- **A cold start must be PERMISSIVE, never 0.0.** A bar of zero looks neutral
  and is not: the edge it is compared against already has costs subtracted, so
  zero refuses every proposal whose expected R does not beat its own round trip.
  On the intraday book that is all of them (prior +0.08R vs MIS cost +0.21R).
  A component with no data must be indistinguishable from that component being
  absent.
- **One symbol, one book — and check it from BOTH sides.** Migration 028 made
  two rows for one name *storable*. That is a storage guarantee and it was read
  as a trading policy. Intraday refused any name swing held; swing never looked
  at the intraday book, so real money could be committed on top of a paper
  position — two exit ladders (15:15 square-off vs a 15-session time stop) on one
  set of shares. `_other_framework_holding()`, called from both sides, health
  check `books`. Switch: `one_framework_per_symbol`.
- **Per-book caps are not a pooled cap.** `alloc_max_slots` (2, "across both
  books") was subtracted from every position entered today in *either*
  framework, so one swing entry capped the intraday book — governed by
  `intraday_max_new_per_day` (4) — at one slot for the rest of the session, and
  two capped it at zero, where `hurdle` returns an infinite bar. Each book
  brings its own budget.
- **"RISK OFF" is not one thing.** Four systems classify market/sector state
  with overlapping vocabulary: swing regime (`market_regime` table, space-
  separated, once/day — `RISK ON`), intraday market context (in-memory,
  underscore, every 15s — `RISK_ON`), sector event bias (per-candidate,
  underscore, per-event), and market structure (`UPTREND`/`DOWNTREND`, pivot-
  based, shared by both frameworks but a different axis entirely). `hurdle.
  regime_bucket()` was written against the swing vocabulary while its only
  caller fed it the intraday one — `"RISK ON" in "RISK_ON"` is `False` — so the
  STRONG bucket was unreachable in every session this ran. Full map, and why
  each system stays separate: `docs/TERMINOLOGY.md`. Migration 048.
- **A direction-aware function's correctness proves nothing about its
  callers.** `direction` shipped as a parameter that defaults to `LONG`, so
  every pre-shorting call site kept compiling — and kept silently scoring,
  gating, or writing every SHORT as if it were a LONG. Found four separate
  times in one feature: `open_positions` had no `direction` column and nothing
  wrote one (migration 047); `evaluate_intraday_setups`' own cost-gate call
  omitted the argument, so a coherent short was refused as "wrong side of
  entry for a LONG"; `allocation/proposal.py`'s `Proposal` had no `direction`
  field at all, so `coherent` used the long-only `0<stop<entry<target` shape
  and rejected every short before scoring ever ran; `tools/simulate.py` — the
  read-only preview tool this file tells you to run first — had the identical
  gate and cost-gate gaps, so it would have reported real shorts as blocked or
  uneconomic. A marker-based health check that greps a function's own
  definition cannot see this class of gap; `check_shorts()` now also greps the
  literal call sites, and one check (`open_positions.direction`) is a live
  schema probe rather than a grep, because "the code mentions this column" and
  "this column exists" are different claims. Migration 049.

## Architecture — the actual data flow

```
EVENING (GitHub Actions, 27 steps, run_pipeline.py)
  ingest bhavcopy/chartink/FII-DII/events
    -> compute_indicators (86 cols on stock_data_daily)
    -> sector_strength -> regime -> screen_stocks (9 engines -> master_shortlist)
    -> compute_msl (entry zones, final_score) -> quality_gate -> signals
    -> ai_decision_engine (ai_tier, conviction) -> signal_snapshot
    -> signal_output_daily   << 114 columns, the day's plans, IMMUTABLE

MARKET HOURS (intraday/run.py, one process, BOTH books)
  KiteTicker websocket, MODE_LTP, ~95 symbols
    every 15s: evaluate_positions -> route by framework -> exit ladder
               evaluate_candidates -> decide() -> swing entry if ranked
               evaluate_intraday_setups -> 7 engines -> gates -> paper entry
    every 300s: gtt_manager.sync (CNC only)
    at close: outcomes.resolve_day  << scores EVERY detection

WEEKLY (brain_sunday_chain.yml)
  weekly_review    engines, gates, ranking -> brain_proposals
  discover_engines refused-but-right + moved-but-unseen -> brain_proposals
```

**Key tables.** `signal_output_daily` (plans, no `id` column — the signal id
lives in `signal_log`), `open_positions` (keyed on symbol+product),
`closed_positions` (gross `realized_pnl` plus separate `charges`),
`intraday_setups` (every detection with its verdict and outcome),
`system_config` (every switch; `cfg()` reads it), `brain_proposals` (learning
output, never auto-applied).

**The seven intraday engines.** ORB, GAP, PDL, VCE, PBK, VWR, RNG — registered in
`strategy_config` with a lifecycle state, scored weekly on resolved outcomes.

**Decision reuse is the core design.** `analysis.trade_decision.decide()` and
`control.position_lifecycle.evaluate_exit()` are called by the pipeline, the
dashboard, Telegram AND the daemon. A second copy would drift; this project
already lived through three divergent R:R models giving three answers for one
stock on one day. Never reimplement a decision — import it.

## Where things live

```
backend/intraday/engine.py       the 15s loop — manages BOTH books
backend/control/position_lifecycle.py   swing exits, reconcile, close
backend/execution/               gates, order_manager, gtt_manager, paper_broker
backend/analysis/entry_ranking.py       which swing plan deserves the entry
backend/tools/                   health, simulate, validate_*, weekly_review,
                                 discover_engines
tradeos.cmd                      the one-click launcher (menu + subcommands)
```

## Communication

Lead with what the operator asked. Show the evidence — actual command output,
not a description of it. Flag anything found along the way that costs money,
even when unasked; that is how most of the serious bugs here were caught.

If a claim cannot be verified in this session, say so explicitly rather than
implying it was checked.
