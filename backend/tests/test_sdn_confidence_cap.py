"""
SDN's confidence runs backwards, and the cap that acts on it (18-Aug-2026).

See `short_distribution.confidence_is_usable` for the measurement. In short:
across 265 TAKEN-and-resolved SDN rows the highest-confidence bucket (0.75+,
n=41) is the only losing one at -0.273R and stops out 63.4% of the time, while
the lowest (0.55-0.62, n=33) makes +0.769R. Confidence is also the SELECTOR —
`registry.evaluate_all` sorts on it — so the book was funding SDN's worst
detections first.

The switch ships INERT. These checks pin both halves of that: nothing changes
until it is armed, and it does the right thing when it is.
"""

from __future__ import annotations

from tests import cfg_ctx
from intraday.strategies.short_distribution import confidence_is_usable


def test_inert_by_default_nothing_is_refused():
    with cfg_ctx({}):
        for c in (0.55, 0.62, 0.70, 0.75, 0.82, 0.94):
            assert confidence_is_usable(c), (
                f"unset, the cap must refuse nothing — {c} was refused")


def test_an_explicit_zero_is_still_inert():
    """
    0.0 must mean "no cap", not "refuse everything". A component with no
    opinion has to be indistinguishable from that component being absent —
    the same rule allocation/hurdle.py learned on 10-Aug at a cost of a
    session's trading.
    """
    with cfg_ctx({"intraday_short_max_confidence": "0.0"}):
        assert confidence_is_usable(0.94)


def test_armed_at_075_refuses_only_the_losing_bucket():
    with cfg_ctx({"intraday_short_max_confidence": "0.75"}):
        for keep in (0.55, 0.62, 0.66, 0.70, 0.75):
            assert confidence_is_usable(keep), f"{keep} is inside the cap"
        for drop in (0.76, 0.79, 0.82, 0.94):
            assert not confidence_is_usable(drop), f"{drop} is above the cap"


def test_every_sdn_setup_path_consults_it():
    """
    A gate nobody calls is this project's most-repeated defect. Asserted
    against the engine source rather than by eye, so a fourth Setup return
    added later cannot silently bypass the cap.
    """
    import inspect
    from intraday.strategies import short_distribution as M
    src = inspect.getsource(M)
    returns = src.count("        return Setup(")
    guards = src.count("if not confidence_is_usable(")
    assert returns >= 3, f"expected at least 3 SDN setup returns, found {returns}"
    assert guards == returns, (
        f"{returns} Setup return(s) but {guards} confidence guard(s) — every "
        f"path that emits an SDN setup must consult the cap")


TESTS = [
    ("inert by default, nothing refused", test_inert_by_default_nothing_is_refused),
    ("an explicit zero is still inert", test_an_explicit_zero_is_still_inert),
    ("armed at 0.75 refuses only the losing bucket",
     test_armed_at_075_refuses_only_the_losing_bucket),
    ("every SDN setup path consults the cap", test_every_sdn_setup_path_consults_it),
]
