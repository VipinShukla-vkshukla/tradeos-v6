"""
How far above the entry zone the swing book may pay.

`decide()` reads `max_chase_pct=None` as NO LIMIT (trade_decision.py's zone
branch). All three swing call sites passed

    max_chase_pct=p.get("ai_max_chase_pct") or None

and `or None` is false for 0.0. So a plan on which the AI wrote an explicit
0 — "do not chase this one" — reached decide() as None and was bought at any
price above the zone: the exact opposite of the instruction. Since 01-Aug the
AI wrote a literal 0 on 306 plans and NULL on 1,526; only the 229 with a
positive number were honoured.

"No opinion" must not mean "unlimited" either. NULL now resolves to the
explicit swing_max_chase_pct policy instead of an accident.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests import cfg_ctx

ROOT = Path(__file__).resolve().parent.parent


def _code_only(src: str) -> str:
    """
    Blank out docstrings, preserving line numbers.

    chase_limit() quotes the defective expression in its own docstring, to
    record what went wrong. Prose is not a caller, and a check that cannot tell
    the two apart would have to be silenced — which is how the documentation
    for a defect ends up deleted to make a test pass.
    """
    return re.sub(r'"""(?:.|\n)*?"""',
                  lambda m: re.sub(r'[^\n]', " ", m.group(0)), src)


def _plan(**kw) -> dict:
    p = {"symbol": "X", "entry_zone_low": 98.0, "entry_zone_high": 100.0,
         "planned_stop": 94.0, "planned_target": 118.0, "sector": "pharma",
         "industry": "pharma", "strategy": "CTL"}
    p.update(kw)
    return p


def test_explicit_zero_is_not_discarded():
    """The defect, stated as a test: 0 must survive the resolution."""
    from analysis.trade_decision import chase_limit
    with cfg_ctx({"swing_max_chase_pct": "5"}):
        assert chase_limit(_plan(ai_max_chase_pct=0)) == 0.0
        assert chase_limit(_plan(ai_max_chase_pct=0.0)) == 0.0
        assert chase_limit(_plan(ai_max_chase_pct="0")) == 0.0


def test_no_opinion_resolves_to_the_configured_policy_never_unlimited():
    from analysis.trade_decision import chase_limit
    with cfg_ctx({"swing_max_chase_pct": "5"}):
        assert chase_limit(_plan()) == 5.0
        assert chase_limit(_plan(ai_max_chase_pct=None)) == 5.0
    with cfg_ctx({"swing_max_chase_pct": "2.5"}):
        assert chase_limit(_plan()) == 2.5
    # A missing key must still be a number, because None means unlimited.
    with cfg_ctx({}):
        assert chase_limit(_plan()) is not None


def test_a_positive_allowance_is_honoured_as_written():
    from analysis.trade_decision import chase_limit
    with cfg_ctx({"swing_max_chase_pct": "5"}):
        assert chase_limit(_plan(ai_max_chase_pct=1.5)) == 1.5
        # tighter than policy, and looser: the AI's number wins either way
        assert chase_limit(_plan(ai_max_chase_pct=9.0)) == 9.0


def test_zero_allowance_refuses_a_price_above_the_zone():
    """End to end through the decision the book actually makes."""
    from analysis.trade_decision import decide, chase_limit
    plan = _plan(ai_max_chase_pct=0)
    with cfg_ctx({"swing_max_chase_pct": "5"}):
        d = decide(plan, 103.0, total_capital=300000, open_positions=[],
                   regime="NEUTRAL", min_rr=1.0, max_chase_pct=chase_limit(plan))
    assert d.action == "SKIP", f"{d.action}: {d.headline}"
    assert "chase" in d.headline.lower()


def test_in_zone_entry_is_unaffected():
    from analysis.trade_decision import decide, chase_limit
    plan = _plan(ai_max_chase_pct=0)
    with cfg_ctx({"swing_max_chase_pct": "5"}):
        d = decide(plan, 99.0, total_capital=300000, open_positions=[],
                   regime="NEUTRAL", min_rr=1.0, max_chase_pct=chase_limit(plan))
    assert d.action == "BUY_NOW", f"{d.action}: {d.headline}"


def test_no_call_site_still_launders_the_field_through_or_none():
    """
    The call-site check, not the definition check.

    A correct chase_limit() proves nothing about the three places that decide
    what to pass — the same gap that let every SHORT be scored as a LONG.
    """
    offenders = []
    for rel in ("intraday/engine.py", "control/paper_entry.py", "tools/simulate.py",
                "analysis/trade_decision.py"):
        src = _code_only((ROOT / rel).read_text(encoding="utf-8"))
        for m in re.finditer(r'ai_max_chase_pct[^\n]*\bor None', src):
            offenders.append(f"{rel}:{src[:m.start()].count(chr(10)) + 1}")
    assert not offenders, (
        "`or None` turns an explicit 0 into unlimited chasing; use "
        f"chase_limit(plan): {offenders}")


def test_every_swing_decide_call_resolves_the_limit():
    """Every caller that passes max_chase_pct must route through chase_limit()."""
    missing = []
    for rel in ("intraday/engine.py", "control/paper_entry.py", "tools/simulate.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for m in re.finditer(r'max_chase_pct\s*=\s*([^,\n]+)', src):
            expr = m.group(1).strip()
            if "chase_limit" not in expr:
                missing.append(f"{rel}:{src[:m.start()].count(chr(10)) + 1} -> {expr}")
    assert not missing, f"call sites bypassing chase_limit(): {missing}"


TESTS = [
    ("explicit zero is not discarded", test_explicit_zero_is_not_discarded),
    ("no opinion resolves to policy, never unlimited", test_no_opinion_resolves_to_the_configured_policy_never_unlimited),
    ("a positive allowance is honoured as written", test_a_positive_allowance_is_honoured_as_written),
    ("zero allowance refuses a price above the zone", test_zero_allowance_refuses_a_price_above_the_zone),
    ("in-zone entry unaffected", test_in_zone_entry_is_unaffected),
    ("no call site launders the field through `or None`", test_no_call_site_still_launders_the_field_through_or_none),
    ("every swing decide() call resolves the limit", test_every_swing_decide_call_resolves_the_limit),
]
