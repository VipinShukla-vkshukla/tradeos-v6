# TradeOS — Red-team review

**Mandate: destroy, not improve.** Scope is the full architecture: the existing
system plus the Phase 4 design in `PHASE4_ARCHITECTURE.md` and
`CLAUDE_CODE_BRIEF.md`. The Phase 4 author's work gets no immunity — several of
the worst findings below are flaws in *that* design, not the original.

One framing rule before the list. This is a ₹20,000, 2-slot, retail-latency
system. Critiques that only bite at institutional scale — co-location, kernel
bypass, order-book queue position, microsecond clock sync — are cosplay here and
are deliberately excluded. The binding constraints at this scale are
**statistical power, free-tier ceilings, and silent-failure modes**. That is
where the knives go.

---

## CRITICAL

### C1 — The allocator's priors are selection-biased, and the design doubles down on the bias

The brief instructs: build swing `p_win` as win rate by rank decile **from
`closed_positions`**. That table contains only trades the *old greedy policy
chose to take* — roughly 2 per day out of 56 candidates. The prior is therefore
conditioned on the old selection policy, and the allocator will apply it to a
region the old policy never sampled: the rank-4-at-14:40 trades, the deferred
morning entries, the whole point of building the thing. This is the classic
off-policy evaluation trap, and the design walks into it with a measurement plan
that makes it worse — 70 trades across 10 deciles is 7 samples per cell, a ±18
percentage-point standard error on each win rate. That is not a prior; it is a
random number generator with a memory of the old policy's taste.

**The fix is already in the building.** Every one of the 56 daily plans has a
measurable forward return whether or not it was taken — `signal_output_daily`
plus subsequent price history is a complete, selection-free outcome record, and
the performance tracker already computes signal-level forward analysis. On the
intraday side, `intraday_setups` resolving *every detection* at the close is
exactly this, already built. Priors must come from **signal-level outcomes of
the full field**, not from executed trades. This dissolves the sample-size
problem too: 56 plans × 250 sessions is ~14,000 outcome observations per year
versus ~500 executed trades. WP1's premise — "p_win is unmeasurable until
`entry_rationale` is fixed" — is overstated. The rationale fix matters for
*scoring the allocator*; it is the wrong source for *feeding* it.

**Impact if unfixed:** the allocator launches with priors that are both noisy
(n=7 per cell) and systematically wrong in the exact region it was built to
explore. Shadow comparison would then "validate" it against greedy using the
same biased currency. The entire evidence chain WP5 gates on is contaminated.

### C2 — "One allocator" is false: two entry paths bypass it entirely

The design claims a single allocation layer between "what could be bought" and
"what is bought." The repository says otherwise. `pipeline_intraday.yml` runs
`control/candidate_monitor` on a GitHub Actions cron — a *second actor* watching
entry-zone touches, outside the daemon, outside the lease's protection scope for
decision-making, and outside the allocator. The Telegram approval flow
(`telegram_bot` → `execution_engine`) is a *third* path that places orders on
human approval and never passes through `select()`.

Consequences, in ascending order of ugliness: the allocator's `slots_left` can
be wrong the moment either path acts, so a TAKE issued with `slots_left=1` can
coexist with a Telegram fill and breach the daily cap; the allocation_log's
counterfactual record is incomplete because proposals acted on elsewhere never
enter it; and the Actions-based zone watcher is unreliable *anyway* — GitHub
cron jitter is documented in this very repo as the reason cronjob.org triggers
the evening pipeline, yet minute-granularity zone-touch detection was left on
the jittery substrate.

**Impact:** cap breaches are a real-money defect (the PPLPHARMA double-sell
class of failure), and a shadow record with holes cannot support the promotion
decision. **Required:** every entry path either routes through the allocator or
is explicitly counted by it as an exogenous slot consumer, and zone-watching
consolidates onto the daemon where the lease actually protects it. Human
override via Telegram can legitimately bypass the allocator's *judgment* — it
must never bypass its *accounting*.

### C3 — The expected-R formula prices out the system's own best feature

`score()` computes `p_win × R_target − (1 − p_win) − cost_R`. That is a binary
model: hit target or hit stop. But the system's most deliberate piece of exit
engineering — `exit_rules` runner conversion — exists precisely to create a
right tail *beyond* the target: 3R trades that become 5R, 8R. A binary formula
assigns those trades the value of their target and nothing more, systematically
understating the expected value of exactly the trade class the runner logic was
built to exploit. Meanwhile intraday, hard-capped by square-off, is priced
almost fairly. The allocator would therefore carry a structural tilt *against*
swing runners — a bias pointed at the system's own edge thesis.

**Fix at the architecture level:** expected R per class comes from the
**empirical R distribution** of resolved outcomes (which C1's data source
provides), not from a two-point model. Where the runner path is verified working
(WP2), the swing R distribution's tail is real and must be in the number.

**Impact if unfixed:** a persistent mis-allocation of the scarcest resource —
the two daily swing slots — away from the highest-expectation trades, with the
error compounding precisely as the runner system improves.

---

## HIGH

### H1 — The promotion gate measures theater, not evidence

"Ten sessions of shadow" sounds rigorous and is nearly information-free. The
allocator and greedy will *agree* on the overwhelming majority of decisions —
agreement observations carry zero discriminating power. What matters is
**disagreements**, and at this system's proposal rate a fortnight of shadow
yields perhaps 5–15 of them. No statistical procedure distinguishes skill from
noise at n=10. The gate as written invites a confident promotion off a
coin-flip sample. **Re-cut the gate in units of disagreements, not sessions:**
a minimum disagreement count (≥30 is a defensible floor) with the counterfactual
R difference reported alongside its dispersion — and accept that this means the
shadow period is measured in months, not weeks. The brief's own philosophy —
"a check that cannot fail is not a check" — applies to its own promotion gate.

### H2 — A synchronous database write was specified into the hot loop

WP3 wires the shadow allocator into the 15-second cycle and has it write every
verdict to `allocation_log` — a synchronous REST round-trip to free-tier
Supabase, inside the loop whose other job is evaluating exits on live
positions. The try/except protects against *failure*; it does nothing against
*latency*. A 3–5 second write stall under free-tier load delays every exit
evaluation behind it, on the machine whose one mandate is "never miss a tick."
The design demanded `on_ticks` purity and then violated the same principle one
layer up. **Verdicts buffer in memory and flush on the 300-second slow timer.**
The log loses at most five minutes of freshness; the exit path loses nothing.

### H3 — One curve for the hurdle, pooled across regimes, is biased in both directions

The hurdle is the percentile of a pooled historical arrival distribution. But
arrivals are regime-conditional: RISK_ON mornings produce a different edge
distribution than CAUTION mornings. Pooling means the hurdle is **too high in
weak regimes** — the few decent setups get deferred against phantom better
arrivals that regime will not produce, and the day ends flat for no reason —
and **too low in strong regimes** — mediocre early proposals clear a bar set by
average days, spending slots the regime's later, better arrivals deserved.
Anti-optimal on both tails. Full per-regime fitting was correctly deferred to
Phase 5 on sample-size grounds, but a **two-bucket split on the already-computed
`market_context` state** costs nothing and removes the worst of the bias.
Deferring all conditioning was the wrong call.

### H4 — The two books were forced into one abstraction, and one of them doesn't fit

Intraday is genuinely a sequential stopping problem: setups arrive unseen, take
or pass. Swing is not. The full field of 56 plans is **known at 09:15** — that
is an *assignment* problem: estimate `P(trigger today)` per plan, reserve slots
for high-edge high-probability plans, release reservations as probabilities
decay through the day. Wrapping both in a shared `hurdle()` forces the
assignment problem into stopping-problem clothes and throws away the swing
book's single greatest informational advantage: it can see the whole day's
menu at the open. The shared abstraction is elegant and wrong. Two thin
policies with a shared *currency* (the score) beat one policy with a shared
*mechanism*.

### H5 — There is no failure-domain matrix, and Supabase is a five-role single point of failure

Supabase is simultaneously: system of record, config store, **kill-switch
host**, daemon lease arbiter, and now allocation log sink. The architecture
never states what happens when it is unreachable during market hours. Walk it:
active daemon cannot renew the lease; standby cannot read the lease to promote;
the kill switch cannot be read *or set*; config-layer rollback — the celebrated
"one toggle" — is unavailable in exactly the incident class where it is most
wanted. The existing design's one genuinely good answer is GTT: swing stops
live broker-side and survive total fleet death. Intraday live (future) has no
stated broker-side stop story. **Required, as a document not code:** a matrix —
Supabase / VM / laptop / GitHub / Kite, each × "fails during hours" — with the
system's actual behavior in each cell, and cached-config fallbacks for the
kill-switch read path. The GTT pattern shows the authors know how to think this
way; it was applied once and never generalized.

### H6 — The feedback loop stops at the decision and never reaches the fill

The system's entire currency is *net* expected R, yet nothing measures
execution shortfall: decision price vs. fill price vs. the slippage assumption
baked into `cost_model`. If real fills run 15bps worse than the 5bps constant,
every edge in `allocation_log` is overstated and the shadow-vs-greedy
comparison is conducted in a fictional currency. This is the cheapest missing
loop in the whole design — both prices already exist in the order path — and
its absence undermines the evidentiary chain everything else depends on.

### H7 — DEFER has no lifecycle, and the counterfactual it generates is flattering fiction

What happens to a deferred proposal? Re-evaluated when? Invalidated by how much
price drift? Expired at what time? The design never says. Worse, the shadow
scoring implicitly assumes a deferred-then-wanted proposal could have been
taken at its recorded level — paper counterfactuals fill at touch with zero
slippage and zero partial-fill risk, which systematically flatters whichever
policy defers more (the allocator). The allocator will look better in shadow
than it is. Define DEFER semantics — re-arm cadence, drift invalidation, expiry
— and haircut counterfactual fills by the measured shortfall from H6, or the
promotion evidence is biased in the new system's favor, which is the most
dangerous direction for bias to point.

---

## MEDIUM

### M1 — "Unrecoverable" depth data is being written to the fleet's most losable disk

The depth-capture argument — this data cannot be re-fetched, so record it now —
is sound, and then the design stores it on a free-tier VM with no backup, no
snapshot, and an idle-reclamation policy, co-resident with the latency-critical
daemon whose disk I/O now spikes exactly when markets are most volatile. Data
whose value proposition is permanence, on the fleet's least permanent surface.
Either sync the Parquet to the laptop on a schedule, or admit WP2.6 is
premature and cut it. At 14 closed intraday trades, cutting is defensible.

### M2 — The Tier-3 LLM veto duplicates a deterministic component and answers to no one

The stated Tier-3 job — "notice four proposals are one sector bet" — is
`portfolio_constraints`' job, done deterministically, already wired. At 2–4
concurrent positions, concentration is countable on one hand. The residual
value ("regime dissonance") is commentary, and commentary with **veto power**
is a liability: an LLM veto on thin context can suppress the best trade of the
week, and nothing in the design audits veto quality against outcomes. Demote
Tier 3 from veto to annotation until a veto scorecard exists. Keeping the
existing intraday advisor is fine; elevating it to doctrine was dressing.

### M3 — Rupee-day normalization assumes redeployment that mostly won't happen

Dividing edge by holding days says a 4-hour intraday trade beats a 12-day swing
trade because the capital comes back. Capital coming back is only worth
something if it redeploys at comparable edge — and at 2 slots with sparse
qualifying setups, freed capital frequently idles. The normalization
overweights intraday relative to *realized* redeployment. Secondary: MIS margin
means a rupee of intraday capital controls different notional than a rupee of
CNC — the "same rupee" framing is loose. Normalize by measured redeployment
rate, or at minimum flag the tilt in the shadow analysis.

### M4 — Live inputs get no staleness guard at the point of use

WP2.5 correctly makes tick fields live, and correctly keeps `on_ticks` pure —
then feeds those fields into `score()` with no age check. A silently stale
websocket (dead socket, undead dict) turns "live VWAP" into confident garbage,
and the allocator's whole pitch is that its inputs are fresh. Every consumer of
the tick cache needs a max-age gate with fallback to the REST value. One
missing guard, one bad Tuesday.

### M5 — More learners than lessons

Seven intraday engines, nine screening engines, a weekly review, a discovery
engine, a brain with thirteen analyses — against ~70 closed swing trades and 14
closed intraday. The learning apparatus's appetite exceeds the system's lesson
generation rate by roughly an order of magnitude. The one saving grace is
`intraday_setups` resolving all detections (detection-level evaluation scales
with detections, not trades). Closed-trade-based learning does not, and the
weekly review's R-based verdicts will be sample-starved for a year. This is not
fatal; it is a reason to stop *adding* learners and to route every learner
possible onto detection-level and signal-level outcomes (the C1 fix helps here
too).

### M6 — WP4 is a multiple-testing machine with a two-item speed limit

Testing 114 columns' tercile separations at n≈70 produces roughly ten spurious
"material" separations by chance alone at conventional thresholds. The brief's
cap ("propose at most two") limits the damage but not the selection bias —
the two proposed will be the two luckiest. Require an out-of-sample
confirmation window (propose in week N, confirm on weeks N+1..N+4 data) before
any weight change lands in `brain_proposals` as actionable.

### M7 — Two change-application paths with two different philosophies

The stated doctrine is "propose, never auto-apply." The brain's change manager
carries a **per-type auto-apply policy**. Both may be individually reasonable;
together they are governance drift — two doors into live config with different
locks. Reconcile: either the auto-apply types are explicitly enumerated,
justified, and surfaced on the dashboard's switch board, or they route through
the same proposal gate as everything else.

### M8 — Mid-cycle slot races are inherited, and the allocator raises the stakes

A GTT fires, or a Telegram order fills, between the allocator's state read and
its TAKE — `slots_left` was true fifteen seconds ago. The greedy path carries
the same race today; preflight checks in the order manager mitigate. But the
allocator concentrates more decision authority on one state snapshot, so the
race's blast radius grows. Cheap containment: the order path re-validates caps
at placement (verify it does), and the allocator treats its own TAKEs as
provisional until the position appears in the reload.

---

## LOW

### L1 — Shadow-log completeness is unaudited
Swallowed write failures (per the H2 try/except) leave silent holes in the
promotion evidence. A daily reconciliation — proposals seen vs. rows written —
is one query and closes it.

### L2 — `minutes_left` trusts the calendar
Muhurat sessions, early closes, and exchange-curtailed days corrupt the hurdle's
time axis if the session module doesn't feed it. Probably handled; verify once.

### L3 — PostgREST round-trip creep
Several REST calls per 15-second cycle is fine at 95 symbols and would not be at
500. Not a current problem; noted so scale-up doesn't discover it in production.

### L4 — The 27-step monolith
Sequential, single-run-per-day, externally triggered because GitHub cron is
unreliable. Fragile-looking, but the fallback-to-latest-date reads and quality
gates make it acceptable. Leave it alone; the urge to microservice this would
be pure overengineering at one run per day.

---

## Where the design is right, and why it should not move

**Separate exits per book.** The scar is documented: a 15-session time stop
applied to a position that must be flat by 15:20. A unified exit policy
re-litigates a decision that already cost money. The allocator unifies the one
thing that should be unified — where the next rupee goes — and nothing else.
Correct, defend against all future elegance-seeking.

**Recording rejections.** `allocation_log` capturing DEFER and DECLINE is the
single best idea in Phase 4. With 2 entries against 56 candidates, >90% of the
system's information is in the untaken trades, currently discarded.
`intraday_setups` resolving every detection is the existing crown jewel of this
codebase; extending that philosophy to swing is the highest-value structural
change on the table. (C1 makes it even more central: it is also the unbiased
prior source.)

**No LLM in the decision loop.** 88.6-second latency against a 15-second cycle,
per-call cost against a statistical task arithmetic does better. The
out-of-band advisor pattern is the correct shape. Ruthlessness cuts both ways:
this decision survives attack.

**Storage first (WP0).** Read-only mode at 500 MB is a total-system failure
with no warning, and the remediation function already exists uncalled. Ordering
this ahead of all feature work is right. So is the observation that no-backups
is the larger risk than the ceiling.

**Shadow mode, additive migrations, config-first rollback, quarantine over
deletion.** Each is the correct instinct for a system with real money and four
health checks that have historically lied. Keep all of it — subject to H2's
correction on *how* shadow writes.

**Scale honesty.** The design refuses HFT cosplay: no depth models on 14
trades, no ML on 70 samples, no microsecond ambitions on a ₹20,000 book. The
discipline of matching architecture to statistical power is rarer than it
should be and is this system's best cultural asset. The critiques above are
almost all about places where that discipline briefly lapsed.

---

## What this review changes, concretely

Before the brief is handed over, five amendments follow directly from the
Critical and High findings:

1. **WP1/WP3 prior source** — signal-level forward outcomes of the full field,
   not `closed_positions` (C1). `entry_rationale` carry-through stays, but its
   role narrows to allocator scoring.
2. **WP3 scoring** — empirical R distributions per class replace the binary
   formula (C3); hurdle gains a two-bucket regime split (H3); swing gets an
   assignment-style policy instead of sharing `hurdle()` (H4); tick inputs get
   staleness gates (M4); verdict writes buffer to the slow timer (H2).
3. **WP3 scope** — every entry path routes through or is counted by the
   allocator; zone-watching consolidates onto the daemon (C2). DEFER gets
   defined semantics and haircut counterfactuals (H7).
4. **WP5 gate** — re-denominated in disagreements (≥30) with dispersion
   reported, not sessions (H1). Expect months; say so.
5. **WP0/WP6 additions** — the failure-domain matrix as a deliverable (H5); the
   execution-shortfall loop (H6); the shadow-log reconciliation (L1).

WP2.6 (depth capture) is the one package this review recommends **cutting or
deferring outright** (M1): its value thesis is permanence and its storage is
the least permanent thing in the fleet.
