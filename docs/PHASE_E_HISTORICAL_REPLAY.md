# TradeOS — Historical Replay (Phase E)

**Append to `docs/EDGE_DIAGNOSTIC.md` as Phase E. Runs after Phase B.**

The diagnostic measures 40 live trades. That cannot separate a 0.1R edge from
zero. This phase manufactures the sample instead of waiting eight months for it:
run the nine existing engines over 6–12 months of real NSE bars.

**This is not scenario simulation.** No synthetic data, no invented market
conditions. Real bars, real gaps, real bad days. Synthetic scenarios test
whether the code does what the code says — a tautology. Only real history tests
whether the signals pay.

---

## The one rule that decides whether this is worth doing

**Walk-forward, or don't bother.**

Nine engines with configurable thresholds have enough freedom to fit any twelve
months perfectly and lose money for the next twelve. A backtest that has seen
its own test period is not evidence, it is a story.

```
Months 1-6     IN-SAMPLE      look freely, tune, explore
Months 7-9     VALIDATION     one look per parameter set
Months 10-12   HOLDOUT        ONE look, ever, after parameters are frozen
```

If the holdout disagrees with the validation set, **the holdout wins and the
strategy is not confirmed.** No re-tuning. No "let me just check one more
variant." The moment you tune against months 10–12 they stop being holdout and
this phase has produced nothing.

Write the frozen parameters to a file and commit it before running the holdout.
Git is the honesty mechanism here.

---

## E.1 — Data

`intraday/bar_builder.py` already fetches from `kite.historical_data()` and
documents the rate-limit budget: 40 calls per refresh is close to the ceiling.
A year of 5-minute bars across 120 names is a large fetch.

Requirements:

- **Cache to disk. Fetch once.** Re-fetching per experiment will exhaust the
  budget and make iteration impossible
- Use the **universe as it was on each date**, not today's universe. Selecting
  today's liquid names and running them back through last year is survivorship
  bias, and it inflates results in a direction that feels like skill
- Record which symbols were unavailable and why. A silent gap is a lie in the
  denominator
- Verify against a known session: replay one day already in `intraday_setups`
  and confirm the harness reproduces those detections. **If it cannot reproduce
  a day you already have, nothing after this is trustworthy**

## E.2 — Harness

The engines are pure — they take a context and return a setup or `None`. Feed
them historical contexts instead of live ones. **Import the engines. Do not
reimplement them.** A replay of a reimplementation measures the
reimplementation.

Reuse from the live path:

| Component | Source |
|---|---|
| Detection | `intraday/strategies/*.py`, unmodified |
| Session phase | `intraday/session.py` |
| Index gate | `market_context.classify()` on historical Nifty |
| Exit ladder | `intraday/exit_policy.py::evaluate_intraday_exit` |
| Outcome resolution | the `outcomes.py` rule below |

**Assume the bad fill.** When one bar contains both stop and target, record the
stop. `outcomes.py` already does this, with the reason stated: a coarse bar
cannot tell you the sequence, and assuming the good one is how a strategy looks
profitable on paper and loses money live.

**Net of the real round trip.** 0.21% MIS, 0.1% STT each way plus DP charges for
CNC. A gross win under the round trip is a loss.

## E.3 — What to measure

Per engine, per year, with n, and never a mean without its count:

| Column | Why separately |
|---|---|
| n (deduplicated) | one observation per symbol/engine/date |
| gross R | is the signal real |
| cost R | `cost_pct / risk_pct` — a tighter stop makes this LARGER |
| net R | the only column that pays |
| hit rate | |
| max drawdown | in R, consecutively |
| longest losing streak | the number that decides whether you can actually run it |

Then segment by `regime_at_detection`, session phase, and direction. **This is
where the 12-Aug hypothesis gets tested properly** — do continuation engines only
pay in continuation regimes? One session cannot answer it. Two hundred can.

## E.4 — The comparison that matters

Against a null model, not against zero.

- Random entry on the same universe, same session phases, same stop and target
  distances, same costs
- Buy-and-hold Nifty over the period

**An engine that does not beat random entry with the same risk parameters has no
signal.** It has a stop-and-target geometry, which is not the same thing and is
free to anyone.

Run the random baseline 1,000 times and report where each engine falls in that
distribution. An engine at the 60th percentile of random is noise wearing a
name.

---

## What this establishes, and what it does not

**Establishes:** whether these nine signals had edge over the tested period,
with a sample large enough to mean something, decomposed into signal versus
friction.

**Does not establish:** that the edge persists. Markets change; a signal that
paid through 2025 can stop paying in 2026 and the backtest will never say so.
Walk-forward reduces this. Nothing eliminates it.

**Cannot establish:** anything about a strategy not already coded. The replay
only looks where the engines already look. For the other blind spot, run the
tool that already exists:

```bash
cd backend && python -m tools.discover_engines --days 30
```

Pass B — "moved but unseen" — sweeps for real intraday moves that produced **no
detection from any engine.** Those cost nothing visible and appear in no P&L:
there is no losing trade to notice, only a winner that never happened. That is
the closest thing in this repo to finding missed opportunity, and it has never
been run.

---

## The result that is most likely, and hardest to accept

Two or three engines will show real edge. Four or five will be noise. One or two
will be reliably negative.

**Retiring the negatives is the largest single improvement available**, it
requires no new logic, and it is the one nobody wants to do because it feels
like deleting work rather than making progress.

The second most likely result: several engines show positive gross R and
negative net R. That is not a selection problem and **more filters make it
worse.** The fix is wider stops or larger clips — fewer, bigger, better-paid
trades.

If the replay shows no edge anywhere, that is the most valuable outcome in this
document. Finding it out on a year of free historical data beats finding it out
on eight months of live capital.

---

## Scope discipline

Build the harness. Run it once. Report.

Do **not**, in this phase: tune thresholds against the results, add engines,
change any live config, or write anything to `system_config`. Findings go to
`brain_proposals` and nothing auto-applies — the same rule every other learning
tool in this repo follows.

A backtest that modifies the system it is testing has stopped being a
measurement.
