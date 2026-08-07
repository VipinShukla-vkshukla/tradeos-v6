"""
`swing_assignment()` and its supporting glue (`_swing_positions`,
`Allocator.score_hypothetical`), wired live 07-Aug-2026 after sitting fully
written and never engaged — `select()` never received a `field` kwarg, so
every swing decline used `intraday_stopping`'s stopping-rule wording and the
reservation logic that lets swing hold a slot for a materially better
untriggered plan had never run once.

This is now part of the LIVE swing veto path (`alloc_live_swing=true` per
this account's own dashboard, and `allocator_permits()` treats DEFER exactly
like DECLINE — see engine.py:2724). Nothing in `tools/verify.py` exercised
this before today's merge: the 67 checks it reports are real, but none of
them touch `swing_assignment`, `_swing_positions`, or `score_hypothetical`,
so "67/67" was never evidence about this specific change. This file is that
evidence, added after the fact rather than not at all.
"""

from __future__ import annotations

from tests import cfg_ctx


def _scored(symbol: str, edge: float):
    """A minimal 'scored' dict — the shape swing_assignment/intraday_stopping
    both consume: {"proposal": ..., "symbol": ..., "edge": ..., ...}."""
    return {"proposal": object(), "symbol": symbol, "edge": edge}


# ── policies.swing_assignment ────────────────────────────────────────────

def test_no_field_degrades_to_plain_stopping_rule():
    from allocation import policies as P
    scored = [_scored("A", 0.05), _scored("B", 0.03)]
    with_field = P.swing_assignment(scored, bar=0.04, slots_left=1, field=None)
    stopping = P.intraday_stopping(scored, bar=0.04, slots_left=1)
    assert [x["verdict"] for x in with_field] == [x["verdict"] for x in stopping], (
        "an empty field must behave exactly like intraday_stopping, not "
        "silently reserve against nothing")


def test_reservation_defers_a_weaker_triggered_candidate():
    from allocation import policies as P
    # Two slots. The field reserves one (edge 0.09 clears reserve_mult*bar
    # and p_trigger clears the floor), leaving one spendable. FIRST fills
    # the spendable slot; WEAK clears the bar on its own merits but does not
    # beat the reservation's edge, so it must wait rather than take the
    # slot the book is holding open.
    scored = [_scored("FIRST", 0.06), _scored("WEAK", 0.045)]
    field = [{"symbol": "STRONG", "triggered": False, "edge": 0.09, "p_trigger": 0.8}]
    out = {x["symbol"]: x["verdict"] for x in
           P.swing_assignment(scored, bar=0.04, slots_left=2, field=field)}
    assert out["FIRST"] == "TAKE", f"expected FIRST to take the spendable slot, got {out['FIRST']!r}"
    assert out["WEAK"] == "DEFER", (
        f"expected WEAK (clears the bar but not the reservation) to be "
        f"deferred once the spendable slot is spent, got {out['WEAK']!r} — "
        f"the reservation mechanism is not actually holding the slot")


def test_a_triggered_candidate_that_beats_the_reservation_still_takes():
    """The reservation is a preference, not a lock — swing_assignment's own docstring."""
    from allocation import policies as P
    # Two slots, one reserved (edge 0.09), one spendable. FIRST fills the
    # spendable slot. STRONGER arrives after spendable is exhausted but its
    # own edge (0.10) beats the reservation's (0.09), so it must take the
    # slot anyway rather than be deferred behind a reservation it outbids.
    scored = [_scored("FIRST", 0.15), _scored("STRONGER", 0.10)]
    field = [{"symbol": "RESERVED", "triggered": False, "edge": 0.09, "p_trigger": 0.8}]
    out = {x["symbol"]: x["verdict"] for x in
           P.swing_assignment(scored, bar=0.04, slots_left=2, field=field)}
    assert out["STRONGER"] == "TAKE", (
        f"a triggered candidate whose edge beats every reservation must "
        f"still take the slot even once spendable is exhausted, got "
        f"{out['STRONGER']!r}")


def test_reservation_never_consumes_every_slot():
    """slots_left=1 must never reserve — there is nothing left to spend if it did."""
    from allocation import policies as P
    scored = [_scored("ONLY", 0.05)]
    field = [{"symbol": "PHANTOM", "triggered": False, "edge": 10.0, "p_trigger": 0.99}]
    out = P.swing_assignment(scored, bar=0.04, slots_left=1, field=field)
    assert out[0]["verdict"] == "TAKE", (
        f"with only 1 slot total, a clearing candidate must never be "
        f"deferred — got {out[0]['verdict']!r}. A reservation that spends "
        f"the book's only slot is a failure mode of its own (no trade this "
        f"cycle produces no evidence to learn from)")


def test_low_probability_untriggered_plan_is_not_reserved():
    """p_trigger below the floor must not hold a slot — an unlikely-to-fire plan is not worth it."""
    from allocation import policies as P
    scored = [_scored("NORMAL", 0.05)]
    field = [{"symbol": "LONGSHOT", "triggered": False, "edge": 0.20, "p_trigger": 0.05}]
    with cfg_ctx({"alloc_reserve_min_p_trigger": "0.35"}):
        out = P.swing_assignment(scored, bar=0.04, slots_left=1, field=field)
    assert out[0]["verdict"] == "TAKE", (
        f"a reservation candidate with p_trigger below the configured floor "
        f"must not hold the slot, got {out[0]['verdict']!r}")


# ── IntradayEngine._swing_positions ──────────────────────────────────────

class _NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


def _engine():
    from intraday.engine import IntradayEngine
    return IntradayEngine(sb=_NoDB())


def test_swing_positions_excludes_intraday_holdings():
    """
    check_new_entry()'s position-count/sector/industry/risk caps are scoped
    to swing by the function's own docstring ("every caller today is the
    swing book"). Before this fix every call site passed self.positions
    unfiltered, so an intraday PAPER position consumed a swing slot — 5 real
    swing holdings + 2 intraday paper ones read as "7/7 slots used" and
    refused every swing candidate regardless of how many swing slots were
    actually free.
    """
    with cfg_ctx():
        eng = _engine()
        eng.positions = [
            {"symbol": "TCS", "framework": "SWING"},
            {"symbol": "INFY", "framework": "SWING"},
            {"symbol": "WIPRO", "framework": "INTRADAY"},
            {"symbol": "SBIN", "framework": "INTRADAY"},
        ]
        swing_only = eng._swing_positions()
        symbols = {p["symbol"] for p in swing_only}
        assert symbols == {"TCS", "INFY"}, (
            f"expected only the two SWING positions, got {symbols} — an "
            f"intraday holding is still leaking into swing's own caps")


def test_swing_positions_treats_missing_framework_as_swing():
    """Matches _other_framework_holding's own default: unlabelled reads as SWING."""
    with cfg_ctx():
        eng = _engine()
        eng.positions = [{"symbol": "RELIANCE"}]   # no "framework" key at all
        swing_only = eng._swing_positions()
        assert len(swing_only) == 1, (
            "a position with no framework label must default to SWING, "
            "not be silently dropped from swing's own caps")


# ── Allocator.score_hypothetical ─────────────────────────────────────────

def test_score_hypothetical_matches_a_live_scored_proposal():
    """
    The reservation field must be comparable to `bar` in the same units a
    live TAKE/DECLINE uses — same prior lookup, same cost model. If this
    drifts, reservations would be compared against the bar in the wrong
    scale and either never fire or always fire.
    """
    from allocation.allocator import Allocator
    from allocation.scoring import Prior, score
    from allocation.proposal import Proposal

    with cfg_ctx():
        alloc = Allocator(sb=_NoDB())
        alloc._priors = {
            "SWING/CTL": Prior("SWING/CTL", 340, 0.212, 0.18, 0.03, -0.5, 1.1,
                               trigger_rate=0.4),
        }
        hyp_edge = alloc.score_hypothetical(
            "RELIANCE", 2500.0, 2450.0, 2650.0, 8, "CNC", "CTL",
            native_rank=80.0, direction="LONG")

        live_proposal = Proposal(symbol="RELIANCE", framework="SWING", product="CNC",
                                 entry=2500.0, stop=2450.0, target=2650.0, quantity=8,
                                 source="CTL", native_rank=80.0, direction="LONG")
        pri = alloc._prior_for(live_proposal)
        days, _ = alloc._hold_days.get("SWING", (1.0, 0))
        live_edge = score(2500.0, 2450.0, 2650.0, 8, "CNC", pri, days,
                          direction="LONG").get("edge")

        assert hyp_edge == live_edge, (
            f"score_hypothetical returned {hyp_edge}, a live-path score() call "
            f"with the same inputs returned {live_edge} — the reservation "
            f"field is not in the same units as the bar it's compared against")


TESTS = [
    ("no field degrades to plain stopping rule",
     test_no_field_degrades_to_plain_stopping_rule),
    ("reservation defers a weaker triggered candidate",
     test_reservation_defers_a_weaker_triggered_candidate),
    ("a triggered candidate that beats the reservation still takes",
     test_a_triggered_candidate_that_beats_the_reservation_still_takes),
    ("reservation never consumes every slot",
     test_reservation_never_consumes_every_slot),
    ("low probability untriggered plan is not reserved",
     test_low_probability_untriggered_plan_is_not_reserved),
    ("_swing_positions excludes intraday holdings",
     test_swing_positions_excludes_intraday_holdings),
    ("_swing_positions treats missing framework as swing",
     test_swing_positions_treats_missing_framework_as_swing),
    ("score_hypothetical matches a live scored proposal",
     test_score_hypothetical_matches_a_live_scored_proposal),
]
