"""
The sign convention: a SHORT is the exact mirror of a LONG, rung for rung.

WHAT THIS CATCHES
-----------------
Before `intraday/direction.py` existed, `exit_policy` computed
`risk = entry - stop`, which is NEGATIVE when the stop sits above entry. The
`stop0 < entry` guard then discarded the real stop and fell back to a 0.5%
default, and `gain_r = (ltp - entry) / risk` ROSE as price rose. A short down
2% measured as **+4.00R** — it would book a partial, move the stop to the wrong
side of entry, and trail away from the trade as the loss grew.

Nothing about that raised. It is exactly the shape of failure this suite exists
for: confident wrong numbers rather than errors.
"""

from __future__ import annotations

from datetime import datetime

from tests import cfg_ctx, approx
from tests._fixtures import EXIT_POLICY, MIDDAY, long_pos, short_pos
from config import IST


def test_short_is_the_exact_mirror_of_long():
    """
    Reflect price about the entry and the ladder must reach the same rung.
    If these ever diverge, one direction is being managed by arithmetic written
    for the other.
    """
    from intraday.exit_policy import evaluate_intraday_exit
    with cfg_ctx():
        for ltp in (98.5, 99.5, 100.0, 100.8, 101.5, 102.0, 103.5):
            mirror = 200.0 - ltp                      # reflect about entry=100
            a = evaluate_intraday_exit(long_pos(), ltp, EXIT_POLICY, MIDDAY)
            b = evaluate_intraday_exit(short_pos(), mirror, EXIT_POLICY, MIDDAY)
            assert a["action"] == b["action"], (
                f"long at {ltp} -> {a['action']} but its mirror short at "
                f"{mirror} -> {b['action']}")


def test_a_losing_short_reads_as_losing():
    """
    THE REGRESSION THAT COST THE MOST. A short entered at 100 with its stop at
    101, trading at 102, is two full risk units AGAINST the position. The old
    arithmetic reported +4.00R.
    """
    from intraday.exit_policy import evaluate_intraday_exit
    from intraday import direction as D
    with cfg_ctx():
        risk = D.risk_per_share(100.0, 101.0, "SHORT")
        assert approx(risk, 1.0), f"a short's risk/share is {risk}, expected 1.0"
        gain = D.gain_r(100.0, 102.0, risk, "SHORT")
        assert gain < 0, f"a short losing 2% reports {gain:+.2f}R — must be negative"
        assert approx(gain, -2.0), f"expected -2.00R, got {gain:+.2f}R"

        r = evaluate_intraday_exit(short_pos(), 102.0, EXIT_POLICY, MIDDAY)
        assert r["action"] == "EXIT_STOP", \
            f"a short 2R against is not being stopped out — action {r['action']}"


def test_stops_only_ever_tighten():
    """
    For a long a HIGHER stop is tighter; for a short a LOWER one is. Written as
    `>` this loosened a short's stop every cycle the trail fired.
    """
    from intraday import direction as D
    with cfg_ctx():
        assert D.is_better_price(99.5, 99.0, "LONG"), "long stop 99->99.5 refused"
        assert not D.is_better_price(98.5, 99.0, "LONG"), "long stop loosened to 98.5"
        assert D.is_better_price(100.5, 101.0, "SHORT"), "short stop 101->100.5 refused"
        assert not D.is_better_price(101.5, 101.0, "SHORT"), "short stop loosened to 101.5"


def test_invalidation_fires_on_the_right_side():
    """
    A long dies losing its level; a short dies RECLAIMING it. In the long form
    this fired on a short the moment the trade started working.
    """
    from intraday.exit_policy import _invalidated
    with cfg_ctx():
        assert _invalidated({"invalidation_level": 100.0, "direction": "LONG"}, 99.5)[0]
        assert not _invalidated({"invalidation_level": 100.0, "direction": "LONG"}, 100.5)[0]
        assert _invalidated({"invalidation_level": 100.0, "direction": "SHORT"}, 100.5)[0]
        assert not _invalidated({"invalidation_level": 100.0, "direction": "SHORT"}, 99.5)[0]


def test_a_short_covers_before_a_long_exits():
    """
    NOT symmetry. A long left open becomes delivery — inconvenient, recoverable.
    A short left open is SHORT DELIVERY: bought back in the auction session at a
    penalty around 20% of the trade value, which dwarfs any stop here.

    The first implementation of this inflated `squareoff_buffer`, which is
    measured against the 15:30 close — but SQUARE_OFF phase begins at 15:20, so
    any buffer at or under ten minutes was unreachable and the phase rung fired
    first, every time. A CRITICAL switch that did nothing. This asserts the
    behaviour, which is what that version would have failed.
    """
    from intraday.exit_policy import evaluate_intraday_exit
    with cfg_ctx({"intraday_short_cover_lead_min": "10"}):
        pol = {**EXIT_POLICY, "must_exit_time": "15:15", "check_invalidation": False}
        at = lambda h, m: IST.localize(datetime(2026, 8, 5, h, m))

        # 15:06 — past the short's 15:05 cover deadline, before the long's 15:15.
        long_r = evaluate_intraday_exit(long_pos(), 100.0, pol, at(15, 6))
        short_r = evaluate_intraday_exit(short_pos(), 100.0, pol, at(15, 6))
        assert short_r["action"] == "EXIT_SQUAREOFF", (
            "a short is not covered ahead of the long deadline — the cover lead "
            "is inert, which is what an unreachable buffer produces")
        assert long_r["action"] != "EXIT_SQUAREOFF", (
            "the long is exiting at the short's deadline too — the lead is being "
            "applied to both, so it is not a short-specific rule")


def test_direction_normalises_unlabelled_rows_to_long():
    """
    A NULL or missing direction must read as LONG. Every row written before the
    shorting work is unlabelled, and reading those as SHORT would invert the
    sign on the entire history at once.
    """
    from intraday import direction as D
    with cfg_ctx():
        for value in (None, "", "  ", "garbage", "long", "LONG"):
            assert D.normalise(value) == "LONG", f"{value!r} did not read as LONG"
        assert D.normalise("short") == "SHORT"
        assert D.normalise(" SHORT ") == "SHORT"
        assert D.sign("LONG") == 1 and D.sign("SHORT") == -1


def test_levels_are_validated_per_direction():
    from intraday import direction as D
    with cfg_ctx():
        ok, _ = D.validate(100.0, 99.0, 103.0, "LONG")
        assert ok, "a textbook long was called incoherent"
        ok, _ = D.validate(100.0, 101.0, 97.0, "SHORT")
        assert ok, "a textbook short was called incoherent"
        # And each is refused when submitted as the other direction.
        ok, why = D.validate(100.0, 101.0, 97.0, "LONG")
        assert not ok and "LONG" in why, "short levels accepted as a LONG"
        ok, why = D.validate(100.0, 99.0, 103.0, "SHORT")
        assert not ok and "SHORT" in why, "long levels accepted as a SHORT"


TESTS = [
    ("a short mirrors a long, rung for rung",   test_short_is_the_exact_mirror_of_long),
    ("a losing short reads as losing",          test_a_losing_short_reads_as_losing),
    ("stops only ever tighten",                 test_stops_only_ever_tighten),
    ("invalidation fires on the right side",    test_invalidation_fires_on_the_right_side),
    ("a short covers before a long exits",      test_a_short_covers_before_a_long_exits),
    ("unlabelled direction reads as LONG",      test_direction_normalises_unlabelled_rows_to_long),
    ("levels validated per direction",          test_levels_are_validated_per_direction),
]
