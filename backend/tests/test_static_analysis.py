"""
No undefined name reaches production silently again.

WHAT THIS CATCHES
------------------
Three real bugs were found in one afternoon (07-Aug-2026) by running
pyflakes over the whole backend, none of them found by reading code or by
seeing something fail — all three were swallowed by a broad `except
Exception` and never logged above DEBUG, or not logged at all:

  · intraday/engine.py::apply_live_quotes — `now` referenced, never assigned.
    Silently meant intraday_quote_mode had ZERO effect from the day it was
    turned on, three days of live sessions, discovered only via a live SQL
    trace showing the parity table it also should have written had zero rows.
  · swing/compute/compute_msl.py — `screener_rows`, a name with no producer
    anywhere in the file, referenced inside a "shadow mode" branch. Reachable
    via the code's own default AND a documented CLI flag; not triggered in
    production only because compute_msl_mode has read "full" since 11-Apr.
  · alerts/send_alerts.py — `datetime` (shadowed by a local `_dt` alias) and
    `IST` (never imported in that scope) referenced while building the "N
    days old" annotation on stale AI advisories — silently caught by a bare
    `except Exception: pass`, so the exact feature built to stop a stale
    advisory being read as current had never once fired.

Static analysis catches this WITHOUT needing to execute the crashing code
path — which is exactly why manual reading and even targeted tests kept
missing it: the branch has to actually run to be seen failing at runtime, and
a below-floor-probability branch (a retired mode, a rarely-hit combination of
switches) can sit broken for months. This is cheap (a few seconds, no DB, no
network) and belongs in the standing suite, not a one-off audit command.

SCOPE: undefined-name findings only (pyflakes' UndefinedName /
UndefinedLocal). Not unused imports, not redefinitions, not style — those are
real but are not the class of defect that has actually cost this project
money and silence; keeping the bar narrow keeps this check from becoming
noisy enough to ignore.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


class _Collector:
    """A pyflakes Reporter that keeps only undefined-name messages."""
    def __init__(self):
        self.undefined: list[str] = []
        self.errors: list[str] = []

    def flake(self, message) -> None:
        from pyflakes.messages import UndefinedName, UndefinedLocal
        if isinstance(message, (UndefinedName, UndefinedLocal)):
            self.undefined.append(str(message))

    def unexpectedError(self, filename, msg) -> None:
        self.errors.append(f"{filename}: {msg}")

    def syntaxError(self, filename, msg, lineno, offset, text) -> None:
        self.errors.append(f"{filename}:{lineno}: {msg}")


def _scan() -> _Collector:
    import pyflakes.api
    collector = _Collector()
    # Exclude tests/ itself and any scratch/venv-style directories that might
    # be sitting alongside the real package on a given machine.
    targets = [
        str(p) for p in BACKEND.iterdir()
        if p.is_dir() and p.name not in ("tests", "__pycache__")
        and not p.name.startswith(".")
    ]
    pyflakes.api.checkRecursive(targets, collector)
    return collector


def test_no_undefined_names_anywhere_in_the_backend():
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        raise AssertionError(
            "pyflakes is not installed (see requirements.txt) — this check "
            "cannot run without it, and skipping silently is exactly the "
            "failure mode this module exists to prevent. pip install pyflakes")

    result = _scan()
    assert not result.undefined, (
        f"{len(result.undefined)} undefined-name reference(s) found — each "
        f"one is a guaranteed crash the first time its branch actually runs:\n  "
        + "\n  ".join(result.undefined))


TESTS = [
    ("no undefined names anywhere in the backend",
     test_no_undefined_names_anywhere_in_the_backend),
]
