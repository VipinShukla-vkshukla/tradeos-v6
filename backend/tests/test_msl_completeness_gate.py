"""
2026-09-15: the evening pipeline halted at step 19 (quality gate) with
C06_msl_completeness ERROR — "compute_msl only wrote 69 enriched rows
(expected >=70)". The regime was RISK OFF, which disables the MOM/SEC
screener engines; combined with the screener's own natural-cutoff selector,
that legitimately produced a 69-name shortlist. compute_msl enriched all 69
of them (100% complete, zero skipped) — there was no data problem at all,
just a flat historical floor (MSL_MIN_ENRICHED = 70) that assumed the
shortlist is always >=70 names.

C06's real job — per its own original docstring — is to catch step 14
(compute_msl) silently using a stale/wrong date, which drops enrichment to
near-zero. That failure mode is visible relative to what screen_stocks
actually wrote to master_shortlist for the day, so the gate now compares
enriched_count against the day's own shortlist size (MSL_MIN_ENRICHED_RATIO)
with a small absolute floor (MSL_MIN_ENRICHED_ABS) to still catch a
catastrophic near-empty run. See swing/compute/data_quality_monitor.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from swing.compute.data_quality_monitor import c06_msl_completeness


class _MSLTable:
    """Fakes just enough of `.table("master_shortlist")` for
    c06_msl_completeness: a count-only query (select(..., count="exact"))
    and a filtered-rows query (select(...).not_.is_("final_score", "null")).
    Both chains run to completion (synchronously) before the other starts,
    so reusing one mutable `_counting` flag across both calls is safe."""

    def __init__(self, shortlist_count: int, enriched_count: int):
        self._shortlist_count = shortlist_count
        self._enriched_count = enriched_count
        self._counting = False

    def select(self, *_args, **kwargs):
        self._counting = kwargs.get("count") == "exact"
        return self

    def eq(self, *_args):
        return self

    def limit(self, *_args):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_args):
        return self

    def execute(self):
        if self._counting:
            return SimpleNamespace(count=self._shortlist_count, data=[])
        rows = [{"symbol": f"SYM{i}", "final_score": 50.0} for i in range(self._enriched_count)]
        return SimpleNamespace(data=rows, count=None)


class _EmptyTable:
    """Stands in for msl_history (and anything else) — always empty, which
    is exactly what C06's score-jump sub-check needs to stay quiet."""

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args):
        return self

    @property
    def not_(self):
        return self

    def is_(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=[], count=0)


class _FakeSB:
    def __init__(self, shortlist_count: int, enriched_count: int):
        self._msl = _MSLTable(shortlist_count, enriched_count)

    def table(self, name):
        if name == "master_shortlist":
            return self._msl
        return _EmptyTable()


def test_full_enrichment_of_shrunk_risk_off_shortlist_passes():
    """The exact 2026-09-15 shape: 69 shortlisted, 69 enriched (100%
    complete). Must pass — this is a healthy run, not a defect."""
    sb = _FakeSB(shortlist_count=69, enriched_count=69)
    result = c06_msl_completeness(sb, "2026-09-15")
    assert result["ok"], result["message"]
    assert result["severity"] == "OK"


def test_wrong_date_disaster_still_fails():
    """The failure C06 exists to catch: step 14 silently used a stale/wrong
    date and enriched almost nothing of a normal-sized shortlist. A check
    that cannot fail is not a check."""
    sb = _FakeSB(shortlist_count=69, enriched_count=2)
    result = c06_msl_completeness(sb, "2026-09-15")
    assert not result["ok"]
    assert result["severity"] == "ERROR"
    assert "step 14 may have used wrong date" in result["message"]


def test_partial_enrichment_below_ratio_fails():
    """72% enriched (50/69) — well above the absolute floor but below the
    90% completeness ratio. Must still fail: compute_msl left real rows
    un-enriched."""
    sb = _FakeSB(shortlist_count=69, enriched_count=50)
    result = c06_msl_completeness(sb, "2026-09-15")
    assert not result["ok"]
    assert result["severity"] == "ERROR"


def test_ratio_boundary_passes_just_above_90_percent():
    """63/69 = 91.3%, just clears the 90% ratio floor. Confirms the gate is
    genuinely reachable, not just wide open."""
    sb = _FakeSB(shortlist_count=69, enriched_count=63)
    result = c06_msl_completeness(sb, "2026-09-15")
    assert result["ok"], result["message"]


def test_near_empty_shortlist_still_fails_absolute_floor():
    """5 shortlisted, 5 enriched — 100% complete by ratio, but the absolute
    floor (MSL_MIN_ENRICHED_ABS=10) must still catch a screener that
    produced almost nothing at all (e.g. a universe fetch that broke)."""
    sb = _FakeSB(shortlist_count=5, enriched_count=5)
    result = c06_msl_completeness(sb, "2026-09-15")
    assert not result["ok"]
    assert result["severity"] == "ERROR"


TESTS = [
    ("full enrichment of a shrunk RISK OFF shortlist passes (2026-09-15 shape)",
     test_full_enrichment_of_shrunk_risk_off_shortlist_passes),
    ("wrong-date disaster (near-zero enrichment) still fails",
     test_wrong_date_disaster_still_fails),
    ("partial enrichment below the 90% ratio still fails",
     test_partial_enrichment_below_ratio_fails),
    ("ratio boundary passes just above 90%",
     test_ratio_boundary_passes_just_above_90_percent),
    ("near-empty shortlist still fails the absolute floor",
     test_near_empty_shortlist_still_fails_absolute_floor),
]
