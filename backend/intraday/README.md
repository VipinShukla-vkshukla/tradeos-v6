# TradeOS Intraday

Continuous, event-driven monitoring during market hours. Replaces the fixed
30-minute digest. Isolated under `backend/intraday/` — the evening pipeline and
morning brief run identically whether or not this is running, and deleting this
folder restores the previous behaviour with no other change.

## Run it

```bash
cd backend
python -m intraday.run --status      # gates + watch list, no side effects
python -m intraday.run --once --dry  # one cycle, no broker writes
python -m intraday.run               # daemon; exits at 15:40 IST
```

Prerequisites: migration `011_intraday_subsystem.sql`, and a Kite session for
the day (the dashboard's Connect button, or `python -m kite.token_manager
--login-url`).

## Why event-driven, not every 30 minutes

A fixed interval gets both halves wrong at once. It is too slow for anything
urgent — a stop breached at 09:31 waits until 10:00 — and too noisy for
everything else, repeating the same non-event at 10:00, 10:30 and 11:00.
Frequency was being tuned as a compromise between those two failures rather than
removing their shared cause: **time is the wrong trigger.**

The right trigger is a change in what you should do. `HOLD → BOOK_PARTIAL` is
worth interrupting you for; `HOLD → HOLD` is not, at any interval.

So the loop evaluates every 15s and notifies on transition. Silence means
nothing changed. A heartbeat every 15 minutes makes that silence trustworthy —
without it, a daemon working correctly and a daemon that died at 09:20 look
identical from outside.

## Phases

Set `intraday_autonomy_phase` in `system_config`. Every gate ships **off**.

| Phase | Adds | Extra switch required |
|---|---|---|
| **2.0** (default) | Monitor + notify. No broker writes at all. | — |
| **2.5** | GTT stop-loss orders resting at Zerodha | `intraday_gtt_enabled=true` |
| **3.0** | Automatic order placement | `intraday_orders_enabled=true` **and** `DRY_RUN` off |

Raising the phase alone never enables anything — each level needs its own
boolean. That is not redundancy: the phase is a deliberate promotion, the
boolean is a fast off-switch that does not require re-reasoning about phases,
and `DRY_RUN` keeps the idiom used everywhere else in this codebase working
here, where it matters most.

### Phase 2.5 — why GTT matters

Until now every stop was notional: a number in a Postgres row that only became
an action if a Python process happened to be running, held a valid session, and
got a price. That process ran **once a day, at 13:00**.

A GTT rests at Zerodha. It fires whether or not this laptop is on, whether or
not the token expired, whether or not the daemon crashed. It turns the stop from
an intention into a commitment.

The engine still runs, because a GTT trigger is a static price and cannot
express "trail 1.5R below the high-water mark" — which is where most of the exit
edge lives. The two are layered:

- **GTT** — the hard floor, surviving everything
- **engine** — ratchets the stop, books partials, times out

`gtt_manager.sync()` reconciles them every 5 minutes. Stops are only ever
**ratcheted up**; if the engine's intended stop is below what is already resting,
the resting one is left alone. Loosening a stop is never an improvement, and a
bug that computes a lower stop must not be able to widen real risk.

No OCO target leg: the exit policy books *half* at 1.5R and lets the rest run,
which a two-leg GTT cannot express. The stop is worth resting at the broker; the
target is worth managing.

### Phase 3 — the rails

Every order passes `preflight()`, which checks things that "cannot" be wrong,
because being wrong costs real money and a redundant check costs microseconds:

- kill switch, phase, `DRY_RUN`, market genuinely open
- **hard rupee cap per order**, independent of what sizing computed
- daily order-count and daily-notional caps
- SELL never exceeds quantity actually held at the broker
- BUY never exceeds available cash
- no duplicate same-symbol/side order within 5 minutes

The caps are the important part. Sizing bugs are what empty an account, and
every sizing input here — capital, risk percent, ATR — has been wrong at some
point in this project's history. A hard ceiling is the one control that does not
depend on any of them being right.

Two asymmetries are deliberate:

- **Exits may consume the last of the daily budget; entries may not.** Being
  unable to reduce risk is strictly worse than being unable to add it.
- **Entries are never auto-placed by this loop**, even at Phase 3. Committing
  new capital on a live tick, without the evening pipeline's full context, is
  the highest-variance thing this system could do. `intraday_auto_exit` covers
  exits only.

## Storage

Supabase free tier is 500 MB. Streaming 40 symbols at ~1 tick/sec over a
6.25-hour session is ~900,000 ticks/day — about **90 MB/day**, the entire quota
inside a week, for data nobody will ever query.

So: **store decisions, never observations.**

| | Written? | Volume |
|---|---|---|
| Ticks | No — memory only | — |
| Evaluations (~1,500/position/day) | No | — |
| Alerts actually sent | Yes | tens of rows/day |
| Broker writes (incl. blocked/failed) | Yes | a few rows/day |

Under 100 KB/month. `prune_intraday()` trims alerts on a 45-day window; the
broker log is kept ~400 days because it is a financial record and it is tiny.

Blocked and failed broker writes are logged deliberately — a log of only
successes cannot answer "why did nothing happen at 10:42", which is the question
that actually gets asked.

## Failure modes, and what happens

| Failure | Behaviour |
|---|---|
| Websocket drops | Falls back to REST polling every 30s; `feed.source` says which |
| Kite session expires | Prices fall through to yfinance (~15 min delayed), flagged as such |
| Supabase unreachable | Chat alerts still send; dashboard write is best-effort |
| Telegram rate-limited | Discord and the dashboard row still land |
| Daemon dies | GTT stops still fire at the broker (Phase 2.5+); heartbeat goes stale |
| Kill switch flipped | Loop exits at the next tick |

## Not 24/7, deliberately

NSE equities trade 09:15–15:30 Mon–Fri: 31 hours of 168. Outside them there are
no ticks to react to, so a 24/7 loop would hold a session it cannot use and
write rows describing nothing. The Kite token expires daily at 07:30 regardless,
so continuous running does not remove the one manual step in the day.

It would also quietly **break** the point-in-time guarantee.
`signal_output_daily` is keyed `(date, symbol)` and upserted, so a second run on
the same day overwrites the first. The immutable daily snapshot is a property of
running once per day — not something more frequency improves. If intraday
history is ever wanted, add append-only `(date, symbol, run_ts)` tables rather
than re-running the snapshot.
