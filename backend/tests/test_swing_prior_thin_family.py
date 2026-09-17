"""
A family too thin AFTER swing_data_since must not score as if it had never been
measured. RVS is the worst swing family on record (-0.45R over 50 plans), but
only 16 of its 31 plans since 01-Sep-2026 resolved — below the 30 floor — so it
was treated as a neutral 0 prior and ranked FIRST of the three families
(edge/day RVS -0.028 vs CONTINUATION -0.146).

CLAUDE.md: "No opinion" and "measured bad" must not give the same answer. A
family with genuinely no history keeps the neutral prior; one with real history
falls back to it.
"""

from __future__ import annotations

from tests import cfg_ctx

CUT = "2026-09-01"


def _row(strategy, date, ret):
    # entry 100, stop 90 -> risk 10%, so R = ret / 10
    return {"strategy": strategy, "outcome_category": "TARGET", "outcome_return_pct": ret,
            "outcome_entered": True, "entry_zone_high": 100.0, "planned_stop": 90.0,
            "symbol": f"S{abs(hash((strategy, date, ret))) % 997}", "date": date}


def _rows():
    rows = []
    # CONTINUATION: 40 plans since the cutoff, mildly negative
    for i in range(40):
        rows.append(_row("CTL", f"2026-09-{(i % 15) + 1:02d}", -4.0))
    # RVS: 5 since the cutoff (below floor), 45 before it, badly negative
    for i in range(5):
        rows.append(_row("RVS", f"2026-09-{(i % 15) + 1:02d}", -1.0))
    for i in range(45):
        rows.append(_row("RVS", f"2026-07-{(i % 28) + 1:02d}", -6.0))
    return rows


class _Q:
    def __init__(self, rows):
        self.rows, self.since, self.lo, self.hi = rows, None, 0, 10 ** 9
    @property
    def not_(self):
        return self
    def select(self, *_):
        return self
    def is_(self, *_):
        return self
    def gte(self, col, v):
        if col == "date":
            self.since = v
        return self
    def order(self, *_a, **_k):
        return self
    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self
    def execute(self):
        rows = [r for r in self.rows if self.since is None or r["date"] >= self.since]
        rows = sorted(rows, key=lambda r: (r["symbol"], r["date"]))
        return type("R", (), {"data": rows[self.lo:self.hi + 1]})()


class _SB:
    def __init__(self, rows):
        self.rows = rows
    def table(self, name):
        assert name == "signal_output_daily"
        return _Q(self.rows)


def _priors(flags):
    from allocation.scoring import swing_priors
    with cfg_ctx({"swing_data_since": CUT, **flags}):
        return swing_priors(_SB(_rows()))


def test_thin_family_falls_back_to_its_full_history():
    p = _priors({"swing_prior_thin_family_fallback": "true"})["SWING/RVS"]
    assert not p.below_floor, p
    assert p.mean_r < -0.5, f"expected RVS's own measured history (~-0.6R), got {p.mean_r}"


def test_thin_family_no_longer_outranks_a_measured_family():
    pri = _priors({"swing_prior_thin_family_fallback": "true"})
    assert pri["SWING/RVS"].mean_r < pri["SWING/CONTINUATION"].mean_r, (
        {k: v.mean_r for k, v in pri.items()})


def test_switch_off_keeps_todays_behaviour():
    p = _priors({})["SWING/RVS"]
    assert p.below_floor, p


def test_family_with_no_history_at_all_stays_neutral():
    """A genuinely new engine must not inherit anyone else's bad record."""
    from allocation.scoring import swing_priors
    rows = [r for r in _rows() if r["strategy"] != "RVS"]
    rows += [_row("RVS", "2026-09-05", -1.0)]
    with cfg_ctx({"swing_data_since": CUT, "swing_prior_thin_family_fallback": "true"}):
        pri = swing_priors(_SB(rows))
    assert pri["SWING/RVS"].below_floor, pri["SWING/RVS"]


def test_well_sampled_family_is_untouched_by_the_fallback():
    on = _priors({"swing_prior_thin_family_fallback": "true"})["SWING/CONTINUATION"]
    off = _priors({})["SWING/CONTINUATION"]
    assert abs(on.mean_r - off.mean_r) < 1e-9 and on.n == off.n, (on, off)


TESTS = [
    ("thin family falls back to its full history", test_thin_family_falls_back_to_its_full_history),
    ("thin family no longer outranks a measured family",
     test_thin_family_no_longer_outranks_a_measured_family),
    ("switch off keeps today's behaviour", test_switch_off_keeps_todays_behaviour),
    ("family with no history at all stays neutral", test_family_with_no_history_at_all_stays_neutral),
    ("well-sampled family untouched by the fallback",
     test_well_sampled_family_is_untouched_by_the_fallback),
]
