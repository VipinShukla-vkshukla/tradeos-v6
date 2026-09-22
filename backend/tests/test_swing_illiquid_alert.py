"""
intraday/engine.py::act_on_candidates() — a name below the SWING liquidity
floor must not read as a plain "BUY", 23-Sep-2026.

WHAT WAS WRONG
---------------
AUROPHARMA alerted "BUY — in zone at 1690.00" and "CHASE OK" dozens of
times across one session (09-16 through 15-33 IST, 22-Sep-2026). Every
gate in `_maybe_enter_swing()` was checked against live data and cleared —
except one: `stock_data_daily.value_cr` sat at ₹147.7 Cr against the
₹200 Cr SWING floor `analysis.overlays.liquidity_ok()` enforces, and that
name had been below the floor on 7 of its last 8 sessions. So the alert
said "buy this" while the code that actually places the order refused it
every single cycle, silently — the operator had no way to know WHY a "BUY"
never became a position.

`_swing_liquidity_check()` factors the existing `_maybe_enter_swing()`
liquidity gate out so `act_on_candidates()` can ask the identical question
before building the alert. This exercises the WIRING — that the alert's
kind/headline/push correctly reflect a liquidity refusal — not
`liquidity_ok()`'s own threshold math, which `tests.test_overlays_
liquidity` already covers.

Built the same way test_ign_bootstrap_override.py exercises this class of
method: IntradayEngine.__new__() plus targeted instance-attribute stubs.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests import cfg_ctx


class _FakeNotifier:
    def __init__(self):
        self.sent: list = []

    def send(self, action):
        self.sent.append(action)
        return True


def _decision(**over):
    d = SimpleNamespace(
        action="BUY_NOW", headline="AUROPHARMA: BUY — in zone at 1690.00, R:R 1.51",
        reason="stop 1616.58 (4.3%) · target 1800.85 (+6.6%)",
        stop=1616.58, target=1800.85, qty=17, invested=28730.0, risk_amount=1248.0,
        rr_live=1.51, rr_at_zone_low=2.35, max_entry=1718.95, min_rr_used=0.8,
    )
    for k, v in over.items():
        setattr(d, k, v)
    return d


def _engine(liquidity_result: tuple[bool, str]):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng._verdicts = {}
    eng.notifier = _FakeNotifier()
    rk = SimpleNamespace(total=9.05, why=lambda: "sector #1 +5 · validity 73 +3")
    eng._swing_contenders = lambda: {"AUROPHARMA": (2, rk)}
    eng._today_totals_cached = lambda fw: (0, 0, 0)
    eng._replacement_case = lambda rk: (False, "")
    eng._swing_liquidity_check = lambda sym, qty, ltp: liquidity_result
    eng._maybe_enter_swing = lambda c, d, ltp: None
    return eng


def _one_entry():
    c = {"symbol": "AUROPHARMA", "sector": "healthcare", "ai_tier": None}
    return [{"candidate": c, "decision": _decision(), "ltp": 1690.00, "state": "BUYABLE"}]


ILLIQUID = (False, "daily traded value Rs 147.7 cr is below the Rs 200 cr "
                   "swing floor — thin names gap through stops")


def test_illiquid_name_gets_its_own_kind_not_plain_entry():
    eng = _engine(ILLIQUID)
    with cfg_ctx():
        eng.act_on_candidates(_one_entry())
    assert len(eng.notifier.sent) == 1
    a = eng.notifier.sent[0]
    assert a.kind == "ENTRY_ILLIQUID", f"expected a distinct illiquidity kind, got {a.kind}"
    assert a.headline == ILLIQUID[1], "the alert must show the real refusal reason, not a fabricated BUY"
    assert "BUY" not in a.headline


def test_illiquid_name_does_not_push():
    eng = _engine(ILLIQUID)
    with cfg_ctx():
        eng.act_on_candidates(_one_entry())
    a = eng.notifier.sent[0]
    assert a.push is False, "nothing became a position — must not reach the phone"
    assert a.meta.get("illiquid") is True


def test_liquid_name_is_unaffected():
    """The control case: a name that clears the floor still alerts plainly."""
    eng = _engine((True, "₹147.7 Cr of ₹200 Cr floor cleared"))
    with cfg_ctx():
        eng.act_on_candidates(_one_entry())
    a = eng.notifier.sent[0]
    assert a.kind == "ENTRY"
    assert "BUY" in a.headline
    assert a.push is True
    assert a.meta.get("illiquid") is False


def test_declined_verdict_is_not_overwritten_by_illiquidity():
    """A DECLINE already names a real reason nothing is happening; layering
    an illiquidity label on top would only muddy it, not add information."""
    eng = _engine(ILLIQUID)
    eng._verdicts = {("AUROPHARMA", "CNC"):
                     {"verdict": "DECLINE", "reason": "edge -0.02 below the bar 0.03"}}
    with cfg_ctx({"swing_alert_reflect_allocator": "true", "swing_paper_research_mode": "false"}):
        eng.act_on_candidates(_one_entry())
    a = eng.notifier.sent[0]
    assert a.kind == "ENTRY_DECLINED", f"expected the allocator's own reason to win, got {a.kind}"


TESTS = [
    ("illiquid name gets its own kind, not plain ENTRY",
     test_illiquid_name_gets_its_own_kind_not_plain_entry),
    ("illiquid name does not push", test_illiquid_name_does_not_push),
    ("liquid name is unaffected", test_liquid_name_is_unaffected),
    ("a DECLINE verdict is not overwritten by illiquidity",
     test_declined_verdict_is_not_overwritten_by_illiquidity),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
