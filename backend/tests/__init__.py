"""
Offline verification — the checks that would otherwise be rewritten every session.

WHY THIS DIRECTORY EXISTS
-------------------------
Every defect fixed in the shorting, allocator and terminology work was verified
by a script written in a scratch directory and thrown away when the session
ended. The next session that touched the same code rewrote them from scratch —
several hundred lines each time, with the same correction rounds — and in
between there was no regression net at all. Two defects shipped through that
gap: `getattr(st, "minutes_to_close")` in the short runway gate, and the same
pattern preserved in the allocator's own call.

So these are the same checks, kept.

WHAT MAKES THEM CHEAP ENOUGH TO RUN EVERY TIME
-----------------------------------------------
No database, no broker session, no network, no clock dependence. Every test
here is pure arithmetic over in-memory objects, which is possible because the
decision logic in this system is already written that way — `evaluate_exit`,
`score`, `is_worth_taking`, `classify`, `regime_bucket` and the engines are all
pure functions over data. That is not luck; it is the property that made them
testable in the first place, and it is worth protecting.

A test that needs the live book is NOT welcome here. It belongs in
`tools/health.py`, which is the thing that asks the database questions.
The division is: health checks the RUNNING SYSTEM, tests check the LOGIC.

HOW TO ADD ONE
--------------
Expose a module-level `TESTS` list of `(name, fn)`. `fn` takes no arguments and
raises `AssertionError` (or anything) on failure; returning normally is a pass.
Register the module in `tools/verify.py::MODULES`.

Use `cfg_ctx()` for anything that reads `system_config` — it saves and restores
the process-wide cache, so one test's configuration cannot leak into the next.
That leak is a real hazard here: `config._sys_config` is a module global and
several of these tests deliberately set switches the others assume are off.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

# tests/ -> backend/. Resolved from this file rather than hardcoded, so the
# suite runs from any working directory and on any machine.
BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@contextmanager
def cfg_ctx(values: dict | None = None):
    """
    Run with a known system_config, then put back whatever was there.

    `config._sys_config` is a process-wide cache. Setting it in one test and
    not restoring it silently changes what every later test reads — and the
    tests here deliberately disagree about switches (one asserts shorts are OFF
    by default, another needs them ON to exercise the engine). Without this,
    the suite's result would depend on the order it happened to run in.
    """
    import config
    saved = config._sys_config
    config._sys_config = dict(values or {})
    try:
        yield
    finally:
        config._sys_config = saved


def approx(a: float, b: float, tol: float = 1e-12) -> bool:
    """Float equality with an explicit tolerance, so a failure names the bound."""
    return abs(a - b) <= tol
