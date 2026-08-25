"""
F-84, 25-Aug-2026 — reconcile_with_broker()'s day_qty merge silently
discarded same-day activity for a symbol that ALREADY had a settled
holdings() entry, using it only to synthesise a brand-new one. HINDCOPPER
had 4 shares settled from T-1 when, same session, it sold the rest of
that lot and bought a fresh 13 — every one of those fills was invisible
to holdings() until the next day, so the stale settled 4 stood all
session. The downstream qty-drift comparison read that stale 4 as ground
truth, misread the difference against the DB's own (correct) count as a
real partial SELL, and wrote phantom shares into partial_booked_qty that
were never actually sold — surviving all the way into the eventual
closed_positions row (actual_qty=22 instead of the true 13).

_merge_day_position() is the fix, extracted as a pure function
(control/position_lifecycle.py, mirroring _mirror_qty_drift()'s own
precedent) so it is testable without reconcile_with_broker()'s
kite_client/holdings/multi-table I/O.
"""

from __future__ import annotations


def _holding(qty, total_qty=None, avg=500.0):
    return {"symbol": "HINDCOPPER", "quantity": qty,
           "total_quantity": total_qty if total_qty is not None else qty,
           "average_price": avg, "last_price": avg, "product": "CNC",
           "t1_quantity": 0, "pnl": 0.0, "close_price": 0.0,
           "day_change_pct": 0.0}


def test_no_prior_holding_synthesises_one_from_day_qty():
    """The case that already worked — a symbol never held before, bought
    today. Regression guard: this shape must be unchanged by the fix."""
    from control.position_lifecycle import _merge_day_position
    merged = _merge_day_position(None, ("HINDCOPPER", "CNC"), 7, 568.40)
    assert merged["quantity"] == 7
    assert merged["total_quantity"] == 7
    assert merged["average_price"] == 568.40
    assert merged["symbol"] == "HINDCOPPER"
    assert merged["product"] == "CNC"


def test_the_real_hindcopper_incident_settled_4_but_day_position_shows_13():
    """THE ACTUAL SHAPE THAT PRODUCED THE PHANTOM PARTIAL. holdings() still
    shows the T-1 settled 4; day_qty already knows the true current 13
    (sold the rest of the old lot, bought a fresh one, same session). The
    merge must trust day_qty's fresher, complete total — not silently keep
    the stale settled figure the way the pre-fix code did."""
    from control.position_lifecycle import _merge_day_position
    stale = _holding(4, avg=523.65)
    merged = _merge_day_position(stale, ("HINDCOPPER", "CNC"), 13, 568.88)
    assert merged["quantity"] == 13, (
        f"got {merged['quantity']} — must override the stale settled "
        f"holdings() figure with day_qty's fresher total, or the exact "
        f"F-84 phantom-partial shape reproduces")
    assert merged["total_quantity"] == 13
    assert merged["average_price"] == 568.88


def test_agreement_is_a_no_op_returning_the_same_object():
    """When holdings() and day_qty already agree — the overwhelmingly
    common case, any ordinary day with no same-day drift — nothing should
    change, and the function should not even allocate a new dict for it."""
    from control.position_lifecycle import _merge_day_position
    existing = _holding(10, avg=500.0)
    merged = _merge_day_position(existing, ("HINDCOPPER", "CNC"), 10, 500.0)
    assert merged is existing, (
        "an already-agreeing entry must be returned unchanged — a caller "
        "that always calls this function must not pay for a needless "
        "rebuild on the common no-drift day")


def test_uses_total_quantity_when_quantity_is_absent():
    """Some holdings() shapes carry only total_quantity (t1 + free), not a
    top-level quantity — the comparison must fall back correctly rather
    than reading a missing key as 0 and always claiming a mismatch."""
    from control.position_lifecycle import _merge_day_position
    existing = {"symbol": "HINDCOPPER", "total_quantity": 10,
               "average_price": 500.0, "product": "CNC"}
    merged = _merge_day_position(existing, ("HINDCOPPER", "CNC"), 10, 500.0)
    assert merged is existing


def test_a_reduction_is_also_trusted_not_only_an_increase():
    """The fix is not "day_qty wins only when it is bigger" — settlement
    lag can just as easily leave holdings() OVERSTATED relative to a
    same-day sell that has not yet cleared. day_qty is the fresher answer
    either direction."""
    from control.position_lifecycle import _merge_day_position
    stale = _holding(10, avg=500.0)
    merged = _merge_day_position(stale, ("HINDCOPPER", "CNC"), 3, 500.0)
    assert merged["quantity"] == 3
    assert merged["total_quantity"] == 3


def test_the_result_carries_the_correct_shape_for_downstream_readers():
    """reconcile_with_broker() reads quantity/total_quantity/average_price/
    product off this dict identically whether it came from real Kite JSON
    or this merge — a partial dict here would raise at the point it is
    finally read, not at the boundary where the bad merge happened."""
    from control.position_lifecycle import _merge_day_position
    merged = _merge_day_position(None, ("HINDCOPPER", "CNC"), 5, 555.0)
    for field in ("symbol", "quantity", "total_quantity", "average_price",
                 "last_price", "product", "pnl", "close_price", "day_change_pct"):
        assert field in merged, f"missing '{field}' — a downstream reader will raise"


TESTS = [
    ("no prior holding synthesises one from day_qty",
     test_no_prior_holding_synthesises_one_from_day_qty),
    ("the real HINDCOPPER incident: settled 4 but day position shows 13",
     test_the_real_hindcopper_incident_settled_4_but_day_position_shows_13),
    ("agreement is a no-op returning the same object",
     test_agreement_is_a_no_op_returning_the_same_object),
    ("uses total_quantity when quantity is absent",
     test_uses_total_quantity_when_quantity_is_absent),
    ("a reduction is also trusted, not only an increase",
     test_a_reduction_is_also_trusted_not_only_an_increase),
    ("the result carries the correct shape for downstream readers",
     test_the_result_carries_the_correct_shape_for_downstream_readers),
]
