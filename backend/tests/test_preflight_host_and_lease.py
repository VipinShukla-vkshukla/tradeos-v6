"""
Who may place an order — the MACHINE, and the PROCESS (14-Aug-2026).

TWO SEPARATE QUESTIONS THAT KEPT GETTING ANSWERED WITH ONE WRONG CHECK
-----------------------------------------------------------------------
The previous session recorded F-12, "the IP allowlist is stale RIGHT NOW", from
a tools.health line reading `public IP is 103.197.74.232 but only
103.197.75.33 is recorded as allowlisted`. That reading was wrong, and wrong in
an instructive way: 103.197.75.33 is the Oracle VCN's STATIC address and it is
correctly allowlisted. 103.197.74.232 was the LAPTOP's dynamic ISP address.
health was checking the machine it ran on, not the machine that places orders,
and reporting the difference as an imminent live-trading failure. Four "distinct
addresses" across four sessions were four DHCP leases on a machine that has
never sent an order.

So an IP-vs-allowlist check is not built here. It is meaningless from any host
but the VCN, and nearly always trivially true on the VCN. What it was reaching
for is a question a machine can answer about ITSELF, offline, correctly, from
anywhere: am I the machine that is supposed to be doing this? That is
order_manager.host_permits_live().

The second question is a different one wearing similar clothes: even on the
right machine, is this PROCESS the one driving the book? F-11 —
position_lifecycle.main(manage=True) sells real shares through place() and
consults no lease at all — is that question going unasked. Both now converge on
preflight(), which is where every order path in this codebase already meets.

WHAT THIS PINS
---------------
  · a wrong host cannot place a LIVE order, and the refusal happens before any
    broker call — the wrong host must not spend a round trip to be told
  · PAPER is exempt from the host check and NOT exempt from the lease check
  · an empty recorded host means the check is ABSENT, not that everything is
    denied (a blank key must not brick a fresh install)
  · the entry/exit asymmetry, which is the whole design: an exit is refused
    ONLY while a different process holds an UNEXPIRED lease, so a handover —
    where the dead holder's lease has lapsed — lets the exit straight through,
    while an entry in that same state is refused
  · an unreadable lease refuses entries and permits exits
  · both switches are exact rollback levers
  · and, through preflight() itself rather than by eye, that the guards are
    actually WIRED — a pure function's correctness proves nothing about its
    callers, which this project has now been bitten by four times in one feature
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from types import SimpleNamespace

from tests import BACKEND, cfg_ctx  # noqa: F401  — path bootstrap


# ── harness ──────────────────────────────────────────────────────────────
#
# preflight() reads a kill switch, DRY_RUN, a phase gate and a WALL CLOCK
# before it reaches anything this module is about. Tests here must not depend
# on any of them, so all four are replaced with known answers, and the broker
# is replaced with a fake that RAISES on contact — which turns "the guard fired
# before any broker call" from a claim into an assertion.

class _BrokerContacted(Exception):
    pass


def _exploding_kite():
    def boom(*a, **k):
        raise _BrokerContacted("preflight reached the broker")
    return SimpleNamespace(fetch_holdings=boom, fetch_margins=boom,
                           fetch_ltp=boom, get_kite=boom)


@contextmanager
def _preflight_ctx(hostname: str, view, config: dict, kite=None):
    """
    Run preflight() with a known machine, a known lease and a known config.

    Every module global preflight touches is saved and restored: _recent,
    _blocked and _blocked_account are process-wide latches, and leaving one set
    would silently change what every later test in the suite reads.
    """
    from execution import order_manager as om
    from intraday import lease as lease_mod

    saved = {k: getattr(om, k) for k in
             ("is_kill_switch_active", "DRY_RUN", "orders_enabled", "is_market_open")}
    saved_recent = dict(om._recent)
    saved_blocked = dict(om._blocked)
    saved_blocked_acct = om._blocked_account
    saved_observe = lease_mod.observe
    saved_kite = sys.modules.get("kite")
    import socket
    saved_gethostname = socket.gethostname

    om.is_kill_switch_active = lambda: False
    om.DRY_RUN = False
    om.orders_enabled = lambda: True
    om.is_market_open = lambda *a, **k: True
    om._recent = {}
    om._blocked = {}
    om._blocked_account = None
    lease_mod.observe = lambda sb=None: view
    socket.gethostname = lambda: hostname
    sys.modules["kite"] = SimpleNamespace(kite_client=kite or _exploding_kite())
    try:
        with cfg_ctx(config):
            yield om
    finally:
        for k, v in saved.items():
            setattr(om, k, v)
        om._recent = saved_recent
        om._blocked = saved_blocked
        om._blocked_account = saved_blocked_acct
        lease_mod.observe = saved_observe
        socket.gethostname = saved_gethostname
        if saved_kite is None:
            sys.modules.pop("kite", None)
        else:
            sys.modules["kite"] = saved_kite


def _view(holder="", hostname="tradeos-vcn", mine=False, other=False, readable=True):
    from intraday.lease import LeaseView
    return LeaseView(holder, hostname, mine, other, readable, "test")


def _mine():
    from intraday import lease
    return _view(holder=lease.instance_id(), mine=True)


def _req(side="SELL", qty=10, price=100.0):
    from execution.order_manager import OrderRequest
    return OrderRequest("TESTCO", side, qty, "LIMIT", price, reason="test")


#: swing is the LIVE book; these two make the mode explicit rather than relying
#: on gates.trading_mode()'s PAPER default.
LIVE_CFG = {"swing_trading_mode": "LIVE", "live_order_host": "tradeos-vcn"}
PAPER_CFG = {"swing_trading_mode": "PAPER", "live_order_host": "tradeos-vcn"}


# ── host_permits_live(), pure ────────────────────────────────────────────

def test_the_recorded_host_is_permitted():
    from execution.order_manager import host_permits_live
    ok, why = host_permits_live("tradeos-vcn", "tradeos-vcn")
    assert ok is True and why == ""


def test_any_other_host_is_refused():
    from execution.order_manager import host_permits_live
    ok, why = host_permits_live("Vipin", "tradeos-vcn")
    assert ok is False
    # The refusal must name BOTH machines, or the operator cannot tell whether
    # the host moved or the key is stale.
    assert "Vipin" in why and "tradeos-vcn" in why


def test_the_match_is_a_case_insensitive_prefix():
    """Same idiom as lease._is_primary(), so a fully-qualified name or a
    different case does not read as a different machine."""
    from execution.order_manager import host_permits_live
    assert host_permits_live("TRADEOS-VCN", "tradeos-vcn")[0] is True
    assert host_permits_live("tradeos-vcn.subnet.oraclevcn.com", "tradeos-vcn")[0] is True
    assert host_permits_live("  tradeos-vcn  ", "tradeos-vcn")[0] is True


def test_a_prefix_is_not_a_substring():
    """'vcn' must not match 'tradeos-vcn' — a looser rule would let any host
    whose name happens to contain the recorded one through."""
    from execution.order_manager import host_permits_live
    assert host_permits_live("laptop-tradeos-vcn", "tradeos-vcn")[0] is False


def test_an_empty_record_means_the_check_is_absent():
    """A component with no data must be indistinguishable from that component
    not being there. A blank key on a fresh install must not refuse every
    order before the operator has been told the key exists."""
    from execution.order_manager import host_permits_live
    assert host_permits_live("anything", "")[0] is True
    assert host_permits_live("anything", "   ")[0] is True
    assert host_permits_live("anything", None)[0] is True


def test_several_hosts_may_be_recorded():
    """kite_allowlisted_ip is already a comma-separated list because Zerodha's
    console accepts several addresses; a single-valued key here could not
    describe the same two-machine reality."""
    from execution.order_manager import host_permits_live
    assert host_permits_live("Vipin", "tradeos-vcn, Vipin")[0] is True
    assert host_permits_live("other", "tradeos-vcn, Vipin")[0] is False


# ── lease_permits(), pure ────────────────────────────────────────────────

def test_the_holder_may_do_anything():
    from execution.order_manager import lease_permits
    for side in ("BUY", "SELL"):
        ok, _ = lease_permits(side, held_by_me=True, held_by_other=False,
                              readable=True)
        assert ok is True, f"the lease holder must not be blocked from a {side}"


def test_a_live_holder_blocks_both_sides():
    """The one state where refusing an exit is safe: something else is
    demonstrably alive and will act on this position on its own cycle."""
    from execution.order_manager import lease_permits
    for side in ("BUY", "SELL"):
        ok, why = lease_permits(side, held_by_me=False, held_by_other=True,
                                readable=True, holder="tradeos-vcn-4021-ab12cd")
        assert ok is False
        assert "tradeos-vcn-4021-ab12cd" in why, "name who holds it"


def test_a_lapsed_lease_lets_the_exit_through_and_refuses_the_entry():
    """THE HANDOVER CASE, and the whole reason the two sides differ.

    When an active daemon dies its lease is not handed over, it LAPSES: for up
    to lease_ttl_seconds the row still names a holder that no longer exists. A
    symmetric check would spend that window refusing exits on behalf of a dead
    process — the wrong answer at the worst moment. Here the lapse reads as
    held_by_other=False and the exit goes straight through, while an entry in
    that same state stays refused because nothing is watching the book."""
    from execution.order_manager import lease_permits
    sell_ok, _ = lease_permits("SELL", held_by_me=False, held_by_other=False,
                               readable=True, holder="tradeos-vcn-4021-ab12cd")
    assert sell_ok is True, "a lapsed lease must never stand between you and an exit"
    buy_ok, why = lease_permits("BUY", held_by_me=False, held_by_other=False,
                                readable=True, holder="tradeos-vcn-4021-ab12cd")
    assert buy_ok is False
    assert "unmanaged" in why


def test_a_free_lease_lets_the_exit_through_and_refuses_the_entry():
    """Same asymmetry with no holder at all — the state after a clean
    release(), which is what the book sits in overnight."""
    from execution.order_manager import lease_permits
    assert lease_permits("SELL", False, False, readable=True)[0] is True
    assert lease_permits("BUY", False, False, readable=True)[0] is False


def test_an_unreadable_lease_refuses_entries_and_permits_exits():
    """A database failure is not an answer. An entry can wait a cycle; unknown
    state must never be the thing that stops you closing a position."""
    from execution.order_manager import lease_permits
    ok_sell, _ = lease_permits("SELL", False, False, readable=False)
    assert ok_sell is True
    ok_buy, why = lease_permits("BUY", False, False, readable=False)
    assert ok_buy is False
    assert "could not read" in why


def test_side_is_read_case_insensitively():
    from execution.order_manager import lease_permits
    assert lease_permits("sell", False, False, readable=True)[0] is True


# ── the guards are WIRED — asserted through preflight(), not by eye ──────

def test_preflight_refuses_a_live_order_from_the_wrong_host():
    with _preflight_ctx("Vipin", _mine(), LIVE_CFG) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.ok is False
    assert r.blocked_by == "WRONG_HOST", r.message


def test_the_wrong_host_is_refused_before_the_broker_is_contacted():
    """ORDERING. The fake broker raises on any contact; reaching it would
    surface as _BrokerContacted rather than a clean refusal. A wrong host that
    still pays a price fetch, a holdings fetch and a margins fetch to be told
    no has learned the same thing three round trips later."""
    with _preflight_ctx("Vipin", _mine(), LIVE_CFG) as om:
        r = om.preflight(_req("SELL", price=None), sb=object(), framework="SWING")
    assert r.blocked_by == "WRONG_HOST"


def test_the_right_host_gets_past_the_host_guard():
    """A check that cannot PASS is the same defect wearing a different hat."""
    with _preflight_ctx("tradeos-vcn", _mine(), LIVE_CFG) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by != "WRONG_HOST", r.message


def test_paper_is_exempt_from_the_host_check():
    """A paper fill never reaches the broker, so no address is involved.
    Refusing paper on the laptop would refuse the one thing a laptop is for."""
    with _preflight_ctx("Vipin", _mine(), PAPER_CFG) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by != "WRONG_HOST", r.message


def test_preflight_refuses_when_another_process_holds_the_lease():
    """F-11 through the path that has it: position_lifecycle's exit calls
    place(), place() calls preflight(), and this is what it now meets."""
    other = _view(holder="tradeos-vcn-4021-ab12cd", other=True)
    with _preflight_ctx("tradeos-vcn", other, LIVE_CFG) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.ok is False
    assert r.blocked_by == "NOT_LEASE_HOLDER", r.message


def test_the_lease_guard_also_fires_before_the_broker():
    other = _view(holder="tradeos-vcn-4021-ab12cd", other=True)
    with _preflight_ctx("tradeos-vcn", other, LIVE_CFG) as om:
        r = om.preflight(_req("SELL", price=None), sb=object(), framework="SWING")
    assert r.blocked_by == "NOT_LEASE_HOLDER"


def test_preflight_lets_an_exit_through_a_handover():
    """End to end, the case the design turns on: the daemon died, its lease has
    lapsed, and a hand-run exit must not be refused on behalf of a dead
    process."""
    lapsed = _view(holder="tradeos-vcn-4021-ab12cd", other=False)
    with _preflight_ctx("tradeos-vcn", lapsed, LIVE_CFG) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by != "NOT_LEASE_HOLDER", r.message


def test_preflight_refuses_an_entry_through_the_same_handover():
    lapsed = _view(holder="tradeos-vcn-4021-ab12cd", other=False)
    with _preflight_ctx("tradeos-vcn", lapsed, LIVE_CFG) as om:
        r = om.preflight(_req("BUY"), sb=object(), framework="SWING")
    assert r.ok is False
    assert r.blocked_by == "NOT_LEASE_HOLDER", r.message


def test_paper_is_NOT_exempt_from_the_lease_check():
    """Two processes writing the same paper position poisons the learning loop
    exactly as two live orders empty an account. The money differs; the
    doubling does not."""
    other = _view(holder="tradeos-vcn-4021-ab12cd", other=True)
    with _preflight_ctx("Vipin", other, PAPER_CFG) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by == "NOT_LEASE_HOLDER", r.message


# ── the switches are exact rollback levers ───────────────────────────────

def test_host_check_off_restores_previous_behaviour():
    cfg = dict(LIVE_CFG, live_order_host_check="false")
    with _preflight_ctx("Vipin", _mine(), cfg) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by != "WRONG_HOST", r.message


def test_lease_check_off_restores_previous_behaviour():
    other = _view(holder="tradeos-vcn-4021-ab12cd", other=True)
    cfg = dict(LIVE_CFG, live_order_lease_check="false")
    with _preflight_ctx("tradeos-vcn", other, cfg) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by != "NOT_LEASE_HOLDER", r.message


def test_both_switches_default_ON():
    """Neither key is seeded until migration 078 runs, and a guard that is
    inert until then would be no guard at all on the day it ships. cfg_ctx({})
    is an EMPTY config — exactly the pre-migration state."""
    with _preflight_ctx("Vipin", _mine(), {"swing_trading_mode": "LIVE",
                                           "intraday_lease_primary_host": "tradeos-vcn"}) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by == "WRONG_HOST", (
        "with no live_order_host row, the check must fall back to "
        "intraday_lease_primary_host and still fire")
    other = _view(holder="tradeos-vcn-4021-ab12cd", other=True)
    with _preflight_ctx("tradeos-vcn", other, {"swing_trading_mode": "LIVE"}) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by == "NOT_LEASE_HOLDER"


def test_live_order_host_overrides_the_lease_primary_fallback():
    """The dedicated key is the truth; intraday_lease_primary_host is only a
    bootstrap for the machine that has not been told about the new key yet."""
    cfg = {"swing_trading_mode": "LIVE", "live_order_host": "Vipin",
           "intraday_lease_primary_host": "tradeos-vcn"}
    with _preflight_ctx("Vipin", _mine(), cfg) as om:
        r = om.preflight(_req("SELL"), sb=object(), framework="SWING")
    assert r.blocked_by != "WRONG_HOST", r.message


# ── lease.observe() reads and does not write ─────────────────────────────

def test_observe_never_writes():
    """The property the whole F-11 fix rests on. acquire() and renew() both
    upsert; calling either from preflight would have the pipeline STEAL the
    lease from the daemon it is meant to defer to. observe() must only read."""
    from intraday import lease

    calls = []

    class _Tbl:
        def select(self, *a, **k):
            calls.append("select")
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return SimpleNamespace(data=[{
                "holder": "tradeos-vcn-4021-ab12cd", "hostname": "tradeos-vcn",
                "expires_at": "2020-01-01T00:00:00+00:00"}])

        def upsert(self, *a, **k):
            calls.append("upsert")
            raise AssertionError("observe() must never write to the lease")

        def update(self, *a, **k):
            calls.append("update")
            raise AssertionError("observe() must never write to the lease")

        def insert(self, *a, **k):
            calls.append("insert")
            raise AssertionError("observe() must never write to the lease")

    v = lease.observe(SimpleNamespace(table=lambda n: _Tbl()))
    assert calls == ["select"], f"observe() issued {calls}"
    assert v.readable is True
    assert v.held_by_me is False
    # 2020 is long past, so this is a LAPSED lease, not a live one.
    assert v.held_by_other is False


def test_observe_separates_unreadable_from_free():
    """'Nobody holds it' and 'I could not find out' are opposite facts.
    Collapsing them into one empty holder is how a database blip reads as
    permission."""
    from intraday import lease

    class _Boom:
        def table(self, *a, **k):
            raise RuntimeError("PostgREST is down")

    v = lease.observe(_Boom())
    assert v.readable is False
    assert v.holder == ""
    assert v.held_by_other is False


def test_observe_treats_an_unreadable_timestamp_as_expired():
    """The conservative direction: a corrupt row must not be able to forbid
    every exit for as long as it stays corrupt."""
    from intraday import lease

    class _Tbl:
        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return SimpleNamespace(data=[{"holder": "someone-else",
                                          "hostname": "h", "expires_at": "not a date"}])

    v = lease.observe(SimpleNamespace(table=lambda n: _Tbl()))
    assert v.readable is True
    assert v.held_by_other is False


TESTS = [
    ("the recorded host is permitted", test_the_recorded_host_is_permitted),
    ("any other host is refused", test_any_other_host_is_refused),
    ("the match is a case-insensitive prefix", test_the_match_is_a_case_insensitive_prefix),
    ("a prefix is not a substring", test_a_prefix_is_not_a_substring),
    ("an empty record means the check is absent",
     test_an_empty_record_means_the_check_is_absent),
    ("several hosts may be recorded", test_several_hosts_may_be_recorded),
    ("the holder may do anything", test_the_holder_may_do_anything),
    ("a live holder blocks both sides", test_a_live_holder_blocks_both_sides),
    ("a lapsed lease lets the exit through and refuses the entry",
     test_a_lapsed_lease_lets_the_exit_through_and_refuses_the_entry),
    ("a free lease lets the exit through and refuses the entry",
     test_a_free_lease_lets_the_exit_through_and_refuses_the_entry),
    ("an unreadable lease refuses entries and permits exits",
     test_an_unreadable_lease_refuses_entries_and_permits_exits),
    ("side is read case-insensitively", test_side_is_read_case_insensitively),
    ("preflight refuses a live order from the wrong host",
     test_preflight_refuses_a_live_order_from_the_wrong_host),
    ("the wrong host is refused before the broker is contacted",
     test_the_wrong_host_is_refused_before_the_broker_is_contacted),
    ("the right host gets past the host guard",
     test_the_right_host_gets_past_the_host_guard),
    ("paper is exempt from the host check", test_paper_is_exempt_from_the_host_check),
    ("preflight refuses when another process holds the lease",
     test_preflight_refuses_when_another_process_holds_the_lease),
    ("the lease guard also fires before the broker",
     test_the_lease_guard_also_fires_before_the_broker),
    ("preflight lets an exit through a handover",
     test_preflight_lets_an_exit_through_a_handover),
    ("preflight refuses an entry through the same handover",
     test_preflight_refuses_an_entry_through_the_same_handover),
    ("paper is NOT exempt from the lease check",
     test_paper_is_NOT_exempt_from_the_lease_check),
    ("host check off restores previous behaviour",
     test_host_check_off_restores_previous_behaviour),
    ("lease check off restores previous behaviour",
     test_lease_check_off_restores_previous_behaviour),
    ("both switches default ON", test_both_switches_default_ON),
    ("live_order_host overrides the lease primary fallback",
     test_live_order_host_overrides_the_lease_primary_fallback),
    ("observe() never writes", test_observe_never_writes),
    ("observe() separates unreadable from free", test_observe_separates_unreadable_from_free),
    ("observe() treats an unreadable timestamp as expired",
     test_observe_treats_an_unreadable_timestamp_as_expired),
]
