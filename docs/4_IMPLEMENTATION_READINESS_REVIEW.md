# TradeOS v7 — Implementation Readiness Review

**Purpose.** One final assessment before development begins. This document
records what remains uncertain, so that uncertainty is a known quantity rather
than a surprise encountered at Stage 6.

**Verdict stated up front: the design is implementation-ready, with seven
documented open items, none of which blocks Stage 1.** All seven resolve through
measurement during the stages that precede their consumers.

---

## 1. Remaining ambiguities

**A1 — The narrowed state-read column list is not enumerated.**
Stage 1 requires narrowing the repeated wide read to consumed columns, but the
exact list is deliberately not written here. Enumerating it from a snapshot risks
omitting a consumer and producing a runtime failure on a live book at market
open. **Resolution:** derive by tracing consumers, verify with the simulator
before and after, confirm identical decisions. *Deliberate ambiguity — an
enumerated wrong list is more dangerous than an unenumerated right one.*

**A2 — Retention windows for append-only tables beyond price history.**
Left unspecified because a retention window determines what the system can still
learn from, and that is an operator decision requiring measured growth rates.
**Resolution:** Stage 1 proposes, operator decides.

**A3 — Sample floors are not numerically fixed.**
Floors are mandated; their values are not stated. Setting them from a snapshot,
without knowing detection rates per consolidated engine, would be arbitrary.
**Resolution:** set after Stage 6, when detection rates per engine are known.

**A4 — The two regime buckets for hurdle fitting are not defined.**
The split is mandated; its boundary is not. **Resolution:** derive from the
existing regime classification's observed distribution, choosing a boundary that
yields adequate sample on both sides.

---

## 2. Missing specifications

**M1 — Execution shortfall instrumentation has no defined home.**
The requirement is stated (decision price versus realised fill, feeding the
counterfactual haircut) but no module is nominated. Both prices exist in the
order path. **Resolution:** implement within the existing order path during
Stage 4; do not create a new module for it.

**M2 — The failure-domain matrix is required but not authored.**
Section 22 of the architecture states the required behaviours; the verified
matrix itself must be produced against the running system. **Resolution:**
author during Stage 1 and verify each cell by inducing the failure in a
non-market window. *An unverified matrix is a comfort document.*

**M3 — Block and bulk deal ingestion has no source specified.**
The accumulation engine's one new external dependency. Deliberately unspecified
because source selection requires checking current availability and format.
**Resolution:** Stage 8; if no reliable free source exists, the engine degrades
to delivery-persistence only and that degradation must be reported, not
silently absorbed.

---

## 3. Hidden assumptions — now surfaced

**H1 — That the runner path can fire at all.**
Stage 3's instrumentation may reveal its evidence inputs are structurally empty,
in which case Stage 3 becomes a data-plumbing task rather than an exit-logic
task. Both outcomes are acceptable; the ordering does not change.

**H2 — That closed-record history is sufficient to measure expected holding
days per book.**
With ~70 swing and ~14 intraday closes, the intraday estimate will be weak.
**Consequence:** the rupee-day normalisation carries known uncertainty on the
intraday side. Report it alongside the number rather than presenting a point
estimate.

**H3 — That the trained model artifact can reach its consumer.**
Established as unresolved. Stage 4 must determine this before depending on it.
The fallback — rank-decile priors from the full field — is defined, so this
cannot block progress, only change which path is taken.

**H4 — That quarterly freezes are operationally sustainable.**
The discipline is correct statistically and will be uncomfortable during
drawdowns. This is a behavioural assumption about the operator, not a technical
one, and it is the assumption most likely to fail in practice.

**H5 — That consolidating engines will not remove a genuinely independent
signal.**
Correlation figures used to justify consolidation are structural estimates, not
measured correlations — the sample does not support measurement. The one-quarter
shadow exists precisely because this assumption may be wrong for one or two of
the retirees.

---

## 4. Implementation risks

| Risk | Likelihood | Impact | Containment |
|---|---|---|---|
| Narrowed read omits a consumed column | Medium | High | A1's verification protocol |
| Migration against a near-full database | Low | **Critical** | Measure before migrating; Stage 1 precedes all |
| Shadow allocator reaches an order path | Low | Critical | Structural dependency prohibition, verifiable by inspection |
| Quote-mode parity differs materially | Medium | Medium | Dual-log one session; enable only on parity |
| Friction ledger optimistic | Medium | High | ≥20 realised round trips within 10% |
| A quarantined module was live | Low | High | Search includes scheduler, launcher, remote crontab; nothing removed without operator decision |
| Buffered writes leave silent record holes | Medium | Medium | Daily reconciliation of proposals seen versus rows written |

---

## 5. Statistical risks

**S1 — Promotion on insufficient disagreements.** The single likeliest way the
allocator destroys value. The gate is denominated correctly; the risk is
operator impatience when "months" becomes concrete.

**S2 — Full-field priors are unbiased but not assumption-free.** A plan's
forward outcome assumes it *could* have been entered at its recorded level.
Where a plan's zone was never touched, that assumption is untested. Segment
priors by whether the entry level was reached rather than pooling.

**S3 — Consolidation changes the denominator mid-stream.** Detections after
Stage 6 are not comparable to detections before it. Priors must be recomputed
post-consolidation, not carried across the boundary.

**S4 — Empirical R distributions are thin in the tail.** The tail is exactly
what Stage 3 exists to create, so early distributions will understate it. Report
tail sample counts explicitly and expect the estimate to improve after Stage 3
matures.

---

## 6. Operational risks

**O1 — Single operator, twelve months, ten sequential stages.** No parallelism
is available and no stage may be skipped. An interruption at Stage 6 leaves the
system mid-consolidation, which is a valid but unusual resting state. Each stage
is individually revertible for this reason.

**O2 — The always-on host is a single point of daemon failure.** Broker-side
stops protect open positions; new entries simply stop. Accepted.

**O3 — Freeze discipline versus drawdown psychology.** See H4. The most likely
real-world failure of this plan is not technical.

---

## 7. Readiness statement

**The design is implementation-ready.**

Every open item above is either a deliberate deferral to measurement (A1–A4,
S3–S4), a specification that must be authored against the running system rather
than a snapshot (M1–M2), or a known dependency with a defined fallback (H3, M3).
None blocks Stage 1, and each resolves within a stage that precedes its
consumer.

The design's principal strength is that its riskiest components arrive last:
the allocator, which carries the most model risk, is built on foundations whose
correctness will have been demonstrated by measurement across the preceding nine
stages. The design's principal weakness is that it demands sustained discipline
from a single operator across a full year, with the most valuable stages being
the least visible.
