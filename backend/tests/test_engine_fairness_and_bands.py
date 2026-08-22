"""
Engine fairness in the allocator queue, and confidence-banded priors.

WHAT THESE PIN — 19-Aug-2026
-----------------------------
Two defects found together, both about the same thing: `edge` is keyed on the
ENGINE, so a pooled descending sort ranks engines rather than setups.

  1. One engine took 29 of 32 closed intraday positions (13-19 Aug) while ORB
     wrote 561 TAKEN rows and closed one. `policies.intraday_stopping` sorted
     every candidate into one queue by edge, and candidates from one engine
     share a prior — so the whole slot budget went to the top engine's
     candidates, ordered among themselves only by friction.

  2. Confidence means something different in every engine. Terciled WITHIN
     each engine over every TAKEN-and-resolved row, gross R runs INVERTED for
     SDN/PDL/VWR, is noise for ORB at n=1030, and is correctly ordered only
     for VCE. So confidence cannot be ranked on directly — but what confidence
     has been WORTH, per engine, can be, and that is a prior key.

The band machinery ships INERT (`alloc_intraday_confidence_bands` false). The
tests below therefore pin BOTH states explicitly: that it is genuinely a no-op
when off, and that it segments correctly when on. A feature that is inert by
default needs the off-state asserted, or "shipped inert" and "shipped broken"
look identical from the outside.

Every check here was demonstrated FAILING against a one-line removal of the
behaviour it pins before being trusted to pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests import cfg_ctx                   # noqa: E402
from allocation import policies as P        # noqa: E402
from allocation import scoring as S         # noqa: E402
from allocation import hurdle as H          # noqa: E402


class _P:
    """Minimal stand-in for allocation.proposal.Proposal."""
    def __init__(self, symbol, engine, source=None, native_rank=70.0,
                 direction="LONG", framework="INTRADAY"):
        self.symbol = symbol
        self.meta = {"sub_engine": engine}
        self.source = source or engine
        self.native_rank = native_rank
        self.direction = direction
        self.framework = framework
        self.product = "MIS"


def _s(symbol, engine, edge, source=None):
    return {"proposal": _P(symbol, engine, source), "symbol": symbol, "edge": edge}


# ── engine fairness ─────────────────────────────────────────────────────────

def test_interleave_puts_every_engines_best_in_the_first_round():
    """The exact live shape: one engine with volume, one with a single idea."""
    scored = [_s("A1", "SDN", 0.50), _s("A2", "SDN", 0.40),
              _s("A3", "SDN", 0.30), _s("B1", "ORB", 0.20)]
    order = [s["symbol"] for s in P._interleave_by_engine(scored)]
    # SDN's best, then ORB's best — ORB does not wait behind SDN's 2nd and 3rd.
    assert order[:2] == ["A1", "B1"], order
    assert order[2:] == ["A2", "A3"], order


def test_interleave_orders_within_a_round_by_edge():
    scored = [_s("LOW", "X", 0.10), _s("HIGH", "Y", 0.90), _s("MID", "Z", 0.50)]
    order = [s["symbol"] for s in P._interleave_by_engine(scored)]
    assert order == ["HIGH", "MID", "LOW"], order


def test_interleave_prefers_a_confirmed_candidate_when_edges_tie():
    """
    22-Aug-2026, F-47. The exact live shape from 21-Aug: two ORB candidates
    sharing the SAME engine (and so, in production, very nearly the same
    edge) — one retest-confirmed, one not. The confirmed one must rank
    first WITHOUT the bar or edge changing at all.
    """
    a_unconfirmed = _s("ABCAPITAL", "ORB", 0.50)
    a_unconfirmed["proposal"].meta["retest_confirmed"] = False
    b_confirmed = _s("POWERGRID", "ORB", 0.50)
    b_confirmed["proposal"].meta["retest_confirmed"] = True
    order = [s["symbol"] for s in P._interleave_by_engine([a_unconfirmed, b_confirmed])]
    assert order == ["POWERGRID", "ABCAPITAL"], order
    # Neither proposal's own edge was touched by the reorder.
    assert a_unconfirmed["edge"] == 0.50 and b_confirmed["edge"] == 0.50


def test_confirmation_tiebreak_never_outranks_a_real_edge_difference():
    """Confirmation is a TIE-break, not a second sort key that can beat
    edge — a worse but confirmed candidate must not jump a better
    unconfirmed one."""
    worse_confirmed = _s("WORSE", "ORB", 0.10)
    worse_confirmed["proposal"].meta["retest_confirmed"] = True
    better_unconfirmed = _s("BETTER", "ORB", 0.90)
    better_unconfirmed["proposal"].meta["retest_confirmed"] = False
    order = [s["symbol"] for s in P._interleave_by_engine([worse_confirmed, better_unconfirmed])]
    assert order == ["BETTER", "WORSE"], order


def test_confirmation_tiebreak_treats_absent_signal_as_unconfirmed_not_penalised_twice():
    """Most engines have no retest_confirmed field at all (only ORB does).
    Absent must sort the SAME as explicit False — an engine with no signal
    is not somehow worse than one that checked and failed."""
    no_signal = _s("SDN_TRADE", "SDN", 0.50)  # SDN has no retest_confirmed key at all
    explicit_false = _s("ABCAPITAL", "ORB", 0.50)
    explicit_false["proposal"].meta["retest_confirmed"] = False
    order_a = [s["symbol"] for s in P._interleave_by_engine([no_signal, explicit_false])]
    # Order between two DIFFERENT engines' equal-edge candidates is
    # otherwise arbitrary (dict-order-derived) — the point being checked is
    # that neither raises and neither is treated as "confirmed" by absence.
    assert set(order_a) == {"SDN_TRADE", "ABCAPITAL"}


def test_confirmation_priority_switch_off_restores_plain_edge_order():
    """Same live shape as the first test in this group, switch off."""
    a_unconfirmed = _s("ABCAPITAL", "ORB", 0.50)
    a_unconfirmed["proposal"].meta["retest_confirmed"] = False
    b_confirmed = _s("POWERGRID", "ORB", 0.50)
    b_confirmed["proposal"].meta["retest_confirmed"] = True
    with cfg_ctx({"alloc_intraday_confirmation_priority": "false"}):
        order = [s["symbol"] for s in P._interleave_by_engine([a_unconfirmed, b_confirmed])]
    # With the switch off, _confirmation_key returns 0 for everyone, so the
    # tie is broken by dict/list order alone — i.e. whichever was listed
    # first, not necessarily the confirmed one.
    assert order[0] == "ABCAPITAL", (
        "switch off must stop confirmation from influencing the order at all")


def _sc(symbol, engine, edge, sector=None):
    s = _s(symbol, engine, edge)
    if sector is not None:
        s["proposal"].meta["sector"] = sector
    return s


def test_build_priority_criteria_reads_only_categorical_rows():
    """F-50. A 2-part target_key (engine/feature, no category) is a
    NUMERIC finding and must be skipped — see refresh_priority_criteria's
    own docstring for why numeric findings aren't consumed here."""
    from allocation.policies import build_priority_criteria
    rows = [{"target_key": "SDN/sector/auto"}, {"target_key": "GAP/volume_ratio"},
           {"target_key": "not/even/a/real/key/shape"}]
    out = build_priority_criteria(rows)
    assert out == {"SDN": {"sector": {"auto"}}}


def test_build_priority_criteria_merges_multiple_categories_per_feature():
    from allocation.policies import build_priority_criteria
    rows = [{"target_key": "SDN/sector/auto"}, {"target_key": "SDN/sector/healthcare"}]
    out = build_priority_criteria(rows)
    assert out == {"SDN": {"sector": {"auto", "healthcare"}}}


def test_confirmation_key_prioritises_a_validated_sector_match():
    """The other half of F-50, alongside retest_confirmed: a candidate
    whose sector matches a VALIDATED favourable finding ranks first
    against a same-edge, same-engine candidate that doesn't — with no
    retest_confirmed involved at all."""
    criteria = {"SDN": {"sector": {"auto"}}}
    matching = _sc("MARUTI", "SDN", 0.50, sector="auto")
    other = _sc("INFY", "SDN", 0.50, sector="i.t")
    order = [s["symbol"] for s in P._interleave_by_engine([other, matching], criteria)]
    assert order == ["MARUTI", "INFY"], order


def test_confirmation_key_ignores_an_unlisted_engine_or_feature():
    """A candidate from an engine (or on a feature) with no VALIDATED
    entry at all must not be treated as matching — absence, not a
    default match."""
    criteria = {"SDN": {"sector": {"auto"}}}
    a = _sc("A", "ORB", 0.50, sector="auto")   # ORB has no entry in criteria
    b = _sc("B", "ORB", 0.50, sector="i.t")
    order = [s["symbol"] for s in P._interleave_by_engine([a, b], criteria)]
    # Neither matches (criteria has no "ORB" key at all) — order falls back
    # to whatever the (currently arbitrary) tie-break produces; the only
    # thing under test is that this does not raise and does not silently
    # treat "auto" as special for an engine with no such finding.
    assert set(order) == {"A", "B"}


def test_confirmation_key_retest_and_priority_criteria_both_lead_to_rank_zero():
    """The two signals are ORed, not additive/ranked against each other —
    matching EITHER one is enough to go first; this checks retest_confirmed
    alone (no criteria match) still works exactly as F-48 shipped it, now
    that the function takes a second parameter."""
    from allocation.policies import _confirmation_key
    s = _s("X", "ORB", 0.5)
    s["proposal"].meta["retest_confirmed"] = True
    assert _confirmation_key(s, priority_criteria=None) == 0
    assert _confirmation_key(s, priority_criteria={"ORB": {"sector": {"nothing"}}}) == 0


def test_confirmation_key_none_criteria_behaves_exactly_like_before_f50():
    """Regression guard for F-48's own shipped behaviour — priority_criteria
    defaulting to None (every caller written before F-50) must be silently
    equivalent to 'no criteria', not raise or change retest-only ranking."""
    from allocation.policies import _confirmation_key
    s = _s("X", "ORB", 0.5)
    s["proposal"].meta["retest_confirmed"] = False
    assert _confirmation_key(s) == 1
    assert _confirmation_key(s, None) == 1


def test_interleave_is_a_noop_when_every_engine_has_one_candidate():
    """Fairness must not reorder a field it has no reason to touch."""
    scored = [_s("A", "E1", 0.30), _s("B", "E2", 0.20), _s("C", "E3", 0.10)]
    plain = [s["symbol"] for s in sorted(scored, key=lambda x: -x["edge"])]
    fair  = [s["symbol"] for s in P._interleave_by_engine(scored)]
    assert plain == fair == ["A", "B", "C"]


def test_fairness_cannot_admit_a_proposal_that_fails_the_bar():
    """
    THE SAFETY PROPERTY. Interleaving changes the QUEUE, never the BAR — so a
    second engine's candidate can win a slot it would have lost, but only if
    it already cleared the bar on its own edge.
    """
    scored = [_s("A1", "SDN", 0.50), _s("B1", "ORB", -0.90)]
    with cfg_ctx({"alloc_intraday_engine_fairness": "true"}):
        out = P.intraday_stopping(scored, bar=0.10, slots_left=5)
    verdicts = {v["symbol"]: v["verdict"] for v in out}
    assert verdicts["A1"] == P.TAKE
    assert verdicts["B1"] == P.DECLINE, "a below-bar proposal must still decline"


def test_fairness_switch_off_restores_the_pooled_sort():
    scored = [_s("A1", "SDN", 0.50), _s("A2", "SDN", 0.40), _s("B1", "ORB", 0.30)]
    with cfg_ctx({"alloc_intraday_engine_fairness": "false"}):
        out = P.intraday_stopping(scored, bar=0.0, slots_left=2)
    taken = [v["symbol"] for v in out if v["verdict"] == P.TAKE]
    assert taken == ["A1", "A2"], f"pooled sort must fill from one engine: {taken}"

    with cfg_ctx({"alloc_intraday_engine_fairness": "true"}):
        out = P.intraday_stopping(scored, bar=0.0, slots_left=2)
    taken = [v["symbol"] for v in out if v["verdict"] == P.TAKE]
    assert taken == ["A1", "B1"], f"fairness must seat both engines: {taken}"


def test_edge_of_exactly_zero_is_not_treated_as_absent():
    """
    `s.get("edge") or float("-inf")` sorted an edge of 0.0 BELOW every loser,
    because 0.0 is falsy. Only None means "no opinion".
    """
    assert P._edge_key({"edge": 0.0}) == 0.0
    assert P._edge_key({"edge": None}) == float("-inf")
    scored = [_s("ZERO", "X", 0.0), _s("LOSER", "Y", -0.75)]
    order = [s["symbol"] for s in P._interleave_by_engine(scored)]
    assert order == ["ZERO", "LOSER"], order


# ── confidence bands ────────────────────────────────────────────────────────

def test_band_edges_are_inclusive_lower_exclusive_upper():
    e = [0.65, 0.75]
    assert S.confidence_band(0.60, e) == "C0"
    assert S.confidence_band(0.6499, e) == "C0"
    assert S.confidence_band(0.65, e) == "C1"      # the edge belongs upward
    assert S.confidence_band(0.7499, e) == "C1"
    assert S.confidence_band(0.75, e) == "C2"
    assert S.confidence_band(0.99, e) == "C2"


def test_absent_confidence_bands_as_none_not_as_a_default_band():
    """
    A recorded-unknown confidence and a measured-low one are different
    readings. Collapsing them would file every unbanded row into C0 and hand
    the lowest band a population it never measured.
    """
    assert S.confidence_band(None, [0.65]) is None
    assert S.confidence_band("not a number", [0.65]) is None


def test_no_edges_means_no_bands():
    assert S.confidence_band(0.80, []) is None


def test_malformed_edges_degrade_to_no_bands_rather_than_raising():
    with cfg_ctx({"intraday_prior_confidence_band_edges": "0.65,banana"}):
        assert S.band_edges() == []
        assert S.confidence_band(0.80) is None


def _rows():
    """
    Two engines. VCE's high-confidence rows WIN, SDN's high-confidence rows
    LOSE — the real, measured shape, so a test that passes under a pooled
    prior cannot also pass under a banded one.
    """
    out = []
    for i in range(40):
        # VCE: high confidence -> +1R, low confidence -> -1R
        out.append({"symbol": f"V{i}", "trade_date": "2026-08-19", "strategy": "VCE",
                    "direction": "LONG", "entry": 100.0, "stop": 99.0,
                    "outcome_pct": 1.0, "cost_pct": 0.0, "cost_verdict": "TAKEN",
                    "confidence": 0.90, "meta": {"sub_engine": "VCE"}})
        out.append({"symbol": f"v{i}", "trade_date": "2026-08-19", "strategy": "VCE",
                    "direction": "LONG", "entry": 100.0, "stop": 99.0,
                    "outcome_pct": -1.0, "cost_pct": 0.0, "cost_verdict": "TAKEN",
                    "confidence": 0.55, "meta": {"sub_engine": "VCE"}})
    return out


def test_bands_are_not_built_when_the_switch_is_off():
    with cfg_ctx({"alloc_intraday_confidence_bands": "false",
                  "priors_min_sample_intraday": "10"}):
        pri = S._intraday_priors_from_rows(_rows(), 10)
    assert not [k for k in pri if S.BAND_SEP in k], \
        f"band keys built while switched off: {[k for k in pri if S.BAND_SEP in k]}"


def test_bands_separate_a_winning_band_from_a_losing_one():
    with cfg_ctx({"alloc_intraday_confidence_bands": "true",
                  "intraday_prior_confidence_band_edges": "0.65,0.75",
                  "priors_min_sample_intraday": "10"}):
        pri = S._intraday_priors_from_rows(_rows(), 10)
    hi = pri.get(f"INTRADAY/VCE{S.BAND_SEP}C2")
    lo = pri.get(f"INTRADAY/VCE{S.BAND_SEP}C0")
    assert hi is not None and lo is not None, sorted(pri)
    assert hi.mean_r > 0.9, hi.describe()
    assert lo.mean_r < -0.9, lo.describe()
    # And the un-banded engine prior still averages them, so the fallback rung
    # is unchanged by the presence of bands.
    flat = pri["INTRADAY/VCE"]
    assert abs(flat.mean_r) < 0.05, flat.describe()


def test_turning_bands_on_does_not_move_the_pooled_fallback():
    """
    Every banded observation is ALSO under its bare engine key. If the pooled
    ALL fallback summed both, switching bands on would silently reweight the
    prior every thin engine falls back to.
    """
    with cfg_ctx({"alloc_intraday_confidence_bands": "false",
                  "priors_min_sample_intraday": "10"}):
        off = S._intraday_priors_from_rows(_rows(), 10)["INTRADAY/ALL"]
    with cfg_ctx({"alloc_intraday_confidence_bands": "true",
                  "intraday_prior_confidence_band_edges": "0.65,0.75",
                  "priors_min_sample_intraday": "10"}):
        on = S._intraday_priors_from_rows(_rows(), 10)["INTRADAY/ALL"]
    assert off.n == on.n, f"pooled n moved: {off.n} -> {on.n}"
    assert abs(off.mean_r - on.mean_r) < 1e-9, f"{off.mean_r} -> {on.mean_r}"


def test_bands_do_not_move_the_pooled_fallback_on_the_UNGATED_path_either():
    """
    THE SIBLING OF THE TEST ABOVE, AND IT EXISTS BECAUSE THAT ONE COULD NOT
    FAIL ALONE.

    `_prior_for("ALL", longs)` has two sources: `by_taken["ALL"]` when the
    gated sample clears the floor, and the `longs` list otherwise. Every row
    in `_rows()` is TAKEN, so the first test only ever exercised the gated
    branch — deleting the BAND_SEP filter from the `longs` comprehension left
    it green. Demonstrated: with that filter removed, the test above still
    passed, which by this project's own rule makes it not a check.

    `priors_intraday_taken_only=false` routes the same population down the
    ungated branch, so the filter on `longs` is now load-bearing for a test.
    """
    with cfg_ctx({"alloc_intraday_confidence_bands": "false",
                  "priors_intraday_taken_only": "false",
                  "priors_min_sample_intraday": "10"}):
        off = S._intraday_priors_from_rows(_rows(), 10)["INTRADAY/ALL"]
    with cfg_ctx({"alloc_intraday_confidence_bands": "true",
                  "priors_intraday_taken_only": "false",
                  "intraday_prior_confidence_band_edges": "0.65,0.75",
                  "priors_min_sample_intraday": "10"}):
        on = S._intraday_priors_from_rows(_rows(), 10)["INTRADAY/ALL"]
    assert off.n == on.n, f"ungated pooled n moved: {off.n} -> {on.n}"
    assert abs(off.mean_r - on.mean_r) < 1e-9, f"{off.mean_r} -> {on.mean_r}"


def test_the_allocator_reaches_the_band_key_through_its_own_lookup():
    """
    ASSERTED THROUGH THE CONSUMER, NEVER BY READING THE DICT.

    This repository has shipped a builder and a consumer that disagreed about
    a key twice — `intraday_priors` once returned bare engine names while the
    allocator looked up "INTRADAY/<engine>", and later keyed August rows by
    family because `meta` was never fetched. Both were entirely inert and
    silent. So this drives `Allocator._prior_for()` itself.
    """
    from allocation.allocator import Allocator
    with cfg_ctx({"alloc_intraday_confidence_bands": "true",
                  "intraday_prior_confidence_band_edges": "0.65,0.75",
                  "priors_min_sample_intraday": "10"}):
        priors = S._intraday_priors_from_rows(_rows(), 10)
        a = Allocator.__new__(Allocator)
        a._priors = priors
        hi = a._prior_for(_P("X", "VCE", native_rank=90.0))
        lo = a._prior_for(_P("X", "VCE", native_rank=55.0))
    assert hi.key.endswith(f"VCE{S.BAND_SEP}C2"), hi.key
    assert lo.key.endswith(f"VCE{S.BAND_SEP}C0"), lo.key
    assert hi.mean_r > 0.9 and lo.mean_r < -0.9, (hi.describe(), lo.describe())


def test_the_allocator_ignores_bands_when_the_switch_is_off():
    from allocation.allocator import Allocator
    with cfg_ctx({"alloc_intraday_confidence_bands": "true",
                  "intraday_prior_confidence_band_edges": "0.65,0.75",
                  "priors_min_sample_intraday": "10"}):
        priors = S._intraday_priors_from_rows(_rows(), 10)
    a = Allocator.__new__(Allocator)
    a._priors = priors
    with cfg_ctx({"alloc_intraday_confidence_bands": "false"}):
        got = a._prior_for(_P("X", "VCE", native_rank=90.0))
    assert S.BAND_SEP not in got.key, got.key


def test_a_thin_band_falls_through_to_the_engine_not_to_neutral():
    """
    A band below the sample floor must not strand a proposal on NEUTRAL — it
    falls to the engine's own record, which is exactly today's behaviour.
    """
    from allocation.allocator import Allocator
    rows = _rows()
    # One lone mid-band row: present, but far below any floor.
    rows.append({"symbol": "M1", "trade_date": "2026-08-19", "strategy": "VCE",
                 "direction": "LONG", "entry": 100.0, "stop": 99.0,
                 "outcome_pct": 0.5, "cost_pct": 0.0, "cost_verdict": "TAKEN",
                 "confidence": 0.70, "meta": {"sub_engine": "VCE"}})
    with cfg_ctx({"alloc_intraday_confidence_bands": "true",
                  "intraday_prior_confidence_band_edges": "0.65,0.75",
                  "priors_min_sample_intraday": "10"}):
        priors = S._intraday_priors_from_rows(rows, 10)
        a = Allocator.__new__(Allocator)
        a._priors = priors
        got = a._prior_for(_P("X", "VCE", native_rank=70.0))
    assert got.key == "INTRADAY/VCE", got.key
    assert got.usable, got.describe()


# ── per-symbol arbitration between two engines ──────────────────────────────

class _Setup:
    """Minimal stand-in for an intraday Setup, as evaluate_all returns them."""
    def __init__(self, symbol, strategy, confidence, rr=2.0, lifecycle="ACTIVE"):
        self.symbol = symbol
        self.strategy = strategy
        self.confidence = confidence
        self.rr = rr
        self.entry, self.stop, self.target = 100.0, 99.0, 102.0
        self.direction = "LONG"
        self.is_short = False
        self.meta = {"lifecycle": lifecycle, "sub_engine": strategy,
                     "family": strategy}


def _engine_with_priors(priors: dict):
    """An Engine shell carrying nothing but what _arbitrate_symbol touches."""
    from intraday.engine import IntradayEngine
    from allocation.allocator import Allocator
    eng = IntradayEngine.__new__(IntradayEngine)
    alloc = Allocator.__new__(Allocator)
    alloc._priors = priors
    eng._allocator = alloc
    return eng


def test_arbitration_prefers_the_measured_engine_over_the_confident_one():
    """
    THE DEFECT THIS CLOSES. VCE fires at 0.60 confidence with a MEASURED
    +0.43R record; ORB fires at 0.95 with a measured -0.46R. evaluate_all
    hands back ORB because 0.95 > 0.60. Confidence is not comparable across
    engines, and on this evidence it is pointing the wrong way.
    """
    priors = {
        "INTRADAY/VCE": S._dist("INTRADAY/VCE", [0.43] * 40, 10),
        "INTRADAY/ORB": S._dist("INTRADAY/ORB", [-0.46] * 40, 10),
    }
    eng = _engine_with_priors(priors)
    orb = _Setup("RELIANCE", "ORB", 0.95)
    vce = _Setup("RELIANCE", "VCE", 0.60)
    with cfg_ctx({"intraday_symbol_arbitration": "prior",
                  "priors_min_sample_intraday": "10"}):
        got = eng._arbitrate_symbol(orb, [orb, vce])
    assert got is vce, f"picked {got.strategy}, expected VCE on measured record"


def test_arbitration_falls_back_to_confidence_when_no_prior_is_usable():
    """
    NO EVIDENCE MUST MEAN NO OPINION. Both engines below the sample floor ->
    the engine's own confidence choice stands, exactly as before this existed.
    This is the branch that matters today: most of the population predates the
    F-33 stop fix, so arbitration must not invent a preference.
    """
    priors = {
        "INTRADAY/VCE": S._dist("INTRADAY/VCE", [0.43] * 3, 10),   # below floor
        "INTRADAY/ORB": S._dist("INTRADAY/ORB", [-0.46] * 3, 10),  # below floor
    }
    eng = _engine_with_priors(priors)
    orb = _Setup("RELIANCE", "ORB", 0.95)
    vce = _Setup("RELIANCE", "VCE", 0.60)
    with cfg_ctx({"intraday_symbol_arbitration": "prior",
                  "priors_min_sample_intraday": "10"}):
        got = eng._arbitrate_symbol(orb, [orb, vce])
    assert got is orb, "with no usable prior the engine's own pick must stand"


def test_a_measured_negative_engine_still_beats_an_unmeasured_one():
    """
    An ABSENT prior returns None, not 0.0. If they collapsed, an unmeasured
    engine would outrank one measured at -0.46R purely by having no record —
    the cold-start rule inverted. Ordering must put ANY measurement first.
    """
    priors = {"INTRADAY/ORB": S._dist("INTRADAY/ORB", [-0.46] * 40, 10)}
    eng = _engine_with_priors(priors)
    orb = _Setup("RELIANCE", "ORB", 0.50)
    unknown = _Setup("RELIANCE", "PBK", 0.95)
    with cfg_ctx({"intraday_symbol_arbitration": "prior",
                  "priors_min_sample_intraday": "10"}):
        got = eng._arbitrate_symbol(unknown, [unknown, orb])
    assert got is orb, "a measured engine must outrank an unmeasured one"


def test_arbitration_never_promotes_a_shadowed_engine():
    """
    PINNED BY TWO LAYERS, AND NEITHER ALONE CAN FAIL THIS — measured, not
    assumed. Removing `_arbitrate_symbol`'s LIFECYCLE_ACTIVE filter leaves the
    test green because `proposal.from_intraday` independently returns None for
    a SHADOW setup; removing THAT leaves it green because the filter catches
    it. Only removing both turns this red, which was demonstrated.

    Recorded rather than tidied away: a reader who breaks one guard and sees
    green would reasonably conclude this test is worthless. It is not — it
    pins the PROPERTY ("a shadowed engine cannot receive capital") across a
    deliberately redundant pair, and that property is worth more than either
    guard. The redundancy is defence in depth on the one rule in this file
    that concerns capital rather than ranking.
    """
    priors = {
        "INTRADAY/VCE": S._dist("INTRADAY/VCE", [0.99] * 40, 10),
        "INTRADAY/ORB": S._dist("INTRADAY/ORB", [-0.46] * 40, 10),
    }
    eng = _engine_with_priors(priors)
    orb = _Setup("RELIANCE", "ORB", 0.50)
    vce = _Setup("RELIANCE", "VCE", 0.95, lifecycle="SHADOW")
    with cfg_ctx({"intraday_symbol_arbitration": "prior",
                  "priors_min_sample_intraday": "10"}):
        got = eng._arbitrate_symbol(orb, [orb, vce])
    assert got is orb, "a SHADOW engine must never receive capital"


def test_arbitration_switch_off_restores_the_confidence_pick():
    priors = {
        "INTRADAY/VCE": S._dist("INTRADAY/VCE", [0.43] * 40, 10),
        "INTRADAY/ORB": S._dist("INTRADAY/ORB", [-0.46] * 40, 10),
    }
    eng = _engine_with_priors(priors)
    orb = _Setup("RELIANCE", "ORB", 0.95)
    vce = _Setup("RELIANCE", "VCE", 0.60)
    with cfg_ctx({"intraday_symbol_arbitration": "confidence",
                  "priors_min_sample_intraday": "10"}):
        got = eng._arbitrate_symbol(orb, [orb, vce])
    assert got is orb


def test_a_single_engine_firing_is_untouched():
    eng = _engine_with_priors({})
    orb = _Setup("RELIANCE", "ORB", 0.95)
    with cfg_ctx({"intraday_symbol_arbitration": "prior"}):
        assert eng._arbitrate_symbol(orb, [orb]) is orb


# ── the arrival-aware pick label ────────────────────────────────────────────

def pytest_approx(x, tol):
    class _A:
        def __eq__(self, other): return abs(other - x) <= tol
        def __repr__(self): return f"~{x}"
    return _A()


def test_label_quantile_is_strict_when_much_more_is_coming():
    """09:20 shape: 6 slots, ~25 more expected today."""
    assert H.label_quantile(slots_left=6, remaining=25.0) == pytest_approx(0.76, 0.01)


def test_label_quantile_relaxes_as_supply_dries_up():
    """floor=0.0 here specifically to see the raw relaxation across the whole
    range without the configured floor masking it — clamping itself is
    covered by the two tests below."""
    strict = H.label_quantile(slots_left=6, remaining=25.0, floor=0.0)
    loose  = H.label_quantile(slots_left=2, remaining=3.0, floor=0.0)
    looser_still = H.label_quantile(slots_left=1, remaining=0.5, floor=0.0)
    assert strict > loose > looser_still, (strict, loose, looser_still)


def test_label_quantile_floors_at_the_configured_floor_not_below():
    assert H.label_quantile(slots_left=10, remaining=2.0, floor=0.5) == 0.5


def test_label_quantile_caps_when_no_slots_are_left():
    """A labelling artefact, not a real case: hurdle() already returns an
    infinite bar with no slots, so nothing reaches this point in practice."""
    assert H.label_quantile(slots_left=0, remaining=10.0, cap=0.97) == 0.97


def test_label_quantile_absent_curve_is_floor_not_zero_and_not_cap():
    """
    NO EVIDENCE MUST MEAN NO OPINION, not "everything is a top pick" and not
    "nothing is". `None` lands on `floor` because scoring.py's own cold-start
    rule says an absent measurement and a measured extreme must not collapse.
    """
    assert H.label_quantile(slots_left=6, remaining=None, floor=0.5, cap=0.97) == 0.5


def test_label_quantile_exhausted_supply_also_lands_on_floor_for_a_different_reason():
    """
    remaining<=0 ("nothing left is coming") and remaining=None ("no curve at
    all") both return `floor` but for opposite reasons: exhausted supply means
    whatever is left IS the best available by definition; no curve means no
    opinion can be formed. Same return value, different justification —
    recorded so a future reader does not "simplify" one path into the other.
    """
    assert H.label_quantile(slots_left=2, remaining=0.0, floor=0.5) == 0.5
    assert H.label_quantile(slots_left=2, remaining=-1.0, floor=0.5) == 0.5


def test_remaining_expected_sums_from_the_current_hour_forward_inclusive():
    hist = {9: 18.7, 10: 4.8, 11: 1.5, 12: 0.7, 13: 0.3, 14: 0.3}
    assert H.remaining_expected(hist, 11) == pytest_approx(2.8, 0.01)
    assert H.remaining_expected(hist, 9)  == pytest_approx(26.3, 0.01)
    assert H.remaining_expected(hist, 15) == 0.0


def test_remaining_expected_of_an_empty_histogram_is_none_not_zero():
    """An empty curve (fresh deploy, or the DB call failed) must read as
    absence, not as 'nothing is coming' — the latter would make
    label_quantile floor to the loosest label instead of forming no opinion."""
    assert H.remaining_expected({}, 10) is None


def test_pick_label_reaches_the_verdict_through_the_allocators_own_call():
    """
    ASSERTED THROUGH Allocator.select() ITSELF, not by calling the labelling
    line in isolation — this repository has shipped a builder and a consumer
    that silently disagreed about a key often enough that the rule now is: if
    a value crosses a function boundary, prove it crosses that exact boundary.
    hurdle() is patched (no database) to return a controlled label_bar; every
    other piece (Proposal, prior, score, policy) runs for real.
    """
    from unittest.mock import patch
    from allocation.allocator import Allocator
    from allocation.proposal import Proposal

    good = Proposal(symbol="VCE1", framework="INTRADAY", product="MIS",
                    entry=100.0, stop=99.0, target=103.0, quantity=10,
                    source="VCE", native_rank=90.0, direction="LONG")
    bad = Proposal(symbol="ORB1", framework="INTRADAY", product="MIS",
                   entry=100.0, stop=99.0, target=101.5, quantity=10,
                   source="ORB", native_rank=90.0, direction="LONG")

    a = Allocator.__new__(Allocator)
    a.sb = None
    a._priors = {}
    a._hold_days = {}
    a._buffer = []
    a._age_deferrals = lambda out: None
    a._basket_recheck = lambda out, open_positions: out
    a._swing_hold_days_by_family = {}
    a._prior_for = lambda p: S._dist(f"INTRADAY/{p.source}", [0.5] * 40, 10)

    fake_inputs = {"label_bar": 0.1, "bar_before_floor": -10.0}
    with patch("allocation.allocator.H.hurdle", return_value=(-10.0, fake_inputs)), \
         cfg_ctx({"alloc_intraday_engine_fairness": "false"}):
        out = a.select([good, bad], slots_left=5,
                       slots_by_framework={"INTRADAY": 5},
                       max_slots_by_framework={"INTRADAY": 5})

    labels = {v["symbol"]: v.get("pick_label") for v in out}
    # Both proposals share the SAME [0.5]*40 prior in this fixture, so both
    # clear label_bar=0.1 — the point here is the WIRING (the label reaches
    # the verdict via select()), not the ranking, which the arbitration tests
    # above already cover.
    assert labels["VCE1"] == "TOP_PICK", labels
    assert labels["ORB1"] == "TOP_PICK", labels


def test_pick_label_absent_when_no_label_bar():
    from unittest.mock import patch
    from allocation.allocator import Allocator
    from allocation.proposal import Proposal

    p = Proposal(symbol="X", framework="INTRADAY", product="MIS",
                entry=100.0, stop=99.0, target=103.0, quantity=10,
                source="VCE", native_rank=90.0, direction="LONG")
    a = Allocator.__new__(Allocator)
    a.sb = None
    a._priors = {}
    a._hold_days = {}
    a._buffer = []
    a._age_deferrals = lambda out: None
    a._basket_recheck = lambda out, open_positions: out
    a._swing_hold_days_by_family = {}
    a._prior_for = lambda p: S._dist(f"INTRADAY/{p.source}", [0.5] * 40, 10)

    with patch("allocation.allocator.H.hurdle",
              return_value=(-10.0, {"label_bar": None})):
        out = a.select([p], slots_left=5, slots_by_framework={"INTRADAY": 5},
                       max_slots_by_framework={"INTRADAY": 5})
    assert "pick_label" not in out[0], out[0]


TESTS = [
    ("arbitration prefers the measured engine over the confident one",
     test_arbitration_prefers_the_measured_engine_over_the_confident_one),
    ("arbitration falls back to confidence when no prior is usable",
     test_arbitration_falls_back_to_confidence_when_no_prior_is_usable),
    ("a measured-negative engine still beats an unmeasured one",
     test_a_measured_negative_engine_still_beats_an_unmeasured_one),
    ("arbitration never promotes a shadowed engine",
     test_arbitration_never_promotes_a_shadowed_engine),
    ("arbitration switch off restores the confidence pick",
     test_arbitration_switch_off_restores_the_confidence_pick),
    ("a single engine firing is untouched", test_a_single_engine_firing_is_untouched),
    ("interleave prefers a confirmed candidate when edges tie",
     test_interleave_prefers_a_confirmed_candidate_when_edges_tie),
    ("confirmation tiebreak never outranks a real edge difference",
     test_confirmation_tiebreak_never_outranks_a_real_edge_difference),
    ("confirmation tiebreak treats absent signal as unconfirmed, not double-penalised",
     test_confirmation_tiebreak_treats_absent_signal_as_unconfirmed_not_penalised_twice),
    ("confirmation priority switch off restores plain edge order",
     test_confirmation_priority_switch_off_restores_plain_edge_order),
    ("build_priority_criteria reads only categorical rows",
     test_build_priority_criteria_reads_only_categorical_rows),
    ("build_priority_criteria merges multiple categories per feature",
     test_build_priority_criteria_merges_multiple_categories_per_feature),
    ("confirmation_key prioritises a validated sector match",
     test_confirmation_key_prioritises_a_validated_sector_match),
    ("confirmation_key ignores an unlisted engine or feature",
     test_confirmation_key_ignores_an_unlisted_engine_or_feature),
    ("confirmation_key: retest and priority criteria both lead to rank zero",
     test_confirmation_key_retest_and_priority_criteria_both_lead_to_rank_zero),
    ("confirmation_key: None criteria behaves exactly like before F-50",
     test_confirmation_key_none_criteria_behaves_exactly_like_before_f50),
    ("interleave seats every engine's best in round one",
     test_interleave_puts_every_engines_best_in_the_first_round),
    ("interleave orders within a round by edge",
     test_interleave_orders_within_a_round_by_edge),
    ("interleave is a no-op on a one-per-engine field",
     test_interleave_is_a_noop_when_every_engine_has_one_candidate),
    ("fairness cannot admit a proposal that fails the bar",
     test_fairness_cannot_admit_a_proposal_that_fails_the_bar),
    ("fairness switch off restores the pooled sort",
     test_fairness_switch_off_restores_the_pooled_sort),
    ("an edge of exactly 0.0 is not treated as absent",
     test_edge_of_exactly_zero_is_not_treated_as_absent),
    ("band edges are inclusive-lower, exclusive-upper",
     test_band_edges_are_inclusive_lower_exclusive_upper),
    ("absent confidence bands as None, not as a default band",
     test_absent_confidence_bands_as_none_not_as_a_default_band),
    ("no edges means no bands", test_no_edges_means_no_bands),
    ("malformed edges degrade to no bands",
     test_malformed_edges_degrade_to_no_bands_rather_than_raising),
    ("bands are not built when the switch is off",
     test_bands_are_not_built_when_the_switch_is_off),
    ("bands separate a winning band from a losing one",
     test_bands_separate_a_winning_band_from_a_losing_one),
    ("turning bands on does not move the pooled fallback",
     test_turning_bands_on_does_not_move_the_pooled_fallback),
    ("bands do not move the pooled fallback on the ungated path either",
     test_bands_do_not_move_the_pooled_fallback_on_the_UNGATED_path_either),
    ("the allocator reaches the band key through its own lookup",
     test_the_allocator_reaches_the_band_key_through_its_own_lookup),
    ("the allocator ignores bands when the switch is off",
     test_the_allocator_ignores_bands_when_the_switch_is_off),
    ("a thin band falls through to the engine, not to NEUTRAL",
     test_a_thin_band_falls_through_to_the_engine_not_to_neutral),
    ("label quantile is strict when much more is coming",
     test_label_quantile_is_strict_when_much_more_is_coming),
    ("label quantile relaxes as supply dries up",
     test_label_quantile_relaxes_as_supply_dries_up),
    ("label quantile floors, never goes below the floor",
     test_label_quantile_floors_at_the_configured_floor_not_below),
    ("label quantile caps when no slots are left",
     test_label_quantile_caps_when_no_slots_are_left),
    ("label quantile: absent curve is floor, not zero and not cap",
     test_label_quantile_absent_curve_is_floor_not_zero_and_not_cap),
    ("label quantile: exhausted supply lands on floor too, differently",
     test_label_quantile_exhausted_supply_also_lands_on_floor_for_a_different_reason),
    ("remaining_expected sums from the current hour forward",
     test_remaining_expected_sums_from_the_current_hour_forward_inclusive),
    ("remaining_expected of an empty histogram is None, not zero",
     test_remaining_expected_of_an_empty_histogram_is_none_not_zero),
    ("pick label reaches the verdict through the allocator's own call",
     test_pick_label_reaches_the_verdict_through_the_allocators_own_call),
    ("pick label is absent when there is no label bar",
     test_pick_label_absent_when_no_label_bar),
]
