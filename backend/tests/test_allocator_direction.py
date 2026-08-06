"""
Setup -> Proposal -> Allocator, end to end, for both directions.

WHAT THIS CATCHES
-----------------
`direction` defaults to LONG at every signature, so a caller that forgets to
pass it does not crash — it silently prices a short as a long. Four separate
sites did exactly that, each found after the one before it was fixed:

  · open_positions had no `direction` column and nothing wrote one
  · evaluate_intraday_setups' own cost-gate call omitted the argument
  · Proposal had no `direction` field at all, so `coherent` used the long-only
    0<stop<entry<target shape and rejected every short before scoring ran
  · tools/simulate.py had the same two gaps

A marker check that greps a function's DEFINITION cannot see any of this — the
definitions were all correct. Only exercising the chain finds it, which is what
this module does.
"""

from __future__ import annotations

from tests import cfg_ctx


def _setups():
    from intraday.strategies.base import Setup
    long_s = Setup("RELIANCE", "ORB", "LONG", 2500.0, 2485.0, 2540.0,
                   0.75, "r", "i", meta={"family": "ORB"})
    short_s = Setup("ZOMATO", "SDN", "SHORT", 250.0, 253.0, 244.0,
                    0.70, "r", "i", meta={"family": "SDN"})
    return long_s, short_s


def test_from_intraday_carries_direction():
    from allocation.proposal import from_intraday
    with cfg_ctx():
        long_s, short_s = _setups()
        pl, ps = from_intraday(long_s, 8), from_intraday(short_s, 40)
        assert pl is not None, "a coherent LONG setup produced no Proposal"
        assert ps is not None, (
            "a coherent SHORT setup produced NO Proposal — from_intraday is "
            "dropping it, most likely because Proposal.coherent still uses the "
            "long-only 0<stop<entry<target shape")
        assert pl.direction == "LONG" and ps.direction == "SHORT"


def test_proposal_coherence_and_risk_are_direction_aware():
    from allocation.proposal import from_intraday
    with cfg_ctx():
        _, short_s = _setups()
        ps = from_intraday(short_s, 40)
        assert ps.coherent, "a short's stop ABOVE entry was called incoherent"
        assert ps.risk_per_share == 3.0, (
            f"short risk/share is {ps.risk_per_share}, expected 3.0 — "
            f"max(entry-stop, 0) returns 0 for a short")


def test_from_swing_is_always_long():
    """Swing is CNC by construction and this system does not short delivery."""
    from allocation.proposal import from_swing
    with cfg_ctx():
        class D:
            symbol = "TCS"; action = "BUY_NOW"; entry = 3800.0; stop = 3750.0
            target = 3950.0; qty = 5; rr_live = 3.0
            headline = "x"; stale_price = False
        p = from_swing(D())
        assert p is not None and p.direction == "LONG"


def test_allocator_scores_both_directions():
    """
    A short must come back with a real `edge`. edge=None is rendered by
    policies.intraday_stopping as "not scoreable — levels incoherent", which is
    a DECLINE that reads like a data problem rather than a policy.
    """
    from allocation.proposal import from_intraday
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    class NoDB:
        def table(self, *a, **k):
            raise RuntimeError("this test must not touch the database")

    with cfg_ctx({"alloc_hurdle_min_sample": "5"}):
        long_s, short_s = _setups()
        pl, ps = from_intraday(long_s, 8), from_intraday(short_s, 40)

        alloc = Allocator(sb=NoDB())
        # Seeded so the loop never queries. The short prior is deliberately
        # WORSE than the long one, so a short falling back to the long book's
        # distribution would be visible in the edge.
        alloc._priors = {
            "INTRADAY/ORB":       Prior("INTRADAY/ORB", 100, 0.35, 0.20, 0.05, -1.0, 2.0),
            "INTRADAY/ALL":       Prior("INTRADAY/ALL", 200, 0.20, 0.10, 0.04, -1.0, 1.8),
            "INTRADAY/SDN/SHORT": Prior("INTRADAY/SDN/SHORT", 60, 0.05, 0.02, 0.06, -1.5, 1.2),
            "INTRADAY/ALL/SHORT": Prior("INTRADAY/ALL/SHORT", 90, 0.02, 0.01, 0.05, -1.5, 1.0),
        }
        alloc._hold_days = {"INTRADAY": (1.0, 50), "SWING": (5.0, 30)}

        verdicts = alloc.select([pl, ps], regime="RISK_ON",
                                slots_left=4, minutes_left=200)
        by_symbol = {v["proposal"].symbol: v for v in verdicts}
        assert by_symbol["RELIANCE"]["edge"] is not None, "the long is not scoreable"
        assert by_symbol["ZOMATO"]["edge"] is not None, (
            "the SHORT returned edge=None — it is being scored as a LONG and "
            "declined as incoherent before its prior or cost was ever weighed")


def test_a_short_uses_its_own_prior_not_the_long_books():
    """
    scoring.intraday_priors keys a short engine as ".../SHORT" — a genuinely
    different population (different base rate, different failure mode, a
    different tail). Falling back to INTRADAY/ALL would price a short against a
    distribution that excludes every short ever observed.
    """
    from allocation.proposal import from_intraday
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    class NoDB:
        def table(self, *a, **k):
            raise RuntimeError("this test must not touch the database")

    with cfg_ctx():
        _, short_s = _setups()
        ps = from_intraday(short_s, 40)
        alloc = Allocator(sb=NoDB())
        alloc._priors = {
            "INTRADAY/ALL":       Prior("INTRADAY/ALL", 200, 0.20, 0.10, 0.04, -1.0, 1.8),
            "INTRADAY/SDN/SHORT": Prior("INTRADAY/SDN/SHORT", 60, 0.05, 0.02, 0.06, -1.5, 1.2),
        }
        prior = alloc._prior_for(ps)
        assert prior.key == "INTRADAY/SDN/SHORT", (
            f"the short matched prior {prior.key!r} — it must never borrow the "
            f"long book's distribution")


def test_the_allocator_cannot_import_an_order_path():
    """
    The structural prohibition that makes shadow mode safe on a live book: a
    component that cannot import `execution` cannot place an order however wrong
    its arithmetic is. Asserted by inspection, the same way health does it.
    """
    import re
    from pathlib import Path
    from tests import BACKEND
    banned = ("execution", "order_manager", "paper_broker", "gtt_manager", "kite")
    offenders = []
    for f in sorted((BACKEND / "allocation").glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.match(r"\s*(import|from)\s", code) and any(b in code for b in banned):
                offenders.append(f"{f.name}:{n}")
    assert not offenders, (
        f"allocation/ imports an order path at {', '.join(offenders)} — shadow "
        f"mode is no longer safe to run against a live book")


TESTS = [
    ("from_intraday carries direction",         test_from_intraday_carries_direction),
    ("Proposal coherence/risk per direction",   test_proposal_coherence_and_risk_are_direction_aware),
    ("from_swing is always LONG",               test_from_swing_is_always_long),
    ("allocator scores both directions",        test_allocator_scores_both_directions),
    ("a short uses its own prior",              test_a_short_uses_its_own_prior_not_the_long_books),
    ("allocation cannot import execution",      test_the_allocator_cannot_import_an_order_path),
]
