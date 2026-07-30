# Design notes — decided, not yet built

Two pieces are designed and agreed but not implemented. This file exists so a
new session executes the decision rather than re-deriving it, and so the
reasoning survives the conversation it came from.

Both were scoped on **30 July 2026**. Read `USER_GUIDE.md` for how the system
works today; this is only what is missing.

---

## 1. Same symbol in both books — key on (symbol, product)

### The problem

`open_positions` is unique on `symbol` alone. Every writer upserts with
`on_conflict="symbol"`. So a name held by swing cannot also be traded intraday:
the second entry overwrites the first — entry price, stop, target, framework and
the R baseline — and the 15:15 square-off then sells a multi-week thesis because
the row says INTRADAY.

All four entry paths currently guard against this by refusing the second entry.
That prevents corruption and costs opportunity.

### Why the guard is the wrong answer

A professional desk holds a swing core and trades around it. Core-and-satellite
is standard, and the case where both frameworks independently like a name is
exactly the case with the most conviction. Refusing it leaves money on the table
precisely when the evidence is strongest.

The constraint is a schema limitation dressed as a risk policy. The data model
should not decide the strategy.

### The design — the operator's idea, and it is the right one

Key on **(symbol, product)**, where product is CNC or MIS.

  swing    = CNC always. Hardcoded in order_manager._product(); a swing position
             held for weeks must never be MIS, which the broker liquidates at
             15:20 on the day it was opened.
  intraday = MIS, set via intraday_product.

**This dissolves the hard problem.** The original concern was reconcile
ambiguity: the broker shows 5 CIPLA, the book says swing 2 + intraday 3, the
broker drops to 3 — whose shares went? That needed trade-id level attribution to
answer.

It does not, because **Kite already separates them**. `positions()` returns
CIPLA-CNC and CIPLA-MIS as distinct rows with distinct quantities. The broker
answers the question directly. No attribution layer is required.

### The trade-off, accepted

MIS has no GTT — Kite GTTs are CNC/NRML only. An intraday tranche therefore
loses its broker-side stop.

That is acceptable, and arguably better: MIS is force-squared by the broker at
~15:20, which is the same protection a GTT was providing (a stop that survives
the daemon dying) delivered by a different mechanism. Intraday is flat by 15:15
under its own policy anyway.

Swing keeps CNC and keeps its GTTs. Nothing about the swing book changes.

### What has to change

| Where | Change |
|---|---|
| migration | drop unique on `symbol`; add unique on `(symbol, product, status)` filtered to ACTIVE |
| 7 call sites | `on_conflict="symbol"` → `on_conflict="symbol,product"` — see `grep -rn 'on_conflict="symbol"'` |
| `intraday_product` | set to MIS so the two books are genuinely distinguishable |
| `reconcile_with_broker` | apportion by product: match CNC rows to CNC holdings/positions, MIS to MIS. Already reads day positions (added 30 Jul) — extend to key on product |
| `gtt_manager.sync` | CNC only. Already excludes PAPER; add `product != 'MIS'` |
| `square_off_paper` / intraday exits | act on the MIS tranche only, never the CNC one |
| `_maybe_enter_swing`, `_maybe_open_paper` | relax the same-symbol guard to same-symbol-same-product |
| exits | sell with the product the tranche was opened under |

### Risks to verify, not assume

- A CNC and MIS position on one symbol must produce **two** rows and two exits.
  Test with a paper intraday tranche against a live swing holding.
- Reconcile must not close either tranche when the other changes. This is the
  failure that erased three paper positions and nearly erased three live ones on
  30 July, arriving from two different directions.
- `_today_totals` and the daily caps count orders, not tranches — confirm a
  two-book day does not double-spend the account guard.

### Sequencing

Do this **after** a few weeks of clean single-book operation. The PPLPHARMA
double-sell (30 Jul) happened because the book and the broker disagreed about
one tranche; two tranches on one symbol makes that failure mode richer. Fix
confidence first, then add the capability.

---

## 2. Discovering engines that do not exist yet

### The gap

`tools/weekly_review.py` scores the seven registered engines. Nothing proposes a
strategy that is not already coded. The loop can only judge what it already
looks at, which makes it structurally blind to the thing worth most: an edge
nobody has named.

### Two approaches, in order of tractability

**A. Mine the near-misses.** Every setup blocked by a gate is already recorded
in `intraday_setups` with `cost_verdict` and a resolved `outcome`. If a refusal
category starts reaching target MORE often than taken setups, that is not a
mis-calibrated gate — it is an unnamed edge living in the rejected population.

`review_gates()` already computes exactly this comparison and proposes
GATE_TOO_STRICT. Extending it to segment by strategy and market state would
surface "PDL setups blocked by structure during RISK_ON reach target 40% of the
time" — which is a new engine described in one sentence.

Cheap: the data exists, the comparison exists, only the segmentation is missing.

**B. Mine the moves nobody saw.** Take every stock that moved more than ~1.5%
intraday and ask which produced NO detection from any engine. Cluster what
preceded them — gap, volume, sector, time of day, prior-day structure. A cluster
that repeats is a candidate engine.

This is the valuable one and the larger one. It needs a bar-history sweep over
the universe and a clustering pass; it is a genuine analysis project rather than
an extension.

### The bar for acting on either

The same as everything else here: **propose, never apply.** A discovered pattern
is a hypothesis. It earns SHADOW lifecycle — detected and scored, never traded —
until it clears the same 20-outcome sample every other engine must clear.

A system that invents engines and trades them is one nobody can audit, and this
project has spent a week proving that unaudited assumptions are where the money
goes.
