# TradeOS Knowledge Base — Living Document

**Last updated:** 6 August 2026 (1 session of evidence)
**Source reviews:** `daily/2026-08-06.md`

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
  Sessions: 6 Aug (n=1 live occurrence — KIMS, TRAVELFOOD).
  Confidence: Medium-high — backed by the codebase's own 70-trade
  historical study (24/28 losing swing trades had been >0.5% in profit
  first); 6 Aug is the first LIVE confirmation, not the origin of the
  claim.
  Next evidence needed: 2-3 more live give-back triggers to confirm the
  exit price consistently beats the alternative of riding to the stop.

- **The swing allocator enforces slot scarcity correctly — declines a
  qualifying edge only while a better candidate holds the slot, admits it
  once a slot frees.**
  Sessions: 6 Aug (n=1 — GABRIEL).
  Confidence: Medium.
  Next evidence needed: a session where slots are NOT the binding
  constraint, to see edge-vs-hurdle comparison do the work alone (so far
  only tested under slot pressure).

---

## Promising Hypotheses

*(Enough evidence to act cautiously on, not enough to call settled.)*

- **Automated live-order execution cannot be trusted unattended — a
  broker-side block can pass every readiness check and only surface when
  the order itself is placed.**
  Sessions: 6 Aug (n=1 — KIMS + TRAVELFOOD SELL both blocked by IP
  allowlist; TRAVELFOOD's give-back guard re-fired 4x over 20 minutes
  without landing).
  Confidence: Low-medium — one session, but the failure mode matches a
  known category (`deploy/validate_server.py`'s own warning about
  ephemeral Oracle IPs).
  Next evidence needed: whether this recurs on sessions with a
  confirmed-stable IP, to separate "one-off network hiccup" from
  "allowlist genuinely needs a standing pre-market check."

---

## Ideas Requiring More Evidence

*(Too early to have a confidence level at all — tracked so tomorrow's
review knows to keep watching, not to re-derive from scratch.)*

- **SDN (short family) engine viability.**
  Sessions: 6 Aug (n=2 trades, both losses — first day live).
  Needed before any verdict: ~20 resolved outcomes across ≥10 sessions
  (this project's own `MIN_SAMPLE`/`MIN_SESSIONS` standard from
  `tools/weekly_review.py`).

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
