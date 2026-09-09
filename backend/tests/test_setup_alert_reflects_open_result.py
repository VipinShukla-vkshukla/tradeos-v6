"""
intraday/engine.py::act_on_setups() — the alert must say what actually
happened, 09-Sep-2026.

WHAT WAS WRONG
---------------
"SDN — buy 19 @ ₹455.00, stop ₹457.75, target ₹447.48 (R:R 2.7)" read
identically whether `_maybe_open_paper()` a few lines above actually wrote
the paper position or was refused underneath it (BLOCKED_CONCURRENCY, a
capacity cap, a race, a failed fill — every branch `_maybe_open_paper()`'s
own docstring enumerates). The operator asked directly: does this alert
mean a buy happened? There was no way to tell from the message itself.

`_maybe_open_paper()` already returns `opened_ok` — "True only once a
position row is confirmed written", per its own docstring, added for the
IGN bootstrap-slot accounting a few lines above this alert. This wires the
SAME signal into the alert: wording says BOUGHT only when true, and
`push=False` when it isn't — a setup that never became a position is not a
trade event by this project's own definition, so it is recorded (the
dashboard row, same as every other alert) but not pushed to a phone.

Built the same way test_ign_bootstrap_override.py exercises act_on_setups():
IntradayEngine.__new__() plus targeted instance-attribute stubs.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests import cfg_ctx


def _fake_setup(strategy="SDN", symbol="TMCV"):
    return SimpleNamespace(
        symbol=symbol, strategy=strategy, meta={}, confidence=0.72,
        entry=455.0, stop=457.75, target=447.48, rr=2.7,
        rationale="rallied into VWAP and was refused",
        invalidation="reclaims the rejection high",
        risk_pct=0.60, reward_pct=1.65,
    )


class _FakeNotifier:
    def __init__(self):
        self.sent: list = []

    def send(self, action):
        self.sent.append(action)
        return True


def _engine(open_result: bool):
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng._verdicts = {}
    eng.notifier = _FakeNotifier()
    eng.allocator_permits = lambda sym, product, fw: (True, "allocator selected it")
    eng._record_setup = lambda *a, **k: None
    eng._record_alerted = lambda st: None
    eng._intraday_alert_worthy = lambda st: True
    eng._maybe_open_paper = (
        lambda st, qty, mc, phase="?", cost_pct=0.0, pick_label=None,
               bootstrap_override_slot=None: open_result)
    return eng


def _one_setup():
    mc = SimpleNamespace(state="CAUTION", size_multiplier=0.35)
    return [{"setup": _fake_setup(), "qty": 19, "market": mc,
            "phase": "PRIME", "cost_pct": 0.21, "cost_note": "net 1.45% after 0.21% costs"}]


def test_a_confirmed_paper_fill_says_bought_and_pushes():
    eng = _engine(open_result=True)
    with cfg_ctx():
        eng.act_on_setups(_one_setup())
    assert len(eng.notifier.sent) == 1
    a = eng.notifier.sent[0]
    assert "BOUGHT" in a.headline, f"a confirmed fill must say so plainly, got {a.headline!r}"
    assert a.push is True, "an actual paper buy is a trade event — must reach the phone"
    assert a.meta.get("opened") is True


def test_a_refused_setup_does_not_say_bought_and_does_not_push():
    """THE BUG, reproduced and closed. Same setup, same headline inputs —
    only _maybe_open_paper()'s outcome differs — must read and behave
    differently."""
    eng = _engine(open_result=False)
    with cfg_ctx():
        eng.act_on_setups(_one_setup())
    assert len(eng.notifier.sent) == 1, (
        "still recorded on the dashboard — a refusal is not the same as silence")
    a = eng.notifier.sent[0]
    assert "BOUGHT" not in a.headline, (
        f"a setup that was never taken must not read as an executed buy, got {a.headline!r}")
    assert "NOT taken" in a.headline
    assert a.push is False, "nothing happened to a position — must not reach the phone"
    assert a.meta.get("opened") is False


def test_both_cases_carry_the_same_levels_only_the_outcome_differs():
    """Guards against a fix that accidentally also drops or corrupts the
    actual trade levels while changing the wording."""
    bought = _engine(open_result=True)
    refused = _engine(open_result=False)
    with cfg_ctx():
        bought.act_on_setups(_one_setup())
        refused.act_on_setups(_one_setup())
    a_bought, a_refused = bought.notifier.sent[0], refused.notifier.sent[0]
    for text in ("455.00", "457.75", "447.48"):
        assert text in a_bought.headline, f"{text!r} missing from the bought headline"
        assert text in a_refused.headline, f"{text!r} missing from the refused headline"


TESTS = [
    ("a confirmed paper fill says BOUGHT and pushes",
     test_a_confirmed_paper_fill_says_bought_and_pushes),
    ("a refused setup does not say BOUGHT and does not push",
     test_a_refused_setup_does_not_say_bought_and_does_not_push),
    ("both outcomes carry the same trade levels",
     test_both_cases_carry_the_same_levels_only_the_outcome_differs),
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
