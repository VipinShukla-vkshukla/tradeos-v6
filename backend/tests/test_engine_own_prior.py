"""
An engine is priced on its OWN record, not its family's (18-Aug-2026).

WHAT THIS CATCHES
-----------------
`registry.FAMILIES` merges GAP and PDL into ORB, and PBK into VWR. That merge
was a REPORTING decision — its own comment says a family exists "so a family
can be split back apart without a deploy". It became a PRICING decision by
accident, because `Allocator._prior_for()` looked up `p.source`, and
`proposal.from_intraday` sets `source` to the family.

Measured over the 1,766 TAKEN-and-resolved rows in `intraday_setups`,
structural-stop rows only (see base.risk_from_structure), the two halves of
the ORB family:

    GAP  n=144  +0.587R          ORB  n=186  -0.534R

Under `alloc_edge_absolute_floor` that is the difference between clearing zero
and never clearing it — decided by evidence belonging to a different engine.

TWO FAILURE MODES, BOTH LIVE AT ONCE
------------------------------------
1. The lookup ladder had no engine rung at all.
2. `intraday_priors`' select string did not fetch `meta`, so `_engine_of` fell
   back to `strategy` — which since the merge holds the FAMILY. The per-engine
   keys were therefore built entirely from pre-merge July rows while every
   August row filed silently under its family. A key nobody fetched: this
   project's most-repeated defect, one word long, invisible in every log.

So these checks assert through the CONSUMER's own lookup and against the
PRODUCTION select string, never by reading the dict by eye.
"""

from __future__ import annotations

from tests import cfg_ctx
from allocation import scoring as S
from allocation.allocator import Allocator
from allocation.proposal import Proposal


def _rows():
    """Two engines of one family with opposite records, plus a thin third."""
    out = []
    for i in range(40):                      # GAP: winners, family ORB
        out.append({"symbol": f"G{i}", "trade_date": f"2026-08-{i % 28 + 1:02d}",
                    "strategy": "ORB", "meta": {"sub_engine": "GAP"},
                    "direction": "LONG", "cost_verdict": "TAKEN", "cost_pct": 0.0,
                    "entry": 100.0, "stop": 99.0, "outcome_pct": 2.0})
    for i in range(40):                      # ORB: losers, same family
        out.append({"symbol": f"O{i}", "trade_date": f"2026-08-{i % 28 + 1:02d}",
                    "strategy": "ORB", "meta": {"sub_engine": "ORB"},
                    "direction": "LONG", "cost_verdict": "TAKEN", "cost_pct": 0.0,
                    "entry": 100.0, "stop": 99.0, "outcome_pct": -1.0})
    for i in range(3):                       # PDL: too thin to price on itself
        out.append({"symbol": f"P{i}", "trade_date": f"2026-08-{i + 1:02d}",
                    "strategy": "ORB", "meta": {"sub_engine": "PDL"},
                    "direction": "LONG", "cost_verdict": "TAKEN", "cost_pct": 0.0,
                    "entry": 100.0, "stop": 99.0, "outcome_pct": -1.0})
    return out


def _priors():
    with cfg_ctx({"priors_intraday_taken_only": "true",
                  "priors_intraday_dedup": "true"}):
        return S._intraday_priors_from_rows(_rows(), floor=30)


def _prior_for(sub: str, family: str = "ORB", direction: str = "LONG"):
    a = Allocator.__new__(Allocator)
    a._priors = _priors()
    p = Proposal(symbol="X", framework="INTRADAY", product="MIS", source=family,
                 direction=direction, entry=100.0, stop=99.0, target=102.0,
                 quantity=10, native_rank=0.0, meta={"sub_engine": sub})
    return a._prior_for(p)


def test_two_engines_of_one_family_get_different_priors():
    gap, orb = _prior_for("GAP"), _prior_for("ORB")
    assert gap.key == "INTRADAY/GAP", f"GAP must price on itself, got {gap.key}"
    assert orb.key == "INTRADAY/ORB", f"ORB must price on itself, got {orb.key}"
    assert gap.mean_r > orb.mean_r, (
        f"the winning engine must not inherit the losing one's mean "
        f"({gap.mean_r:+.3f} vs {orb.mean_r:+.3f})")


def test_a_thin_engine_falls_back_to_its_family_not_to_nothing():
    pdl = _prior_for("PDL")
    assert pdl.key == "INTRADAY/ORB", (
        f"3 observations is under the floor — PDL must inherit its family, "
        f"got {pdl.key}")


def test_the_family_key_still_exists_and_pools_both_halves():
    fam = _priors().get("INTRADAY/ORB")
    assert fam is not None and fam.n >= 80, (
        "the family prior must still be built from every member — it is the "
        f"fallback rung; got {fam.n if fam else None}")


def test_the_engine_is_read_from_meta_not_from_the_strategy_column():
    """
    Every row in the fixture has strategy='ORB'. If `_engine_of` read that
    column the GAP key could not exist at all — which is what shipped.
    """
    assert S._engine_of({"strategy": "ORB", "meta": {"sub_engine": "GAP"}}) == "GAP"
    assert S._engine_of({"strategy": "GAP", "meta": {}}) == "GAP", \
        "pre-merge rows carry the engine in `strategy` and must still resolve"
    assert S._engine_of({"strategy": "ORB", "meta": '{"sub_engine": "PDL"}'}) == "PDL", \
        "meta arrives as a JSON string from some clients"


def test_the_production_select_string_fetches_what_the_keying_reads():
    """
    The defect that made all of the above inert. Pinned against the source of
    `intraday_priors` itself so the two cannot drift apart.
    """
    import inspect
    src = inspect.getsource(S.intraday_priors)
    assert '"symbol,trade_date,strategy' in src, "select string moved — retarget this check"
    assert ",meta" in src, (
        "`_engine_of` reads meta.sub_engine; the production select must fetch "
        "`meta` or every post-merge row keys as its family and no per-engine "
        "prior is ever built from current data")


TESTS = [
    ("two engines of one family get different priors",
     test_two_engines_of_one_family_get_different_priors),
    ("a thin engine falls back to its family, not to nothing",
     test_a_thin_engine_falls_back_to_its_family_not_to_nothing),
    ("the family key still exists and pools both halves",
     test_the_family_key_still_exists_and_pools_both_halves),
    ("the engine is read from meta, not the strategy column",
     test_the_engine_is_read_from_meta_not_from_the_strategy_column),
    ("the production select fetches what the keying reads",
     test_the_production_select_string_fetches_what_the_keying_reads),
]
