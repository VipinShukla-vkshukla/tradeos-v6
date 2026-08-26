"""
Phase 3b of the swing framework evolution blueprint, 26-Aug-2026 — the
resting-limit entry ladder. The direct fix for "RKFORGE quoted 720, then
717, never filled": a genuine non-marketable resting limit
(status='PENDING_ENTRY') instead of a fresh marketable chase every 15s
cycle, repriced in steps toward max_entry if unfilled, falling back to
today's ordinary chase once the ladder is exhausted so a resting attempt
can never cause a trade to be missed entirely.

FAKES, NOT MOCKS OF THE DECISION ITSELF — matching this project's own
testing philosophy (see test_stage_e7_scale_in_execution.py's own header):
only the Supabase client and the Kite broker session are faked; every
state-transition rule under test runs for real against
`intraday.engine.IntradayEngine._resolve_pending_entries()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from tests import cfg_ctx

IST = None  # set below from config, so a real IST-aware datetime is used


class _Query:
    """Minimal fluent PostgREST stand-in — select/update/delete/eq/execute,
    matching test_stage_e7_scale_in_execution.py's own shape plus delete()
    support (_discard_pending_fill needs it, that file's fixture never did)."""

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

    def delete(self):
        self._mode = "delete"
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
        elif self._mode == "delete":
            for r in matched:
                self._store.pop(r["symbol"], None)
        return _Result([dict(r) for r in matched])


class FakeSB:
    def __init__(self, rows: list[dict]):
        self.rows = {r["symbol"]: dict(r) for r in rows}

    def table(self, name):
        return _Query(self.rows, name)


class FakeNotifier:
    def __init__(self):
        self.sent: list = []

    def send(self, action, force=False):
        self.sent.append(action)


def _pending_row(symbol="RKFORGE", step=0, deadline_past=True, max_price=730.0,
                 **kw) -> dict:
    from config import IST as _IST
    now = datetime.now(_IST)
    deadline = (now - timedelta(minutes=1)) if deadline_past else (now + timedelta(minutes=10))
    p = {"symbol": symbol, "entry_price": 720.0, "planned_stop": 691.53,
         "active_sl": 691.53, "planned_target": 808.0, "target_price": 808.0,
         "current_qty": 27, "actual_qty": 27, "original_qty": 27,
         "invested_value": 19440.0, "framework": "SWING", "product": "CNC",
         "status": "PENDING_ENTRY", "entry_order_id": "ORD-REST-1",
         "entry_ladder_step": step, "entry_ladder_deadline": deadline.isoformat(),
         "entry_ladder_max_price": max_price, "sector": "test sector",
         "entry_date": datetime.now(_IST).date().isoformat()}
    p.update(kw)
    return p


def _engine(pos: dict, ltp: float = 721.0):
    from intraday.engine import IntradayEngine
    sb = FakeSB([pos])
    eng = IntradayEngine(sb=sb, notifier=FakeNotifier())
    eng._contexts = {pos["symbol"]: SimpleNamespace(ltp=ltp)}
    eng._pending_entries = {pos["symbol"]: pos["entry_order_id"]}
    return eng, sb


def test_switch_off_is_a_pure_noop():
    """swing_pending_entry_enabled off (the real default) must not even
    read the table — a resting entry that does not exist yet must not
    become a query cost every 300s on a book that never uses this."""
    pos = _pending_row()
    eng, sb = _engine(pos)
    eng.sb = _NeverQuery()
    with cfg_ctx({}):
        eng._resolve_pending_entries()   # must not raise, must not touch eng.sb


class _NeverQuery:
    def table(self, *_a, **_k):
        raise AssertionError("must not query the DB with the switch off")


def test_fill_confirmed_promotes_to_active_via_shared_logic():
    """COMPLETE reuses _promote_pending_fill's exact logic — this is the
    same reuse test_stage_e7's own execution tests demand, now proven for
    the from_status='PENDING_ENTRY' generalization."""
    pos = _pending_row()
    eng, sb = _engine(pos)

    class _FakeKite:
        def order_history(self, order_id):
            assert order_id == "ORD-REST-1"
            return [{"status": "COMPLETE", "filled_quantity": 27, "average_price": 719.50}]

    from kite import kite_client
    orig = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()
    try:
        with cfg_ctx({"swing_pending_entry_enabled": "true"}):
            eng._resolve_pending_entries()
    finally:
        kite_client.get_kite = orig

    row = sb.rows["RKFORGE"]
    assert row["status"] == "ACTIVE"
    assert row["entry_price"] == 719.50
    assert row["actual_qty"] == 27
    assert "RKFORGE" not in eng._pending_entries


def test_rejected_at_broker_discards_the_row():
    pos = _pending_row()
    eng, sb = _engine(pos)

    class _FakeKite:
        def order_history(self, order_id):
            return [{"status": "REJECTED", "status_message": "insufficient margin"}]

    from kite import kite_client
    orig = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()
    try:
        with cfg_ctx({"swing_pending_entry_enabled": "true"}):
            eng._resolve_pending_entries()
    finally:
        kite_client.get_kite = orig

    assert "RKFORGE" not in sb.rows
    assert "RKFORGE" not in eng._pending_entries


def test_deadline_not_yet_passed_is_left_alone():
    """The normal state of a resting order — must not reprice or cancel
    early just because this cycle happened to run."""
    pos = _pending_row(deadline_past=False)
    eng, sb = _engine(pos)

    class _FakeKite:
        def order_history(self, order_id):
            return [{"status": "OPEN"}]
        def modify_order(self, **kw):
            raise AssertionError("must not reprice before its own deadline")
        def cancel_order(self, **kw):
            raise AssertionError("must not cancel before its own deadline")

    from kite import kite_client
    orig = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()
    try:
        with cfg_ctx({"swing_pending_entry_enabled": "true"}):
            eng._resolve_pending_entries()
    finally:
        kite_client.get_kite = orig

    row = sb.rows["RKFORGE"]
    assert row["entry_ladder_step"] == 0
    assert row["entry_price"] == 720.0


def test_deadline_passed_reprices_one_step_toward_max_entry():
    pos = _pending_row(step=0, deadline_past=True, max_price=730.0)
    eng, sb = _engine(pos, ltp=721.0)
    modify_calls = []

    class _FakeKite:
        VARIETY_REGULAR = "regular"
        ORDER_TYPE_LIMIT = "LIMIT"
        def order_history(self, order_id):
            return [{"status": "OPEN"}]
        def modify_order(self, **kw):
            modify_calls.append(kw)

    from kite import kite_client
    orig = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()
    try:
        with cfg_ctx({"swing_pending_entry_enabled": "true",
                      "swing_entry_slip_bps": "20"}):
            eng._resolve_pending_entries()
    finally:
        kite_client.get_kite = orig

    assert len(modify_calls) == 1
    row = sb.rows["RKFORGE"]
    assert row["entry_ladder_step"] == 1
    assert row["entry_price"] == modify_calls[0]["price"]
    assert row["entry_price"] > 720.0, "must move toward the chase, not stay flat"
    assert row["entry_price"] <= 730.0, "must never reprice past max_entry"


def test_ladder_exhausted_cancels_and_falls_back_to_chase():
    """The guarantee the blueprint names: a resting attempt must never
    cause a trade the system already decided to take to be missed
    entirely."""
    pos = _pending_row(step=3, deadline_past=True, max_price=730.0)   # step == max_steps default
    eng, sb = _engine(pos, ltp=725.0)
    cancel_calls = []

    class _FakeKite:
        VARIETY_REGULAR = "regular"
        ORDER_TYPE_LIMIT = "LIMIT"
        def order_history(self, order_id):
            return [{"status": "OPEN"}]
        def cancel_order(self, **kw):
            cancel_calls.append(kw)

    from kite import kite_client
    orig = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()

    import execution.order_manager as om
    orig_place = om.place
    placed = []
    om.place = lambda req, *a, **k: (placed.append(req),
                                     SimpleNamespace(ok=True, order_id="ORD-FALLBACK", message="ok"))[-1]

    import control.position_lifecycle as pl
    orig_upsert = pl._upsert_position
    upserted = []
    pl._upsert_position = lambda sb, row: upserted.append(row)

    try:
        with cfg_ctx({"swing_pending_entry_enabled": "true",
                      "swing_pending_entry_ladder_steps": "3",
                      "swing_pending_entry_fallback_to_chase": "true",
                      "swing_entry_slip_bps": "20"}):
            eng._resolve_pending_entries()
    finally:
        kite_client.get_kite = orig
        om.place = orig_place
        pl._upsert_position = orig_upsert

    assert len(cancel_calls) == 1, "ladder-exhausted order must be cancelled at the broker"
    assert "RKFORGE" not in sb.rows or sb.rows.get("RKFORGE", {}).get("status") != "PENDING_ENTRY"
    assert len(placed) == 1, "must attempt the ordinary chase fallback exactly once"
    assert placed[0].symbol == "RKFORGE" and placed[0].side == "BUY"
    assert len(upserted) == 1 and upserted[0]["status"] == "PENDING_FILL"
    assert "RKFORGE" in eng._pending_fills


def test_ladder_exhausted_without_fallback_just_stands_down():
    pos = _pending_row(step=3, deadline_past=True)
    eng, sb = _engine(pos, ltp=725.0)

    class _FakeKite:
        VARIETY_REGULAR = "regular"
        def order_history(self, order_id):
            return [{"status": "OPEN"}]
        def cancel_order(self, **kw):
            pass

    from kite import kite_client
    orig = kite_client.get_kite
    kite_client.get_kite = lambda: _FakeKite()

    import execution.order_manager as om
    orig_place = om.place
    om.place = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not place a fallback order with the switch off"))

    try:
        with cfg_ctx({"swing_pending_entry_enabled": "true",
                      "swing_pending_entry_ladder_steps": "3",
                      "swing_pending_entry_fallback_to_chase": "false"}):
            eng._resolve_pending_entries()
    finally:
        kite_client.get_kite = orig
        om.place = orig_place

    assert "RKFORGE" not in sb.rows
    assert "RKFORGE" not in eng._pending_fills


TESTS = [
    ("switch off is a pure no-op", test_switch_off_is_a_pure_noop),
    ("fill confirmed promotes to ACTIVE via shared logic",
     test_fill_confirmed_promotes_to_active_via_shared_logic),
    ("rejected at broker discards the row", test_rejected_at_broker_discards_the_row),
    ("deadline not yet passed is left alone", test_deadline_not_yet_passed_is_left_alone),
    ("deadline passed reprices one step toward max_entry",
     test_deadline_passed_reprices_one_step_toward_max_entry),
    ("ladder exhausted cancels and falls back to chase",
     test_ladder_exhausted_cancels_and_falls_back_to_chase),
    ("ladder exhausted without fallback just stands down",
     test_ladder_exhausted_without_fallback_just_stands_down),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
        except Exception as e:
            fails += 1
            print(f"  ERROR {name} — {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
