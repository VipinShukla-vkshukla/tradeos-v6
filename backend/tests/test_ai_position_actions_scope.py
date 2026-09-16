"""
ai_decision_engine.write_position_actions() writes the AI's TIGHTEN_SL/HOLD/...
onto open_positions, and evaluate_exit() executes TIGHTEN_SL. open_positions is
keyed on (symbol, product) since migration 028, so an update on symbol alone
would stamp an intraday MIS row holding the same name with a swing review.
"""

from __future__ import annotations


class _Q:
    def __init__(self, log):
        self.log, self.filters = log, {}
    def update(self, payload):
        self.payload = payload
        return self
    def eq(self, col, val):
        self.filters[col] = val
        return self
    def execute(self):
        self.log.append(self.filters)
        return type("R", (), {"data": []})()


class _SB:
    def __init__(self):
        self.log = []
    def table(self, name):
        assert name == "open_positions"
        return _Q(self.log)


def test_ai_action_update_is_scoped_to_the_swing_row():
    from ai.ai_decision_engine import write_position_actions
    sb = _SB()
    result = {"position_actions": [{"symbol": "HAL", "recommended_action": "TIGHTEN_SL",
                                    "confidence": 0.7, "reason": "event", "urgency": "LOW"}]}
    n = write_position_actions(sb, result, [{"symbol": "HAL"}], "2026-09-17")
    assert n == 1 and len(sb.log) == 1, sb.log
    f = sb.log[0]
    assert f.get("symbol") == "HAL" and f.get("status") == "ACTIVE", f
    assert f.get("framework") == "SWING" and f.get("product") == "CNC", (
        f"AI position action updated on {f} — an intraday row with the same symbol "
        f"would receive the swing review")


TESTS = [
    ("AI position actions update the SWING/CNC row only",
     test_ai_action_update_is_scoped_to_the_swing_row),
]
