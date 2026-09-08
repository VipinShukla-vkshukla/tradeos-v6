"""
Phase 1 of the swing framework evolution blueprint, 26-Aug-2026.

RKFORGE fired repeated "BUY — in zone" Telegram alerts all morning while the
allocator DECLINE'd it every single cycle (edge -0.016 vs hurdle 0.029,
negative net of cost) — the alert was built purely from decide()/
entry_ranking and never consulted self._verdicts, the same dict
_maybe_enter_swing's own allocator_permits() veto already reads a few dozen
lines later in the same call chain. `intraday.engine._swing_alert_kind()`
closes that gap; these tests exercise it directly as a pure function, no
Engine instance needed.
"""

from __future__ import annotations

from tests import cfg_ctx


def test_declined_verdict_overrides_entry_kind():
    from intraday.engine import _swing_alert_kind
    verdict = {"verdict": "DECLINE", "reason": "edge -0.0162 below the bar 0.0291",
               "edge": -0.0162, "hurdle": 0.0291}
    kind, declined = _swing_alert_kind(verdict, room=True)
    assert kind == "ENTRY_DECLINED", (
        f"RKFORGE-shaped case: allocator DECLINEd, alert must not say ENTRY, got {kind}")
    assert declined is True


def test_declined_verdict_overrides_swap_candidate_too():
    """A DECLINE wins regardless of the room/swap question — there is
    nothing to swap toward on a trade the allocator has already refused
    this cycle."""
    from intraday.engine import _swing_alert_kind
    verdict = {"verdict": "DECLINE", "reason": "slots spent on higher-edge plans"}
    kind, declined = _swing_alert_kind(verdict, room=False)
    assert kind == "ENTRY_DECLINED"
    assert declined is True


def test_take_verdict_is_unchanged():
    from intraday.engine import _swing_alert_kind
    verdict = {"verdict": "TAKE", "reason": "edge 0.0250 clears the bar 0.0233"}
    kind, declined = _swing_alert_kind(verdict, room=True)
    assert kind == "ENTRY", "a TAKE verdict must not change today's alert shape"
    assert declined is False


def test_no_verdict_falls_back_to_room_swap_exactly_as_before():
    """allocator off, or failed open (never scored this candidate) — the
    prior behaviour, unconditionally."""
    from intraday.engine import _swing_alert_kind
    kind_room, declined_room = _swing_alert_kind(None, room=True)
    kind_swap, declined_swap = _swing_alert_kind(None, room=False)
    assert kind_room == "ENTRY" and declined_room is False
    assert kind_swap == "SWAP_CANDIDATE" and declined_swap is False


def test_defer_verdict_gets_its_own_kind_not_folded_into_entry_or_declined():
    """PETRONET, 08-Sep-2026: DEFER means a slot is held for a stronger
    same-cycle proposal — allocator_permits() refuses it exactly like a
    DECLINE a few lines later in the same call chain, so the alert must not
    say plain "ENTRY" (confirmed live: PETRONET alternated DECLINE/DEFER all
    session while the operator kept receiving "BUY — in zone" every few
    minutes for a trade the system had no intention of taking). Also not
    folded into ENTRY_DECLINED: a decline is "no", a defer is "not yet" —
    different dedup buckets, different words. Supersedes the old
    test_defer_verdict_is_not_treated_as_declined, which documented this gap
    as deliberately out of scope for the narrower 26-Aug fix."""
    from intraday.engine import _swing_alert_kind
    verdict = {"verdict": "DEFER", "reason": "a slot is held for a better proposal"}
    kind, blocked = _swing_alert_kind(verdict, room=True)
    assert kind == "ENTRY_DEFERRED", f"expected a distinct DEFER kind, got {kind}"
    assert blocked is True, "DEFER must be treated as blocked — allocator_permits() refuses it too"


def test_swing_alert_reflect_allocator_defaults_true():
    from config import cfg_bool
    with cfg_ctx({}):
        assert cfg_bool("swing_alert_reflect_allocator", True) is True, (
            "must default on — migration 118 ships it true")


TESTS = [
    ("declined verdict overrides ENTRY kind", test_declined_verdict_overrides_entry_kind),
    ("declined verdict overrides SWAP_CANDIDATE too", test_declined_verdict_overrides_swap_candidate_too),
    ("TAKE verdict leaves today's alert unchanged", test_take_verdict_is_unchanged),
    ("no verdict falls back to room/swap exactly as before", test_no_verdict_falls_back_to_room_swap_exactly_as_before),
    ("DEFER gets its own kind, not folded into ENTRY or ENTRY_DECLINED",
     test_defer_verdict_gets_its_own_kind_not_folded_into_entry_or_declined),
    ("swing_alert_reflect_allocator defaults true", test_swing_alert_reflect_allocator_defaults_true),
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
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
