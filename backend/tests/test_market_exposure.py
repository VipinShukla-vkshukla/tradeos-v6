"""
analysis/market_exposure.py — the swing book's size and pace in a weak market.

ROWS are the real market_regime (date, nifty_price, advance_decline_ratio)
sessions from 20-May to 08-Sep-2026, so the states below are the ones the
entry path would have seen.
"""

from __future__ import annotations

from tests import cfg_ctx

ROWS = [
    ('2026-05-20', 23659.0, 1), ('2026-05-21', 23654.7, 1.48), ('2026-05-22', 23719.3, 0.99),
    ('2026-05-25', 24031.7, 3.02), ('2026-05-26', 23913.7, 0.93),
    ('2026-05-27', 23907.2, 1.28), ('2026-05-29', 23547.8, 0.37),
    ('2026-06-01', 23382.6, 0.37), ('2026-06-02', 23483.5, 1.47),
    ('2026-06-03', 23405.6, 0.7), ('2026-06-04', 23416.5, 1.14),
    ('2026-06-05', 23366.7, 0.94), ('2026-06-08', 23123.0, 0.16),
    ('2026-06-09', 23242.1, 3.75), ('2026-06-10', 23215.0, 0.3),
    ('2026-06-11', 23161.6, 0.32), ('2026-06-12', 23622.9, 8.26),
    ('2026-06-15', 23853.9, 3.03), ('2026-06-16', 23989.2, 1.38),
    ('2026-06-17', 24033.2, 1.42), ('2026-06-18', 24126.0, 1.23), ('2026-06-19', 24013.1, 1),
    ('2026-06-22', 24102.9, 2.12), ('2026-06-23', 23824.1, 0.29),
    ('2026-06-24', 24021.7, 1.07), ('2026-06-25', 24056.0, 0.53),
    ('2026-06-29', 23946.2, 0.53), ('2026-06-30', 23865.8, 1.34),
    ('2026-07-01', 24005.8, 1.37), ('2026-07-02', 24175.7, 2.28),
    ('2026-07-03', 24270.8, 0.91), ('2026-07-06', 24430.3, 1.08),
    ('2026-07-07', 24398.7, 0.53), ('2026-07-08', 23882.0, 0.12),
    ('2026-07-09', 23962.8, 4.1), ('2026-07-10', 23962.8, 5.32),
    ('2026-07-13', 24211.0, 0.96), ('2026-07-14', 24052.0, 0.31),
    ('2026-07-15', 24078.5, 1.53), ('2026-07-16', 24072.8, 0.76),
    ('2026-07-17', 24334.3, 0.7), ('2026-07-20', 24238.5, 1.42),
    ('2026-07-21', 24187.7, 1.27), ('2026-07-22', 23996.2, 0.23),
    ('2026-07-23', 23869.6, 0.26), ('2026-07-24', 23767.5, 0.76),
    ('2026-07-27', 23996.0, 3.53), ('2026-07-28', 23985.3, 0.59),
    ('2026-07-29', 24250.2, 3.02), ('2026-07-30', 24317.2, 0.44),
    ('2026-07-31', 24383.6, 1.56), ('2026-08-03', 24774.3, 3.83),
    ('2026-08-04', 24614.9, 0.64), ('2026-08-05', 24624.7, 1.39),
    ('2026-08-06', 24636.0, 0.75), ('2026-08-07', 24570.7, 0.89),
    ('2026-08-10', 24583.8, 0.9), ('2026-08-11', 24471.7, 0.67),
    ('2026-08-12', 24436.0, 0.8), ('2026-08-13', 24395.8, 1.01),
    ('2026-08-14', 24366.0, 0.49), ('2026-08-17', 24287.7, 0.86),
    ('2026-08-18', 24154.9, 0.6), ('2026-08-19', 24078.3, 0.44),
    ('2026-08-20', 24231.8, 1.57), ('2026-08-21', 24252.0, 0.84),
    ('2026-08-24', 24219.0, 0.71), ('2026-08-25', 24334.5, 1.04),
    ('2026-08-26', 24207.8, 1.05), ('2026-08-27', 24090.8, 0.62),
    ('2026-08-28', 24175.7, 0.83), ('2026-08-31', 24080.4, 0.56),
    ('2026-09-01', 24055.8, 0.64), ('2026-09-02', 23914.5, 0.49),
    ('2026-09-04', 23897.7, 0.88), ('2026-09-07', 23779.2, 0.53),
    ('2026-09-08', 23635.1, 0.81),
]

ON = {"swing_exposure_enabled": "true"}


def _rows(until: str) -> list[dict]:
    return [{"date": d, "nifty_price": p, "advance_decline_ratio": a}
            for d, p, a in ROWS if d < until]


def test_off_by_default():
    from analysis.market_exposure import load_exposure, exposure_for_day
    with cfg_ctx({}):
        assert exposure_for_day("NEUTRAL", _rows("2026-09-09")).state == "NORMAL"


def test_real_sep_slide_is_a_correction():
    from analysis.market_exposure import exposure_for_day
    with cfg_ctx(ON):
        e = exposure_for_day("NEUTRAL", _rows("2026-09-09"))
    assert e.state == "CORRECTION", e
    assert e.max_new == 2 and e.size_mult == 0.5, e


def test_real_mid_aug_market_is_normal():
    from analysis.market_exposure import exposure_for_day
    with cfg_ctx(ON):
        e = exposure_for_day("NEUTRAL", _rows("2026-08-12"))
    assert e.state == "NORMAL", e


def test_risk_off_does_not_block_unless_switched():
    """24..30-Jul-2026 read RISK OFF and its plans won 69-78%: not a block by default."""
    from analysis.market_exposure import exposure_for_day, daily_cap
    with cfg_ctx(ON):
        e = exposure_for_day("RISK OFF", _rows("2026-08-12"))
    assert not e.block_new and daily_cap(10, e) == 10, e
    with cfg_ctx({**ON, "swing_risk_off_block_entries": "true"}):
        e = exposure_for_day("RISK OFF", _rows("2026-08-12"))
    assert e.block_new and daily_cap(10, e) == 0, e


def test_daily_cap():
    from analysis.market_exposure import Exposure, NORMAL, daily_cap
    assert daily_cap(10, NORMAL) == 10
    assert daily_cap(10, Exposure("CORRECTION", max_new=2)) == 2
    assert daily_cap(1, Exposure("CORRECTION", max_new=2)) == 1


def test_selection_filters_default_off():
    from analysis.market_exposure import exposure_for_day, selection_refusal
    plan = {"entry_zone_high": 100.0, "entry_timing_type": "CHASING", "rs_vs_nifty": 1.0}
    field = [{"rs_vs_nifty": v} for v in (1, 5, 9, 12, 20, 30)]
    with cfg_ctx(ON):
        e = exposure_for_day("NEUTRAL", _rows("2026-09-09"))
        assert selection_refusal(e, plan, field, 104.0) == ""
    with cfg_ctx({**ON, "swing_correction_refuse_chase": "true",
                  "swing_correction_rs_pctile": "67"}):
        e = exposure_for_day("NEUTRAL", _rows("2026-09-09"))
        assert "above the zone" in selection_refusal(e, plan, field, 104.0)
        assert "RS vs Nifty" in selection_refusal(e, dict(plan, entry_timing_type="OPTIMAL"),
                                                  field, 99.0)


def test_size_multiplier_halves_decide_qty():
    """The daemon passes size_mult through decide()'s vol_mult; assert on the consumer."""
    from analysis.trade_decision import decide
    plan = {"symbol": "X", "sector": "S", "industry": "I", "planned_stop": 95.0,
            "planned_target": 115.0, "entry_zone_low": 99.0, "entry_zone_high": 101.0}
    with cfg_ctx({}):
        full = decide(plan, 100.0, total_capital=300000, open_positions=[])
        half = decide(plan, 100.0, total_capital=300000, open_positions=[], vol_mult=0.5)
    assert full.qty and half.qty and abs(half.qty - full.qty // 2) <= 1, (full.qty, half.qty)


def test_load_exposure_reads_rows_before_today():
    from analysis.market_exposure import load_exposure

    class Q:
        def __init__(self, rows): self.rows, self.lt_val = rows, None
        def select(self, *_): return self
        def lt(self, _c, v): self.lt_val = v; return self
        def order(self, *_a, **_k): return self
        def limit(self, n): self.n = n; return self
        def execute(self):
            got = sorted((r for r in self.rows if r["date"] < self.lt_val),
                         key=lambda r: r["date"], reverse=True)[:self.n]
            return type("R", (), {"data": got})()

    class SB:
        def table(self, name):
            assert name == "market_regime"
            return Q(_rows("2099-01-01"))

    with cfg_ctx(ON):
        assert load_exposure(SB(), "2026-09-09").state == "CORRECTION"
        assert load_exposure(SB(), "2026-08-12").state == "NORMAL"


def test_daemon_call_sites_use_the_exposure():
    import inspect
    from intraday.engine import IntradayEngine
    enter = inspect.getsource(IntradayEngine._maybe_enter_swing)
    cands = inspect.getsource(IntradayEngine.evaluate_candidates)
    assert "daily_cap(" in enter and "selection_refusal(" in enter
    assert "_swing_exposure()" in cands and "exp_mult" in cands


TESTS = [
    ("exposure off by default", test_off_by_default),
    ("real 01..08-Sep-2026 rows read CORRECTION", test_real_sep_slide_is_a_correction),
    ("real mid-Aug-2026 rows read NORMAL", test_real_mid_aug_market_is_normal),
    ("RISK OFF does not block unless switched on", test_risk_off_does_not_block_unless_switched),
    ("daily cap", test_daily_cap),
    ("selection filters default off, work when on", test_selection_filters_default_off),
    ("size multiplier halves decide() qty", test_size_multiplier_halves_decide_qty),
    ("load_exposure reads rows before today", test_load_exposure_reads_rows_before_today),
    ("daemon call sites use the exposure", test_daemon_call_sites_use_the_exposure),
]
