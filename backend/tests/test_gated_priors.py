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
    """A fake that HONOURS the select string, because PostgREST does.

    It did not, and that is the only reason a real defect survived every test
    in this module. `intraday_priors()` dedups on (symbol, strategy,
    trade_date) but its SELECT named neither `symbol` nor `trade_date`; the
    live fetch therefore returned rows where `r.get("symbol")` is None for
    every row, every observation of one engine collapsed into a single
    (None, strategy, None) group, and every prior in the system fell to n=1
    -- under the sample floor, so NEUTRAL, so `edge` reduced to -cost_r
    uniformly for every proposal. On the real book that was 3,066 resolved
    rows and 410 genuine opportunities being read as 7.

    Every test below built its own rows WITH those keys and passed happily,
    because a fake that ignores the projection cannot express the one claim
    that mattered: that the function reads only what it asked the database
    for. Projecting here makes all of them assert it at once, which is worth
    more than one dedicated test would be -- there is no way to add a column
    to the dedup key later and not have this catch it.
    """

    def __init__(self, rows): self._rows = rows

    def select(self, *a, **k):
        cols = ",".join(str(x) for x in a)
        if not cols or "*" in cols:
            return self
        want = {c.strip() for c in cols.split(",") if c.strip()}
        self._rows = [{k: v for k, v in r.items() if k in want} for r in self._rows]
        return self

    def eq(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def is_(self, *a, **k): return self

    @property
    def not_(self): return self

    def order(self, col, *a, **k):
        """Paged reads sort on a unique key (config.fetch_all, 15-Aug-2026).

        LIMIT/OFFSET with no ORDER BY has no stable row order between
        requests, so pages repeat rows and drop others — 8324 matching rows
        came back as 5000 distinct on the live book. A fake without this
        method does not fail loudly: the AttributeError is swallowed by the
        caller's non-fatal except, or turns a paged fetch into an empty list,
        which is how test_setup_rehydration silently lost 4 of 7 checks.
        """
        try:
            self._rows.sort(key=lambda r: (r.get(col) is None, r.get(col)))
        except (AttributeError, TypeError):
            pass
        return self

    def range(self, start, end):
        return _FakeExec(self._rows[start:end + 1])


class _FakeExec:
    def __init__(self, rows): self.data = rows
    def execute(self): return self


class _SB:
    def __init__(self, rows): self._rows = rows
    def table(self, name): return _FakeQuery(self._rows)


_SEQ = iter(range(1, 10_000_000))


def _row(strategy, outcome_pct, verdict, direction="LONG", entry=100.0, stop=99.0):
    """risk_pct is 1.0% by construction, so outcome_pct IS the R multiple.

    Each call gets a UNIQUE (symbol, trade_date) so these rows survive
    `priors_intraday_dedup` as independent observations — that dedup collapses
    on (symbol, engine, day), and a helper that emitted a constant identity
    would silently turn every multi-row fixture below into a sample of one.
    Tests that specifically exercise duplication override `symbol` explicitly.
    """
    if direction == "SHORT":
        entry, stop = 100.0, 101.0
    i = next(_SEQ)
    return {"strategy": strategy, "outcome": None, "outcome_pct": outcome_pct,
            "entry": entry, "stop": stop, "direction": direction,
            "cost_verdict": verdict,
            "symbol": f"SYM{i}", "trade_date": "2026-08-05"}


def test_refused_detections_do_not_drag_the_prior_down():
    """40 TAKEN winners at +1R and 200 refused losers at -2R. Pre-fix the prior
    was strongly negative; it must now reflect the trades that were actually
    takeable."""
    from allocation.scoring import intraday_priors
    rows = ([_row("ORB", 1.0, "TAKEN") for _ in range(40)]
            + [_row("ORB", -2.0, "BLOCKED_STRUCTURE") for _ in range(120)]
            + [_row("ORB", -2.0, "REJECTED_COST") for _ in range(80)])
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
    rows = ([_row("ORB", 1.0, "TAKEN") for _ in range(40)]
            + [_row("ORB", -2.0, "BLOCKED_STRUCTURE") for _ in range(200)])
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
    rows = ([_row("VCE", 0.5, "TAKEN") for _ in range(5)]
            + [_row("VCE", -0.4, "BELOW_CONVICTION") for _ in range(60)])
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
    rows = ([_row("SDN", 1.0, "TAKEN", "SHORT") for _ in range(40)]
            + [_row("SDN", -2.0, "VETOED_AI", "SHORT") for _ in range(150)])
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
    rows = ([_row("ORB", 1.0, "TAKEN") for _ in range(40)]
            + [_row("SDN", -1.0, "TAKEN", "SHORT") for _ in range(40)])
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

    rows = ([_row("ORB", 2.0, "TAKEN") for _ in range(40)]
            + [_row("VWR", -2.0, "TAKEN") for _ in range(40)])
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


def test_the_cost_is_charged_once_not_twice():
    """`outcomes.resolve_day` writes outcome_pct ALREADY NET of the round trip
    (`pct - cost`), and `score()` then subtracts the identical quantity again as
    cost_r. The prior must therefore be reconstructed GROSS, or every gate-passed
    observation is double-charged.

    This survived because it was not uniform: `_record_setup` receives a real
    cost_pct only on the TAKEN / REJECTED_COST / ALLOCATOR_DECLINED paths and a
    literal 0.0 everywhere else, so refused rows were gross and gate-passed rows
    were net. Selecting TAKEN rows — which is what priors_intraday_taken_only
    does — turns a minority effect into a systematic one, which with the new
    absolute floor means a book that refuses everything. The two changes had to
    land together, and this is the check that says so."""
    from allocation.scoring import intraday_priors
    # risk_pct is 1.0%, so an R of exactly 1.0 gross was recorded as
    # outcome_pct = 1.0 - 0.21 = 0.79 net.
    rows = [{**_row("ORB", 0.79, "TAKEN"), "cost_pct": 0.21} for _ in range(40)]
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        p = intraday_priors(_SB(rows))["INTRADAY/ORB"]
    assert abs(p.mean_r - 1.0) < 1e-9, (
        f"mean_r {p.mean_r:+.4f} — expected +1.0 gross. score() subtracts "
        f"cost_r itself, so a net prior charges the round trip twice")


def test_rows_without_a_cost_pct_are_unchanged():
    """Refused rows carry cost_pct = 0 and were already gross. Adding zero back
    must be a no-op, or the reconstruction breaks the majority of the table."""
    from allocation.scoring import intraday_priors
    rows = [{**_row("ORB", 1.0, "BLOCKED_STRUCTURE"), "cost_pct": 0} for _ in range(40)]
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "false"}):
        p = intraday_priors(_SB(rows))["INTRADAY/ORB"]
    assert abs(p.mean_r - 1.0) < 1e-9, f"mean_r {p.mean_r}"


TESTS = [
    ("the cost is charged once, not twice",
     test_the_cost_is_charged_once_not_twice),
    ("rows without a cost_pct are unchanged",
     test_rows_without_a_cost_pct_are_unchanged),
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


def test_a_lingering_setup_counts_once_not_once_per_cycle():
    """`intraday_setups` holds a row per (setup, evaluation cycle). Measured on
    the live table: RNG's entire n=11 prior was ONE setup counted eleven times,
    and PDL cleared the 30-sample floor on eight real trades. `n` is a claim
    about INDEPENDENT observations, and duplication makes it false — while also
    weighting whichever setup lingered longest most heavily, which is not
    independent of outcome."""
    from allocation.scoring import intraday_priors
    rows = ([{**_row("RNG", -1.0, "TAKEN"), "symbol": "X", "trade_date": "2026-08-05"} for _ in range(11)]
            + [{**_row("RNG", 1.0, "TAKEN"), "symbol": f"D{i}", "trade_date": "2026-08-05"}
               for i in range(4)])
    with cfg_ctx({"priors_min_sample_intraday": "3",
                  "priors_intraday_taken_only": "true",
                  "priors_intraday_dedup": "true"}):
        p = intraday_priors(_SB(rows))["INTRADAY/RNG"]
    assert p.n == 5, f"n {p.n} — expected 5 distinct setups, not 15 rows"
    assert p.mean_r > 0, f"mean_r {p.mean_r} — one lingering loser outvoted four winners"


def test_dedup_keeps_distinct_symbols_and_days_apart():
    """The key is (symbol, engine, day). Same setup on two different days is
    two observations; two symbols on one day is two observations."""
    from allocation.scoring import intraday_priors
    rows = [{**_row("ORB", 1.0, "TAKEN"), "symbol": "A", "trade_date": "2026-08-05"},
            {**_row("ORB", 1.0, "TAKEN"), "symbol": "A", "trade_date": "2026-08-06"},
            {**_row("ORB", 1.0, "TAKEN"), "symbol": "B", "trade_date": "2026-08-05"}]
    with cfg_ctx({"priors_min_sample_intraday": "3",
                  "priors_intraday_taken_only": "true",
                  "priors_intraday_dedup": "true"}):
        p = intraday_priors(_SB(rows))["INTRADAY/ORB"]
    assert p.n == 3, f"n {p.n} — distinct setups were merged"


TESTS += [
    ("a lingering setup counts once, not once per cycle",
     test_a_lingering_setup_counts_once_not_once_per_cycle),
    ("dedup keeps distinct symbols and days apart",
     test_dedup_keeps_distinct_symbols_and_days_apart),
]
