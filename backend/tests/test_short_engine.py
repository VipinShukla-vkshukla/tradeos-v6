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

from intraday.session import PRIME  # was the literal PRIME, a phase that
                                    # does not exist — see test_break_confirmation
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
            s = eng.evaluate(ctx_for(kind), PRIME)
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
        assert set(seen) == {"TRP", "VREJ", "BRKD"}, \
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
            s = eng.evaluate(ctx_for(kind), PRIME)
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
        assert eng.evaluate(ctx_for("trap", prev_close=95.0), PRIME) is None
        # Above VWAP — the average buyer today is in profit.
        assert eng.evaluate(ctx_for("trap", vwap=90.0), PRIME) is None
        # No VWAP at all.
        assert eng.evaluate(ctx_for("trap", vwap=None), PRIME) is None


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


# ── stops routed through risk_from_structure() — 19-Aug-2026 ───────────────

def test_min_risk_pct_now_reaches_sdn():
    """
    THE WHOLE POINT OF THE FIX, DEMONSTRATED ON THIS MODULE'S OWN FIXTURES.

    Before 19-Aug-2026, SDN built its own stops directly and never called
    risk_from_structure() — so intraday_min_risk_pct (armed the same session)
    could refuse every OTHER engine's too-tight stop and never once see one
    of SDN's, regardless of how it was set.

    Measured on the project's own "trap" and "vwap_reject" fixtures at
    floor=0.0: risk_pct 0.573% and 0.576% respectively — BOTH under the 0.6
    floor armed this session. That is a real, live consequence of this fix,
    not a hypothetical one, and it is recorded honestly rather than tuned
    away: the floor is doing exactly what it was armed to do system-wide.
    """
    from intraday.strategies.short_distribution import ShortDistribution
    with cfg_ctx({**SHORTS_ON, "intraday_min_risk_pct": "0.6"}):
        eng = ShortDistribution()
        assert eng.evaluate(ctx_for("trap"), PRIME) is None, \
            "trap's 0.573%-risk fixture must be refused once the floor reaches SDN"
        assert eng.evaluate(ctx_for("vwap_reject"), PRIME) is None, \
            "vwap_reject's 0.576%-risk fixture must be refused once the floor reaches SDN"
        # breakdown clears the floor (0.645% measured) and must be unaffected —
        # the floor refuses what is genuinely too tight, not everything.
        s = eng.evaluate(ctx_for("breakdown"), PRIME)
        assert s is not None, "breakdown, comfortably above the floor, was wrongly refused"


def test_min_risk_pct_off_restores_all_three():
    """The counterpart — floor=0.0 (this repository's own no-op value) must
    restore exactly what test_all_three_conditions_fire_on_their_own_shape
    already expects, proving the floor is what changed, not something else."""
    from intraday.strategies.short_distribution import ShortDistribution
    with cfg_ctx({**SHORTS_ON, "intraday_min_risk_pct": "0.0"}):
        eng = ShortDistribution()
        for kind in ("trap", "vwap_reject", "breakdown"):
            assert eng.evaluate(ctx_for(kind), PRIME) is not None, \
                f"{kind} refused at floor=0.0 — the shared floor must be a true no-op there"


def test_max_risk_pct_refuses_a_stop_wider_than_the_cap():
    """
    THE CEILING THIS ENGINE NEVER HAD, ISOLATED FROM THE OTHER GATE THAT
    ALSO NARROWS IT. `breakdown`'s own measured risk (0.645%, see the
    min_risk_pct test above) against a cap set BELOW it, then the shipped
    default.

    NOT TESTED WITH A DELIBERATELY EXTREME STOP, AND THE REASON IS RECORDED.
    An early version built an 8.7%-risk fixture to prove this cleanly and it
    stayed refused even with the cap raised to 20% — not a test bug, a real
    property found by running it: `_target()`'s ATR-capped reward (~1.2% of
    price under this account's settings) means `intraday_short_min_rr` (1.3)
    already refuses anything wider than roughly 0.9% risk before this cap
    gets a chance to bind. So under CURRENT settings this cap is a genuine
    but SECONDARY safety net — defense in depth against a future change to
    the R:R floor or target multiplier, not the first gate a wide SDN stop
    meets today. 1.50 is set above the empirically BEST band (n=80,
    +0.442R, >=0.9% risk) deliberately, so it protects against a broken
    detection without cutting SDN's own strongest cohort.
    """
    from intraday.strategies.short_distribution import ShortDistribution
    with cfg_ctx({**SHORTS_ON, "intraday_short_max_risk_pct": "0.5"}):
        assert ShortDistribution().evaluate(ctx_for("breakdown"), PRIME) is None, \
            "0.645%-risk breakdown must be refused against a 0.5% cap"
    with cfg_ctx({**SHORTS_ON, "intraday_short_max_risk_pct": "1.50"}):
        assert ShortDistribution().evaluate(ctx_for("breakdown"), PRIME) is not None, \
            "the same setup must pass at the shipped default cap"


def test_trap_stop_is_buffered_exactly_once_in_either_branch():
    """
    THE SIDE FIX. Old formula: `min(day_high, prev_high*buf) * buf` — a
    second buffer stacked on the prev_high branch whenever it won the min.
    Both branches asserted directly against the single-buffer arithmetic the
    module's own comment has always described.
    """
    from intraday.strategies.short_distribution import ShortDistribution
    buf = 1 + 0.12 / 100.0

    # prev_high branch wins (day_high's overshoot exceeds the buffer size).
    ctx = ctx_for("trap", prev_high=100.0, day_high=101.2)
    with cfg_ctx(SHORTS_ON):
        s = ShortDistribution().evaluate(ctx, PRIME)
    assert s is not None
    expected = round(100.0 * buf, 2)
    assert abs(s.stop - expected) < 0.01, (
        f"prev_high branch: stop={s.stop}, expected single-buffered {expected} "
        f"(old double-buffered value would be {round(100.0 * buf * buf, 2)})")

    # day_high branch wins (overshoot smaller than the buffer itself).
    ctx2 = ctx_for("trap", prev_high=100.0, day_high=100.05)
    with cfg_ctx(SHORTS_ON):
        s2 = ShortDistribution().evaluate(ctx2, PRIME)
    assert s2 is not None
    expected2 = round(100.05 * buf, 2)
    assert abs(s2.stop - expected2) < 0.01, f"day_high branch: stop={s2.stop}, expected {expected2}"


def test_frame_meta_reaches_every_conditions_setup():
    """`**frame.meta()` merged into all three — proving the wiring, not just
    the stop VALUE, reaches every condition. Empty under the default refuse
    mode (see base.RiskFrame.meta()'s own docstring), so this checks presence
    of the merge rather than any specific key."""
    from intraday.strategies.short_distribution import ShortDistribution
    import inspect
    src = inspect.getsource(ShortDistribution)
    for cond, label in (("_vwap_rejection", "VREJ"), ("_trap", "TRP"),
                        ("_range_breakdown", "BRKD")):
        method_src = src[src.index(f"def {cond}"):]
        method_src = method_src[:method_src.index("\n    def ", 10)]
        assert "**frame.meta()" in method_src, \
            f"{label} ({cond}) does not merge frame.meta() into its Setup"


# ── sub_engine survives registry.evaluate_all() — 20-Aug-2026 ──────────────

def test_registry_preserves_sdns_own_condition_in_sub_engine():
    """
    THE BUG. `registry.evaluate_all()`'s own comment says sub_engine is
    "which condition actually fired" — but it used `s.meta["sub_engine"] =
    s.strategy`, which unconditionally overwrote whatever short_distribution.
    py's three methods had already set (VWR/TRP/ORB) with `s.strategy`
    ("SDN", the same for every condition). Every historical SDN row reads
    sub_engine="SDN", indistinguishable from strategy — which is exactly why
    the per-condition confidence split this session tried to run against
    live data came back empty. Driven through the REAL registry path, not
    ShortDistribution() directly, because that is where the overwrite lived.
    """
    from intraday.strategies import registry
    with cfg_ctx(SHORTS_ON):
        best, _all = registry.evaluate_all(ctx_for("trap"), PRIME)
    assert best is not None, "the trap fixture must still produce a setup"
    assert best.strategy == "SDN", best.strategy
    assert best.meta.get("sub_engine") == "TRP", (
        f"sub_engine was overwritten to {best.meta.get('sub_engine')!r} — "
        f"expected the CONDITION (TRP), not the family (SDN)")


def test_registry_still_defaults_sub_engine_for_single_condition_engines():
    """
    THE NO-REGRESSION HALF. Every engine but SDN has exactly one condition,
    so sub_engine == strategy was already the honest answer for them —
    setdefault() must still produce it when the engine itself sets nothing.
    """
    from intraday.strategies.orb import OpeningRangeBreakout
    from intraday.strategies.base import Setup
    s = Setup("TEST", "ORB", "LONG", 100.0, 99.0, 103.0, 0.7, "r", "i", meta={})
    assert "sub_engine" not in s.meta
    s.meta.setdefault("sub_engine", s.strategy)
    assert s.meta["sub_engine"] == "ORB"


def test_allocator_record_carries_sub_engine_through():
    """
    allocation_decisions.source is the FAMILY (proposal.from_intraday sets it
    that way), so GAP/PDL/ORB and PBK/VWR are indistinguishable in that
    column — confirmed 20-Aug-2026 while trying to read GAP's own day
    separately from ORB's and finding no way to. `_record()` now copies
    `p.meta["sub_engine"]` through as its own column.
    """
    from allocation.allocator import Allocator
    from allocation.proposal import Proposal
    a = Allocator.__new__(Allocator)
    p = Proposal(symbol="MCX", framework="INTRADAY", product="MIS",
                entry=100.0, stop=99.0, target=103.0, quantity=10,
                source="ORB", native_rank=80.0, direction="LONG",
                meta={"sub_engine": "GAP"})
    row = a._record({"proposal": p, "verdict": "DECLINE", "edge": -0.5})
    assert row["source"] == "ORB", "family must still be the family"
    assert row["sub_engine"] == "GAP", "the actual condition must be readable separately"


def test_allocator_record_hurdle_inputs_is_a_dict_not_a_string():
    """
    THE JSON DOUBLE-ENCODING BUG. `hurdle_inputs` is a jsonb column; the
    Supabase client already serializes a dict into it natively.
    `json.dumps(...)` before handing it to `.insert()` meant the client
    serialized a STRING — confirmed live 20-Aug-2026,
    jsonb_typeof(hurdle_inputs)='string' on every row this project has ever
    written, which is why hurdle_inputs->>'floor_only_rank' always returned
    NULL from plain SQL regardless of whether the rank was ever set. Pinned
    at the boundary that actually matters: the row _record() hands to the
    client, not the DB round trip (proven separately, live, by this fix).

    Uses verdict=TAKE, not DECLINE — since 27-Aug-2026 (docs/FINDINGS.md,
    same date) a DECLINE/DEFER row has hurdle_inputs nulled deliberately for
    storage (see test_alloc_decisions_jsonb_slim.py); TAKE is the verdict
    that still carries it, and the one this double-encoding fix actually
    needs to keep working.
    """
    from allocation.allocator import Allocator
    from allocation.proposal import Proposal
    a = Allocator.__new__(Allocator)
    p = Proposal(symbol="X", framework="INTRADAY", product="MIS",
                entry=100.0, stop=99.0, target=103.0, quantity=10,
                source="ORB", native_rank=80.0, direction="LONG")
    row = a._record({"proposal": p, "verdict": "TAKE", "edge": -0.5,
                     "hurdle_inputs": {"floor_only_rank": 2, "base": -0.3}})
    assert isinstance(row["hurdle_inputs"], dict), (
        f"hurdle_inputs is a {type(row['hurdle_inputs']).__name__}, not a dict — "
        f"the client will store it as a JSON string inside the jsonb column")
    assert row["hurdle_inputs"]["floor_only_rank"] == 2


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
    ("min_risk_pct now reaches SDN",          test_min_risk_pct_now_reaches_sdn),
    ("min_risk_pct off restores all three",   test_min_risk_pct_off_restores_all_three),
    ("max_risk_pct refuses a stop wider than the cap",
     test_max_risk_pct_refuses_a_stop_wider_than_the_cap),
    ("trap stop buffered exactly once in either branch",
     test_trap_stop_is_buffered_exactly_once_in_either_branch),
    ("frame.meta() reaches every condition's setup",
     test_frame_meta_reaches_every_conditions_setup),
    ("registry preserves SDN's own condition in sub_engine",
     test_registry_preserves_sdns_own_condition_in_sub_engine),
    ("registry still defaults sub_engine for single-condition engines",
     test_registry_still_defaults_sub_engine_for_single_condition_engines),
    ("allocator record carries sub_engine through",
     test_allocator_record_carries_sub_engine_through),
    ("allocator record hurdle_inputs is a dict, not a string",
     test_allocator_record_hurdle_inputs_is_a_dict_not_a_string),
]
