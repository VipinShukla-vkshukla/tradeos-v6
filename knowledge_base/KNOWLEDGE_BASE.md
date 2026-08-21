# TradeOS Knowledge Base — Living Document

**Last updated:** 18 August 2026 (9 sessions of evidence, plus direct
`final_score` and `implied_rr` tercile measurements against the full
historical sample)
**Source reviews:** `daily/2026-08-06.md`, `daily/2026-08-07.md`,
`daily/2026-08-10.md`, `daily/2026-08-11.md`, `daily/2026-08-12.md`,
`daily/2026-08-13.md`, `daily/2026-08-14.md`, `daily/2026-08-17.md`,
`daily/2026-08-18.md`

**18-Aug note:** this file (and `daily/2026-08-13.md`) briefly reverted to
its 12-Aug state twice — the `fix/quote-parity-and-gabriel-gap-gates`
branch this review was committing to turned out to have an in-progress,
unresolved merge (`fix/resolve-day-session-guard` → this branch, conflict
in `docs/FINDINGS.md`) sitting underneath it, which is almost certainly
why commits kept appearing to vanish. From 17-Aug onward this file lives
on `main`, which was already carrying the correct, intact history through
14-Aug (commit `bbe6059`) even while the feature branch's copy kept
reverting — content below is restored from that stable copy, not
reconstructed from memory.

Read this before starting a new day's review. Update it after, per the
workflow in `README.md`. An item's confidence should track its sample
size explicitly — "n=1 session" is not the same claim as "n=5 sessions,"
and this file should always say which one it's making.

---

## Validated Rules

*(Confirmed across enough sessions to be treated as settled, pending
contradiction.)*

- **The give-back guard protects real profit rather than letting a winner
  round-trip.**
  Sessions: 6 Aug (n=1 — KIMS, TRAVELFOOD), 10 Aug (n=1 — ETERNAL, peaked
  +4.41%, gave back 53%, exited +2.06%). Now n=3 live occurrences across
  2 sessions.
  Confidence: High — three live confirmations, all the same shape,
  backed by the codebase's own 70-trade historical study.
  Next evidence needed: none pressing — this can be treated as settled
  unless a future session contradicts it.
  **11-Aug addendum — `intraday_giveback_min_r` recalibrated 1.0 → 0.5.**
  The guard's EXISTENCE was settled above; its THRESHOLD was not re-derived
  until this session. `closed_positions` MFE quantiles (framework=INTRADAY,
  n=27 with excursion data): winners' minimum peak 0.499R, losers' 75th-
  percentile peak 0.490R — the two coincide almost exactly, which is what
  makes 0.5 the boundary the data itself draws rather than a round number.
  At the old 1.0R floor a full quarter of winners (p25 MFE 0.849R) never
  had the guard active during their most fragile stretch.
  `tools/exit_ladder_replay.py --min-r 0.5` against the same window: 9
  positions affected (vs 3 at 1.0), ceiling +5.60R (vs +1.42R) — KAYNES,
  SAPPHIRE, ADANIGREEN, IFCI, HINDCOPPER, SWIGGY all peaked 0.5-1.1R and
  reversed to a loss via SETUP_INVALIDATED/TIME_EXIT, never touching a
  rung the old floor could engage on.
  Confidence: Medium — n=27 is real but still thin, the same caveat
  migration 059 itself carried. Re-run the replay as the sample grows.
  **11-Aug, same session, first live confirmation.** RKFORGE (MFE 0.504R)
  and MCX (MFE 1.587R) both triggered the recalibrated guard — the first
  real trade to test the LOW end of the new range specifically (RKFORGE
  would never have engaged the guard under the old 1.0R floor). Exit was
  essentially flat (−0.02R), so this doesn't prove the recalibration
  "saved" money here, only that it engages where the underlying data said
  it should.
  **12-Aug: VIJAYA (swing), fifth occurrence.** Peaked +0.92R (+7.40%),
  gave back exactly 50%, exited +0.45R/+3.65%. n=3 occurrences (2
  sessions) → n=5 occurrences (4 sessions). Confidence unchanged at High.
  **13-Aug: no new evidence** — zero triggers. Explicitly checked and
  empty.
  **14-Aug: extended to SHORT positions for the first time.** TMPV
  (+0.955R), CONCOR (+0.278R), SWIGGY (+0.988R) — all SDN, all SHORT, all
  `GAVE_BACK_THE_MOVE`. Every prior confirmation had been LONG. n=5
  occurrences (4 sessions) → **n=8 occurrences (5 sessions)**. Confidence
  stays High — this strengthens the existing rule across a new dimension
  rather than changing it.
  **17-Aug: SUMICHEM, ninth occurrence.** Peaked +0.67R (+5.05%), gave
  back exactly 50%, exited +0.34R/+2.57%, clean execution (4 minutes,
  zero blocks). n=8 → **n=9**. Confidence stays High.
  **18-Aug: four more, all SDN/SHORT.** INOXWIND (+0.269R), TATASTEEL
  (+0.405R), COFORGE (+0.413R), GLAND (+0.160R). n=9 → **n=13**.
  Confidence stays High.

- **The stall exit closes a swing position that peaked and then reversed
  without ever earning its own conviction, rather than letting the slot
  sit — PROMOTED from Ideas Requiring More Evidence, 14 Aug.**
  Sessions: 10 Aug (n=1 — CIPLA: 11 sessions, never cleared 0.5R, exited
  +0.089R rather than sit dead). 14 Aug (n=1 — MANAPPURAM: 10 sessions,
  peaked +0.43R/+2.56%, reversed to −0.75R; guard fired correctly at
  −0.69R). n=2 confirming sessions — the bar this item's own "needed
  before any verdict" note set on 10 Aug.
  Confidence: Medium-high — two live confirmations, both the mechanism
  correctly recognizing a stalled-then-reversing position, not just a
  stalled one. MANAPPURAM's case additionally surfaced a real execution-
  timing finding (see Ideas Requiring More Evidence) that is about
  EXECUTION, not the decision to exit, so it doesn't weaken this rule.
  Next evidence needed: a third confirmation would move this toward
  High, matching the give-back guard's own path.
  **17-Aug: third confirmation — moves to High.** GABRIEL: 11 sessions,
  best-ever +0.00R (+0.03%, essentially never favorable), reversed to
  −0.64R at trigger. Same shape as MANAPPURAM and CIPLA. n=2 → **n=3
  confirming sessions**. Confidence Medium-high → **High**, matching the
  give-back guard's own path as anticipated.

- **Automated live-order execution can be trusted once a decision fires —
  PROMOTED from Promising Hypotheses, 17 Aug.**
  Sessions: 6 Aug (n=1 failure — KIMS + TRAVELFOOD SELL both blocked by
  IP allowlist). 10 Aug (n=1 success — ETERNAL + CIPLA). 12 Aug (n=1
  success — VIJAYA). 17 Aug (n=1 success — SUMICHEM, fired 09:42, closed
  09:46, zero blocks of any kind). **3 successes, 1 failure.**
  Confidence: Medium-high → **High**. The 6-Aug failure stays on record
  as a known, understood risk category (IP-allowlist can drift) rather
  than being erased — this promotion says the mechanism is trusted by
  default now, not that it is infallible.
  Next evidence needed: none pressing, matching the give-back guard's own
  "settled unless contradicted" status. A recurrence of an IP-allowlist-
  style block would be the thing to watch for, not routine confirmation.

- **`final_score` does not predict forward R within the CONTINUATION swing
  family, on the full resolved sample measured so far.**
  Measured 6 Aug directly against `signal_output_daily` (n=125 entered +
  resolved CONTINUATION plans — CTL/SEC/TPO/SBS/VBD/RSB/IAD, alone or
  combined). Mean R by `final_score` tercile: 0.516 / 0.491 / 0.511 — flat,
  no monotonic separation. This was the direct test of whether the
  allocator's swing prior should condition on `final_score` in addition to
  engine identity (it was previously conditioning on neither — see
  `docs/6_IMPLEMENTATION_STATUS.md`'s attribution-bug entry, 6 Aug); the
  conditioning was NOT shipped on this evidence.
  Confidence: Medium — a real, fairly large n, but the same survivorship-
  in-time bias `scoring._swing_bias_warning()` already documents for the R
  sample generally (fast winners resolve first in a young dataset) applies
  here too. MOM (n=17) and RVS (n=5) were too thin to read anything into.
  Next evidence needed: re-run `python -m allocation.scoring --tercile` as
  the resolved sample grows, particularly once 15-session windows opened in
  the last month have had time to close.

- **`implied_rr` (falling back to `expected_r`) shows no monotonic positive
  relationship with forward R within the CONTINUATION swing family, on the
  full resolved sample measured so far.**
  Measured 11 Aug directly against `signal_output_daily` (n=188 entered +
  resolved CONTINUATION plans — a larger sample than the `final_score`
  measurement above), using the identical fallback chain
  `entry_ranking.score_plan()` itself reads. Mean R by rr tercile:
  LOW +0.561 (n=63) / MID +0.439 (n=79) / HIGH +0.448 (n=46) — not flat like
  `final_score`, but INVERTED: the plans with the lowest claimed R:R
  outperformed the plans with the highest. This was the direct test of
  whether `rank_weight_rr` (silently defaulting to 1.0 — no `system_config`
  row existed for it before this measurement) deserved to be the
  single largest-magnitude component left in `score_plan()` once
  `final_score` was rescaled (migration 060). It did not: `rank_weight_rr`
  was reduced 1.0 → 0.4 on this evidence (code default in
  `analysis/entry_ranking.py`, registered in migration 065).
  Confidence: Medium — n=188 is a real, fairly large sample, but the
  LOW-vs-HIGH gap (≈0.11R) is within roughly one combined standard error, so
  this is evidence AGAINST the assumed positive relationship, not proof of a
  true inverse one. A plausible mechanism: every plan reaching this ranking
  already cleared `generate_signals.py`'s own minimum-R:R entry gate, so the
  residual variation above that gate may be dominated by the same kind of
  stop/target noise `rank_rr_cap` already exists to contain (the KIMS 19.67
  case) rather than by real edge. The same survivorship-in-time bias noted
  for `final_score` applies here too.
  Next evidence needed: re-run `python -m allocation.scoring --rr-tercile`
  as the resolved sample grows. If the inversion strengthens rather than
  flattens, that argues for reducing `rank_weight_rr` further; if it
  flattens toward zero, 0.4 or higher becomes defensible again.

---

## Promising Hypotheses

*(Enough evidence to act cautiously on, not enough to call settled.)*

- **Intraday engines structurally suit different market regimes — momentum/
  breakout engines (ORB, GAP, PDL, PBK, VCE, SDN) should outperform in
  RISK_ON, mean-reversion engines (RNG, VWR) should outperform in NEUTRAL/
  CAUTION — but this is UNTESTED, not measured.**
  11-Aug-2026: no engine has ever been chosen or weighted per market regime
  in this system — `hurdle.regime_bucket()` gates the allocator's BAR, not
  which engine's opinion to trust more. `allocation.scoring.
  regime_fit_multiplier()` now exists and is fully wired into `score()`
  (`engine_family`/`market_state` params, both optional), but shipped at
  `intraday_regime_fit_weight=0.0` — an exact no-op — because the
  classification is STRUCTURAL (each engine's own module docstring: does it
  need a trend to break into, or the absence of one) rather than measured.
  `hurdle.py`'s own docstring already states the project's position on
  this exact question: "Per-regime fitting is Phase 5 and is gated on
  years of data, not on cleverness" — paid for twice already by the STRONG
  hurdle bucket's two self-reference failures (05-Aug, 10-Aug).
  Confidence: Theoretical only — zero measured evidence. `regime_at_
  detection` (migration 068) was added this session specifically to
  accumulate the evidence `allocation.scoring.regime_fit_report()` would
  need: does MOMENTUM's RISK_ON mean R actually exceed its NEUTRAL/CAUTION
  mean, and does MEAN_REVERSION's NEUTRAL/CAUTION mean actually exceed its
  RISK_ON mean? Both columns are brand new and hold zero historical rows as
  of this writing.
  Next evidence needed: re-run `python -m allocation.scoring --regime-fit`
  after a few weeks of live sessions have accumulated TAKEN rows with
  `regime_at_detection` populated. Raise `intraday_regime_fit_weight` only
  if the report confirms the direction in both archetypes — the same
  evidence bar `rank_weight_screener` and `rank_weight_rr` were both raised
  on, not the theory alone.

- **The swing allocator's "slots spent" decline reflects real slot
  scarcity — DEMOTED from Validated Rules, 7 Aug.**
  Sessions: 6 Aug (n=1 — GABRIEL: slots genuinely occupied, freed, admitted
  immediately after). 7 Aug (n=1 — the opposite texture: 25 candidates
  declined all day citing "slots spent" while the counter sat at 0/3 used
  all session; `allocation_decisions` disagreed with the live veto on 5
  symbols). 10 Aug (n=1 — clean again: two give-back/stall exits freed two
  slots, three fresh candidates (SCI, AUBANK, VIJAYA) admitted inside the
  same ~20-second window, counter correctly read 3/3 by close).
  Confidence: Low → **Medium** — 2 of 3 sessions now show the mechanism
  working exactly as designed; the 7 Aug anomaly is looking more like an
  outlier than a standing pattern, but it is still unexplained, not
  outvoted, so this stays short of Validated.
  Next evidence needed: the `_verdicts`-vs-`allocation_decisions`
  reconciliation from 7 Aug is still open — a majority of clean sessions
  doesn't retire that investigation. DEVYANI's 10 Aug allocator numbers
  also didn't fully reconcile against `CLAUDE.md`'s account of the same
  trade (see `daily/2026-08-10.md` Item 2) — a related, still-open
  bookkeeping question about whether `allocation_decisions` reliably
  reflects what the live engine actually decided.
  **12-Aug: third clean session.** CARBORUNIV entered within 2 seconds of
  VIJAYA's give-back exit; day ended with the counter matching reality
  (2/3 used). 3 of 4 sessions now clean. Confidence unchanged at Medium —
  majority-clean still doesn't retire the open 7-Aug investigation.
  **13-Aug: no new evidence** — zero qualifying candidates. Explicitly
  checked and empty.
  **14-Aug: one more clean data point.** Three entries (SUMICHEM,
  TRAVELFOOD, HINDCOPPER) landed as slots freed, count matched reality.
  Confidence unchanged at Medium.
  **17-Aug: one more clean data point.** TATATECH and AARTIIND entered
  15 and 5 minutes after their respective freed slots — looser timing
  than prior sessions but the same mechanism. Confidence unchanged at
  Medium.
  **18-Aug: no new evidence** — zero swing candidates to test. Explicitly
  checked and empty.

- **Pre-market evaluation producing an exit decision that must wait for
  market open, adding execution slippage — PROMOTED from Ideas Requiring
  More Evidence, 17 Aug.**
  Sessions: 14 Aug (n=1 — MANAPPURAM's stall exit fired 09:00:41 IST, 14
  minutes before market open, diagnosing −0.69R; retried 56 times against
  `MARKET_CLOSED`, executed 09:16:08 at −0.75R, ~0.06R worse). 17 Aug
  (n=1 — GABRIEL, essentially the SAME trigger time, 09:00:40, retried
  against `MARKET_CLOSED` again, executed at −0.81R vs a −0.64R trigger,
  ~0.17R worse).
  Confidence: none → **Low-medium** — two occurrences with near-identical
  clock times and the same failure shape is enough to treat as a real,
  systematic pattern (likely a fixed pre-market evaluation schedule) not
  a coincidence, though n=2 sessions is still short of full confidence.
  Next evidence needed: a third occurrence, and whether the ~09:00 IST
  timing is exactly fixed. Worth a real code look (defer pre-market
  evaluation until market-open confirmation) if it recurs once more —
  not yet, per this file's own discipline against acting on two sessions.
  **18-Aug: no new evidence** — zero swing exits, zero `MARKET_CLOSED`
  occurrences. Stays at n=2, Low-medium confidence.

---

## Ideas Requiring More Evidence

*(Too early to have a confidence level at all — tracked so tomorrow's
review knows to keep watching, not to re-derive from scratch.)*

- **SDN (short family) engine viability.**
  Sessions: 6 Aug (n=2, both losses). 7 Aug: +3 (BLUESTARCO win, COFORGE +
  HINDCOPPER losses). 10 Aug: +3 (SONACOMS win +1.00R; KAYNES −0.98R;
  DEVYANI −0.81R, 99-second invalidation — see Section 2 of
  `daily/2026-08-10.md` for the unreconciled allocator-numbers caveat on
  this specific trade). Running total ~8-9 legs, 2 wins. **11 Aug: first
  quiet session** — 25 setups detected, zero taken or closed. Sample
  unchanged. **12 Aug: busiest detection day yet (91 distinct setups),
  still zero closed.** Rejection profile shifted — `BLOCKED_SHORTABILITY`
  (dominant in the engine's first days) nearly absent (2 of 693 raw);
  `BLOCKED_SHORTS_MARKET` (the regime gate) now dominates. Reads as the
  shortability infrastructure maturing while the regime gate remains the
  real constraint — noted, not yet a separate tracked claim. Sample
  unchanged at ~8-9 legs.
  **13 Aug: first close in three sessions.** TATACHEM (+0.06R,
  `TIME_EXIT`). ~8-9 legs, 2 wins → **~9-10 legs, 3 wins**.
  **14 Aug: biggest single-session sample by far.** 9 more trades, 4
  wins (TMPV, CONCOR, SWIGGY, BAJFINANCE) vs 5 losses. ~9-10 legs, 3 wins
  → **~18-19 legs, 7 wins** — approaching the `MIN_SAMPLE=20` floor for
  the first time.
  **17 Aug: no new evidence** — zero intraday trades closed today (first
  such session on real detection volume, 185 setups). Stays ~18-19 legs.
  **18 Aug: biggest single-session sample by a wide margin — sample-size
  floor cleared, session-count floor not yet.** 12 trades (5 wins, 7
  losses), all SDN/SHORT, on the lowest-regime-score session yet (44).
  ~18-19 legs, 7 wins → **~30-31 legs, 12 wins** (~40% cumulative).
  `MIN_SAMPLE=20` now cleared; the ≥10-session requirement is not (6 of
  10 sessions with closes so far: 6, 7, 10, 13, 14, 18 Aug). Reporting
  progress on one axis of the bar, not a verdict on the whole.
  Needed before any verdict: ~20 resolved outcomes across ≥10 sessions
  (this project's own `MIN_SAMPLE`/`MIN_SESSIONS` standard from
  `tools/weekly_review.py`).

- **Whether `swing_max_new_per_day` is too conservative.**
  Sessions: 6 Aug (n=1 — slot count was the binding constraint on 11 of 17
  candidates, most missing the edge bar by a narrow margin).
  Needed before any verdict: several more sessions in a similar regime, to
  see if slot-exhaustion is typical or 6 Aug was unusually candidate-rich.
  **11 & 13 Aug: a different condition, worth distinguishing.** Both
  sessions had ZERO qualifying candidates all day under a soft-NEUTRAL
  regime (score 48-49) — the cap never bound because nothing reached it,
  the opposite texture from 6 Aug's candidate-rich, cap-binding day. n=2
  sessions now for "does a soft regime empty the candidate pool," a
  separate question from the original one about whether the cap itself is
  too tight.
  **18 Aug: third confirming session, confidence rises.** Score 44 — the
  lowest regime reading in the series — again produced zero buyable
  swing candidates. n=2 → **n=3** sessions, all three among the lowest
  regime scores recorded. Confidence this is regime-linked rather than
  coincidence: Low → **Medium**.

- **Small CNC clip sizes turning gross wins into net losses on fixed
  charges.**
  Sessions: 6 Aug (n=1 — KIMS: +₹8.65 gross → −₹7.23 net).
  Status: not new — this is the same friction problem already named in
  `sizing_max_cost_r`'s own code comments (currently disabled: "the clip
  size is the problem, not the gate"). Today re-confirms it live; not
  tracked as a fresh finding, and not a reason to revisit the sizing
  policy on one trade.

---

## Rules That Did Not Work

*(An item lands here only once evidence actively contradicts a rule that
was believed to hold — not simply because a trade lost.)*

- **A minimum wall-clock gap between entries, to stop the day's whole
  budget being spent in one burst — replaced same-day by a count-before-
  cutoff reserve, 11-Aug-2026.**
  Built on a real incident (10-Aug: three swing entries in ~40 seconds)
  but the wrong mechanism for it. Checked against the actual sequence
  before merging: the allocator scored 8 proposals together at 09:36:23
  ("3 to take, 5 refused") and picked AUBANK and SCI as two of that day's
  three best trades in ONE joint, opportunity-cost-aware decision, two
  seconds apart. A 20-minute gap would have let AUBANK through and
  refused SCI for 20 minutes for no reason — overriding a decision the
  allocator had already made well, at zero benefit. The actual failure
  was that all three slots were spent on a single snapshot of the first
  ~21 minutes (itself mostly a symptom of the Kite token being stale
  until then), not that entries arrived close together.
  Caught by the operator asking, before merge, whether a gap would cost
  genuine simultaneous opportunities — not by this session's own testing,
  which had verified the mechanism did what it was built to do without
  questioning whether what it was built to do was the right thing.
  Replaced with `entry_reserved()` — a cap on how many entries may land
  before a cutoff clock time, with no restriction on how close together
  they are otherwise. See `execution/order_manager.py`.

---

## Watching Against Prior Evidence

*(Deliberate operator decisions made KNOWING the existing measurement points
the other way — not oversights, not "not enough data yet." Tracked
separately so the next review can ask specifically whether the new evidence
still supports the bet, rather than re-deriving the original decision.)*

- **MOM and RVS (swing) and VCE and RNG (intraday) promoted from SHADOW to
  ACTIVE, 07-Aug-2026, against their own measured track record.**
  Evidence at time of promotion: RVS 42% win / **−0.49%** avg forward return
  (negative, n=4,747 detections); MOM 50% / +0.05% (barely positive); VCE
  E[R] −0.94%; RNG E[R] −1.38%. All four were shadowed FOR these numbers, not
  for a thin sample.
  Mechanism found alongside the decision: MOM/RVS had actually been
  functionally ACTIVE the whole time regardless of the documented SHADOW
  status — `strategy_config` carries lifecycle in two disagreeing places
  (outer column vs nested `params.lifecycle`), and the code path that gates
  capital (`engine_registry.load_registry()`) reads the nested one, which was
  never updated when the 04-Aug demotion set the outer column. So the "SHADOW"
  demotion had a real hole in it that this promotion also happens to close —
  the promotion is a deliberate choice, the field-consistency fix is a
  separate, incidental correctness fix riding along with it.
  Confidence this was the right call: unmeasured — this is the bet, not a
  verdict on it. Real capital exposure differs by book: swing (MOM/RVS) is
  LIVE, real money, `alloc_live_swing=true`; intraday (VCE/RNG) is PAPER
  only, zero capital risk, and additionally protected by `alloc_live_intraday`
  now being live (a weak candidate still has to clear the cost-netted edge
  hurdle regardless of lifecycle).
  Next evidence needed: re-run the win-rate/forward-return measurement
  (`docs/6_IMPLEMENTATION_STATUS.md`'s methodology) after these four have
  accumulated resolved outcomes UNDER ACTIVE status specifically — the old
  numbers describe what they did as SHADOW/UNDER-DEMOTED, which per the
  mechanism above is actually close to what they've been doing regardless.
  If the re-measurement still shows RVS negative, that is the point to
  revisit this decision, not a fixed calendar date.
  **7 Aug check: zero new resolved outcomes** — no swing exits and no
  VCE/RNG intraday closes that session. Explicitly checked and empty, not
  skipped.
  **10 Aug check: still zero resolved outcomes, but first activity.** SCI
  entered live under `strategy: MOM` — first MOM position since promotion,
  unresolved. VCE showed heavy allocator-veto activity (32 raw
  ALLOCATOR_DECLINED instances) under the now-live `alloc_live_intraday`
  enforcement — the promotion's own stated protection for a weak candidate
  visibly firing — but zero VCE trades reached `closed_positions`. Still
  nothing to verdict on win rate or forward return; noting the mechanism
  is engaged, not idle.
  **11 Aug: first resolved VCE outcome since promotion.** ATHERENERG
  closed −0.35R via `SETUP_INVALIDATED` (MFE only 0.053 — never got going).
  n=0 → n=1. Nowhere near enough to read against VCE's pre-promotion E[R]
  of −0.94%, but the first real data point. MOM/RVS still zero resolved
  (SCI, MANAPPURAM both remain open). RNG: detection only (9 raw), no
  closes.
  **12 Aug: exposure growing, evidence still zero.** Two more MOM
  positions opened (CARBORUNIV, AIIL) alongside already-open SCI,
  MANAPPURAM — **four live MOM positions, real capital, n=0 resolved**
  since the 07-Aug promotion. VCE/RNG: no new closes. Flagging explicitly
  that real money exposure to this bet is now larger than when it was
  made, with no additional evidence either direction — worth a direct
  look the day any MOM position finally resolves.
  **13 Aug: still zero resolved, priority carried forward.** Zero swing
  exits for a second consecutive reviewed session — restated so it isn't
  lost to a run of quiet sessions.
  **14 Aug: first three resolved outcomes since promotion.**
  MANAPPURAM/MOM: −0.75R (peaked +0.43R, stalled 10 sessions, reversed —
  correctly caught by the newly-promoted stall exit, with a real if minor
  execution-timing footnote, see Ideas Requiring More Evidence).
  PPLPHARMA/MOM+SEC: +0.863R (14 sessions, peaked 12.49%, banked 8.05%).
  AIIL/MOM: +0.377R (2 sessions). **2 wins, 1 loss (67%) on n=3** — better
  than MOM's 50% pre-promotion win rate, but n=3 is far short of anything
  actionable. Encouraging, not confirming. VCE/RNG: no new closes.
  Next: this thread no longer needs restating as "still zero" every
  session — it has real data now and can return to normal per-session
  tracking rather than standing as the review's top priority.
  **17 Aug: two more resolutions.** SUMICHEM/MOM: +0.341R win (3
  sessions, peaked 5.05%, banked 2.57%). GABRIEL/MOM+SEC: −0.81R loss
  (11 sessions, essentially never favorable, caught by the stall exit —
  see Validated Rules). n=3 → **n=5 resolved** (3 wins, 2 losses, 60%),
  continuing to track at or above the 50% pre-promotion MOM baseline.
  VCE/RNG: no new closes.
  **18 Aug: no new evidence** — zero swing activity today. Stays at n=5
  resolved, 60% win rate.

---

## Standing Operational Watch Items

*(Not trading rules — infrastructure facts worth carrying forward because
they bound what the rules above can even be tested against.)*

- `intraday_capital` (₹1,00,000) is ~3.3x `TOTAL_CAPITAL` (₹30,000).
  Confirmed safe only because `intraday_trading_mode=PAPER`; a guardrail
  in `config.py` now refuses to let swing size against ₹0 if intraday ever
  goes LIVE in this state. Verified firing correctly, 6 Aug. Do not flip
  intraday live without first resolving this gap.
- The Oracle server (`tradeos-vcn`) runs the actual daemon; the laptop is
  standby/dashboard-only per `deploy/README.md`. Confirm its IP is
  currently allowlisted before trusting live automated exits — see the
  IP-allowlist hypothesis above.
  **11-Aug: this is live right now, not hypothetical.** `tools.health`'s
  `kite` check reports the current public IP as `103.197.74.243`, but only
  `103.197.75.33` is recorded as allowlisted — every order placement will
  be REJECTED until this is fixed. Not something this session can fix (it
  requires updating the Kite Connect app's allowed IP list on Zerodha's
  side); flagged here so it is not lost.
  **11-Aug reconfirmation: still unresolved.** `tools.health`'s 17:40 IST
  run today shows the identical mismatch. Two sessions running now with
  zero swing exits to actually test it against — the risk hasn't
  decreased, it just hasn't been exercised.
- **11-Aug — a partial book left `actual_qty`/`kite_qty` permanently stale,
  hiding real drift behind a false `reconcile_status=MATCHED`.**
  PPLPHARMA booked 5 of 11 shares on 10-Aug. The write recorded it
  (`intraday/engine.py::_auto_exit`, `BOOK_PARTIAL`) touched `current_qty`
  alone (6, correctly) and left `actual_qty`/`kite_qty` at the pre-partial
  11. Confirmed against the live broker holding a full day later: 6 real
  shares, DB still showing 11 in both mirror columns. It never
  self-corrected because `control.position_lifecycle`'s reconcile
  "already matches" fast path compared ONLY `current_qty` against the
  broker, found agreement, and declared `reconcile_status=MATCHED`
  without ever checking the other two — and the dashboard's own mismatch
  banner is gated on `reconcile_status != 'MATCHED'`, so the false
  MATCHED did not just miss the drift, it hid a real one on the same row.
  Fixed at three levels: the `BOOK_PARTIAL` write itself (both PAPER and
  LIVE paths) now keeps all three fields together;
  `position_lifecycle.py`'s reconcile fast path (`_mirror_qty_drift()`)
  now re-syncs the mirrors even when `current_qty` alone already agrees;
  and `tools.health`'s new `qty_fields` check directly asks whether the
  three fields agree, on every open position, every run — the automated
  safety net asked for, independent of which future code path might
  repeat the mistake. The frontend (`PositionsTab.tsx`) also gained a
  "Booked N @ ₹X (+₹Y)" line — realised profit from a partial was
  previously invisible on an open position's card until the whole
  position eventually closed. Live PPLPHARMA row corrected
  (`actual_qty`/`kite_qty` 11 → 6) after confirming the true value against
  the broker directly.
- **11-Aug addendum — resolved, but not the way it looked.** `paper_starting_
  capital` (₹1,00,000) tripping `validate_config`'s 1.5x threshold turned
  out not to be a value problem: `execution.paper_broker.capacity()`
  migrated from reading `paper_starting_capital` to reading
  `capital_for("INTRADAY")` (== `intraday_capital`) on 07-Aug-2026, and
  `paper_starting_capital` was never removed from the coherence check —
  confirmed by grep to be read by NO code anywhere for behaviour, and
  invisible to `check_wiring()` because it is `risk_level=SAFE`, not
  `CRITICAL`. `tools/validate_config.py` now reports it as INFO (unread)
  and checks the real transferability property
  (`paper_capacity_transfer`) against `capital_for("INTRADAY")` instead —
  which is STILL ₹1,00,000 against a ₹30,000 account, so the underlying
  "paper results may not fully transfer" fact has not gone away, only the
  check pointing at the wrong key has. `intraday_capital` staying elevated
  remains the pre-existing, deliberate, deferred decision named above —
  this addendum did not change it.
  Also found and fixed in the same pass: `apply_fixes()` (`--fix`) wrote
  `suggested` straight into `system_config.value` with no check it parsed
  as a number. Two findings (`intraday_capital`, both branches) carried
  PROSE there ("below ₹30,000 before going live"), which strips to a
  non-numeric string — `--fix`, run while either was active, would have
  silently corrupted a live capital-sizing key. Fixed at the call sites
  (`suggested=None` — a judgment call has no single correct number) and
  hardened in `apply_fixes()` itself (refuses a non-numeric suggestion and
  a key with no matching `system_config` row).
  `paper_max_open_positions` (10) DID change — 10 × the ₹25,000
  `intraday_max_order_value` needs ₹2,50,000, far more than the real
  ₹1,00,000 paper capacity. Lowered to 4, migration 071.
  `tools.health`'s `config` check is fully green for the first time this
  session as a result.
- `brain_proposals.backtest_result` has existed since the table was
  created and, confirmed by direct query 11-Aug-2026, had never once been
  populated (0 of 54 rows). `tools/proposal_backtest.py`
  (`tradeos proposals-backtest`) now fills it, but only for proposals
  shaped as a real `system_config` key with two literal values — run
  against the live backlog the same day, 0 of 48 PENDING proposals were in
  that shape (43 `CODE_SUGGESTION`, the rest `ENGINE_CANDIDATE`/
  `ENGINE_LIFECYCLE`/`ENGINE_PARAMETERS`/prose). The infrastructure works;
  today's actual backlog does not yet contain anything for it to test.
  Worth steering `weekly_review.py`/`discover_engines.py` toward emitting
  `CONFIG_VALUE`-shaped proposals (a real key, a literal before/after) when
  the underlying finding allows it, specifically so future proposals land
  in the testable shape rather than needing a human to translate prose
  into a concrete change first.
