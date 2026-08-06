"""
The SDN short family and the two gates that are short-only.

WHAT THIS CATCHES
-----------------
`shortability.can_short` is a SOLVENCY filter, not a quality one: it asks
whether a position can be COVERED, not whether the trade is good. A stock locked
at its upper circuit has buyers queued and no sellers — there is no price at
which a short can be bought back — and an uncovered short goes to the exchange's
auction at a penalty around 20% of the trade value.

NSE circuit bands are in no feed this system ingests (migration 040 records
this), so the upper-band guard is a PROXY built from today's move and the name's
own ATR, and says so everywhere it appears.

The structure gate is the other short-only one: judged by `gate_long`, a short
would be blocked by the DOWNTREND it most wants and permitted in the uptrend
that squeezes it.
"""

from __future__ import annotations

from tests import cfg_ctx
from tests._fixtures import CLEAN_STOCK, ctx_for

#: Shorts must be ON for the engine to be exercised at all.
SHORTS_ON = {"intraday_allow_shorts": "true", "intraday_short_min_bars": "15"}


def test_all_three_conditions_fire_on_their_own_shape():
    """
    Each condition on the session built for it, producing coherent, correctly
    directed levels that clear the engine's own R:R floor.

    Guards the chase rule and the target construction, both of which were wrong
    in the first version: they generated a setup and then discarded it on
    arithmetic, which in the logs reads as "the engine found nothing" rather
    than "the engine arrived late".

    DELIBERATELY DOES NOT REQUIRE EVERY SETUP TO CLEAR THE COST GATE. That is
    the cost model's job, not the engine's, and refusing a marginal setup is the
    cost model working. Asserting otherwise would make this test fail whenever
    the cost schedule is tightened — punishing a correct change. See
    `test_the_cost_gate_refuses_a_stop_too_tight_to_pay_for_itself` for the
    other half of that contract.
    """
    from intraday.strategies.short_distribution import ShortDistribution
    with cfg_ctx(SHORTS_ON):
        eng = ShortDistribution()
        seen = {}
        for kind in ("trap", "vwap_reject", "breakdown"):
            s = eng.evaluate(ctx_for(kind), "MORNING")
            assert s is not None, (
                f"the {kind} session produced NO setup — most likely the chase "
                f"rule or the target construction, both of which generate a "
                f"setup and then discard it on arithmetic, which reads as "
                f"'found nothing' rather than 'arrived late'")
            assert s.direction == "SHORT"
            ok, why = s.coherent()
            assert ok, f"{kind}: engine emitted incoherent levels — {why}"
            assert s.rr >= 1.3, f"{kind}: R:R {s.rr:.2f} below the engine's own floor"
            seen[s.meta["sub_engine"]] = kind
        assert set(seen) == {"TRP", "VWR", "ORB"}, \
            f"expected all three conditions, got {seen}"


def test_at_least_one_condition_is_economic_at_realistic_size():
    """
    The engine must not be UNIVERSALLY uneconomic. If no condition can ever
    clear the cost gate at a realistic clip, the family is a detector that can
    never trade — and it would look exactly like a quiet market.

    Measured on this account: MIS round trip ~0.21%, and the cost model requires
    the stop to be a MULTIPLE of friction, so a stop under roughly 0.59% is
    refused however good the R:R. TRP (~0.69%) and ORB (~0.65%) clear it; VWR
    (~0.58%) is the tightest of the three and sits just under. That is a real
    property of shorting a ₹20,000 book at MIS costs, not a defect — but if it
    ever becomes true of ALL THREE, the family needs wider stops or a bigger
    clip, and this test is what says so.
    """
    from intraday.strategies.short_distribution import ShortDistribution
    from intraday.cost_model import is_worth_taking
    with cfg_ctx(SHORTS_ON):
        eng = ShortDistribution()
        economic = []
        for kind in ("trap", "vwap_reject", "breakdown"):
            s = eng.evaluate(ctx_for(kind), "MORNING")
            qty = int(5000 // s.entry) or 1
            ok, _ = is_worth_taking(s.entry, qty, s.target, s.stop, "MIS", s.direction)
            if ok:
                economic.append(s.meta["sub_engine"])
        assert economic, (
            "NO short condition survives its own costs at a ₹5,000 clip — the "
            "family can detect but can never trade, which in production is "
            "indistinguishable from finding nothing")


def test_the_cost_gate_refuses_a_stop_too_tight_to_pay_for_itself():
    """
    The other half of the contract above. A stop tighter than a multiple of the
    round trip means every loss is larger than the risk that was sized for —
    the trade cannot lose small, only badly. Asserted in the SHORT direction,
    because the long form of this check would pass on levels a short fails.
    """
    from intraday.cost_model import is_worth_taking
    with cfg_ctx(SHORTS_ON):
        # entry 250, stop 250.6 (0.24% — well under the friction multiple)
        ok, why = is_worth_taking(250.0, 20, 244.0, 250.6, "MIS", "SHORT")
        assert not ok, "a 0.24% stop was accepted against a ~0.21% round trip"
        assert "stop" in why.lower(), f"refused for the wrong reason: {why}"
        # The same setup with room passes, so the gate is not simply always-no.
        ok, why = is_worth_taking(250.0, 20, 244.0, 252.5, "MIS", "SHORT")
        assert ok, f"a 1.0% stop with a 2.4% target was refused — {why}"


def test_preconditions_refuse_what_they_should():
    """Every condition requires the name to be down on the day AND under VWAP."""
    from intraday.strategies.short_distribution import ShortDistribution
    with cfg_ctx(SHORTS_ON):
        eng = ShortDistribution()
        # Green on the day — shorting demonstrated demand, and the population
        # most likely to lock at its upper circuit.
        assert eng.evaluate(ctx_for("trap", prev_close=95.0), "MORNING") is None
        # Above VWAP — the average buyer today is in profit.
        assert eng.evaluate(ctx_for("trap", vwap=90.0), "MORNING") is None
        # No VWAP at all.
        assert eng.evaluate(ctx_for("trap", vwap=None), "MORNING") is None


def test_structure_gate_inverts_for_a_short():
    """DOWNTREND: long BLOCKED, short allowed. UPTREND: the reverse."""
    from analysis.market_structure import (gate_long, gate_short, Structure,
                                           UPTREND, DOWNTREND, CONFIRMED_UP,
                                           REVERSAL_UP, RANGE, UNKNOWN)
    with cfg_ctx():
        cases = {
            UPTREND:      (True,  False),   # (long allowed, short allowed)
            DOWNTREND:    (False, True),
            CONFIRMED_UP: (True,  False),
            RANGE:        (True,  True),
            UNKNOWN:      (True,  True),
        }
        for state, (want_long, want_short) in cases.items():
            s = Structure(state=state, detail="test")
            assert gate_long(s)[0] is want_long, f"{state}: long gate wrong"
            assert gate_short(s)[0] is want_short, f"{state}: short gate wrong"
        # REVERSAL_UP blocks a short with NO config override — standing in front
        # of a forming reversal is a squeeze, not an entry-timing choice.
        assert gate_short(Structure(state=REVERSAL_UP, detail="t"))[0] is False


def test_shortability_is_a_solvency_filter():
    """Each refusal below is about being unable to COVER, not about trade quality."""
    from intraday import shortability
    with cfg_ctx(SHORTS_ON):
        ok, why, _ = shortability.can_short(ctx_for("trap"), CLEAN_STOCK, minutes_left=200)
        assert ok, f"a clean, liquid, unflagged name was refused — {why}"

        refusals = {
            "upper-circuit proxy": (ctx_for("trap", ltp=105.5, prev_close=100.0),
                                    CLEAN_STOCK, 200),
            "ASM/GSM surveillance": (ctx_for("trap"),
                                     {**CLEAN_STOCK, "asm_flag": True}, 200),
            "squeeze (88% delivery)": (ctx_for("trap"),
                                       {**CLEAN_STOCK, "delivery_pct": 88.0}, 200),
            "illiquid": (ctx_for("trap", value_cr=30.0),
                         {**CLEAN_STOCK, "value_cr": 30.0}, 200),
            "no runway to cover": (ctx_for("trap"), CLEAN_STOCK, 40),
            "already collapsed": (ctx_for("trap", ltp=92.0, prev_close=100.0),
                                  CLEAN_STOCK, 200),
        }
        for label, (ctx, stock, mins) in refusals.items():
            ok, why, _ = shortability.can_short(ctx, stock, minutes_left=mins)
            assert not ok, f"{label}: SHORTABLE when it must be refused"
            assert why, f"{label}: refused with no reason given"


def test_every_shortability_switch_is_actually_read():
    """
    A CRITICAL config key that nothing consumes is this project's most-repeated
    defect — `intraday_short_cover_buffer_min` was one, unreachable behind the
    15:20 phase boundary. `describe_gates()` enumerates what the module reads;
    each must resolve.
    """
    from intraday import shortability
    with cfg_ctx(SHORTS_ON):
        gates = shortability.describe_gates()
        assert len(gates) >= 6, f"only {len(gates)} switches enumerated"
        for key, value, what in gates:
            assert key.startswith("intraday_short_"), f"{key} is misnamed"
            assert value not in (None, ""), f"{key} resolved to nothing"
            assert what, f"{key} has no description of what it protects"


def test_sdn_receives_no_capital_while_shadowed():
    """
    SHADOW means evaluates and records, never receives capital. If a shadowed
    engine can produce a Proposal, it has reached capital through a second door.
    """
    from allocation.proposal import from_intraday
    from intraday.strategies.base import Setup
    with cfg_ctx(SHORTS_ON):
        s = Setup("ZOMATO", "SDN", "SHORT", 250.0, 253.0, 244.0, 0.7, "r", "i",
                  meta={"family": "SDN", "lifecycle": "SHADOW"})
        assert from_intraday(s, 40) is None, \
            "a SHADOW engine produced a Proposal — shadow is not shadow"


TESTS = [
    ("all three conditions fire",             test_all_three_conditions_fire_on_their_own_shape),
    ("at least one is economic at size",      test_at_least_one_condition_is_economic_at_realistic_size),
    ("cost gate refuses a too-tight stop",    test_the_cost_gate_refuses_a_stop_too_tight_to_pay_for_itself),
    ("preconditions refuse correctly",        test_preconditions_refuse_what_they_should),
    ("structure gate inverts for a short",    test_structure_gate_inverts_for_a_short),
    ("shortability refuses uncoverable",      test_shortability_is_a_solvency_filter),
    ("every shortability switch is read",     test_every_shortability_switch_is_actually_read),
    ("a SHADOW engine gets no capital",       test_sdn_receives_no_capital_while_shadowed),
]
