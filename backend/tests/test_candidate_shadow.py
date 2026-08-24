"""
intraday/candidate_shadow.py — Stage D6, 24-Aug-2026 (docs/TRADEOS_
ROADMAP.md, Track D, branch feat/intraday-evolution).

WHAT THIS COVERS
-----------------
load_active_candidates(): reads only ENGINE_CANDIDATE + APPROVED
rows, skips (does not crash on) a row from_proposal() cannot use. check():
disabled by default is a hard no-op (no DB calls at all — same discipline
D4's set_depth_symbols() gate was built to, after the bug that switch's
own absence caused); writes go ONLY to intraday_candidate_shadow, never
anywhere else; the same (proposal, symbol) is never logged twice on one
trade_date even across repeated check() calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

from tests import cfg_ctx


class _FakeTable:
    """Enough of the Supabase query builder for one .table(name).select/
    eq/insert().execute() chain, recording every insert for inspection."""
    def __init__(self, store: dict):
        self._store = store
        self._name = None
        self._filters = {}

    def table(self, name):
        self._name = name
        self._filters = {}
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def insert(self, row):
        self._store.setdefault(self._name, []).append(dict(row))
        return self

    def execute(self):
        class _R:
            pass
        r = _R()
        if self._name == "brain_proposals":
            r.data = [row for row in self._store.get("brain_proposals", [])
                     if all(row.get(k) == v for k, v in self._filters.items())]
        elif self._name == "intraday_candidate_shadow":
            r.data = [row for row in self._store.get("intraday_candidate_shadow", [])
                     if all(row.get(k) == v for k, v in self._filters.items())]
        else:
            r.data = []
        return r


class _FakeSB:
    def __init__(self, store: dict):
        self._store = store

    def table(self, name):
        return _FakeTable(self._store).table(name)


def _proposal(id_, target_key="UNSEEN/ADX > 25 (trending)",
             status="APPROVED", proposal_type="ENGINE_CANDIDATE",
             confidence=0.55):
    return {"id": id_, "proposal_type": proposal_type, "status": status,
            "target_key": target_key, "confidence": confidence,
            "evidence": {"summary": "x", "avg_move_pct": 3.0, "lift": 2.0, "n_miss": 10}}


def _ctx(**over):
    from intraday.strategies.base import Bar, SymbolContext
    # 3 bars below vwap=100.0 (a real flush), the 4th crossing back above
    # on its own close -- the exact single-bar-reclaim shape evaluate()
    # requires. Same verified-correct shape as test_candidate_template.py's
    # own _reclaim_bars().
    bars = [
        Bar(datetime(2026, 8, 24, 10, 0), 99.6, 99.9, 99.4, 99.6, 1000),
        Bar(datetime(2026, 8, 24, 10, 1), 99.6, 99.8, 99.5, 99.7, 1000),
        Bar(datetime(2026, 8, 24, 10, 2), 99.7, 99.9, 99.6, 99.75, 1000),
        Bar(datetime(2026, 8, 24, 10, 3), 99.75, 100.6, 99.7, 100.5, 1000),
    ]
    base = dict(symbol="TEST", ltp=100.5, bars=bars, vwap=100.0, adx_daily=30.0)
    base.update(over)
    return SymbolContext(**base)


class _FakeEngine:
    def __init__(self, sb, contexts):
        self.sb = sb
        self._contexts = contexts


@contextmanager
def _frozen_phase(phase: str):
    """
    check() calls intraday.session.session_state() with no argument,
    which reads the WALL CLOCK — correct for production (candidate_
    shadow.py runs on the live slow timer and must see the real phase),
    unusable for a test that needs a specific phase regardless of when it
    happens to run. session_state() already accepts an explicit `now`
    for exactly this reason; check() just never threads it through, since
    production never wants the override. Monkeypatch the module attribute
    for the duration of the test instead, restored after — the local
    `from intraday.session import session_state` inside check() looks up
    the CURRENT module attribute at call time, so this reaches it.
    """
    import intraday.session as session_mod

    class _Frozen:
        pass
    frozen = _Frozen()
    frozen.phase = phase

    original = session_mod.session_state
    session_mod.session_state = lambda *a, **k: frozen
    try:
        yield
    finally:
        session_mod.session_state = original


# ── load_active_candidates() ────────────────────────────────────────────────

def test_load_active_candidates_reads_only_shadow_approved_engine_candidates():
    from intraday.candidate_shadow import load_active_candidates
    store = {"brain_proposals": [
        _proposal(1, status="APPROVED"),
        _proposal(2, status="PENDING"),
        _proposal(3, status="APPROVED", proposal_type="FEATURE_FILTER"),
    ]}
    sb = _FakeSB(store)
    cands = load_active_candidates(sb)
    assert [c.proposal_id for c in cands] == [1]


def test_load_active_candidates_skips_unparseable_rows_without_crashing():
    from intraday.candidate_shadow import load_active_candidates
    store = {"brain_proposals": [
        _proposal(1, status="APPROVED"),
        _proposal(2, status="APPROVED", target_key="UNSEEN/gap down > 1%"),  # covered by GDB
    ]}
    sb = _FakeSB(store)
    cands = load_active_candidates(sb)
    assert [c.proposal_id for c in cands] == [1]


# ── check() ──────────────────────────────────────────────────────────────────

def test_check_is_a_hard_noop_when_disabled():
    """No DB call at all when the switch is off -- same discipline D4's
    set_depth_symbols() gate needed after the bug where the switch was
    checked too late to actually prevent the live side effect."""
    from intraday.candidate_shadow import check
    store = {"brain_proposals": [_proposal(1)]}
    sb = _FakeSB(store)
    engine = _FakeEngine(sb, {"TEST": _ctx()})
    with cfg_ctx({"intraday_candidate_shadow_enabled": "false"}):
        n = check(engine)
    assert n == 0
    assert store.get("intraday_candidate_shadow", []) == []


def test_check_logs_a_detection_when_enabled():
    from intraday.candidate_shadow import check
    store = {"brain_proposals": [_proposal(1)]}
    sb = _FakeSB(store)
    engine = _FakeEngine(sb, {"TEST": _ctx()})
    with cfg_ctx({"intraday_candidate_shadow_enabled": "true",
                  "candidate_max_risk_pct": "5.0"}), _frozen_phase("OPENING"):
        n = check(engine)
    assert n == 1
    rows = store["intraday_candidate_shadow"]
    assert len(rows) == 1
    assert rows[0]["proposal_id"] == 1
    assert rows[0]["symbol"] == "TEST"
    assert rows[0]["direction"] == "LONG"


def test_check_never_writes_to_any_table_but_the_shadow_one():
    from intraday.candidate_shadow import check
    store = {"brain_proposals": [_proposal(1)]}
    sb = _FakeSB(store)
    engine = _FakeEngine(sb, {"TEST": _ctx()})
    with cfg_ctx({"intraday_candidate_shadow_enabled": "true",
                  "candidate_max_risk_pct": "5.0"}), _frozen_phase("OPENING"):
        check(engine)
    assert set(store.keys()) <= {"brain_proposals", "intraday_candidate_shadow"}


def test_check_deduplicates_within_the_same_call():
    """Two engines... one context, called twice in the same process --
    the second call must not re-log the same (proposal, symbol) today."""
    from intraday.candidate_shadow import check
    store = {"brain_proposals": [_proposal(1)]}
    sb = _FakeSB(store)
    engine = _FakeEngine(sb, {"TEST": _ctx()})
    with cfg_ctx({"intraday_candidate_shadow_enabled": "true",
                  "candidate_max_risk_pct": "5.0"}), _frozen_phase("OPENING"):
        first = check(engine)
        second = check(engine)
    assert first == 1
    assert second == 0, "the same (proposal, symbol) must not be logged twice in one day"
    assert len(store["intraday_candidate_shadow"]) == 1


def test_check_returns_zero_with_no_engine_or_no_sb():
    from intraday.candidate_shadow import check
    assert check(None) == 0
    with cfg_ctx({"intraday_candidate_shadow_enabled": "true"}):
        assert check(_FakeEngine(None, {})) == 0


TESTS = [
    ("load_active_candidates reads only APPROVED ENGINE_CANDIDATEs",
     test_load_active_candidates_reads_only_shadow_approved_engine_candidates),
    ("load_active_candidates skips unparseable rows without crashing",
     test_load_active_candidates_skips_unparseable_rows_without_crashing),
    ("check is a hard no-op when disabled", test_check_is_a_hard_noop_when_disabled),
    ("check logs a detection when enabled", test_check_logs_a_detection_when_enabled),
    ("check never writes to any table but the shadow one",
     test_check_never_writes_to_any_table_but_the_shadow_one),
    ("check deduplicates within the same call", test_check_deduplicates_within_the_same_call),
    ("check returns zero with no engine or no sb", test_check_returns_zero_with_no_engine_or_no_sb),
]
