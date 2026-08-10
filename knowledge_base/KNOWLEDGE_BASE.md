# TradeOS Knowledge Base — Living Document

**Last updated:** 10 August 2026 (3 sessions of evidence, plus a direct
`final_score` tercile measurement against the full historical sample)
**Source reviews:** `daily/2026-08-06.md`, `daily/2026-08-07.md`,
`daily/2026-08-10.md`

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

---

## Promising Hypotheses

*(Enough evidence to act cautiously on, not enough to call settled.)*

- **Automated live-order execution cannot be trusted unattended — a
  broker-side block can pass every readiness check and only surface when
  the order itself is placed.**
  Sessions: 6 Aug (n=1 — KIMS + TRAVELFOOD SELL both blocked by IP
  allowlist; TRAVELFOOD's give-back guard re-fired 4x over 20 minutes
  without landing). 7 Aug: no swing exit fired, untested. 10 Aug (n=1 —
  ETERNAL + CIPLA SELLs both succeeded cleanly, zero IP blocks, GTT
  cancel + re-entry inside the same 20-second window).
  Confidence: Low-medium → **Medium** — one failure, one clean success;
  early lean toward "6 Aug was a one-off," not yet confirmed.
  Next evidence needed: one more clean session to move this toward
  Validated, or a recurrence to move it the other way — currently 1-1.

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

---

## Ideas Requiring More Evidence

*(Too early to have a confidence level at all — tracked so tomorrow's
review knows to keep watching, not to re-derive from scratch.)*

- **SDN (short family) engine viability.**
  Sessions: 6 Aug (n=2, both losses). 7 Aug: +3 (BLUESTARCO win, COFORGE +
  HINDCOPPER losses). 10 Aug: +3 (SONACOMS win +1.00R; KAYNES −0.98R;
  DEVYANI −0.81R, 99-second invalidation — see Section 2 of
  `daily/2026-08-10.md` for the unreconciled allocator-numbers caveat on
  this specific trade). Running total ~8-9 legs, 2 wins.
  Needed before any verdict: ~20 resolved outcomes across ≥10 sessions
  (this project's own `MIN_SAMPLE`/`MIN_SESSIONS` standard from
  `tools/weekly_review.py`).

- **CIPLA-style stall exit (swing, 11 sessions never clearing 0.5R).**
  Sessions: 10 Aug (n=1 — CIPLA, exited +0.089R rather than let a dead
  slot sit).
  Needed before any verdict: a second confirming session, same bar the
  give-back guard cleared on its way to Validated.

- **Whether `swing_max_new_per_day` is too conservative.**
  Sessions: 6 Aug (n=1 — slot count was the binding constraint on 11 of 17
  candidates, most missing the edge bar by a narrow margin).
  Needed before any verdict: several more sessions in a similar regime, to
  see if slot-exhaustion is typical or 6 Aug was unusually candidate-rich.

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

*(None yet. An item lands here only once evidence actively contradicts a
rule that was believed to hold — not simply because a trade lost.)*

- *(empty as of 6 Aug 2026)*

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
- `paper_starting_capital` (₹1,00,000) also exceeds `validate_config`'s
  1.5x-of-real-capital threshold (tripped 8 Aug's diagnostic run) — same
  shape as the `intraday_capital` item above, on a second config key. Not
  urgent (safe while paper), but two keys now carry this pattern.
