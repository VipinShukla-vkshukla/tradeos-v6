"""
The allocator's absolute edge floor, and the bucket self-reference (10-Aug-2026).

WHAT THIS CATCHES
-----------------
Two defects that combined to let the INTRADAY book open a trade it had itself
measured as losing.

1. `hurdle()` was purely RELATIVE — a percentile of `allocation_decisions.edge`.
   A percentile of an all-negative population is negative, so the bar admitted
   proposals with negative net-of-cost expected R. Measured live on 10-Aug: the
   STRONG bucket's bar was -1.09359, DEVYANI/SDN cleared it at edge -1.0935
   (by 0.00009) and closed 99 seconds later at -0.813R. The DECLINEd proposals
   in that bucket averaged -1.0937 against the TAKEn ones' -1.0935 — two
   ten-thousandths of separation across 142 proposals.

   The bar also DECAYS toward its base as the session runs out (time_mult -> 1),
   so the system got MORE willing to take a measured loser the less time it had
   to work. These tests pin both: the floor binds, and it binds harder late in
   the day rather than less.

2. `_empirical_base()` decided segment-vs-pool on rows that included TODAY's.
   `regime_bucket()`'s STRONG branch was unreachable dead code until 05-Aug, so
   STRONG had no history; on 10-Aug 142 proposals landed in it and it crossed
   the 40-sample floor on rows the allocator had written that same morning.
   From 13:12 its bar was the 75th percentile of its own output. A closed loop.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows, bucket_filter=None):
        self._rows = rows
        self._bucket = bucket_filter
        self._eq = {}

    def select(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self): return self

    def eq(self, col, val):
        # A FRESH OBJECT, matching what supabase-py does NOT do — the real
        # builder mutates in place, which is the bug _empirical_base's
        # base_query() exists to work around. Copying here keeps the filtered
        # and unfiltered fetches genuinely independent so the pooling path is
        # exercised rather than accidentally short-circuited.
        q = _FakeQuery(self._rows, self._bucket)
        q._eq = {**self._eq, col: val}
        return q

    def order(self, col, *a, **k):
        """Paged reads sort on a unique key (config.fetch_all, 15-Aug-2026).

        LIMIT/OFFSET with no ORDER BY has no stable row order between
        requests, so pages repeat rows and drop others — 8324 matching rows
        came back as 5000 distinct on the live book. A fake without this
        method does not fail loudly: the AttributeError is swallowed by the
        caller's non-fatal except, or turns a paged fetch into an empty list,
        which is how test_setup_rehydration silently lost 4 of 7 checks.
        """
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, start, end):
        rows = self._rows
        for col, val in self._eq.items():
            if col == "regime_bucket":
                rows = [r for r in rows if r.get("regime_bucket") == val]
        return _FakeExec(rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _FakeQuery(self._rows)


def _rows(n, edge, bucket, trade_date, prefix="S"):
    return [{"edge": edge, "framework": "INTRADAY", "regime_bucket": bucket,
             "symbol": f"{prefix}{i}", "trade_date": trade_date} for i in range(n)]


# ── 1. THE FLOOR ────────────────────────────────────────────────────────────

def test_all_negative_population_cannot_produce_a_negative_bar():
    """The DEVYANI path. Demonstrated failing against the pre-fix code, which
    returned bar == -1.0937 here and would have TAKEN a -1.09 proposal."""
    from allocation.hurdle import hurdle
    sb = _SB(_rows(200, -1.0937, "STRONG", "2026-08-01"))
    with cfg_ctx({"alloc_hurdle_min_sample": "40", "alloc_hurdle_cold_start": "",
                  "alloc_edge_absolute_floor": "0.0"}):
        bar, inputs = hurdle("STRONG", 4, 20, "INTRADAY", sb, max_slots=4)
    assert bar == 0.0, f"bar {bar} — an all-negative population produced a negative bar"
    assert inputs["absolute_floor_applied"] is True
    # The unclamped percentile must still be recorded, or the decision cannot
    # be reconstructed (§19).
    assert inputs["base"] is not None and inputs["base"] < 0


def test_floor_binds_late_in_the_session_not_only_early():
    """The bar decays toward its base as time runs out. That is the moment the
    floor matters most, and the moment the old code was loosest."""
    from allocation.hurdle import hurdle
    sb = _SB(_rows(200, -0.5, "STRONG", "2026-08-01"))
    with cfg_ctx({"alloc_hurdle_min_sample": "40", "alloc_hurdle_cold_start": "",
                  "alloc_edge_absolute_floor": "0.0"}):
        early, _ = hurdle("STRONG", 4, 355, "INTRADAY", sb, max_slots=4)
        late,  _ = hurdle("STRONG", 4, 5,   "INTRADAY", sb, max_slots=4)
    assert early == 0.0 and late == 0.0, f"early {early}, late {late}"


def test_a_healthy_positive_population_is_untouched():
    """The floor must be inert when it is not needed — otherwise it is not a
    floor, it is a second cost charge, which is what _empirical_base's own
    comment (correctly) warns against."""
    from allocation.hurdle import hurdle
    edges = [0.05 + i * 0.001 for i in range(200)]
    rows = [{"edge": e, "framework": "INTRADAY", "regime_bucket": "STRONG",
             "symbol": f"S{i}", "trade_date": "2026-08-01"}
            for i, e in enumerate(edges)]
    with cfg_ctx({"alloc_hurdle_min_sample": "40", "alloc_hurdle_cold_start": "",
                  "alloc_edge_absolute_floor": "0.0"}):
        bar, inputs = hurdle("STRONG", 4, 200, "INTRADAY", _SB(rows), max_slots=4)
    assert bar > 0.05, f"bar {bar} — the percentile stopped selecting"
    assert inputs["absolute_floor_applied"] is False


def test_cold_start_is_exempt_from_the_floor():
    """"No opinion" and "measured bad" must not produce the same answer. A cold
    start stays permissive at -inf, or an allocator with no history becomes
    indistinguishable from one that refuses everything — the exact inversion of
    the rule in CLAUDE.md."""
    from allocation.hurdle import hurdle
    with cfg_ctx({"alloc_hurdle_min_sample": "40", "alloc_hurdle_cold_start": "",
                  "alloc_edge_absolute_floor": "0.0"}):
        bar, inputs = hurdle("STRONG", 4, 200, "INTRADAY", _SB([]), max_slots=4)
    assert bar == float("-inf"), f"bar {bar} — a cold start was floored"
    assert inputs.get("absolute_floor_applied") is False


def test_no_slots_still_returns_an_infinite_bar():
    """The floor must not accidentally convert "no slots" into "take anything"."""
    from allocation.hurdle import hurdle
    sb = _SB(_rows(200, -1.09, "STRONG", "2026-08-01"))
    with cfg_ctx({"alloc_edge_absolute_floor": "0.0"}):
        bar, _ = hurdle("STRONG", 0, 200, "INTRADAY", sb, max_slots=4)
    assert bar == float("inf")


# ── 2. THE BUCKET SELF-REFERENCE ────────────────────────────────────────────

def test_a_bucket_may_not_segment_on_todays_own_rows():
    """10-Aug: STRONG crossed the 40-sample floor on rows written that morning
    and its bar became the 75th percentile of its own output. Pre-fix this
    returned pooled_across_buckets == False; it must now pool."""
    from allocation.hurdle import _empirical_base
    from config import today_ist
    today = today_ist().isoformat()
    rows = (_rows(150, -1.09, "STRONG", today, "T")          # today only
            + _rows(150, 0.04, "WEAK", "2026-07-20", "W"))   # settled history
    with cfg_ctx({"alloc_hurdle_min_sample": "40", "alloc_hurdle_cold_start": "",
                  "alloc_hurdle_dedup_intraday": "false"}):
        base, meta, edges = _empirical_base("STRONG", "INTRADAY", _SB(rows))
    assert meta["pooled_across_buckets"] is True, \
        "STRONG segmented on rows from the session being decided"
    # And the bar it ends up with must come from the SETTLED history it pooled
    # into, not from the negative rows today wrote. `settled_n` describes the
    # population actually in use, which after pooling is the 150 prior-day WEAK
    # rows — so a positive base here is the proof that today's -1.09 rows did
    # not set their own bar.
    assert meta["settled_n"] == 150, f"settled_n {meta['settled_n']}"
    assert base > 0, f"base {base} — today's own rows still set the bar"


def test_a_bucket_with_settled_history_still_segments():
    """The fix must not disable segmentation generally — a bucket that has
    earned its own evidence on PRIOR days keeps using it, and today's rows
    still count toward the percentile once that right is earned."""
    from allocation.hurdle import _empirical_base
    from config import today_ist
    rows = (_rows(100, -1.09, "STRONG", "2026-07-15", "H")            # settled
            + _rows(50, -1.09, "STRONG", today_ist().isoformat(), "T")  # today
            + _rows(200, 0.04, "WEAK", "2026-07-20", "W"))
    with cfg_ctx({"alloc_hurdle_min_sample": "40", "alloc_hurdle_cold_start": "",
                  "alloc_hurdle_dedup_intraday": "false"}):
        base, meta, edges = _empirical_base("STRONG", "INTRADAY", _SB(rows))
    assert meta["pooled_across_buckets"] is False, "a settled bucket was forced to pool"
    assert meta["settled_n"] == 100
    assert meta["n"] == 150, f"today's rows were dropped from the percentile ({meta['n']})"


# ── 3. THE DYNAMIC EDGE FLOOR (Change 4, 2026-11-20) ─────────────────────────
#
# The static floor refused every proposal for a full session when the measured
# population was negative (2026-11-19: 510 of 610 refusals, zero entries). The
# dynamic floor decays across the session instead. These pin the parts that
# must hold: the toggle defaults OFF so nothing changes until armed; each time
# window maps to its configured value; the same negative population is clamped
# early but deployable late; and `bar_before_floor` survives so the ranking
# (floor_only_rank) is never lost.


def _at_ist(hour: int, minute: int):
    """A real IST-aware datetime at the given wall-clock time."""
    from datetime import datetime as _real_dt
    import config
    return config.IST.localize(_real_dt(2026, 11, 20, hour, minute))


_DYNAMIC_CFG = {
    "alloc_dynamic_edge_floor_enabled": "true",
    "alloc_edge_floor_open": "0.02", "alloc_edge_floor_mid": "0.00",
    "alloc_edge_floor_late": "-0.01", "alloc_edge_floor_close": "-0.03",
}


def test_dynamic_floor_defaults_off_and_reproduces_static_floor():
    """Unset toggle must behave exactly like the pre-change static floor —
    nothing about live allocation changes until the operator arms it."""
    from allocation.hurdle import hurdle
    sb = _SB(_rows(200, -1.0937, "STRONG", "2026-08-01"))
    with cfg_ctx({"alloc_hurdle_min_sample": "40", "alloc_hurdle_cold_start": "",
                  "alloc_edge_absolute_floor": "0.0"}):
        bar, inputs = hurdle("STRONG", 4, 20, "INTRADAY", sb, max_slots=4)
    assert bar == 0.0, f"default-off must reproduce the static floor, got {bar}"
    assert inputs["absolute_floor_applied"] is True


def test_dynamic_floor_windows_map_to_configured_values():
    from unittest.mock import patch
    from allocation.hurdle import _dynamic_edge_floor
    cases = [(9, 30, 0.02), (10, 59, 0.02),      # open window
             (11, 0, 0.00), (13, 59, 0.00),      # mid window
             (14, 0, -0.01), (14, 44, -0.01),     # late window
             (14, 45, -0.03), (15, 20, -0.03)]    # close window
    with cfg_ctx(_DYNAMIC_CFG):
        for h, m_, expected in cases:
            with patch("allocation.hurdle.datetime") as md:
                md.now.return_value = _at_ist(h, m_)
                got = _dynamic_edge_floor()
            assert got == expected, \
                f"{h:02d}:{m_:02d} expected {expected}, got {got}"


def test_dynamic_floor_clamps_early_but_deploys_late():
    """The whole point: the SAME negative population is refused at the open
    and deployable near the close, because the floor decays rather than being
    a single constant all day."""
    from unittest.mock import patch
    from allocation.hurdle import hurdle
    sb = _SB(_rows(200, -0.02, "STRONG", "2026-08-01"))

    with cfg_ctx({**_DYNAMIC_CFG, "alloc_hurdle_min_sample": "40",
                  "alloc_hurdle_cold_start": ""}):
        with patch("allocation.hurdle.datetime") as md:
            md.now.return_value = _at_ist(9, 30)
            early, ei = hurdle("STRONG", 4, 355, "INTRADAY", sb, max_slots=4)
        with patch("allocation.hurdle.datetime") as md:
            md.now.return_value = _at_ist(15, 0)
            late, li = hurdle("STRONG", 4, 20, "INTRADAY", sb, max_slots=4)

    assert early == 0.02, f"open floor should clamp -0.02 up to 0.02, got {early}"
    assert ei["absolute_floor_applied"] is True
    assert late == -0.02, f"close floor -0.03 must NOT clamp -0.02, got {late}"
    assert li["absolute_floor_applied"] is False


def test_bar_before_floor_survives_the_dynamic_clamp():
    """The ranking is the product: the pre-floor percentile must be published
    even when the dynamic floor binds, or floor_only_rank cannot exist."""
    from unittest.mock import patch
    from allocation.hurdle import hurdle
    sb = _SB(_rows(200, -0.02, "STRONG", "2026-08-01"))
    with cfg_ctx({**_DYNAMIC_CFG, "alloc_hurdle_min_sample": "40",
                  "alloc_hurdle_cold_start": ""}):
        with patch("allocation.hurdle.datetime") as md:
            md.now.return_value = _at_ist(9, 30)
            bar, inputs = hurdle("STRONG", 4, 20, "INTRADAY", sb, max_slots=4)
    assert bar == 0.02
    assert inputs["bar_before_floor"] == -0.02, (
        f"pre-floor bar must be preserved, got {inputs['bar_before_floor']}")
    assert inputs["base"] is not None and inputs["base"] < 0


TESTS = [
    ("an all-negative population cannot produce a negative bar",
     test_all_negative_population_cannot_produce_a_negative_bar),
    ("the floor binds late in the session, not only early",
     test_floor_binds_late_in_the_session_not_only_early),
    ("a healthy positive population is untouched",
     test_a_healthy_positive_population_is_untouched),
    ("cold start is exempt from the floor",
     test_cold_start_is_exempt_from_the_floor),
    ("no slots still returns an infinite bar",
     test_no_slots_still_returns_an_infinite_bar),
    ("a bucket may not segment on today's own rows",
     test_a_bucket_may_not_segment_on_todays_own_rows),
    ("a bucket with settled history still segments",
     test_a_bucket_with_settled_history_still_segments),
    ("dynamic floor defaults off and reproduces the static floor",
     test_dynamic_floor_defaults_off_and_reproduces_static_floor),
    ("dynamic floor windows map to configured values",
     test_dynamic_floor_windows_map_to_configured_values),
    ("dynamic floor clamps early but deploys late",
     test_dynamic_floor_clamps_early_but_deploys_late),
    ("bar_before_floor survives the dynamic clamp",
     test_bar_before_floor_survives_the_dynamic_clamp),
]
