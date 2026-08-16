"""
Historical replay harness. A NEW TOOL — it changes nothing it replays.

Implements `docs/REPLAY_DESIGN.md`. Read that first; this package is the
mechanism, that document is the argument.

THE ONE RULE THIS PACKAGE IS BUILT AROUND
------------------------------------------
The harness recomputes everything it reports from bars and imported engine
code. It does not read the system's own record of what it decided, because
twelve sessions of this ledger have shown those readers cannot be trusted (the
scorecard reader paging with `.range()` and no `.order()`; `check_selects`
printing a pass over its own error output). A replay that reads the pipeline's
conclusions is replaying its conclusions.

The forbidden names are listed once, in `independence.py`, and nowhere else in
this package — including in prose. That is not fussiness: the check is a literal
grep, and a literal grep is the only kind that cannot be talked out of a match.
Keeping comments clear of the names is a cheaper price than a check clever
enough to tell a comment from a read.

`independence.py` enforces that with a static scan, and the scan is registered
in `tools/verify.py` so it runs on every change rather than when someone
remembers.

WHAT IS COPIED AND WHY, RATHER THAN IMPORTED
---------------------------------------------
Three things are deliberately copied into this package rather than called:

  universe.py       `scanner.build_universe` resolves its own date internally
                    (`_latest_date`), so it cannot be pointed at history.
  swing_inputs.py   `screen_stocks.load_data` is not point-in-time on five of
                    its nine reads — fatal in replay, harmless in production
                    where `today` really is today.
  outcomes_port.py  the resolver loop is welded to Supabase I/O inside
                    `resolve_day` and cannot be called without writing.

**Production is untouched in all three cases.** That is the instruction this
package was built under and it is also the safer engineering: a point-in-time
fix applied to `screen_stocks` would change what the live pipeline screens
tonight, to serve a backtest.

Everything else is IMPORTED, never reimplemented — the nine engines, the
invalidation filter, both exit ladders, the direction arithmetic, the cost
model. A replay of a reimplementation measures the reimplementation.
"""
