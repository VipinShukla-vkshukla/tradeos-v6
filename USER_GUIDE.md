# TradeOS v6 — User Guide

Last updated: **29 July 2026** · Account capital: **₹20,000**
Swing: **LIVE** (auto-exit on) · Intraday: **PAPER**

This is the reference for how the system behaves. If something surprises you,
find the feature here first — every section says what a thing does, what value
controls it, and which file actually reads that value.

> **The rule this guide follows:** if a setting is described here, code reads it.
> A setting that appears configured but is not read is the failure mode this
> project has hit repeatedly, so `python -m tools.validate_config` checks that
> claim on every run. If you change behaviour, update this guide in the same
> commit.

---

## 1. Your day, start to finish

Double-click **`tradeos.cmd`**. It asks one question:

```
   1   Both frameworks          swing + intraday  (default)
   2   Swing only               intraday stands down
   3   Intraday only            swing automation stands down

   4   Check readiness          start nothing
   5   Show current status
```

Press Enter for both. Everything after that is automatic:

| What it does | Why it matters |
|---|---|
| Applies your choice | Writes to `system_config`, so the **Oracle server daemon obeys it too** |
| Preflight checks | Refuses to start on a broken config rather than trading on one |
| Public IP check | Kite rejects orders from non-allowlisted IPs; this tells you before the market does |
| Kite login | The access token expires **daily at 07:30 IST** — there is no way around this |
| Opens the dashboard | http://localhost:3000 — serves both books, so it starts either way |
| Starts the intraday daemon | Only if intraday was selected |

Then you watch Telegram/Discord and the dashboard. You do not need to touch
anything else during the day.

**Why the choice writes to the database rather than just skipping a step.** The
intraday daemon also runs on the Oracle server. If "swing only" merely skipped
starting the local daemon, intraday would keep trading from the server while the
menu told you it was off — a switch that reports a state it does not produce.
So the unselected framework is turned off everywhere, and the selected one is
turned back on (in its existing mode) so yesterday's choice does not silently
persist.

**Paper/live is never changed by this prompt.** That is a rarer, more
consequential decision — `tradeos swing live` — and folding it into a daily
routine is how it gets made by accident.

### Other commands

```bash
tradeos check
```
Verifies readiness and starts nothing. Use this when you want to know if the
system *would* work without committing to a session.

```bash
tradeos status
```
What is live, what is paper, what is off.

```bash
tradeos ip
```
This machine's public IP, and whether it matches the one recorded for the Kite
allowlist. Zerodha permits **two** IPs — one for this laptop, one for the daemon
server.

```bash
tradeos both
```
Runs both frameworks without the prompt — same as choosing 1.

```bash
tradeos server
```
Validates the Oracle daemon server. Run it **on the server**; on Windows it
prints the ssh command instead, because it records the public IP it sees as the
server's and running it at home would poison that.

```bash
tradeos stop
```
Sets the master kill switch. Everything stops trading immediately. Both
frameworks, entries and exits alike.

```bash
tradeos evening
```
Runs the swing pipeline by hand (it normally runs on its own schedule).

```bash
tradeos swing paper
```
Mode changes are deliberately **separate** from the daily launch. You launch
every morning but promote a framework to live once, and folding a rare,
consequential decision into a daily routine is how it gets made by accident.
Accepts `paper`, `live`, or `off`; `tradeos intraday …` is the same for the
other framework.

---

## 2. The risk numbers — and why each one is what it is

**All of these are checked against your ₹20,000 by `tools/validate_config.py`.**
Run it any time you change capital or a cap:

```bash
cd backend && python -m tools.validate_config
```

It reports `0 incoherent` when the numbers tell one story. `--fix` applies its
suggestions.

### What sizing actually produces

| Framework | Formula | On ₹20,000 |
|---|---|---|
| **Swing** | `min(risk budget, max_position_pct)` — `analysis/risk_model.py` | ₹4,000 max position, ₹200 risked |
| **Intraday** | `TOTAL_CAPITAL × intraday_max_position_pct × market multiplier` — `intraday/engine.py` | ₹5,000 risk-on, ₹3,000 neutral, ₹0 risk-off |

Swing sizes by **risk parity**: every trade risks the same 1% of capital, so a
wide-stop setup gets fewer shares than a tight-stop one. This is what makes the
expectancy arithmetic work — sizing by equal rupee value instead means a 9%-stop
trade loses three times what a 3%-stop trade loses, and your average loss drifts
upward independently of signal quality.

### The caps, and what each one blocks

| Key | Value | What happens when it binds | Read by |
|---|---|---|---|
| `TOTAL_CAPITAL` (.env) | ₹20,000 | The base every other number derives from | `config.py` |
| `risk_pct_per_trade` | 1.0 | Risks ₹200 per swing trade | `analysis/risk_model.py` |
| `max_position_pct` | 20 | Caps a swing position at ₹4,000 | `analysis/risk_model.py`, `portfolio_constraints.py` |
| `intraday_max_position_pct` | 25 | Caps an intraday position at ₹5,000 | `intraday/engine.py` |
| `swing_max_order_value` | ₹6,000 | **Rejects a swing BUY** above this | `execution/gates.py` → `order_manager.preflight()` |
| `intraday_max_order_value` | ₹6,000 | **Rejects an intraday BUY** above this; also **clamps intraday sizing** | `gates.py`, `intraday/engine.py` |
| `swing_max_orders_per_day` | 4 | Blocks further swing **entries** that day | `order_manager.preflight()` |
| `intraday_max_orders_per_day` | 5 | Blocks further intraday **entries** that day | `order_manager.preflight()` |
| `swing_max_notional_per_day` | ₹20,000 | Blocks entries once swing has committed the account | `order_manager.preflight()` |
| `intraday_max_notional_per_day` | ₹20,000 | Same, for intraday | `order_manager.preflight()` |
| *(account guard)* | ₹20,000 | Blocks entries once **both books combined** reach capital | `order_manager.preflight()` |
| `paper_starting_capital` | ₹20,000 | Paper simulates the account you actually have | `execution/paper_broker.py` |
| `paper_max_open_positions` | 4 | 4 × ₹5,000 fits inside the paper account | `paper_broker.capacity()` |

**Why the per-order caps sit at ₹6,000 and not higher.** A cap exists to catch a
**sizing bug**, not to shape normal trades. Sizing tops out at ₹4,000 (swing) and
₹5,000 (intraday), so ₹6,000 never blocks a legitimate order but catches anything
that computes wrong. The old value was ₹25,000 on a ₹20,000 account — **125% of
everything you have**, which can never bind and therefore protected nothing.
Every sizing *input* in this system (capital, risk percent, ATR) has been wrong
at some point; the rupee ceiling is the one control that does not depend on any
of them being right.

**Why the daily notional is ₹20,000 and not more.** It stops the account being
recycled repeatedly into a bad day. Four swing orders at the ₹6,000 cap would be
₹24,000 — more than you have — so the daily figure clamps it to the account.

**Why there is a combined account guard.** Swing and intraday each have their own
₹20,000 daily allowance, and they spend the **same account**. Without a combined
check, both staying inside their own limits could still commit ₹40,000 in a day.

**Worst case the caps permit, today:** ₹20,000 across both books. That is the
account, once. It cannot exceed it.

**Exits are never blocked by these caps.** Every cap above applies to **BUY**
only. An exit's quantity is not computed from capital — it is what you already
hold, verified against broker holdings — so capping it would mean a position
that grew past ₹6,000 could not be closed. Being unable to reduce risk is a
strictly worse failure than being unable to add it.

### Costs, and the floor they put under intraday

Zerodha charges **₹20 or 0.03% per order, whichever is lower**, plus statutory
charges — roughly **0.21% round trip**. Because it is percentage-based at your
size (0.03% of ₹5,000 is ₹1.50, well under ₹20), small positions are **not**
penalised. But it means an intraday target under about **0.7%** is not worth
taking: costs eat it. `intraday/cost_model.py` enforces this via
`is_worth_taking()`, and you will see setups rejected with `BLOCKED cost`.

---

## 3. Paper vs LIVE

Both frameworks are independently PAPER or LIVE. They are at genuinely different
stages — swing has traded real money, the intraday engines never have — and
forcing them into one mode would mean either risking capital on untested engines
or freezing a book that already works.

|  | PAPER | LIVE |
|---|---|---|
| Decisions | Identical | Identical |
| Fills | Simulated at realistic slippage | Real orders to Zerodha |
| Charges | Modelled | Real |
| Recorded in | `open_positions` with `mode='PAPER'` | same table, `mode='LIVE'` |

**PAPER is the default for anything not explicitly set to LIVE.** A framework
that reaches production by *forgetting to configure it* has not earned it.

### How paper makes the system learn

Paper trades run through the **same** exit engine, the same attribution, the same
R-multiple and MFE/MAE recording as live ones. That is what makes them useful:
the paper book accumulates complete round trips, and `signal_outcomes` gets the
same feedback it would from real money. Because `paper_starting_capital` now
matches your real ₹20,000, the results **transfer** — paper is no longer taking
positions you could not fund.

Your dashboard shows PAPER and LIVE side by side, so you can compare directly.

---

## 4. The switches, and where to set them

**Dashboard → Operator Controls** is the full set, and it writes straight to
`system_config` — the same row Python reads. There is no cache and nothing to
sync: **what you set today is what tomorrow's daemon reads.** The panel re-reads
after every write, because "I set it" and "it is set" are different claims.

| Switch | Effect |
|---|---|
| `master_kill_switch` | Stops everything, both frameworks. Needs a second click to clear |
| `swing_trading_mode` / `intraday_trading_mode` | PAPER or LIVE. Going LIVE needs a second click |
| `swing_auto_exit` / `intraday_auto_exit` | Whether exits execute without asking |
| `swing_auto_entry` / `intraday_auto_entry` | Whether entries are taken at all |
| `swing_live_auto_entry` / `intraday_live_auto_entry` | Whether auto-entry may spend **real** money |
| `intraday_autonomy_phase` | 2.0 monitor · 2.5 broker-side stops · 3.0 order placement |
| `intraday_orders_enabled` | Fast off-switch that does not require re-reasoning about phases |
| `intraday_gtt_enabled` | Resting broker-side stops that survive this process dying |
| `intraday_strategies_enabled` | Master switch for the intraday engines |
| `intraday_structure_gate` | Blocks setups whose market structure is wrong |
| `intraday_news_gate_enabled` | Blocks setups with an event risk |
| `exit_runners_enabled` | Lets strong trends run past target |
| `exit_deterioration_enabled` | Exits on trend breakdown before the stop |

**Why entry needs two switches.** Promoting a framework from paper to live must
not silently also promote "simulate an entry" into "buy something". So
`*_auto_entry` says whether setups are taken, and `*_live_auto_entry` says
whether that may spend money. Both must be on.

> **Live auto-entry is intentionally not implemented for intraday.** Committing
> capital on a single live tick, without the evening pipeline's context, is the
> highest-variance action this system can take. With intraday LIVE and
> `intraday_live_auto_entry` on, you get an alert and place the order yourself.

---

## 5. How a swing trade is decided

1. **Evening pipeline** screens the universe, scores setups, and writes
   `signal_output_daily`.
2. `analysis/trade_decision.py` → `decide()` returns `BUY_NOW`, `CHASE_LIMIT`,
   `WAIT`, or `SKIP`. The dashboard, Telegram digest, and paper entry all call
   the **same** function — if they ever disagree about a symbol, one is broken.
3. Portfolio constraints apply (sector caps, position limits, minimum size).
4. In paper mode, `control/paper_entry.py` takes what came back buyable.

### Exits — how profits get protected

`control/position_lifecycle.py` and `control/exit_rules.py`, evaluated in order
of how expensive it is to be wrong:

| Stage | Default | What it does |
|---|---|---|
| Stop | planned stop | Exits |
| Partial book | `exit_partial_book_r` = 1.5R | Books `exit_partial_book_pct` = 50% |
| Breakeven | `exit_move_to_breakeven` = on | Stop to entry after the partial |
| Trail | starts `exit_trail_after_r` = 2.0R | Trails at `exit_trail_r` = 1.5R |
| Target | `exit_target_r` = 3.0R | Exits — **unless the runner logic says otherwise** |
| Time stop | `exit_time_stop_days` = 15 | Exits if below `exit_time_stop_min_r` = 0.5R |
| Deterioration | `exit_deterioration_enabled` | Exits on trend breakdown above 1.0R |

**Runners — staying invested when the setup deserves it.** At target,
`assess_trend()` scores the position STRONG / INTACT / FADING / BROKEN from RSI,
ADX, volume ratio and sector rank. STRONG (≥0.75) lets it **run** past target
with a 2.0R trail; at most `exit_max_runners` = 2 at a time. This is the
mechanism that answers "stay invested if the setup has high potential" without
turning every trade into a hold.

---

## 6. How an intraday trade is decided

The daemon cycles every 15 seconds:

1. **Universe** refresh, then live contexts.
2. **Market context** (`intraday/market_context.py`) reads the index and returns
   a permission flag and size multiplier: risk-on ×1.0, neutral ×0.6,
   **risk-off ×0.0** — which means no new intraday positions at all.
3. **Engines** evaluate every symbol. Each returns a setup with entry, stop,
   target, R:R and confidence.
4. **Gates**, in order — you see each one in the logs:
   - **event** — `intraday/news_gate.py`
   - **structure** — `analysis/market_structure.py`
   - **cost** — `intraday/cost_model.py`
5. Survivors are sized, alerted, and (in paper) taken.

### Price action: down-move then higher highs

`analysis/market_structure.py` reads the **sequence** of fractal pivots, not just
the level. States: `UPTREND`, `DOWNTREND`, `REVERSAL_UP`, `CONFIRMED_UP`,
`RANGE`, `UNKNOWN`.

A price falling and then breaking a prior high is `REVERSAL_UP` — **blocked by
default**, because a first higher high inside a downtrend fails often. Once a
higher **low** also forms, it becomes `CONFIRMED_UP` and is takeable.

**The parameters differ by framework, deliberately:**

| | pivot_k | tolerance | lookback |
|---|---|---|---|
| SWING | 2 | 0.50% | 60 bars |
| INTRADAY | 3 | 0.15% | 120 bars |

Intraday needs a stricter pivot (3 bars either side) and a tighter tolerance
because intraday noise would otherwise manufacture pivots that are not there.
To allow reversals for one framework, set `structure_swing_allow_reversal` or
`structure_intraday_allow_reversal` to 1.

### Intraday exits

`intraday/exit_policy.py`, in order:

1. **Session end** — square-off phase reached.
2. **`intraday_must_exit_time`** = 15:15 — a hard wall-clock deadline.
3. **`intraday_squareoff_buffer_min`** = 12 — exit 12 minutes before the close,
   on our terms rather than the broker's. Whichever of 2 and 3 comes first wins.
4. **Stop.**
5. **Invalidation** — `intraday_check_invalidation`. Deliberately **above** the
   target check: a setup whose structure has broken does not deserve to be held
   for its target. This is the rule that cuts losses *before* the stop.
6. **Partial book** at `intraday_partial_book_r` = 1.2R, 50%.
7. **Breakeven**, then **trail** at 1.0R after 1.5R.
8. **Target** at the setup's own target (`intraday_use_setup_target`).
9. **Time stop** — `intraday_time_stop_minutes` = 75, if below 0.3R.

`intraday_product` = CNC. **MIS is available but adds leverage *and* a
broker-forced square-off.** If you switch to MIS, note that Kite GTTs are
CNC/NRML only — intraday stops would have to come from the live loop, not a
resting GTT. Swing is never MIS regardless of this setting.

---

## 7. Alerts

Swing and intraday have **separate** bots and webhooks, so an intraday scalp
alert never lands in the middle of your swing digest:

| | Telegram | Discord |
|---|---|---|
| Swing | `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | `DISCORD_WEBHOOK_URL` |
| Intraday | `TELEGRAM_INTRADAY_BOT_TOKEN` / `TELEGRAM_INTRADAY_CHAT_ID` | `DISCORD_INTRADAY_WEBHOOK_URL` |

Paper alerts are prefixed `[PAPER]` and say plainly that no real order was placed.

---

## 8. When something looks wrong

### "Is the machinery actually wired?"

```bash
cd backend && python -m tools.simulate
```

Runs both frameworks end to end against live data and **changes nothing**. It
prints what each stage produced rather than whether it completed — which catches
the failure this project keeps hitting: decisions that never ran. A filter
returning zero rows without raising, a monitor watching nothing, a column read
that was always NULL. None of those show up in a unit test.

Add `--phase PRIME` to evaluate as if the session were mid-morning; prices stay
real, only the clock is simulated. Useful outside market hours.

### "Are the numbers coherent?"

```bash
cd backend && python -m tools.validate_config
```

Also confirms every CRITICAL key is read by at least one module.

### "Are my database reads valid?"

```bash
cd backend && python -m tools.validate_selects
```

### Orders are rejected: "No IPs configured for this app"

Your public IP is not on the Kite allowlist. Run `tradeos ip`, then add it at
https://developers.kite.trade/apps. **Zerodha allows two IPs** — laptop and
daemon server. Until this is done, live exits cannot be placed.

### Kite says the token is valid but calls fail

The token may belong to a retired app. `start_day.py` probes `/user/profile`
rather than trusting the validity flag.

### A setting on the dashboard does not seem to do anything

Run `validate_config` — it will tell you if the key is unread. If the key is
read but the value is not what you set, check that the write actually landed:
the config API returns 404 if no row matched, rather than reporting success.

### The daemon runs in two places

Only one acts. `intraday/lease.py` holds a singleton lease — one instance is
ACTIVE, the other STANDBY, and the standby takes over if the active one stops
renewing. Two daemons both trading would double every position.

---

## 9. The Oracle daemon server

The intraday daemon runs on an Oracle Cloud Always Free VM so it has a **fixed
IP**. Zerodha requires an IP allowlist for order placement, and home broadband
rotates its address — the failure surfaces mid-session when a stop needs acting
on. You have **two allowlist slots**: one for home, one for the server.

| Where | What runs there |
|---|---|
| Laptop | Kite login, dashboard, evening pipeline, manual entries |
| Server | Intraday daemon: engines, monitoring, exits, GTT sync |

Both read and write the same Supabase, so neither has a private view of the book.

### Validating it

```bash
ssh -i your-key.pem ubuntu@<SERVER_IP>
cd ~/tradeos-v6 && .venv/bin/python deploy/validate_server.py
```

Nothing is inspected — every check makes the **actual call the daemon makes**. If
it passes, the daemon works, because it has just done the same things. It covers:

| Check | Why it is not obvious |
|---|---|
| Timezone is IST | On UTC every session-phase decision is wrong by 5h30m |
| Clock skew | Moves the square-off deadline and the token expiry check |
| `.env` present, required keys set | `KITE_ACCESS_TOKEN` is **not** needed — that comes from Supabase |
| Outbound 443 to Supabase, Kite REST, **Kite WebSocket**, Telegram, Discord | A VCN egress rule that blocks one of these is invisible until 09:20 |
| **WebSocket held open 10s** | A rule permitting the handshake but dropping idle flows passes a port test and fails in the session |
| Supabase **writable** | The singleton lease is a write; without it two daemons both act and double every position |
| Kite token is today's, and works from this IP | A token from a retired app reports valid and fails on the first real call |
| Public IP vs the recorded one | Catches an ephemeral IP that rotated out of the allowlist |
| systemd service + timer | The service being `disabled` is correct — the timer starts it |
| Kill switch, mode, engines, capital | A daemon that starts, finds nothing, and looks healthy |

### The one thing the script cannot check

**Whether your public IP is Reserved or Ephemeral.** That lives only in the
Oracle console, and it is the most expensive thing to get wrong: an ephemeral IP
changes when the instance is stopped and started, silently invalidating your Kite
allowlist entry. Orders then fail with *"No IPs configured for this app"* — the
error you already hit once.

Check it at **Compute → Instances → your instance → Attached VNICs → IPv4
Addresses**. If it says *Ephemeral*, convert it to *Reserved*.

The script records the IP each run and shouts if it changed, so an ephemeral IP
is at least detectable after the fact. Reserving it prevents the problem instead.

### Two daemons is not redundancy

If you run the daemon at home **and** on the server, `intraday/lease.py` ensures
exactly one is ACTIVE — the other stands by and takes over only if the active one
stops renewing (TTL 120s). Without that lease, both would act and every position
would be doubled.

If the server is down the daemon simply stops. Positions keep their broker-side
GTT stops, which is precisely why those exist.

---

## 10. Quick reference

```bash
tradeos
```
Start the trading day.

```bash
cd backend && python -m tools.validate_config
```
Check the risk numbers cohere and every critical key is read.

```bash
cd backend && python -m tools.simulate
```
Dry-run both frameworks against live data.

```bash
cd backend && python control_panel.py
```
Show what is live, paper, and off.

---

## Change log

Update this section whenever behaviour changes.

**29 July 2026 — later**
- `tradeos.cmd` double-click now **asks** swing / intraday / both, and the choice
  writes to `system_config` so the Oracle server daemon obeys it too. Added
  `start_day.py --only`, plus `tradeos both` and `tradeos server`.
- Added `deploy/validate_server.py` — proves the server can run the daemon by
  making the calls the daemon makes, including a 10-second WebSocket hold that a
  plain port test would pass and a stateful egress rule would fail.

**29 July 2026**
- Rebased every cap on ₹20,000 (migration `024_risk_coherence.sql`). Per-order
  caps ₹25,000/₹10,000 → ₹6,000; daily notional ₹50,000/₹25,000 → ₹20,000;
  paper capital ₹100,000 → ₹20,000; paper positions 5 → 4.
- Added `intraday_max_position_pct` (25%). Intraday sizing previously used the
  **entire account** for one setup on a risk-on day.
- Added a **combined account notional guard** — both books share ₹20,000/day.
- Fixed: `preflight()` never received `framework`, so **every swing order was
  checked against the intraday caps**. `swing_max_order_value` was configured,
  shown on the panel, and never consulted.
- Added `framework` to `intraday_broker_log` so daily spend is attributable.
- Wired four keys that nothing read: `intraday_auto_entry`,
  `intraday_live_auto_entry`, `intraday_must_exit_time`, `intraday_product`.
- `validate_config.py` now resolves f-string config keys, so dynamically built
  keys are not reported as unread.
- Per-order caps now apply to **BUY only**. Tightening them to ₹6,000 would
  otherwise have blocked the exit of any position that grew past that.
