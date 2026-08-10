"""
The intraday prior is built from TRADES, not from DETECTIONS (10-Aug-2026).

WHAT THIS CATCHES
-----------------
`scoring.intraday_priors()` read every `intraday_setups` row with a resolved
outcome — including the ones the safety gates threw out (BLOCKED_STRUCTURE,
REJECTED_COST, VETOED_AI, BELOW_CONVICTION, BLOCKED_LIQUIDITY). On 10-Aug-2026
that was 127 rows of which 15 were TAKEN, so the prior pricing every new
candidate was ~88% composed of trades the system had deliberately REFUSED.

That inverts the learning loop. Every gate that WORKS pushes more bad outcomes
into the prior, which lowers the expected R of every future candidate, which
lowers the allocator's bar (a percentile of that same scored population), which
admits worse trades. The better the gates get, the more negative the system
believes itself to be.

The engine-level effect is what these tests pin, but the case that actually
mattered on 10-Aug was the BOOK-LEVEL fallback: SDN sits below
`priors_min_sample_intraday`, so it falls back to `INTRADAY/ALL/SHORT`, which
means that pool — not SDN's own record — is what priced DEVYANI at edge
-1.0935 before it lost 0.813R in 99 seconds.
"""

from __future__ import annotations

from tests import cfg_ctx


class _FakeQuery:
    def __init__(self, rows): self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self): return self

    def range(self, start, end):
        return _FakeExec(self._rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _FakeQuery(self._rows)


def _row(strategy, outcome_pct, verdict, direction="LONG", entry=100.0, stop=99.0):
    """risk_pct is 1.0% by construction, so outcome_pct IS the R multiple."""
    if direction == "SHORT":
        entry, stop = 100.0, 101.0
    return {"strategy": strategy, "outcome": None, "outcome_pct": outcome_pct,
            "entry": entry, "stop": stop, "direction": direction,
            "cost_verdict": verdict}


def test_refused_detections_do_not_drag_the_prior_down():
    """40 TAKEN winners at +1R and 200 refused losers at -2R. Pre-fix the prior
    was strongly negative; it must now reflect the trades that were actually
    takeable."""
    from allocation.scoring import intraday_priors
    rows = ([_row("ORB", 1.0, "TAKEN")] * 40
            + [_row("ORB", -2.0, "BLOCKED_STRUCTURE")] * 120
            + [_row("ORB", -2.0, "REJECTED_COST")] * 80)
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        p = intraday_priors(_SB(rows))["INTRADAY/ORB"]
    assert p.usable, f"prior unusable: {p.note}"
    assert abs(p.mean_r - 1.0) < 1e-9, f"mean_r {p.mean_r} — refused rows leaked in"
    assert p.n == 40, f"n {p.n}"
    assert "TAKEN" in p.note


def test_switch_off_restores_the_old_population():
    """The pre-10-Aug behaviour must remain reachable, and must be the thing
    that reproduces the defect — a switch that changes nothing is not a switch."""
    from allocation.scoring import intraday_priors
    rows = ([_row("ORB", 1.0, "TAKEN")] * 40
            + [_row("ORB", -2.0, "BLOCKED_STRUCTURE")] * 200)
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "false"}):
        p = intraday_priors(_SB(rows))["INTRADAY/ORB"]
    assert p.n == 240, f"n {p.n}"
    assert p.mean_r < 0, f"mean_r {p.mean_r} — the old population was not negative"


def test_a_young_engine_falls_back_rather_than_going_neutral():
    """An engine with too few TAKEN rows keeps its full detection history. A
    fabricated prior is worse than no prior, but so is discarding real evidence
    from an engine that simply has not been funded often yet — and dropping to
    NEUTRAL would silently re-price it at exactly 0 - cost_r."""
    from allocation.scoring import intraday_priors
    rows = ([_row("VCE", 0.5, "TAKEN")] * 5
            + [_row("VCE", -0.4, "BELOW_CONVICTION")] * 60)
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        p = intraday_priors(_SB(rows))["INTRADAY/VCE"]
    assert p.usable, "a young engine lost its prior entirely"
    assert p.n == 65, f"n {p.n}"
    assert "FALLBACK" in p.note, f"the fallback was silent: {p.note!r}"


def test_the_short_book_fallback_is_gated_too():
    """SDN is below the sample floor and falls back to INTRADAY/ALL/SHORT, so
    that pool is what actually prices a short. Leaving the book-level fallbacks
    on the raw population would put the contaminated prior back underneath
    exactly the engines too young to have escaped it."""
    from allocation.scoring import intraday_priors
    rows = ([_row("SDN", 1.0, "TAKEN", "SHORT")] * 40
            + [_row("SDN", -2.0, "VETOED_AI", "SHORT")] * 150)
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        priors = intraday_priors(_SB(rows))
    p = priors["INTRADAY/ALL/SHORT"]
    assert p.usable, f"unusable: {p.note}"
    assert abs(p.mean_r - 1.0) < 1e-9, f"mean_r {p.mean_r} — refused shorts leaked in"


def test_longs_and_shorts_still_never_pool_together():
    """The gating must not quietly undo the direction split — a long and a short
    in the same name are not two samples of one distribution."""
    from allocation.scoring import intraday_priors
    rows = ([_row("ORB", 1.0, "TAKEN")] * 40
            + [_row("SDN", -1.0, "TAKEN", "SHORT")] * 40)
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        priors = intraday_priors(_SB(rows))
    assert abs(priors["INTRADAY/ALL"].mean_r - 1.0) < 1e-9
    assert abs(priors["INTRADAY/ALL/SHORT"].mean_r + 1.0) < 1e-9


def test_the_prior_dict_is_keyed_the_way_the_allocator_looks_it_up():
    """THE BUG THAT MADE THE 10-AUG DISTRIBUTION DEGENERATE.

    `intraday_priors()` keyed its dict on the bare engine name ("ORB") while
    the Prior's own `.key` field carried the prefix, and
    `Allocator._prior_for()` looks up "INTRADAY/ORB". The only entries that
    matched were INTRADAY/ALL and INTRADAY/ALL/SHORT, so every intraday
    proposal ever scored fell through to the pooled book distribution and no
    engine was ever priced on its own record.

    With one shared prior, edge varies only through cost_r — which is why 141
    DECLINEs averaged -1.0937 against 1 TAKE at -1.0935. Asserted through the
    real Allocator lookup, not by comparing strings, because the previous
    failure was precisely two strings that looked interchangeable."""
    from allocation.scoring import intraday_priors
    from allocation.allocator import Allocator
    from allocation.proposal import Proposal

    rows = ([_row("ORB", 2.0, "TAKEN")] * 40
            + [_row("VWR", -2.0, "TAKEN")] * 40)
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        priors = intraday_priors(_SB(rows))
        alloc = Allocator.__new__(Allocator)
        alloc._priors = priors

        def _p(source):
            return Proposal(symbol="X", framework="INTRADAY", product="MIS",
                            entry=100.0, stop=99.0, target=103.0, quantity=10,
                            source=source, native_rank=0.0, direction="LONG")

        orb = alloc._prior_for(_p("ORB"))
        vwr = alloc._prior_for(_p("VWR"))

    assert orb.mean_r > 0 > vwr.mean_r, (
        f"ORB {orb.mean_r:+.3f} / VWR {vwr.mean_r:+.3f} — a winning and a "
        f"losing engine resolved to the same prior, so the allocator cannot "
        f"tell them apart")
    assert orb.key == "INTRADAY/ORB" and vwr.key == "INTRADAY/VWR"


TESTS = [
    ("the prior dict is keyed the way the allocator looks it up",
     test_the_prior_dict_is_keyed_the_way_the_allocator_looks_it_up),
    ("refused detections do not drag the prior down",
     test_refused_detections_do_not_drag_the_prior_down),
    ("switch off restores the old population",
     test_switch_off_restores_the_old_population),
    ("a young engine falls back rather than going NEUTRAL",
     test_a_young_engine_falls_back_rather_than_going_neutral),
    ("the short book fallback is gated too",
     test_the_short_book_fallback_is_gated_too),
    ("longs and shorts still never pool together",
     test_longs_and_shorts_still_never_pool_together),
]
