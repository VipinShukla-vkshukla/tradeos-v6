"""
allocation_decisions storage — 27-Aug-2026.

hurdle_inputs/meta (both jsonb) average ~761 of a ~1000-1100 byte row (70-
75%), on the ~95%+ of rows that are DECLINE/DEFER and never become a trade.
Grepped every read of this table first: hurdle.py's population query selects
symbol/edge/framework/regime_bucket/trade_date only, outcomes.py's select/
update never name either JSON column, and allocator_report.py's select("*")
fetches everything but its output code only ever reads native_rank/symbol/
product/trade_date/verdict/shadow/outcome_r/edge/hurdle/reason/
prior_below_floor/source. Nothing reads hurdle_inputs or meta back. So a
DECLINE/DEFER row can have both nulled at write time with zero effect on any
consumer; a TAKE keeps both in full since it is a real trade's permanent
record. See docs/FINDINGS.md, 27-Aug-2026, for the full investigation.
"""
from allocation.allocator import Allocator
from allocation.proposal import Proposal
from allocation.policies import TAKE, DECLINE, DEFER


def _proposal(**kw) -> Proposal:
    base = dict(symbol="TESTSTK", framework="SWING", product="CNC",
                entry=100.0, stop=95.0, target=115.0, quantity=10,
                source="CTL", native_rank=80.0, meta={"screener_score": 62})
    base.update(kw)
    return Proposal(**base)


def _verdict(verdict: str, **kw) -> dict:
    base = dict(proposal=_proposal(), verdict=verdict, edge=0.05, hurdle=0.03,
                hurdle_inputs={"lookback_days": 60, "percentile": 0.75,
                               "population": "allocation_decisions.edge"},
                regime_bucket="NEUTRAL")
    base.update(kw)
    return base


def test_decline_row_has_both_json_columns_nulled():
    row = Allocator.__new__(Allocator)._record(_verdict(DECLINE))
    assert row["hurdle_inputs"] is None
    assert row["meta"] is None


def test_defer_row_has_both_json_columns_nulled():
    row = Allocator.__new__(Allocator)._record(_verdict(DEFER))
    assert row["hurdle_inputs"] is None
    assert row["meta"] is None


def test_take_row_keeps_both_json_columns_in_full():
    row = Allocator.__new__(Allocator)._record(_verdict(TAKE))
    assert row["hurdle_inputs"] == {"lookback_days": 60, "percentile": 0.75,
                                     "population": "allocation_decisions.edge"}
    assert row["meta"] == '{"screener_score": 62}'


def test_slimming_never_touches_the_columns_hurdle_or_outcomes_actually_read():
    """The one property that actually matters: edge, regime_bucket,
    trade_date-equivalents (framework/symbol here), and verdict survive a
    DECLINE row's slimming completely untouched — the population hurdle()
    reads and outcomes.py writes back to is byte-for-byte identical to what
    it would have been without this change."""
    v = _verdict(DECLINE)
    row = Allocator.__new__(Allocator)._record(v)
    assert row["edge"] == v["edge"]
    assert row["regime_bucket"] == v["regime_bucket"]
    assert row["symbol"] == v["proposal"].symbol
    assert row["framework"] == v["proposal"].framework
    assert row["verdict"] == DECLINE


def test_take_verdict_is_the_only_thing_that_decides_slimming_not_shadow_or_source():
    """Confirms slimming keys ONLY on verdict == TAKE, not on any other field
    that happens to vary between rows (shadow flag, source engine, framework)
    -- a DECLINE stays slimmed regardless of what else is true about it."""
    for kw in ({"proposal": _proposal(framework="INTRADAY", product="MIS")},
               {"proposal": _proposal(source="ORB")}):
        v = _verdict(DECLINE, **kw)
        row = Allocator.__new__(Allocator)._record(v)
        assert row["hurdle_inputs"] is None
        assert row["meta"] is None


TESTS = [
    ("DECLINE row has both JSON columns nulled",
     test_decline_row_has_both_json_columns_nulled),
    ("DEFER row has both JSON columns nulled",
     test_defer_row_has_both_json_columns_nulled),
    ("TAKE row keeps both JSON columns in full",
     test_take_row_keeps_both_json_columns_in_full),
    ("slimming never touches what hurdle()/outcomes.py actually read",
     test_slimming_never_touches_the_columns_hurdle_or_outcomes_actually_read),
    ("verdict==TAKE is the only thing that decides slimming",
     test_take_verdict_is_the_only_thing_that_decides_slimming_not_shadow_or_source),
]
