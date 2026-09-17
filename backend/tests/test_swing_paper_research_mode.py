"""
swing_paper_research_mode — on PAPER the swing book exists to collect trade
data. From 09-Sep-2026 the allocator declined every swing proposal ("edge below
the bar") and the paper book took no trades at all. Research mode records the
allocator and market-exposure verdicts but does not let them block, ONLY while
swing is PAPER. In LIVE the flag must change nothing.
"""

from __future__ import annotations

from tests import cfg_ctx

PAPER = {"swing_trading_mode": "PAPER", "alloc_live_swing": "true"}
LIVE = {"swing_trading_mode": "LIVE", "alloc_live_swing": "true"}
ON = {"swing_paper_research_mode": "true"}


class _Eng:
    _verdicts = {("HAL", "CNC"): {"verdict": "DECLINE", "reason": "edge -0.0104 below the bar 0"}}


def _permit(flags):
    from intraday.engine import IntradayEngine
    with cfg_ctx(flags):
        return IntradayEngine.allocator_permits(_Eng(), "HAL", "CNC", "SWING")


def test_declined_swing_entry_permitted_in_paper_research_mode():
    ok, why = _permit({**PAPER, **ON})
    assert ok and "research" in why.lower(), (ok, why)


def test_live_ignores_research_mode():
    ok, why = _permit({**LIVE, **ON})
    assert not ok, f"LIVE must keep the allocator veto: {why}"


def test_paper_without_research_mode_keeps_the_veto():
    ok, _why = _permit(PAPER)
    assert not ok


def test_intraday_unaffected():
    from intraday.engine import IntradayEngine

    class E:
        _verdicts = {("HAL", "MIS"): {"verdict": "DECLINE", "reason": "x"}}
    with cfg_ctx({**PAPER, **ON, "alloc_live_intraday": "true", "intraday_trading_mode": "PAPER"}):
        ok, _ = IntradayEngine.allocator_permits(E(), "HAL", "MIS", "INTRADAY")
    assert not ok


def test_exposure_recorded_not_applied_in_paper_research_mode():
    from analysis.market_exposure import Exposure, for_entries
    corr = Exposure("CORRECTION", max_new=2, size_mult=0.5, reasons=("x",))
    with cfg_ctx({**PAPER, **ON}):
        assert for_entries(corr).state == "NORMAL"
    with cfg_ctx({**LIVE, **ON}):
        assert for_entries(corr).state == "CORRECTION"
    with cfg_ctx(PAPER):
        assert for_entries(corr).state == "CORRECTION"


def test_daemon_and_simulate_route_exposure_through_for_entries():
    import inspect
    from intraday.engine import IntradayEngine
    import tools.simulate as sim
    assert "for_entries(" in inspect.getsource(IntradayEngine._swing_exposure)
    assert "for_entries(" in inspect.getsource(sim)


def test_health_fails_if_research_mode_is_on_while_live():
    from tools.health import research_mode_problem
    with cfg_ctx({**LIVE, **ON}):
        assert research_mode_problem()
    with cfg_ctx({**PAPER, **ON}):
        assert not research_mode_problem()


TESTS = [
    ("declined swing entry permitted in paper research mode",
     test_declined_swing_entry_permitted_in_paper_research_mode),
    ("LIVE ignores research mode", test_live_ignores_research_mode),
    ("paper without research mode keeps the veto", test_paper_without_research_mode_keeps_the_veto),
    ("intraday allocator veto unaffected", test_intraday_unaffected),
    ("exposure recorded, not applied, in paper research mode",
     test_exposure_recorded_not_applied_in_paper_research_mode),
    ("daemon and simulate route exposure through for_entries",
     test_daemon_and_simulate_route_exposure_through_for_entries),
    ("health fails if research mode is on while LIVE", test_health_fails_if_research_mode_is_on_while_live),
]
