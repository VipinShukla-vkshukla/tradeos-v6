"""
intraday/price_feed.py's depth (Kite FULL-mode) plumbing — Stage D4,
24-Aug-2026 (docs/TRADEOS_ROADMAP.md, Track D, branch
feat/intraday-depth-gate).

WHAT THIS COVERS
-----------------
The capture side: depth() as an accessor with the same "None means no data
yet, never an empty book" contract quote() already carries, and
set_depth_symbols()'s diff logic — which symbols get put into FULL mode,
which get reverted to baseline mode, and that a no-op call touches nothing.

THE intraday_depth_mode_enabled GATE ITSELF — the most important tests
here, not an afterthought. The first version of set_depth_symbols() had
NO config check at all: run.py's slow-timer call was unconditional, so a
symbol would have been put into real Kite FULL mode regardless of the
switch — the "ships OFF" story in migration 107 would have been false the
moment this code ran. Caught before it was ever committed. Fixed by
treating a disabled switch as "the desired depth set is empty" rather
than an early return, so the SAME revert-to-baseline path also un-asks
for depth already in flight if the switch is turned off mid-session,
not just refuses new asks.

The actual on_ticks() websocket callback lives nested inside
start_websocket(), which opens a real Kite connection and is out of scope
for an offline test the same way the rest of that method already is — the
depth-capture ordering fix (moved above the unrelated _capture_quote gate,
so a FULL-mode tick's depth field is never silently dropped) is covered by
inspection and by this project's "verify, never assert" discipline rather
than a unit test that would have to fake a live websocket.
"""

from __future__ import annotations

from tests import cfg_ctx


def _feed(symbols=("A", "B", "C")):
    from intraday.price_feed import PriceFeed
    return PriceFeed(list(symbols))


class _FakeTicker:
    """Stands in for kiteconnect.KiteTicker — just enough surface for
    set_depth_symbols() to drive: the three mode constants and a set_mode()
    call log."""
    MODE_FULL = "full"
    MODE_QUOTE = "quote"
    MODE_LTP = "ltp"

    def __init__(self):
        self.calls = []   # [(mode, [tokens])]

    def set_mode(self, mode, tokens):
        self.calls.append((mode, sorted(tokens)))


def _wired(feed, symbols_to_tokens: dict[str, int]):
    """Puts a feed into the 'connected with a fake ticker' state
    set_depth_symbols() requires, without touching a real socket."""
    feed._connected = True
    feed._ticker = _FakeTicker()
    feed._token_to_symbol = {v: k for k, v in symbols_to_tokens.items()}
    return feed._ticker


def test_depth_accessor_returns_none_when_nothing_captured_yet():
    """None means 'no data yet' -- must never be confused with an empty book."""
    f = _feed()
    assert f.depth("A") is None


def test_depth_accessor_returns_what_was_captured():
    """A shallow copy, same contract as quote() -- equal, not the same
    object, so a caller mutating its own read never corrupts the feed's
    internal state."""
    f = _feed()
    book = {"buy": [{"price": 100.0, "quantity": 10, "orders": 1}], "sell": []}
    with f._lock:
        f._depth["A"] = book
    assert f.depth("A") == book
    assert f.depth("B") is None


def test_switch_off_by_default_takes_no_live_action_even_when_wired():
    """THE gate this project has been burned by before: default config
    (switch off) must produce ZERO set_mode() calls, even though the feed
    is fully connected and would otherwise have work to do."""
    with cfg_ctx({"intraday_depth_mode_enabled": "false"}):
        f = _feed()
        ticker = _wired(f, {"A": 1, "B": 2})
        f.set_depth_symbols(["A", "B"])
    assert ticker.calls == []
    assert f._depth_symbols == set()


def test_switch_off_is_the_default_with_no_config_at_all():
    """Same as above with nothing set in system_config -- cfg_bool's own
    False default must be what actually governs, not an assumption.

    MUST use cfg_ctx({}), not the ambient process-wide config cache: without
    it, this reads whatever config._sys_config already holds -- None on a
    clean process, which triggers a real, uncontrolled Supabase fetch the
    moment cfg_bool() is called. That happened to return an empty dict (and
    pass) for as long as Supabase was unreachable this session; the instant
    it came back, the live intraday_depth_mode_enabled='true' loaded into
    the cache instead and this test failed -- a verify-suite check whose
    result depended on live infrastructure state, exactly what tools.verify
    is supposed to never do."""
    with cfg_ctx({}):
        f = _feed()
        ticker = _wired(f, {"A": 1, "B": 2})
        f.set_depth_symbols(["A", "B"])
    assert ticker.calls == []


def test_switch_on_reverts_already_full_symbols_when_turned_off_mid_session():
    """The specific failure mode the gate fix targets: a symbol already in
    FULL mode must be actively reverted to baseline the next time this
    runs with the switch off, not left in FULL forever."""
    f = _feed()
    f.mode = "ltp"
    ticker = _wired(f, {"A": 1, "B": 2})
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f.set_depth_symbols(["A", "B"])
    assert f._depth_symbols == {"A", "B"}
    ticker.calls.clear()
    with cfg_ctx({"intraday_depth_mode_enabled": "false"}):
        f.set_depth_symbols(["A", "B"])   # same requested set -- switch alone must revert
    assert f._depth_symbols == set()
    assert ("ltp", [1, 2]) in ticker.calls


def test_disconnected_feed_ignores_set_depth_symbols():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed()
        f._connected = False
        f.set_depth_symbols(["A", "B"])
    assert f._depth_symbols == set()


def test_set_depth_symbols_puts_new_symbols_into_full_mode():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed()
        ticker = _wired(f, {"A": 1, "B": 2, "C": 3})
        f.set_depth_symbols(["A", "B"])
    assert f._depth_symbols == {"A", "B"}
    assert ticker.calls == [("full", [1, 2])]


def test_set_depth_symbols_ignores_symbols_not_in_the_watch_list():
    """context_symbols() can include a name the feed never resolved a
    token for -- intersect, don't KeyError."""
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed()
        ticker = _wired(f, {"A": 1})
        f.set_depth_symbols(["A", "ZZZZ"])
    assert f._depth_symbols == {"A"}
    assert ticker.calls == [("full", [1])]


def test_set_depth_symbols_no_op_when_the_set_is_unchanged():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed()
        ticker = _wired(f, {"A": 1, "B": 2})
        f.set_depth_symbols(["A", "B"])
        ticker.calls.clear()
        f.set_depth_symbols(["B", "A"])   # same set, different order
    assert ticker.calls == []


def test_set_depth_symbols_reverts_removed_symbols_to_ltp_baseline():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed()
        ticker = _wired(f, {"A": 1, "B": 2})
        f.mode = "ltp"
        f.set_depth_symbols(["A", "B"])
        ticker.calls.clear()
        f.set_depth_symbols(["A"])   # B dropped from the depth-worthy set
    assert f._depth_symbols == {"A"}
    assert ticker.calls == [("ltp", [2])]


def test_set_depth_symbols_reverts_to_quote_baseline_when_thats_the_connections_mode():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed()
        ticker = _wired(f, {"A": 1, "B": 2})
        f.mode = "quote"
        f.set_depth_symbols(["A", "B"])
        ticker.calls.clear()
        f.set_depth_symbols(["A"])
    assert ticker.calls == [("quote", [2])]


def test_set_depth_symbols_drops_stale_depth_for_removed_symbols():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed()
        _wired(f, {"A": 1, "B": 2})
        f.set_depth_symbols(["A", "B"])
        with f._lock:
            f._depth["B"] = {"buy": [], "sell": []}
        f.set_depth_symbols(["A"])
    assert f.depth("B") is None


def test_set_depth_symbols_handles_simultaneous_add_and_remove():
    with cfg_ctx({"intraday_depth_mode_enabled": "true"}):
        f = _feed(["A", "B", "C"])
        ticker = _wired(f, {"A": 1, "B": 2, "C": 3})
        f.set_depth_symbols(["A"])
        ticker.calls.clear()
        f.set_depth_symbols(["B"])   # A removed, B added, in one call
    assert f._depth_symbols == {"B"}
    modes_called = {mode for mode, _ in ticker.calls}
    assert "full" in modes_called
    assert ("full", [2]) in ticker.calls
    assert any(mode != "full" and toks == [1] for mode, toks in ticker.calls)


TESTS = [
    ("depth() returns None when nothing captured yet", test_depth_accessor_returns_none_when_nothing_captured_yet),
    ("depth() returns what was captured", test_depth_accessor_returns_what_was_captured),
    ("switch off by default takes no live action even when wired", test_switch_off_by_default_takes_no_live_action_even_when_wired),
    ("switch off is the default with no config at all", test_switch_off_is_the_default_with_no_config_at_all),
    ("switch on reverts already-FULL symbols when turned off mid-session", test_switch_on_reverts_already_full_symbols_when_turned_off_mid_session),
    ("disconnected feed ignores set_depth_symbols", test_disconnected_feed_ignores_set_depth_symbols),
    ("set_depth_symbols puts new symbols into FULL mode", test_set_depth_symbols_puts_new_symbols_into_full_mode),
    ("set_depth_symbols ignores symbols outside the watch list", test_set_depth_symbols_ignores_symbols_not_in_the_watch_list),
    ("set_depth_symbols is a no-op when the set is unchanged", test_set_depth_symbols_no_op_when_the_set_is_unchanged),
    ("set_depth_symbols reverts removed symbols to LTP baseline", test_set_depth_symbols_reverts_removed_symbols_to_ltp_baseline),
    ("set_depth_symbols reverts to QUOTE baseline when that's the connection's mode", test_set_depth_symbols_reverts_to_quote_baseline_when_thats_the_connections_mode),
    ("set_depth_symbols drops stale depth for removed symbols", test_set_depth_symbols_drops_stale_depth_for_removed_symbols),
    ("set_depth_symbols handles simultaneous add and remove", test_set_depth_symbols_handles_simultaneous_add_and_remove),
]
