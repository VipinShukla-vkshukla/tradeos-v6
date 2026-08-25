"""
Track E, Stage E7 continuation (F-81, migration 114) — position scale-in
EXECUTION, closing the gap `evaluate_scale_in()`'s own docstring named
since F-78: "never places an order, never writes to open_positions, no
config switch to arm".

Tests `evaluate_scale_in()` itself (already covered by
tests/test_stage_e7_scale_in.py) is unchanged — this module covers the
NEW execution layer in intraday/engine.py: two switches (both OFF by
default, mirroring swing_auto_entry/swing_live_auto_entry), the submit-
then-confirm shape mirroring `_maybe_enter_swing`/`_resolve_pending_fills`,
and — the point of the whole exercise — that a confirmed add NEVER writes
entry_price/planned_stop/active_sl/planned_target/target_price. Those four
fields are what evaluate_exit()'s gain_r/giveback/trailing math reads, and
migration 114's own header explains why blending them would corrupt R
already secured by the original tranche.

FAKES, NOT MOCKS OF THE DECISION ITSELF — `evaluate_scale_in()` and
`paper_broker.simulate_fill()` are the REAL functions, exercised for
real, matching this project's own testing philosophy (see F-78's own
"sized through the REAL check_new_entry(), not mocked"). Only the
Supabase client and the Kite broker session are faked, because those are
the "live book" tests/__init__.py says does not belong here — a fake
in-memory table is not that; it is what lets the STATE-TRANSITION logic
run at all without a network call, exactly like test_pending_fill_race.py
(F-67) already does for the entry side.
"""

from __future__ import annotations

import contextlib

from tests import cfg_ctx


class _TQ:
    def __init__(self, verdict="STRONG", has_evidence=True, checks=6):
        self.verdict = verdict
        self.has_evidence = has_evidence
        self.checks = checks


class _Query:
    """Minimal fluent stand-in for a PostgREST query chain — enough of
    .select()/.update()/.eq()/.execute() to exercise _update_position and
    _resolve_pending_scale_ins, nothing more."""

    def __init__(self, store: dict, table: str):
        self._store = store
        self._table = table
        self._filters: list[tuple[str, object]] = []
        self._patch: dict | None = None
        self._mode = None

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def update(self, patch: dict):
        self._mode = "update"
        self._patch = patch
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def _matches(self, row: dict) -> bool:
        return all(row.get(col) == val for col, val in self._filters)

    def execute(self):
        class _Result:
            def __init__(self, data):
                self.data = data

        if self._table != "open_positions":
            return _Result([])

        matched = [r for r in self._store.values() if self._matches(r)]
        if self._mode == "update":
            for r in matched:
                r.update(self._patch)
        return _Result([dict(r) for r in matched])


class FakeSB:
    """symbol -> row dict. Real chained-filter semantics, no network."""

    def __init__(self, rows: list[dict]):
        self.rows = {r["symbol"]: dict(r) for r in rows}

    def table(self, name):
        return _Query(self.rows, name)


class FakeNotifier:
    def __init__(self):
        self.sent: list = []

    def send(self, action, force=False):
        self.sent.append(action)


def _pos(symbol="X", entry=100.0, stop=94.0, active=None, **kw) -> dict:
    p = {"symbol": symbol, "entry_price": entry, "planned_stop": stop,
         "active_sl": active if active is not None else stop,
         "planned_target": 130.0, "target_price": 130.0,
         "current_qty": 10, "actual_qty": 10, "original_qty": 10,
         "invested_value": 1000.0, "framework": "SWING", "product": "CNC",
         "status": "ACTIVE", "sector": "test sector", "industry": "test industry",
         "scaled_in": False, "scale_in_status": None, "scale_in_order_id": None}
    p.update(kw)
    return p


def _engine(pos: dict):
    from intraday.engine import IntradayEngine
    sb = FakeSB([pos])
    eng = IntradayEngine(sb=sb, notifier=FakeNotifier())
    # A truthy entry so _shadow_scale_in's `if sig:` branch calls
    # assess_trend() at all — the value itself is irrelevant once
    # _strong_trend() below has replaced assess_trend with a stub.
    eng._policy = {"_trend_ctx": {pos["symbol"]: {"forced": True}},
                   "_current_regime": "NEUTRAL"}
    # _swing_positions() (which evaluate_scale_in()'s sizing reads for its
    # slot/sector/industry counts) filters self.positions — production
    # code populates that via load_state(); tests set it directly so the
    # position competes for its own slot/budget exactly as F-78's own
    # design requires ("the position itself still in it, unfiltered").
    eng.positions = [pos]
    return eng, sb


@contextlib.contextmanager
def _strong_trend():
    """
    `_shadow_scale_in` builds its own TrendQuality via
    control.exit_rules.assess_trend(sig, p) — real trend assessment needs
    real bar history this fake harness has no reason to fabricate. Since
    that function is not the thing under test here (evaluate_scale_in()'s
    own STRONG-with-evidence rail is already covered by
    tests/test_stage_e7_scale_in.py), it is stubbed to a fixed STRONG
    verdict so evaluate_scale_in()'s rail 2 clears deterministically and
    the EXECUTION logic below it is what actually gets exercised.
    """
    import control.exit_rules as er
    orig = er.assess_trend
    er.assess_trend = lambda sig, p: _TQ("STRONG")
    try:
        yield
    finally:
        er.assess_trend = orig


def _qualifying_pos(**kw):
    # active=108 (breakeven-plus) and ltp=115 -> +2.5R past the 1.0R line,
    # matching test_stage_e7_scale_in.py's own qualifying fixture exactly.
    return _pos(active=108.0, **kw)


# ── zero regression: switches OFF is byte-for-byte the old shadow-only path ──

def test_switches_off_never_places_an_order():
    """The whole point of shipping OFF by default: with both switches at
    their real default (unset -> false), a qualifying SCALE_IN decision
    must still never reach execution/order_manager.place — the exact
    shadow-only contract F-78 shipped, now with an OFF ramp on top of it
    rather than in place of it."""
    import execution.order_manager as om
    pos = _qualifying_pos()
    eng, sb = _engine(pos)
    ltp = 100.0 + 2.5 * 6.0

    orig = om.place
    om.place = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("place() must not be called with the switch off"))
    try:
        with _strong_trend(), cfg_ctx({}):
            eng._shadow_scale_in(pos, ltp)
    finally:
        om.place = orig

    row = sb.rows["X"]
    assert row["entry_price"] == 100.0
    assert row["current_qty"] == 10
    assert row["scaled_in"] is False


def test_in_flight_guard_skips_even_a_qualifying_position():
    """A symbol already PENDING_FILL on its add must not be re-decided —
    the same double-submission risk F-67 found on the entry side."""
    import execution.order_manager as om
    pos = _qualifying_pos(scale_in_status="PENDING_FILL")
    eng, sb = _engine(pos)
    ltp = 100.0 + 2.5 * 6.0

    orig = om.place
    om.place = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("place() must not be called while an add is in flight"))
    try:
        with _strong_trend(), cfg_ctx({"swing_scale_in_auto_entry": "true"}):
            eng._shadow_scale_in(pos, ltp)
    finally:
        om.place = orig


def test_in_memory_pending_guard_also_skips():
    """Same guard, via self._pending_scale_ins rather than the row's own
    field — the in-memory half of the same split _pending_fills uses."""
    pos = _qualifying_pos()
    eng, sb = _engine(pos)
    eng._pending_scale_ins["X"] = "order999"
    ltp = 100.0 + 2.5 * 6.0

    import execution.order_manager as om
    orig = om.place
    om.place = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("place() must not be called for an in-flight symbol"))
    try:
        with _strong_trend(), cfg_ctx({"swing_scale_in_auto_entry": "true"}):
            eng._shadow_scale_in(pos, ltp)
    finally:
        om.place = orig


# ── armed, PAPER: the real accounting contract ──────────────────────────────

def test_armed_paper_fill_merges_without_touching_original_baseline():
    """The entire point of migration 114's design: qty/invested_value grow,
    scaled_in_* records the add's own economics, and entry_price/
    planned_stop/active_sl/planned_target/target_price are BIT-FOR-BIT
    unchanged — so evaluate_exit()'s gain_r on the next cycle reads
    exactly what it read before the add."""
    pos = _qualifying_pos()
    eng, sb = _engine(pos)
    ltp = 100.0 + 2.5 * 6.0   # 115.0

    with _strong_trend(), cfg_ctx({"swing_scale_in_auto_entry": "true"}):
        eng._shadow_scale_in(pos, ltp)

    row = sb.rows["X"]
    # Untouched — the accounting question this whole stage was blocked on.
    assert row["entry_price"] == 100.0
    assert row["planned_stop"] == 94.0
    assert row["active_sl"] == 108.0
    assert row["planned_target"] == 130.0
    assert row["target_price"] == 130.0
    # Grown — the add is real shares now held.
    assert row["current_qty"] > 10
    assert row["actual_qty"] == row["current_qty"]
    assert row["original_qty"] == row["current_qty"]
    assert row["invested_value"] > 1000.0
    # The add's own audit trail, separate from the original tranche's.
    assert row["scaled_in"] is True
    assert row["scaled_in_qty"] == row["current_qty"] - 10
    assert row["scaled_in_price"] is not None
    assert row["scaled_in_stop"] == 7.0   # ltp(115) - active_sl(108), F-78's own number
    assert row["scale_in_status"] is None
    assert row["scale_in_order_id"] is None


def test_armed_paper_caps_at_one_add():
    """A position that already has scaled_in=True must not receive a
    second one — evaluate_scale_in()'s own rail 3, still respected once
    execution can act on rail 4's output."""
    pos = _qualifying_pos(scaled_in=True)
    eng, sb = _engine(pos)
    ltp = 100.0 + 2.5 * 6.0

    import execution.order_manager as om
    orig = om.place
    om.place = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not even reach order placement — already scaled"))
    try:
        with _strong_trend(), cfg_ctx({"swing_scale_in_auto_entry": "true"}):
            eng._shadow_scale_in(pos, ltp)
    finally:
        om.place = orig

    assert sb.rows["X"]["current_qty"] == 10


# ── armed, LIVE: submit -> pending -> confirm, mirroring the entry side ─────

class _FakeOrderResult:
    def __init__(self, ok=True, order_id="ORD1"):
        self.ok = ok
        self.order_id = order_id
        self.message = "cleared"
        self.blocked_by = None


def test_armed_live_submits_then_leaves_pending_not_active_status():
    """LIVE must submit an order and mark ONLY scale_in_status pending —
    the row's own status must stay ACTIVE throughout, or the original
    tranche would vanish from every exit reader while the add resolves."""
    pos = _qualifying_pos()
    eng, sb = _engine(pos)
    ltp = 100.0 + 2.5 * 6.0

    import execution.order_manager as om
    orig = om.place
    captured = {}
    def _fake_place(req, sb_, notifier_, framework=None):
        captured["req"] = req
        return _FakeOrderResult(ok=True, order_id="ORD-SCALE-1")
    om.place = _fake_place
    try:
        with _strong_trend(), cfg_ctx({"swing_scale_in_auto_entry": "true",
                                       "swing_scale_in_live_auto_entry": "true",
                                       "swing_trading_mode": "LIVE"}):
            eng._shadow_scale_in(pos, ltp)
    finally:
        om.place = orig

    assert captured["req"].side == "BUY"
    assert captured["req"].symbol == "X"
    row = sb.rows["X"]
    assert row["status"] == "ACTIVE"          # never PENDING_FILL
    assert row["scale_in_status"] == "PENDING_FILL"
    assert row["scale_in_order_id"] == "ORD-SCALE-1"
    assert row["current_qty"] == 10           # not yet merged
    assert row["entry_price"] == 100.0
    assert "X" in eng._pending_scale_ins


def test_live_second_switch_off_stays_shadow_only():
    """swing_scale_in_auto_entry alone is not enough once SWING is LIVE —
    mirrors swing_auto_entry/swing_live_auto_entry's own two-switch shape
    exactly."""
    pos = _qualifying_pos()
    eng, sb = _engine(pos)
    ltp = 100.0 + 2.5 * 6.0

    import execution.order_manager as om
    orig = om.place
    om.place = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not place a live order without the second switch"))
    try:
        with _strong_trend(), cfg_ctx({"swing_scale_in_auto_entry": "true",
                                       "swing_trading_mode": "LIVE"}):
            eng._shadow_scale_in(pos, ltp)
    finally:
        om.place = orig

    assert sb.rows["X"]["scale_in_status"] is None


def test_resolve_pending_scale_in_confirms_without_touching_baseline():
    """The confirm half of the LIVE path — order_history COMPLETE folds
    the fill into qty/invested_value, clears the pending flag, and
    (same as the paper path) never touches entry_price/planned_stop/
    active_sl."""
    pos = _qualifying_pos(scale_in_status="PENDING_FILL",
                          scale_in_order_id="ORD-SCALE-1",
                          scaled_in_stop=7.0)
    eng, sb = _engine(pos)

    class _FakeKite:
        def order_history(self, order_id):
            assert order_id == "ORD-SCALE-1"
            return [{"status": "COMPLETE", "filled_quantity": 3,
                     "average_price": 116.20}]

    from kite import kite_client
    orig_get_kite = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()
    try:
        eng._resolve_pending_scale_ins()
    finally:
        kite_client.get_kite = orig_get_kite

    row = sb.rows["X"]
    assert row["scale_in_status"] is None
    assert row["scale_in_order_id"] is None
    assert row["scaled_in"] is True
    assert row["scaled_in_qty"] == 3
    assert row["scaled_in_price"] == 116.20
    assert row["scaled_in_stop"] == 7.0        # set at submission, untouched here
    assert row["current_qty"] == 13
    assert row["invested_value"] == round(1000.0 + 3 * 116.20, 2)
    assert row["entry_price"] == 100.0
    assert row["planned_stop"] == 94.0
    assert row["active_sl"] == 108.0
    assert "X" not in eng._pending_scale_ins


def test_resolve_pending_scale_in_rejected_clears_flag_row_survives():
    """REJECTED/CANCELLED must clear the pending flag and leave the
    ORIGINAL position exactly as it was — this is an add to a real
    position, not a speculative new row, so nothing here may delete it
    the way _discard_pending_fill deletes a never-filled fresh entry."""
    pos = _qualifying_pos(scale_in_status="PENDING_FILL",
                          scale_in_order_id="ORD-SCALE-2",
                          scaled_in_stop=7.0)
    eng, sb = _engine(pos)

    class _FakeKite:
        def order_history(self, order_id):
            return [{"status": "CANCELLED"}]

    from kite import kite_client
    orig_get_kite = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()
    try:
        eng._resolve_pending_scale_ins()
    finally:
        kite_client.get_kite = orig_get_kite

    row = sb.rows["X"]
    assert row["scale_in_status"] is None
    assert row["scale_in_order_id"] is None
    assert row["scaled_in_stop"] is None
    assert row["scaled_in"] is False
    assert row["current_qty"] == 10            # unaffected
    assert row["entry_price"] == 100.0
    assert "X" not in eng._pending_scale_ins


def test_load_state_rebuilds_pending_scale_ins_from_scale_in_status():
    """The restart-survival half — same rebuild _pending_fills already
    gets from status=PENDING_FILL, mirrored for scale_in_status."""
    pos = _qualifying_pos(status="ACTIVE", scale_in_status="PENDING_FILL",
                          scale_in_order_id="ORD-RESTART")
    from intraday.engine import IntradayEngine
    sb = FakeSB([pos])
    eng = IntradayEngine(sb=sb, notifier=FakeNotifier())
    eng.load_state()
    assert eng._pending_scale_ins.get("X") == "ORD-RESTART"


TESTS = [
    ("switches off never places an order (zero regression)",
     test_switches_off_never_places_an_order),
    ("in-flight guard (DB field) skips a qualifying position",
     test_in_flight_guard_skips_even_a_qualifying_position),
    ("in-flight guard (in-memory) skips a qualifying position",
     test_in_memory_pending_guard_also_skips),
    ("armed paper fill merges without touching the original baseline",
     test_armed_paper_fill_merges_without_touching_original_baseline),
    ("armed paper caps at one add", test_armed_paper_caps_at_one_add),
    ("armed live submits then leaves status=ACTIVE, scale_in_status pending",
     test_armed_live_submits_then_leaves_pending_not_active_status),
    ("live second switch off stays shadow-only",
     test_live_second_switch_off_stays_shadow_only),
    ("resolve pending scale-in confirms without touching the baseline",
     test_resolve_pending_scale_in_confirms_without_touching_baseline),
    ("resolve pending scale-in REJECTED clears the flag, row survives",
     test_resolve_pending_scale_in_rejected_clears_flag_row_survives),
    ("load_state rebuilds _pending_scale_ins from scale_in_status",
     test_load_state_rebuilds_pending_scale_ins_from_scale_in_status),
]
