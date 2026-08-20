"""
Rung 7a of evaluate_intraday_exit(): follow-through volume fading below what
a trade opened on, tightening the stop before the fixed-clock time stop
(rung 7) would otherwise be the first thing to notice anything is wrong.

OFF BY DEFAULT, AND DELIBERATELY — same posture as giveback_pct shipped
with (0cdd3c8) and short_runway_tighten_enabled still ships with: this is a
plausible, professionally-grounded hypothesis (a discretionary trader
watches exactly this) with zero hours of calibration against THIS book's
own resolved trades. Every other rung in this file is priced off something
already measured; this one is not yet, so it ships correctly wired and
inert until an operator arms it on evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config import IST


@dataclass
class _Bar:
    ts: datetime
    volume: float


def _policy(**overrides):
    base = {
        "use_setup_target":   True,
        "partial_book_r":     1.2,
        "partial_book_pct":   50.0,
        "move_to_breakeven":  True,
        "trail_r":            1.0,
        "trail_after_r":      1.5,
        "target_r":           2.0,
        "time_stop_min":      75,
        "time_stop_min_r":    0.3,
        "squareoff_buffer":   12,
        "check_invalidation": False,
        "must_exit_time":     "15:15",
        "breakeven_at_r":     1.2,
        "cost_buffer_pct":    0.21,
        "giveback_pct":       0.0,
        "short_runway_tighten_enabled": False,
        "short_runway_min":              75,
        "short_runway_tighten_floor_pct": 40.0,
        "volume_decay_enabled":     False,
        "volume_decay_window_min": 15,
        "volume_decay_floor_pct":  40.0,
        "volume_decay_tighten_pct": 50.0,
    }
    base.update(overrides)
    return base


def _long_pos(active_sl=98.0):
    return {"symbol": "TESTCO", "direction": "LONG", "entry_price": 100.0,
           "planned_stop": 98.0, "active_sl": active_sl, "planned_target": 106.0,
           "current_qty": 10, "partial_booked_qty": 0,
           "entry_date": "2026-08-20T04:00:00+00:00"}  # 09:30 IST


# IST.localize(), NOT datetime(..., tzinfo=IST) — pytz's classic gotcha.
# Direct tzinfo= construction attaches the pre-1945 Kolkata LMT offset
# (+05:53:20) instead of the standard +05:30, so a naive `+timedelta` and
# a SEPARATELY .astimezone(IST)-converted timestamp (exactly what
# evaluate_intraday_exit does with pos["entry_date"]) silently disagree by
# 23 minutes — enough to shift a 15-minute window clean off the bars this
# file builds. Caught by three tests failing with plausible-looking HOLDs
# instead of an exception, not by inspection.
_ENTRY = IST.localize(datetime(2026, 8, 20, 9, 30))


def _bars(initial_vol_each: float, recent_vol_each: float, window_min: int = 15,
          n_each: int = 5, gap_min: int = 20):
    """`n_each` bars spread through the first `window_min` minutes after
    entry (the "initial" window), then a gap, then `n_each` bars spread
    through the last `window_min` minutes ending at `now` (the "recent"
    window). `now` is derived so both windows fall where the ratio function
    expects them."""
    out = []
    step = max(1, window_min // n_each)
    for i in range(n_each):
        out.append(_Bar(_ENTRY + timedelta(minutes=i * step), initial_vol_each))
    now = _ENTRY + timedelta(minutes=window_min + gap_min + window_min)
    recent_start = now - timedelta(minutes=window_min)
    for i in range(n_each):
        out.append(_Bar(recent_start + timedelta(minutes=i * step), recent_vol_each))
    return out, now


# ── _volume_decay_ratio, pure ────────────────────────────────────────────

def test_ratio_below_one_when_recent_volume_has_faded():
    from intraday.exit_policy import _volume_decay_ratio
    bars, now = _bars(initial_vol_each=1000, recent_vol_each=300)
    ratio = _volume_decay_ratio(bars, _ENTRY, now, window_min=15)
    assert ratio is not None and abs(ratio - 0.3) < 1e-9


def test_ratio_above_one_when_volume_is_still_strong():
    from intraday.exit_policy import _volume_decay_ratio
    bars, now = _bars(initial_vol_each=500, recent_vol_each=1200)
    ratio = _volume_decay_ratio(bars, _ENTRY, now, window_min=15)
    assert ratio is not None and ratio > 1.0


def test_ratio_is_none_with_too_few_bars_in_either_window():
    """The exact shape of the first ~2*window_min minutes of every trade's
    life — not enough closed bars yet on one side or the other."""
    from intraday.exit_policy import _volume_decay_ratio
    bars = [_Bar(_ENTRY, 500), _Bar(_ENTRY + timedelta(minutes=2), 600)]
    now = _ENTRY + timedelta(minutes=5)  # recent window has the same 2 bars
    # only ONE bar actually falls in a 15-min "recent" window ending at now
    # minus the entry-window overlap — regardless, too early to have two
    # genuinely separate windows.
    ratio = _volume_decay_ratio(bars, _ENTRY, now, window_min=15)
    assert ratio is None


def test_ratio_is_none_without_bars_or_entry_ts():
    from intraday.exit_policy import _volume_decay_ratio
    now = _ENTRY + timedelta(minutes=40)
    assert _volume_decay_ratio([], _ENTRY, now, 15) is None
    assert _volume_decay_ratio([_Bar(_ENTRY, 100)], None, now, 15) is None


# ── the rung inside evaluate_intraday_exit ───────────────────────────────

def test_off_by_default_never_fires_even_with_decaying_volume():
    """Regression guard — the single most important test in this file. A
    trade with textbook decaying volume must produce IDENTICAL behaviour to
    today's shipped code when the switch is not explicitly turned on."""
    from intraday.exit_policy import evaluate_intraday_exit
    bars, now = _bars(initial_vol_each=1000, recent_vol_each=100)  # 10% pace
    d = evaluate_intraday_exit(_long_pos(), ltp=100.5, policy=_policy(), now=now, bars=bars)
    assert d["reason"] != "VOLUME_DECAY"


def test_fires_when_enabled_and_volume_has_decayed_below_the_floor():
    from intraday.exit_policy import evaluate_intraday_exit
    bars, now = _bars(initial_vol_each=1000, recent_vol_each=100)  # 10%, well under 40% floor
    d = evaluate_intraday_exit(_long_pos(), ltp=100.5, policy=_policy(volume_decay_enabled=True),
                               now=now, bars=bars)
    assert d["action"] == "TRAIL_SL" and d["reason"] == "VOLUME_DECAY", d
    # tighten_pct=50% of a 2.0 risk width, off the 100.5 ltp
    assert d["new_sl"] == 99.5, d


def test_does_not_fire_when_volume_is_healthy():
    from intraday.exit_policy import evaluate_intraday_exit
    bars, now = _bars(initial_vol_each=1000, recent_vol_each=900)  # 90%, above the floor
    d = evaluate_intraday_exit(_long_pos(), ltp=100.5, policy=_policy(volume_decay_enabled=True),
                               now=now, bars=bars)
    assert d["reason"] != "VOLUME_DECAY"


def test_does_not_fire_once_the_trade_has_already_proven_itself():
    """gain_r >= partial_book_r means breakeven/trail/giveback are already
    the right protection — this rung backing off here is what stops it
    fighting them for the same stop.

    single-share qty (current_qty=1, rung 5/BOOK_PARTIAL needs qty>1) and a
    stop already past the breakeven+cost-buffer level (rung 5c needs its OWN
    tightening to still be an IMPROVEMENT to fire) so neither of the two
    other rungs that also key off gain_r>=partial_book_r can return first
    and make this assertion trivially true either way — isolates rung 7a's
    own gate specifically."""
    from intraday.exit_policy import evaluate_intraday_exit
    bars, now = _bars(initial_vol_each=1000, recent_vol_each=50)  # severe decay
    pos = _long_pos(active_sl=100.5)
    pos["current_qty"] = 1
    d = evaluate_intraday_exit(pos, ltp=102.5,  # 1.25R, above partial_book_r=1.2
                               policy=_policy(volume_decay_enabled=True), now=now, bars=bars)
    assert d["reason"] != "VOLUME_DECAY"
    assert d["reason"] not in ("PARTIAL_TARGET", "BREAKEVEN"), (
        f"test setup failed to isolate rung 7a, got {d['reason']!r}")


def test_never_loosens_the_stop():
    """If the tightened level would sit WORSE than the current active_sl
    (e.g. a trail rung already moved it tighter than volume-decay would),
    this rung must stay silent — same invariant every other rung enforces."""
    from intraday.exit_policy import evaluate_intraday_exit
    bars, now = _bars(initial_vol_each=1000, recent_vol_each=50)
    # active_sl already at 99.9 — tighter than volume-decay's 99.5 would be
    d = evaluate_intraday_exit(_long_pos(active_sl=99.9), ltp=100.5,
                               policy=_policy(volume_decay_enabled=True, trail_after_r=99.0),
                               now=now, bars=bars)
    assert d["reason"] != "VOLUME_DECAY"


def test_direction_correct_for_a_short():
    from intraday.exit_policy import evaluate_intraday_exit
    bars, now = _bars(initial_vol_each=1000, recent_vol_each=100)
    pos = {"symbol": "TESTSH", "direction": "SHORT", "entry_price": 100.0,
          "planned_stop": 102.0, "active_sl": 102.0, "planned_target": 94.0,
          "current_qty": 10, "partial_booked_qty": 0,
          "entry_date": "2026-08-20T04:00:00+00:00"}
    d = evaluate_intraday_exit(pos, ltp=99.5, policy=_policy(volume_decay_enabled=True),
                               now=now, bars=bars)
    assert d["action"] == "TRAIL_SL" and d["reason"] == "VOLUME_DECAY", d
    # tighten 50% of the 2.0 risk width ABOVE the 99.5 ltp for a short
    assert d["new_sl"] == 100.5, d


def test_missing_bars_argument_does_not_crash_existing_callers():
    """Every caller written before this rung passes no `bars` at all —
    the default None must behave exactly like an empty list, not raise."""
    from intraday.exit_policy import evaluate_intraday_exit
    d = evaluate_intraday_exit(_long_pos(), ltp=100.5,
                               policy=_policy(volume_decay_enabled=True))
    assert d["reason"] != "VOLUME_DECAY"


TESTS = [
    ("ratio below one when recent volume has faded", test_ratio_below_one_when_recent_volume_has_faded),
    ("ratio above one when volume is still strong", test_ratio_above_one_when_volume_is_still_strong),
    ("ratio is None with too few bars in either window", test_ratio_is_none_with_too_few_bars_in_either_window),
    ("ratio is None without bars or entry_ts", test_ratio_is_none_without_bars_or_entry_ts),
    ("off by default never fires even with decaying volume", test_off_by_default_never_fires_even_with_decaying_volume),
    ("fires when enabled and volume has decayed below the floor", test_fires_when_enabled_and_volume_has_decayed_below_the_floor),
    ("does not fire when volume is healthy", test_does_not_fire_when_volume_is_healthy),
    ("does not fire once the trade has already proven itself", test_does_not_fire_once_the_trade_has_already_proven_itself),
    ("never loosens the stop", test_never_loosens_the_stop),
    ("direction correct for a short", test_direction_correct_for_a_short),
    ("missing bars argument does not crash existing callers", test_missing_bars_argument_does_not_crash_existing_callers),
]
