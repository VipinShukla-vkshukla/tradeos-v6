"""
validate_config's paper-capacity check was validating a dead key (11-Aug-2026).

WHAT THIS CATCHES
------------------
`execution.paper_broker.capacity()` carries its own account of this: until
07-Aug-2026 it read its own `paper_starting_capital` key, then migrated to
`capital_for("INTRADAY")` — the same sleeve live sizing reads — and
`paper_starting_capital` was never removed from `system_config` or from
`tools.validate_config`'s coherence check. So the check kept validating a
key confirmed (by grep, before writing the fix) to be read by NO code
anywhere for behaviour — a wiring failure of exactly the kind
`check_wiring()` exists to catch, missed only because this key is
`risk_level=SAFE`, not `CRITICAL`, outside that check's own scope.

Fixing this ALSO surfaced a second, independent bug while testing it:
`main()`'s severity-to-icon/logger dicts had no "INFO" entry, so adding an
informational (not ERROR/WARN/OK) finding for the now-dead key would have
raised a bare `KeyError` and taken the whole tool down on its very next
run — the checker itself uncheckable.

These tests pin: `paper_starting_capital`'s finding is now INFO (not
ERROR) and says the key is unread; the real transferability check
(`paper_capacity_transfer`) and the `paper_max_open_positions` recommendation
both key off `capital_for("INTRADAY")`, not the dead value — proven by
constructing a scenario where the two numbers DIFFER, so a test built
against the old (buggy) code would fail while this one passes; and `main()`
does not crash on an INFO-severity finding.
"""

from __future__ import annotations

from unittest.mock import patch

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows):
        self._all = rows
        self._filtered = rows

    def select(self, *a, **k):
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._filtered = [r for r in self._all if r.get(col) in vals]
        return self

    def eq(self, col, val):
        self._filtered = [r for r in self._all if r.get(col) == val]
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def execute(self):
        if hasattr(self, "_update_payload"):
            for r in self._filtered:
                r["value"] = self._update_payload["value"]
                self._sb.updated.append(dict(r))
            self.data = self._filtered
        else:
            self.data = self._filtered
        return self


class _FakeSB:
    def __init__(self, config_rows):
        self._rows = config_rows
        self.updated: list = []

    def table(self, name):
        assert name == "system_config"
        q = _FakeQuery(self._rows)
        q._sb = self
        return q


def _row(key, value, risk_level="TUNING"):
    return {"key": key, "value": value, "risk_level": risk_level}


def _config_rows(**overrides):
    """A coherent baseline: paper_starting_capital (dead) deliberately set
    to a DIFFERENT number than intraday_capital (the live one), so any test
    reading the wrong key produces a visibly wrong, easily caught number."""
    base = {
        "swing_max_order_value": "6000", "swing_max_orders_per_day": "5",
        "swing_max_notional_per_day": "30000", "intraday_max_order_value": "25000",
        "intraday_max_orders_per_day": "5", "intraday_max_notional_per_day": "100000",
        "risk_pct_per_trade": "1.0", "max_position_pct": "20.0",
        "portfolio_min_position_pct": "3.0",
        "paper_starting_capital": "999999",     # dead — must NOT drive any finding
        "capital_tolerance_pct": "5.0",
        "intraday_max_position_pct": "25.0", "paper_max_open_positions": "10",
        "intraday_capital": "100000",           # the REAL driver
        "intraday_trading_mode": "PAPER",
    }
    base.update(overrides)
    return [_row(k, v) for k, v in base.items()]


def _find(findings, key):
    return next(f for f in findings if f.key == key)


def test_paper_starting_capital_finding_is_info_not_error():
    from tools.validate_config import check_coherence
    with patch("tools.validate_config.TOTAL_CAPITAL", 30000.0), \
         patch("tools.validate_config.capital_for",
              lambda fw: 100000.0 if fw == "INTRADAY" else 30000.0):
        findings = check_coherence(_FakeSB(_config_rows()))
    f = _find(findings, "paper_starting_capital")
    assert f.severity == "INFO", (
        f"got {f.severity} — a dead key must not be reported as ERROR/WARN, "
        f"which would misdirect a fix at a number nothing reads")
    assert "not read" in f.why.lower() or "NOT READ" in f.why


def test_paper_capacity_transfer_reads_intraday_capital_not_the_dead_key():
    """The decisive proof: paper_starting_capital is set to a wildly
    different number (999999) than intraday_capital (100000). A check that
    still reads the dead key would report transferability against 999999;
    the fixed check must report it against 100000."""
    from tools.validate_config import check_coherence
    with patch("tools.validate_config.TOTAL_CAPITAL", 30000.0), \
         patch("tools.validate_config.capital_for",
              lambda fw: 100000.0 if fw == "INTRADAY" else 30000.0):
        findings = check_coherence(_FakeSB(_config_rows()))
    f = _find(findings, "paper_capacity_transfer")
    assert "100,000" in f.current, (
        f"got current={f.current!r} — must reflect capital_for('INTRADAY') "
        f"(100000), not paper_starting_capital's dead 999999")
    assert "999,999" not in f.current and "1,000,000" not in f.current


def test_paper_max_open_positions_recommendation_uses_the_live_capital():
    """max(1, capital_for('INTRADAY') // biggest_order). With 100000 and a
    biggest order of 25000, that is 4 — the SAME answer the tool gave before
    this fix, because intraday_capital (the real driver) never changed. A
    version still reading the dead key (999999) would recommend 39 instead."""
    from tools.validate_config import check_coherence
    with patch("tools.validate_config.TOTAL_CAPITAL", 30000.0), \
         patch("tools.validate_config.capital_for",
              lambda fw: 100000.0 if fw == "INTRADAY" else 30000.0):
        findings = check_coherence(_FakeSB(_config_rows()))
    f = _find(findings, "paper_max_open_positions")
    assert f.suggested == "4", f"got suggested={f.suggested!r}, expected '4'"


# ── apply_fixes() hardening ──────────────────────────────────────────────────

def test_apply_fixes_refuses_a_non_numeric_suggestion():
    """Regression pin for the corruption bug: a finding whose suggested
    value is prose (the exact shape intraday_capital's findings used to
    carry) must be REFUSED, not written."""
    from tools.validate_config import apply_fixes, Finding
    sb = _FakeSB([_row("intraday_capital", "100000")])
    bad = Finding("intraday_capital", "WARN", "₹100,000",
                  "below ₹30,000 before going live", "prose, not a number")
    n = apply_fixes([bad], sb)
    assert n == 0
    assert sb.updated == [], "a non-numeric suggestion must never reach update()"


def test_apply_fixes_refuses_a_key_with_no_matching_row():
    """paper_capacity_transfer names a CONCERN, not a real config key —
    apply_fixes must not report success while silently fixing nothing."""
    from tools.validate_config import apply_fixes, Finding
    sb = _FakeSB([_row("intraday_capital", "100000")])
    ghost = Finding("paper_capacity_transfer", "WARN", "₹100,000",
                    "₹30,000", "not an actual system_config row")
    n = apply_fixes([ghost], sb)
    assert n == 0
    assert sb.updated == []


def test_apply_fixes_still_applies_a_genuine_numeric_fix():
    """The hardening must not break the ordinary, correct case."""
    from tools.validate_config import apply_fixes, Finding
    sb = _FakeSB([_row("paper_max_open_positions", "10")])
    good = Finding("paper_max_open_positions", "WARN", "10", "4",
                   "10 positions x ... more than paper capacity")
    n = apply_fixes([good], sb)
    assert n == 1
    assert sb.updated[0]["value"] == "4"


def test_the_two_intraday_capital_findings_no_longer_carry_prose():
    """Direct proof the actual call sites were fixed, not just documented."""
    from tools.validate_config import check_coherence
    with patch("tools.validate_config.TOTAL_CAPITAL", 30000.0), \
         patch("tools.validate_config.capital_for",
              lambda fw: 100000.0 if fw == "INTRADAY" else 30000.0):
        findings = check_coherence(_FakeSB(_config_rows(intraday_trading_mode="LIVE")))
    f = _find(findings, "intraday_capital")
    assert f.suggested is None, (
        f"got suggested={f.suggested!r} — intraday_capital's suggested value "
        f"must be None (a judgment call), not a re-derived prose string")


def test_main_does_not_crash_on_an_info_severity_finding():
    """Regression pin for the second bug found while fixing the first: the
    icon/logger dicts in main() had no 'INFO' entry and would KeyError the
    whole tool the first time this finding fired."""
    from tools.validate_config import main
    with cfg_ctx({}), \
         patch("tools.validate_config.get_supabase",
              return_value=_FakeSB(_config_rows())), \
         patch("tools.validate_config.TOTAL_CAPITAL", 30000.0), \
         patch("tools.validate_config.capital_for",
              lambda fw: 100000.0 if fw == "INTRADAY" else 30000.0):
        rc = main(fix=False)   # must not raise KeyError
    assert rc in (0, 1)


TESTS = [
    ("paper_starting_capital finding is INFO, not ERROR",
     test_paper_starting_capital_finding_is_info_not_error),
    ("paper_capacity_transfer reads intraday_capital, not the dead key",
     test_paper_capacity_transfer_reads_intraday_capital_not_the_dead_key),
    ("paper_max_open_positions recommendation uses the live capital",
     test_paper_max_open_positions_recommendation_uses_the_live_capital),
    ("main() does not crash on an INFO-severity finding",
     test_main_does_not_crash_on_an_info_severity_finding),
    ("apply_fixes refuses a non-numeric suggestion",
     test_apply_fixes_refuses_a_non_numeric_suggestion),
    ("apply_fixes refuses a key with no matching row",
     test_apply_fixes_refuses_a_key_with_no_matching_row),
    ("apply_fixes still applies a genuine numeric fix",
     test_apply_fixes_still_applies_a_genuine_numeric_fix),
    ("the two intraday_capital findings no longer carry prose",
     test_the_two_intraday_capital_findings_no_longer_carry_prose),
]
