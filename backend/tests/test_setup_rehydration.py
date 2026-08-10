"""
A daemon restart must not re-record the morning (10-Aug-2026).

WHAT THIS CATCHES
-----------------
`_setup_is_new` stops the 15-second loop writing the same detection over and
over, but the map it consults lived only in memory. `systemd` restarting the
daemon mid-session handed the day a blank one, so every setup already recorded
that morning counted as new again. Observed live on 10-Aug: the service
restarted at 13:11:57 and the very next AI review went from 11 setups to 31.

That is not merely duplicate rows. `intraday_setups` is the population
`scoring.intraday_priors()` builds every prior from, and `allocation_decisions`
is the population `hurdle()` takes its percentile of — so a restart re-inflates
both, and a re-inflated arrival population LOWERS the bar. A daemon restart was
quietly making the allocator more permissive for the rest of the session.

THE KEY IS THE ENGINE, THE COLUMN IS THE FAMILY. `_record_setup` writes
`strategy = family` on purpose (so the learning loop groups ORB's 30 detections
into the family's 230) and keeps the engine that fired in `meta.sub_engine`,
while `_setup_is_new` keys on the ENGINE. Rehydrating off the column alone
would build keys the live dedup test can never match — it would report a count
and change nothing, which is this project's signature failure shape.
"""

from __future__ import annotations

import json


class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): return _FakeExec(self._rows)


class _FakeExec:
    def __init__(self, rows): self.data = rows


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _FakeQuery(self._rows)


class _Setup:
    def __init__(self, symbol, strategy, entry):
        self.symbol, self.strategy, self.entry = symbol, strategy, entry


def _engine(rows):
    """An IntradayEngine with just enough state to exercise the two methods,
    built without running __init__ so no database or broker is touched."""
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.sb = _SB(rows)
    eng._recorded = {}
    return eng


def test_rehydration_keys_on_the_engine_not_the_family():
    """A GAP detection is stored under family ORB. The dedup map must come back
    keyed 'PAYTM:GAP', or the live test asking about GAP never matches and the
    rehydration is decoration."""
    eng = _engine([{"symbol": "PAYTM", "strategy": "ORB", "entry": 101.5,
                    "cost_verdict": "TAKEN",
                    "meta": json.dumps({"sub_engine": "GAP"})}])
    eng._rehydrate_recorded()
    assert "PAYTM:GAP" in eng._recorded, f"keys: {sorted(eng._recorded)}"
    assert eng._recorded["PAYTM:GAP"] == (101.5, "TAKEN")


def test_a_rehydrated_setup_is_not_new_again():
    """The whole point: after a restart the same setup at the same price must
    not be recorded a second time."""
    from intraday.engine import IntradayEngine
    eng = _engine([{"symbol": "PAYTM", "strategy": "ORB", "entry": 101.5,
                    "cost_verdict": "TAKEN",
                    "meta": json.dumps({"sub_engine": "GAP"})}])
    eng._rehydrate_recorded()
    assert IntradayEngine._setup_is_new(
        eng, _Setup("PAYTM", "GAP", 101.5), "TAKEN") is False


def test_a_materially_moved_level_is_still_new_after_rehydration():
    """Rehydration must not freeze the book. A genuinely different trade at a
    different price is still a new detection."""
    from intraday.engine import IntradayEngine
    eng = _engine([{"symbol": "PAYTM", "strategy": "ORB", "entry": 100.0,
                    "cost_verdict": "TAKEN",
                    "meta": json.dumps({"sub_engine": "GAP"})}])
    eng._rehydrate_recorded()
    assert IntradayEngine._setup_is_new(
        eng, _Setup("PAYTM", "GAP", 110.0), "TAKEN") is True


def test_a_verdict_flip_is_still_new_after_rehydration():
    """A setup becoming takeable (or ceasing to be) is a real change and must
    survive the restart path."""
    from intraday.engine import IntradayEngine
    eng = _engine([{"symbol": "PAYTM", "strategy": "ORB", "entry": 100.0,
                    "cost_verdict": "REJECTED_COST",
                    "meta": json.dumps({"sub_engine": "GAP"})}])
    eng._rehydrate_recorded()
    assert IntradayEngine._setup_is_new(
        eng, _Setup("PAYTM", "GAP", 100.0), "TAKEN") is True


def test_meta_may_be_a_dict_or_a_string():
    """PostgREST returns a jsonb column as a dict and a text column as a
    string. Both must work, or the fix is one schema change from silent."""
    eng = _engine([{"symbol": "A", "strategy": "ORB", "entry": 10.0,
                    "cost_verdict": "TAKEN", "meta": {"sub_engine": "PDL"}},
                   {"symbol": "B", "strategy": "VWR", "entry": 20.0,
                    "cost_verdict": "TAKEN", "meta": '{"sub_engine": "PBK"}'}])
    eng._rehydrate_recorded()
    assert "A:PDL" in eng._recorded and "B:PBK" in eng._recorded


def test_missing_meta_falls_back_to_the_family_column():
    """A row written before sub_engine existed must still contribute a key
    rather than being dropped."""
    eng = _engine([{"symbol": "C", "strategy": "RNG", "entry": 5.0,
                    "cost_verdict": "TAKEN", "meta": None}])
    eng._rehydrate_recorded()
    assert "C:RNG" in eng._recorded


def test_a_query_failure_leaves_an_empty_map_rather_than_raising():
    """Non-fatal by design — an empty map is the old behaviour, which is
    survivable. It must not take the daemon down at startup."""
    class _Boom:
        def table(self, name): raise RuntimeError("PostgREST 503")
    from intraday.engine import IntradayEngine
    eng = IntradayEngine.__new__(IntradayEngine)
    eng.sb, eng._recorded = _Boom(), {}
    eng._rehydrate_recorded()
    assert eng._recorded == {}


TESTS = [
    ("rehydration keys on the engine, not the family",
     test_rehydration_keys_on_the_engine_not_the_family),
    ("a rehydrated setup is not new again",
     test_a_rehydrated_setup_is_not_new_again),
    ("a materially moved level is still new after rehydration",
     test_a_materially_moved_level_is_still_new_after_rehydration),
    ("a verdict flip is still new after rehydration",
     test_a_verdict_flip_is_still_new_after_rehydration),
    ("meta may be a dict or a string",
     test_meta_may_be_a_dict_or_a_string),
    ("missing meta falls back to the family column",
     test_missing_meta_falls_back_to_the_family_column),
    ("a query failure leaves an empty map rather than raising",
     test_a_query_failure_leaves_an_empty_map_rather_than_raising),
]
