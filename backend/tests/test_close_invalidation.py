"""
A close is not a tick, and a prior must be able to forget (12-Aug-2026).

TWO CHANGES, ONE THEME: the intraday book was reacting to single prints and
remembering forever. Both are the opposite of how a disciplined intraday trader
operates — concede an idea when the bar closes against it, and weight recent
evidence over ancient evidence.

CLOSE-BASED INVALIDATION
------------------------
Every engine's invalidation prose promises a CLOSE — "a close back below
1081.80", "a close back under VWAP" — and `_invalidated()` compared the last
traded price. On 12-Aug six of nine exits were SETUP_INVALIDATED between −0.15R
and −0.72R, and not one trade reached its target. SBIN's exit was ten paise
through the line on a trade risking 1.2%: one seller, not a structure failing.

Behind `intraday_invalidation_require_close`, off by default. When on, the
reference price becomes the close of the last COMPLETED bar. No completed bar
means no invalidation — the stop, which is the actual safety control, is
untouched and still runs.

THE ROLLING PRIOR WINDOW
------------------------
`intraday_priors()` fetched every resolved row in `intraday_setups`, ever, with
no time bound. So the book could never demonstrate improvement (pre-fix trades
sit at equal weight with post-fix ones, permanently) and the negative-prior
absorbing state had no exit (no window to age out of). `priors_intraday_
lookback_days` bounds it; 0 restores the old unbounded read exactly.

WHAT THIS CATCHES
-----------------
That `last_completed_close` never returns the forming bar's close — which
would make the whole feature a silent no-op, this project's most-repeated
failure. That the switch OFF reproduces today's behaviour exactly. That the
switch ON refuses a tick and still fires on a real close. That the prior fetch
actually applies the window, asserted through the query the fetch builds rather
than by eye.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from config import IST
from tests import cfg_ctx


class _B:
    """Minimal bar: what last_completed_close() actually reads."""
    def __init__(self, ts, close):
        self.ts, self.close = ts, close


def _bars(n=4, interval_min=5, closes=None, end=None):
    end = end or datetime.now(IST).replace(second=0, microsecond=0)
    closes = closes or [100.0] * n
    return [_B(end - timedelta(minutes=interval_min * (n - 1 - i)), closes[i])
            for i in range(n)]


# ── last_completed_close() ───────────────────────────────────────────────

def test_the_forming_bar_is_never_used():
    """
    THE WHOLE FEATURE LIVES OR DIES HERE. The last bar is still forming and its
    close is the last tick — i.e. `ltp` under another name. Returning it would
    make the close rule identical to the tick rule it replaces, and the change
    would read as shipped while doing nothing.
    """
    from intraday.exit_policy import last_completed_close
    now = datetime.now(IST).replace(second=0, microsecond=0)
    bars = _bars(n=4, interval_min=5, closes=[100.0, 101.0, 102.0, 99.0], end=now)
    got = last_completed_close(bars, now=now)
    assert got == 102.0, f"expected the last COMPLETED close (102.0), got {got}"
    assert got != 99.0, "the forming bar's close must never be returned"


def test_a_finished_last_bar_is_used():
    """Once the interval has elapsed, the last bar IS complete."""
    from intraday.exit_policy import last_completed_close
    now = datetime.now(IST).replace(second=0, microsecond=0)
    bars = _bars(n=3, interval_min=5, closes=[100.0, 101.0, 97.0], end=now)
    assert last_completed_close(bars, now=now + timedelta(minutes=5)) == 97.0


def test_too_few_bars_returns_none():
    from intraday.exit_policy import last_completed_close
    assert last_completed_close([]) is None
    assert last_completed_close(_bars(n=1)) is None


def test_garbage_bars_degrade_to_none_rather_than_raising():
    from intraday.exit_policy import last_completed_close
    assert last_completed_close([object(), object()]) is None


# ── _invalidated() under the switch ──────────────────────────────────────

def _pos(level=1081.80, direction="LONG"):
    return {"invalidation_level": level, "direction": direction,
            "invalidation_note": "closed back inside the opening range"}


def test_switch_off_reproduces_todays_behaviour_exactly():
    """The rollback lever must be a true no-op."""
    from intraday.exit_policy import _invalidated
    with cfg_ctx({"intraday_invalidation_require_close": "false",
                  "intraday_invalidation_buffer_pct": "0.12"}):
        bad, _ = _invalidated(_pos(), ltp=1080.40, last_close=1082.00)
    assert bad, "with the switch off the tick must still invalidate, as it did"


def test_the_sbin_tick_no_longer_exits():
    """
    SBIN, 12-Aug: level 1081.80, cut line 1080.50, exit printed at 1080.40 —
    ten paise through, on a trade risking 1.2%. The last completed bar had
    closed at 1082.00, above the level. That is not a structure failing.
    """
    from intraday.exit_policy import _invalidated
    with cfg_ctx({"intraday_invalidation_require_close": "true",
                  "intraday_invalidation_buffer_pct": "0.12"}):
        bad, _ = _invalidated(_pos(), ltp=1080.40, last_close=1082.00)
    assert not bad, "a tick through the line must not kill the thesis"


def test_a_real_close_below_still_exits():
    """
    THE OTHER DIRECTION. A threshold nothing can clear would be worse than the
    bug: when a bar actually CLOSES beyond the level, the setup is dead and the
    exit must still fire.
    """
    from intraday.exit_policy import _invalidated
    with cfg_ctx({"intraday_invalidation_require_close": "true",
                  "intraday_invalidation_buffer_pct": "0.12"}):
        bad, why = _invalidated(_pos(), ltp=1079.00, last_close=1079.50)
    assert bad and "opening range" in why


def test_no_completed_bar_means_not_yet_confirmed():
    """
    Early in a trade there may be no finished bar. Falling back to the tick
    there would reintroduce the wick exit at the moment it does most damage,
    so the answer is "not confirmed" — and nothing is unprotected, because the
    stop is checked before this and is untouched.
    """
    from intraday.exit_policy import _invalidated
    with cfg_ctx({"intraday_invalidation_require_close": "true"}):
        bad, _ = _invalidated(_pos(), ltp=1000.00, last_close=None)
    assert not bad


def test_the_close_rule_is_directional():
    """A short dies when price CLOSES back above the level it broke."""
    from intraday.exit_policy import _invalidated
    with cfg_ctx({"intraday_invalidation_require_close": "true",
                  "intraday_invalidation_buffer_pct": "0.12"}):
        alive, _ = _invalidated(_pos(100.0, "SHORT"), ltp=101.0, last_close=99.0)
        dead, _ = _invalidated(_pos(100.0, "SHORT"), ltp=101.0, last_close=101.0)
    assert not alive, "a short must not be invalidated by price falling"
    assert dead, "a short IS invalidated by a close back above its level"


# ── the rolling prior window, asserted through the query it builds ───────

class _FakeQuery:
    """Records the filters the fetch applies, then returns one empty page."""
    def __init__(self, log): self.log = log
    def select(self, *a, **k): return self
    # production chains `.not_.is_(col, "null")` — not_ is a PROPERTY there.
    @property
    def not_(self): return self
    def is_(self, *a, **k): return self
    def gte(self, col, val):
        self.log.append((col, val)); return self
    def range(self, *a, **k): return self
    def execute(self):
        class R: data = []
        return R()


class _FakeSB:
    def __init__(self): self.log = []
    def table(self, *_): return _FakeQuery(self.log)


def test_the_window_is_actually_applied_to_the_fetch():
    """
    Asserted through the query the production fetch builds, not by reading the
    code — the same reason test_priors_survive_the_production_select_string()
    exists. A window computed and never sent is the silent no-op again.
    """
    from allocation.scoring import intraday_priors
    from config import today_ist
    sb = _FakeSB()
    with cfg_ctx({"priors_intraday_lookback_days": "90"}):
        intraday_priors(sb)
    gte = [x for x in sb.log if x[0] == "trade_date"]
    assert gte, "no trade_date lower bound reached the query"
    expected = (today_ist() - timedelta(days=90)).isoformat()
    assert gte[0][1] == expected, f"{gte[0][1]} != {expected}"


def test_zero_restores_the_unbounded_read():
    """The rollback lever: no bound sent at all."""
    from allocation.scoring import intraday_priors
    sb = _FakeSB()
    with cfg_ctx({"priors_intraday_lookback_days": "0"}):
        intraday_priors(sb)
    assert not [x for x in sb.log if x[0] == "trade_date"], \
        "lookback=0 must send no date filter"


def test_a_shorter_window_moves_the_bound():
    from allocation.scoring import intraday_priors
    from config import today_ist
    sb = _FakeSB()
    with cfg_ctx({"priors_intraday_lookback_days": "30"}):
        intraday_priors(sb)
    gte = [x for x in sb.log if x[0] == "trade_date"]
    assert gte[0][1] == (today_ist() - timedelta(days=30)).isoformat()


TESTS = [
    ("the forming bar is never used", test_the_forming_bar_is_never_used),
    ("a finished last bar is used", test_a_finished_last_bar_is_used),
    ("too few bars returns none", test_too_few_bars_returns_none),
    ("garbage bars degrade to none", test_garbage_bars_degrade_to_none_rather_than_raising),
    ("switch off reproduces today's behaviour",
     test_switch_off_reproduces_todays_behaviour_exactly),
    ("the SBIN tick no longer exits", test_the_sbin_tick_no_longer_exits),
    ("a real close below still exits", test_a_real_close_below_still_exits),
    ("no completed bar means not yet confirmed", test_no_completed_bar_means_not_yet_confirmed),
    ("the close rule is directional", test_the_close_rule_is_directional),
    ("the window is applied to the fetch", test_the_window_is_actually_applied_to_the_fetch),
    ("zero restores the unbounded read", test_zero_restores_the_unbounded_read),
    ("a shorter window moves the bound", test_a_shorter_window_moves_the_bound),
]
