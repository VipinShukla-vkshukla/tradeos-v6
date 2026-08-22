"""
BRKD's retest-and-held signal — F-49, 22-Aug-2026.

WHAT THIS IS AND IS NOT. Mirrors ORB's own retest confirmation (F-37) for
SDN's range-breakdown condition, whose 8% win rate over the post-fix
sample is structurally the same "raw break, no hold check" shape ORB's
own unconfirmed fallback measured 0% on. UNLIKE ORB, this is
INFORMATIONAL ONLY — the operator was explicit ("add it as priority
criteria and not the hard filter to block everything"): _range_breakdown
must produce a setup regardless of what the retest check finds, stamping
the result into meta for allocation.policies._confirmation_key's existing
priority tie-break (F-48) to read. This file's job is proving BOTH halves:
the pure detection logic, and that the wiring never gates on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from unittest.mock import patch

from config import IST
from tests import cfg_ctx
from tests._fixtures import ctx_for

SHORTS_ON = {"intraday_allow_shorts": "true", "intraday_short_min_bars": "15"}


@dataclass
class _Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


_START = IST.localize(datetime(2026, 8, 22, 9, 30))


def _bar(i, o, h, l, c, v=10000):
    return _Bar(_START + timedelta(minutes=i), o, h, l, c, v)


# ── _retest_and_held_short, pure ─────────────────────────────────────────

def test_never_probed_returns_false():
    """Price never traded below `level` at all — nothing to retest."""
    from intraday.strategies.short_distribution import _retest_and_held_short
    bars = [_bar(0, 100, 100.2, 99.9, 100.0), _bar(1, 100, 100.3, 99.95, 100.1)]
    assert _retest_and_held_short(bars, level=99.5, tolerance_pct=0.15) is False


def test_probed_but_never_retested_returns_false():
    """The exact shape of the existing 'breakdown' fixture — breaks and
    keeps falling in a straight line, never comes back up toward the
    level. This is the realistic, common case, not an edge case."""
    from intraday.strategies.short_distribution import _retest_and_held_short
    bars = [_bar(0, 100, 100.1, 99.3, 99.4),   # probes below 99.65
           _bar(1, 99.3, 99.4, 98.9, 99.0),    # keeps falling, no retest
           _bar(2, 99.0, 99.1, 98.5, 98.6)]
    assert _retest_and_held_short(bars, level=99.65, tolerance_pct=0.15) is False


def test_probed_retested_and_held_returns_true():
    """Breaks the level, rallies back up to test it as new resistance,
    then continues down without reclaiming it — the textbook confirmation."""
    from intraday.strategies.short_distribution import _retest_and_held_short
    level = 99.65
    bars = [_bar(0, 100, 100.1, 99.3, 99.4),          # probe: low < level
           _bar(1, 99.4, 99.70, 99.35, 99.5),         # retest: high >= level*(1-tol)
           _bar(2, 99.5, 99.55, 99.0, 99.1)]           # held: close <= level
    assert _retest_and_held_short(bars, level=level, tolerance_pct=0.15) is True


def test_retest_then_reclaim_returns_false():
    """One bar closing back above the level after the retest kills the
    whole sequence — a level reclaimed once is not 'held' because a later
    bar happens to fall back below it again."""
    from intraday.strategies.short_distribution import _retest_and_held_short
    level = 99.65
    bars = [_bar(0, 100, 100.1, 99.3, 99.4),          # probe
           _bar(1, 99.4, 99.70, 99.35, 99.5),         # retest
           _bar(2, 99.5, 99.9, 99.4, 99.80),           # RECLAIMED: close > level
           _bar(3, 99.80, 99.85, 99.2, 99.3)]          # falls again — too late
    assert _retest_and_held_short(bars, level=level, tolerance_pct=0.15) is False


def test_only_uses_closed_bars_the_caller_hands_it():
    """Pure function, no opinion on live ticks — matches
    orb.py::_retest_and_held's identical contract. Documented here so the
    two functions' behaviour cannot silently diverge without a test
    noticing on at least one of them."""
    from intraday.strategies.short_distribution import _retest_and_held_short
    assert _retest_and_held_short([], level=99.65, tolerance_pct=0.15) is False


# ── wiring: stamped into meta, NEVER gates ───────────────────────────────

def test_breakdown_setup_produced_regardless_of_retest_result_false():
    """
    Isolates wiring from detection, same as the True-case test below —
    patches the pure helper rather than relying on a fixture's numeric
    shape to happen to produce False.

    THE FIXTURE-SHAPE SURPRISE THIS REPLACED. The first version of this
    test assumed the shared 'breakdown' fixture (a straight decline after
    the opening range) would NOT retest, and asserted retest_confirmed is
    False. It measured True instead: bar 1 of the post-range bars has a
    high of 99.92, which clears the retest tolerance band around the
    99.65 level even though price never closes back near it — the same
    "no upper bound on the retest touch" shape orb.py's own
    _retest_and_held has (mirrored deliberately, see this module's
    docstring). Not a bug this file introduced; the fixture-dependent
    assertion was simply wrong, so it was replaced with the same
    mock-based isolation the True case already uses, rather than
    hand-tuning a second bars fixture to fit a specific outcome.
    """
    from intraday.strategies.short_distribution import ShortDistribution
    with cfg_ctx(SHORTS_ON), \
         patch("intraday.strategies.short_distribution._retest_and_held_short",
               return_value=False):
        s = ShortDistribution().evaluate(ctx_for("breakdown"), "PRIME")
    assert s is not None, "an unconfirmed breakdown must still produce a setup"
    assert s.meta.get("retest_confirmed") is False


def test_breakdown_setup_produced_and_stamped_true_when_confirmed():
    """Isolates the WIRING from the DETECTION logic — patches the pure
    helper directly rather than constructing a second, fragile bars
    fixture that has to simultaneously satisfy volume/chase/R:R gates AND
    a specific retest shape. What this checks: when the helper says True,
    that True reaches meta unchanged, and the setup is still produced."""
    from intraday.strategies.short_distribution import ShortDistribution
    with cfg_ctx(SHORTS_ON), \
         patch("intraday.strategies.short_distribution._retest_and_held_short",
               return_value=True):
        s = ShortDistribution().evaluate(ctx_for("breakdown"), "PRIME")
    assert s is not None
    assert s.meta.get("retest_confirmed") is True


TESTS = [
    ("never probed returns False", test_never_probed_returns_false),
    ("probed but never retested returns False", test_probed_but_never_retested_returns_false),
    ("probed, retested and held returns True", test_probed_retested_and_held_returns_true),
    ("retest then reclaim returns False", test_retest_then_reclaim_returns_false),
    ("only uses closed bars the caller hands it", test_only_uses_closed_bars_the_caller_hands_it),
    ("breakdown setup produced regardless of a False retest result",
     test_breakdown_setup_produced_regardless_of_retest_result_false),
    ("breakdown setup produced and stamped True when confirmed",
     test_breakdown_setup_produced_and_stamped_true_when_confirmed),
]
