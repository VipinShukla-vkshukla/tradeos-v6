# Phase 5 — readiness assessment, and what it would actually cost

**High-level intelligence only. Nothing here is designed, and nothing here may
be built.** `5_PHASE5_VISION.md` is explicit that anticipatory architecture for
Phase 5 is the single most likely way Phase 4 fails, because every hook built
for a future capability is complexity carried without measurable alpha.

This document exists to answer three questions the operator asked: what would be
worth adding, how does the system tell you it is ready, and how far would
execution deviate from what is written today.

---

## 1. Where Phase 4 actually leaves the system

Stages 1–10 are built. What that means concretely:

| | |
|---|---|
| Friction | Modelled to **−0.01%** of the broker's own statement |
| Unbiased denominator | Intraday 631/631 resolved; swing 860 plans resolved, from 0 |
| Priors | Empirical, per engine, with `n` and standard error, floors enforced |
| Governance | One door. Auto-apply removed in code, freeze calendar live |
| Allocator | Built, shadow-only, structurally unable to reach an order path |
| Guards | 14 health checks, every new one demonstrated failing |

**And what it does not mean.** Per-trade expectancy is still **unmeasured**:
+₹269 across 25 attributable closes, of which **3 are real money**. The system
can now tell the truth about its own edge. It has not yet had time to.

---

## 2. The four preconditions, and where each stands

`5_PHASE5_VISION.md` §3 states four, none negotiable.

| Precondition | Required | Today | Gap |
|---|---|---|---|
| Scored allocation decisions | ~200 | **0** | Shadow starts on migration 041 |
| Closed positions with rank, R, excursion | ~100 | **~21** with a usable R | 79 |
| Allocator live and beating greedy a full quarter | 1 quarter | **not promoted** | ≥30 disagreements first |
| Capital where sizing differences are material | — | **₹20,000** | The binding one |

**Three of the four are a matter of time. The fourth is not.**

At 4–5 trades a month, 100 closed positions is roughly **two years**. At the
current proposal rate, 200 scored allocation decisions is perhaps 6–9 months of
shadow — sooner, because every DECLINE counts and there are ~50 a day.

**The capital precondition is the one that should be read carefully.** Phase 5's
headline capability is edge-proportional sizing, and at ₹20,000 the difference
between fixed-fraction and Kelly-weighted sizing is a few hundred rupees a
month — while the mechanism itself can concentrate risk. The vision document
says Phase 5 is "gated on capital as much as on time" and it is right.

**The ₹500/month API charge sharpens this.** It is 2.5% of capital per month —
roughly 30% a year — and it is charged whether or not a trade is placed. No
sizing sophistication overcomes a 30% fixed drag. Below roughly **₹5,00,000**,
where the same charge is 0.12%/month, Phase 5's sizing work is arithmetically
irrelevant. That is a firmer capital floor than "material", and it is derived
rather than asserted.

---

## 3. How the system should tell you it is ready

It should not be a judgement call, and it should not require reading this
document. The honest mechanism is a single check that fails until all four
preconditions hold, in the same style as every other guard here — **and it
should be built at the START of Phase 5, not now.** A readiness check written
today is a hook for a future capability, which is the thing the vision document
forbids.

What it would assert, in the order the constraints actually bind:

1. `allocation_decisions` holds ≥200 rows with a resolved outcome
2. ≥30 of those are **disagreements** with the greedy baseline, scored
3. `closed_positions` holds ≥100 rows carrying entry rank, R and excursion
4. The allocator has been live for a full quarter and its counterfactual-R
   difference is positive net of measured execution shortfall
5. Account capital ≥ the point where the fixed API charge falls below ~0.5%/month

**Every one of those is already recorded by something Phase 4 built.** No new
instrumentation is required, which is the strongest evidence that Phase 4 was
scoped correctly.

---

## 4. What is worth adding — beyond what Phase 5 already lists

The vision document's own list (edge-proportional sizing, capital recycling,
self-managing lifecycle, per-regime conditioning, hypothesis generation,
attribution, alternative data) stands. Four things this implementation surfaced
that are **not** in it and would earn their place:

**4.1 Fixed-cost amortisation as a first-class input.** No model in this
repository carries the ₹500/month subscription, and it is 5.6× total trading
friction. Any sizing or capacity work that ignores it optimises the smaller
number. This is small, and it is the highest-value item on this list.

**4.2 A real F&O expiry calendar.** Stage 9's day-type flag is a heuristic with
a known 1–3 session error on the monthly flag. Tolerable while the overlay only
sizes down; **not** tolerable as an input to a per-day-type prior, which is
exactly what Phase 5's per-regime conditioning would want.

**4.3 Execution-shortfall instrumentation, closed.** M1 nominated the order path
and Phase 4 did not build it. Counterfactual fills are meant to be haircut by
measured decision-price-versus-fill, and until that exists every counterfactual
comparison flatters whichever policy defers more — which is the allocator.
**This is a precondition for trusting the promotion gate, not a Phase 5
nicety.**

**4.4 A static egress path for order placement.** The Kite allowlist went stale
three times in one day on a dynamic residential IP, and each time order
placement would have been silently rejected. See §6.

---

## 5. How far execution would deviate

Honestly: **more than the roadmap implies, and in a specific direction.**

Phase 4's ten stages produced **eleven documented deviations** and **five places
where measurement contradicted a frozen claim** — including the "binding number"
of the whole engine-consolidation argument, which was wrong by 2–10×. That is
not a criticism of the planning; it is what happens when a plan written without
the live database meets it.

Expect the same rate. Concretely:

- **The plan's stated dependency was not a dependency.** Block/bulk deals were
  called "the one new external dependency" and had been ingesting since March.
  Assume at least one Phase 5 prerequisite is already met and at least one
  "already flows" input does not.
- **Premises fail.** Stage 3's entire basis — 3% capture — was a pooled
  statistic; the real figure on winners was 62.6%. Phase 5's riskiest premise is
  that measured win probability is stable enough to size on. If it is not,
  §2.1 collapses and §2.2 with it.
- **Sequencing is the thing that slips, not scope.** Every Phase 4 stage was
  buildable; several were buildable only after something unlisted was fixed
  first. Budget for prerequisites that are not in the plan.
- **Capital recycling (§2.2) is the one to be most sceptical of.** It charges a
  round-trip cost against a small improvement margin, and it can destroy value
  that already exists — an asymmetry Phase 4 does not have. It requires the
  friction ledger to be exact rather than approximately right. Phase 4's ledger
  is validated to −0.01% on **4 round trips**, not the 20 the spec asks for.
  That is the gap to close before touching recycling.

**A reasonable planning assumption: 1.5–2× the stated effort, with the excess
falling almost entirely on prerequisites rather than on the capabilities
themselves.**

---

## 6. The operational item that should not wait

The Kite IP allowlist went stale **three times in one session** (.208 → .71 →
.253). Each time, `orders_enabled` was true, every readiness check except one
passed, and order placement would have been rejected at the moment it mattered.

**Zerodha's allowlist cannot be automated — there is no API for it; it is a
manual action in the developer console.** So the fix is not automation, it is
removing the need:

- **Place orders only from the VM.** It has a static address, it is already the
  ACTIVE lease holder, and architecture §26 already assigns it "anything that
  must not miss a tick". The laptop keeps its read-only session for research and
  never needs an allowlist slot.
- **Use the second slot for the laptop, and alert on drift** rather than
  discovering it at order time. The health check already detects the mismatch;
  what is missing is that it reaches a phone rather than a terminal nobody is
  watching.

This is a deployment decision, not a code one, and it is worth taking before
any Phase 5 conversation.

---

## 7. The verdict this document exists to support

**Phase 5 should not begin, and the reason is not readiness — it is that Phase 4
has not yet been allowed to answer its own question.**

The system was built to tell the truth about its own edge. As of today it can,
and the truth is: unmeasured, on 25 attributable closes. Every Phase 4
instrument is now recording. The correct next action is to run it, unchanged,
for a quarter, and read what it says.

If the answer is favourable, Phase 5's preconditions will have started
accumulating on their own. If it is unfavourable, `5_PHASE5_VISION.md` §5 is
explicit that Phase 5 is not the response — stopping is, and the measurement
spine is what makes stopping a decision rather than a slow discovery.
