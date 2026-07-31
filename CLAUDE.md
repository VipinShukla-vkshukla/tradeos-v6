# TradeOS v6 — read this before answering anything

Claude Code loads this file automatically at the start of every session in this
repository. It exists so a new conversation starts with the design rather than
re-deriving it, and so advice never contradicts decisions already made and paid
for.

**Read `USER_GUIDE.md` for how the system behaves, and `DESIGN_NOTES.md` for what
is decided but unbuilt.** This file is the operating context around both.

---

## What this is

An Indian equity trading system with **two separate frameworks** sharing one
₹20,000 account:

| | Swing | Intraday |
|---|---|---|
| Horizon | 1–3 weeks | flat by 15:15 IST |
| Mode | **LIVE** — real orders | **PAPER** — simulated fills |
| Product | CNC (never MIS) | MIS |
| Universe | ~60 plans from the evening pipeline | 40 names, own scanner |
| Exits | `control/position_lifecycle.py` | `intraday/exit_policy.py` |

They are deliberately **not** unified. Different horizons need different rules,
and one policy applied to both is how a 15-session time stop ended up on a trade
that must be flat by 15:20.

## The operator

Trades their own capital, is not a programmer, and reads every explanation. They
ask precise questions and have repeatedly been right when they pushed back —
the (symbol, product) key, the alert-noise problem and the ranking gap were all
their calls. Take their domain instinct seriously; they know Indian markets and
Zerodha better than the codebase does.

They want to know **why**, not just what. An answer that skips the reasoning
gets asked for the reasoning.

---

## Rules this project runs on

These are earned, not stylistic. Each one has a failure behind it.

**Verify, never assert.** "It should work" has been wrong repeatedly here. Run
it, read the output, then say what happened. Six exit-path bugs surfaced in one
day in July and every one was found by running something, not by reading it.

**A check that cannot fail is not a check.** Four separate health checks were
found reporting green while the thing they watched was broken — a GTT check that
could not see, a config check that read keys it never fetched, a quality audit
checking its own output, and a token check reading a key that did not exist.
Test that a check FAILS when it should.

**Silent defaults are the enemy.** A config key nobody reads, a column nobody
writes, a step that completes producing nothing — this is the dominant failure
mode. `tools/validate_config.py` and `tools/health.py` exist because of it.

**Propose, never auto-apply.** Learning tools write to `brain_proposals` and
change nothing. A system that re-tunes itself becomes one nobody can audit.

**Money moves only behind explicit switches.** Entries need two (`*_auto_entry`
AND `*_live_auto_entry`). Never widen a gate to make something work.

**Own mistakes plainly.** Several were made and corrected in-session. Say what
broke, what it cost, and what was restored. Do not bury it.

---

## Before changing anything

```bash
cd backend && python -m tools.health        # 7 checks, is anything broken
cd backend && python -m tools.simulate      # what BOTH books would do, writes nothing
```

After changing anything that touches positions, orders or reconciliation, run
both again. `simulate` is read-only and safe at any time.

## Landmines, learned the hard way

- **`open_positions` is keyed on `(symbol, product)`** since migration 028. Never
  upsert on `symbol` alone. Use `control.position_lifecycle._upsert_position`.
- **Reconcile must read day positions**, not just holdings. A CNC buy is not in
  `holdings()` until T+1; treating it as sold closes a position bought that
  morning. It must also skip PAPER rows.
- **Record a fill immediately.** An order placed and not written back is
  re-derived next cycle and placed again. PPLPHARMA sold twice this way.
- **Kite's order allowlist is IPv4 only.** `config._force_ipv4()` exists because
  a v6 source address rejects every order while every readiness check passes.
- **PostgREST caps responses at 1000 rows silently.** Page anything larger.
- **Migrations run against a live book.** Verify preconditions first.

## Where things live

```
backend/intraday/engine.py       the 15s loop — manages BOTH books
backend/control/position_lifecycle.py   swing exits, reconcile, close
backend/execution/               gates, order_manager, gtt_manager, paper_broker
backend/analysis/entry_ranking.py       which swing plan deserves the entry
backend/tools/                   health, simulate, validate_*, weekly_review,
                                 discover_engines
tradeos.cmd                      the one-click launcher (menu + subcommands)
```

## Communication

Lead with what the operator asked. Show the evidence — actual command output,
not a description of it. Flag anything found along the way that costs money,
even when unasked; that is how most of the serious bugs here were caught.

If a claim cannot be verified in this session, say so explicitly rather than
implying it was checked.
