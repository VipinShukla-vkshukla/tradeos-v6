# TradeOS v7 — Phase 5 Vision

**Status: REFERENCE ONLY. Explicitly excluded from implementation.**

Nothing in this document may be built, prototyped, scaffolded, or prepared for
during Phase 4. It exists so that Phase 4 decisions can be made with knowledge
of what comes after — not so that Phase 4 can anticipate it. **Anticipatory
architecture for Phase 5 is the single most likely way Phase 4 fails**, because
every hook built for a future capability is complexity carried without
measurable alpha.

Phase 5 begins only after Phase 4 is implemented, validated on live evidence,
and has demonstrated that the allocator beats greedy across a sufficient
disagreement sample. If Phase 4's honest verdict is that the allocator did not
earn its place, Phase 5 does not begin at all.

---

## 1. The organising idea

Phase 4 decides *which* proposal receives a rupee, sized by a fixed fraction,
with the book's existing holdings untouched by the decision.

Phase 5 completes that: **capital becomes adaptive in size and recyclable in
place.** Size follows measured edge; an existing holding competes against
incoming proposals on the same scale and can be closed to fund a better one.

The distinction matters because it changes the failure mode. Phase 4 can fail by
declining to create value. **Phase 5 can fail by destroying value that already
exists** — a wrong comparison closes a winner to fund a loser. That asymmetry is
why the gate is high.

---

## 2. Capabilities, in rough order of readiness

### 2.1 Edge-proportional sizing

Fractional-Kelly sizing off measured win probability, hard-capped well below
full Kelly, floored at one viable clip. The cap is what prevents a good run
becoming a concentrated bet.

**Precondition beyond sample size:** account capital where sizing differences
are material. At small capital the gap between fixed-fraction and edge-weighted
sizing is trivial and does not justify a mechanism that can concentrate risk.
**Phase 5 is gated on capital as much as on time.**

### 2.2 Capital recycling

An open position is evaluated against incoming proposals on the common scoring
currency; a materially better proposal may fund itself by closing a weaker
holding, with the round-trip cost of the swap charged against the improvement.
The existing replacement-case comparison is a crude ancestor of this.

The highest-risk capability in the entire roadmap. Requires the friction ledger
to be exact, not approximately right, because the swap cost is charged against a
small improvement margin.

### 2.3 Self-managing strategy lifecycle

Engines move through shadow, active, degraded and retired states on scored
evidence, presented as one quarterly batch the operator approves in a single
action rather than adjudicating proposal by proposal.

This is governance automation, not decision automation. The human still decides;
the system prepares the decision. Note that it must not reopen the second
governance door Phase 4 closes — batch approval is one door with a wider
threshold, not a second door.

### 2.4 Per-regime conditioning throughout

Phase 4 fits two hurdle buckets. Phase 5 fits per regime — hurdles, engine
weights, exposure scaling — once each regime carries adequate sample. Requires
years of observation, not quarters.

### 2.5 Automated hypothesis generation

The system proposes candidate relationships from its own accumulated outcome
record, with out-of-sample confirmation built into the proposal rather than
applied afterwards. The discovery engine is the seed of this; what Phase 5 adds
is the ability to propose a *hypothesis class* rather than a parameter change.

**The safeguard that must survive:** a machine that generates hypotheses faster
than they can be confirmed is a multiple-testing engine. Generation rate must be
bounded by confirmation capacity, which is bounded by information arrival.

### 2.6 Explainable model attribution

For any decision, an attribution of which inputs moved it and by how much — not
a narrative explanation, a decomposition. Phase 4 makes decisions
*reconstructable*; Phase 5 makes them *attributable*.

### 2.7 Alternative data

Corporate-action calendars, insider disclosures, shareholding-pattern changes,
promoter pledging. Each is a distinct ingestion and a distinct hypothesis. Value
is real but modest relative to the discipline required to evaluate each one
honestly against a slow-arriving outcome record.

### 2.8 Online learning and reinforcement approaches

Listed last deliberately. Both are frequently proposed for systems like this
and both are **poorly matched to the constraint that defines it**: information
arrives at 5–10 closed observations per week. Reinforcement learning requires
sample volumes several orders of magnitude beyond what this system will ever
generate at this capital scale. If they enter at all, they enter at
detection-level granularity where sample is 20–50× larger, and even then only
with a demonstrated advantage over the empirical priors Phase 4 establishes.

**A Phase 5 that adopts these techniques because they are prestigious rather
than because they beat a measured baseline would undo everything Phase 4 was
built to protect.**

### 2.9 Institutional-scale optimisation

Explicitly out of scope for the foreseeable roadmap. The sizing framework,
liquidity assumptions, and capacity analysis are built for lakhs. Pursuing
crores would invalidate the design rather than extend it, and would require a
different instrument set entirely.

---

## 3. Preconditions for Phase 5 to begin

All four, none negotiable:

1. Roughly **200 scored allocation decisions** with resolved outcomes.
2. Roughly **100 closed positions** carrying rank, R, and excursion.
3. The allocator **live and beating greedy across a full quarter**.
4. Account capital at a level where sizing differences are material.

At the current trade rate, conditions 1 and 2 alone represent a year or more.
That is the honest timeline and it should not be compressed.

---

## 4. What Phase 5 must not do

- **Reopen Phase 4's frozen architecture.** Phase 5 extends; it does not
  redesign.
- **Reintroduce auto-application** under a new name.
- **Accelerate the adaptation cadence.** More capability does not mean faster
  tuning; the information arrival rate is unchanged by cleverness.
- **Adopt a technique without beating the Phase 4 baseline on measured
  evidence.**
- **Pursue capacity the instrument set cannot support.**

---

## 5. The line between the two phases

Phase 4 is complete when the system can tell the operator the truth about its
own edge. Phase 5 is what becomes justified once that truth is known and
favourable.

If the truth turns out to be unfavourable, Phase 5 is not the response. The
response is to stop, and the measurement spine built in Phase 4 is what makes
stopping a decision rather than a slow discovery.
