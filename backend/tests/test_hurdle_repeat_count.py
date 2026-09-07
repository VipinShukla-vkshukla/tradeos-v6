"""
hurdle.py's repeat_count expansion — 08-Sep-2026, migration 129.

_empirical_base() must expand each row's edge by its repeat_count before
taking a percentile — reconstructing the exact population the OLD
one-row-per-15s-cycle scheme produced, not an approximation of it.
repeat_count=1 (every row, while alloc_write_collapse_swing_enabled is off,
and always for INTRADAY) must be a no-op: reads identical to before this
column existed. This is what makes the write-time collapse
(tests/test_alloc_write_collapse.py) safe to arm — the bar itself is
provably unaffected, unlike the reverted alloc_hurdle_dedup_swing, which
discarded rows outright and moved the bar. See docs/FINDINGS.md, 08-Sep-2026.
"""
from __future__ import annotations

from tests import cfg_ctx
from tests.test_hurdle_dedup import _FakeSB


def _row(symbol, trade_date, edge, repeat_count=1):
    return {"symbol": symbol, "edge": edge, "framework": "SWING",
            "regime_bucket": "STRONG", "trade_date": trade_date,
            "repeat_count": repeat_count}


def test_repeat_count_of_one_on_every_row_is_a_no_op():
    """The default, and every row written today or by any non-collapsing
    path: must produce IDENTICAL output to a fake response with no
    repeat_count column at all (KeyError-safe via .get)."""
    from allocation.hurdle import _empirical_base
    rows_with = [_row(f"SYM{i}", "2026-09-01", 0.01 * i, repeat_count=1) for i in range(50)]
    rows_without = [{"symbol": r["symbol"], "edge": r["edge"], "framework": r["framework"],
                     "regime_bucket": r["regime_bucket"], "trade_date": r["trade_date"]}
                    for r in rows_with]
    with cfg_ctx({"alloc_hurdle_min_sample": "10"}):
        _, _, edges_with = _empirical_base("STRONG", "SWING", _FakeSB(rows_with))
    with cfg_ctx({"alloc_hurdle_min_sample": "10"}):
        _, _, edges_without = _empirical_base("STRONG", "SWING", _FakeSB(rows_without))
    assert sorted(edges_with) == sorted(edges_without), (
        "repeat_count=1 (or missing entirely) must be a byte-identical no-op")


def test_repeat_count_reconstructs_the_exact_collapsed_multiset():
    """The actual point: one physical row with repeat_count=N must produce
    the SAME population a percentile would see if that row had instead been
    written N separate times at 15s cadence — not N/2, not a mean, N."""
    from allocation.hurdle import _empirical_base
    # One collapsed anchor representing 600 identical 15s-cycle observations
    # at edge 0.10 (the NETWEB/TVSMOTOR shape), plus 40 real singles at 0.01.
    rows = [_row("REPEATED", "2026-09-01", 0.10, repeat_count=600)]
    rows += [_row(f"SYM{i}", "2026-09-01", 0.01, repeat_count=1) for i in range(40)]
    with cfg_ctx({"alloc_hurdle_min_sample": "10"}):
        base, meta, edges = _empirical_base("STRONG", "SWING", _FakeSB(rows))
    assert len(edges) == 640, (
        f"expected 600 expanded + 40 singles = 640 population entries, got {len(edges)}")
    assert edges.count(0.10) == 600
    assert edges.count(0.01) == 40


def test_repeat_count_expansion_matches_the_pre_collapse_raw_bar_exactly():
    """The precise claim migration 129 makes: replaying the SAME real
    population two ways -- (a) one row per original observation, unweighted
    (today's shape), (b) collapsed rows with repeat_count, expanded back --
    must produce an IDENTICAL p75/p95 bar. This is the property that makes
    write-time collapse different in kind from the reverted mean-collapse
    dedup, which measurably moved the bar."""
    from allocation.hurdle import _empirical_base, _quantile
    raw_rows = ([_row("REPEATED", "2026-09-01", 0.10) for _ in range(600)]
                + [_row(f"SYM{i}", "2026-09-01", 0.01) for i in range(40)])
    collapsed_rows = [_row("REPEATED", "2026-09-01", 0.10, repeat_count=600)]
    collapsed_rows += [_row(f"SYM{i}", "2026-09-01", 0.01, repeat_count=1) for i in range(40)]

    with cfg_ctx({"alloc_hurdle_min_sample": "10"}):
        raw_base, _, raw_edges = _empirical_base("STRONG", "SWING", _FakeSB(raw_rows))
    with cfg_ctx({"alloc_hurdle_min_sample": "10"}):
        collapsed_base, _, collapsed_edges = _empirical_base(
            "STRONG", "SWING", _FakeSB(collapsed_rows))

    assert sorted(raw_edges) == sorted(collapsed_edges), (
        "the reconstructed population must be identical to the raw one, "
        "not merely close")
    assert raw_base == collapsed_base, (
        f"p75 must be byte-identical: raw={raw_base} collapsed={collapsed_base}")
    for q in (0.75, 0.95):
        assert _quantile(sorted(raw_edges), q) == _quantile(sorted(collapsed_edges), q)


TESTS = [
    ("repeat_count=1 (or absent) is a byte-identical no-op",
     test_repeat_count_of_one_on_every_row_is_a_no_op),
    ("repeat_count reconstructs the exact collapsed multiset",
     test_repeat_count_reconstructs_the_exact_collapsed_multiset),
    ("repeat_count expansion matches the pre-collapse raw bar exactly",
     test_repeat_count_expansion_matches_the_pre_collapse_raw_bar_exactly),
]
