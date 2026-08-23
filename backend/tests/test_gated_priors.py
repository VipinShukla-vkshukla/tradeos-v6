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


def _row(strategy, outcome_pct, verdict, direction="LONG", entry=100.0, stop=99.0,
        population=None):
    """risk_pct is 1.0% by construction, so outcome_pct IS the R multiple.

    Each call gets a UNIQUE (symbol, trade_date) so these rows survive
    `priors_intraday_dedup` as independent observations — that dedup collapses
    on (symbol, engine, day), and a helper that emitted a constant identity
    would silently turn every multi-row fixture below into a sample of one.
    Tests that specifically exercise duplication override `symbol` explicitly.

    `population`, Stage D2h: omitted (None) means no `meta` key at all,
    which `_population_class()` reads as "bench" -> established -- every
    existing test above this line is unaffected by that split.
    """
    if direction == "SHORT":
        entry, stop = 100.0, 101.0
    i = next(_SEQ)
    row = {"strategy": strategy, "outcome": None, "outcome_pct": outcome_pct,
          "entry": entry, "stop": stop, "direction": direction,
          "cost_verdict": verdict,
          "symbol": f"SYM{i}", "trade_date": "2026-08-05"}
    if population is not None:
        row["meta"] = {"universe_population": population}
    return row


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


# ── ESTABLISHED VS ADMITTED — Stage D2h, 24-Aug-2026 ────────────────────────
# Track D widened the intraday universe to ~270 names by adding Population
# B/C (no stock_data_daily history at all). scoring.py splits the prior by
# `meta.universe_population` so a noisy run of newly-admitted trades cannot
# drag an established engine's mean around, and vice versa. Asserted through
# the real Allocator lookup, same discipline as the test above it — the
# 10-Aug bug this file exists for was exactly two strings that looked
# interchangeable.

def _proposal(source, population="bench", direction="LONG"):
    from allocation.proposal import Proposal
    return Proposal(symbol="X", framework="INTRADAY", product="MIS",
                    entry=100.0, stop=99.0, target=103.0, quantity=10,
                    source=source, native_rank=0.0, direction=direction,
                    meta={"universe_population": population})


def test_admitted_trades_do_not_move_the_established_prior():
    """40 established ORB winners at +2R, 40 ADMITTED ORB losers at -2R.
    Pre-fix (single pooled key) these average to ~0 and every proposal —
    established or admitted — is scored on that wash. Post-fix, established
    proposals must see ONLY their own +2R record."""
    from allocation.scoring import intraday_priors
    from allocation.allocator import Allocator

    rows = ([_row("ORB", 2.0, "TAKEN", population="bench") for _ in range(40)]
            + [_row("ORB", -2.0, "TAKEN", population="population_b") for _ in range(40)])
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        priors = intraday_priors(_SB(rows))
        alloc = Allocator.__new__(Allocator)
        alloc._priors = priors
        established = alloc._prior_for(_proposal("ORB", population="bench"))

    assert established.mean_r > 1.5, (
        f"established ORB prior read {established.mean_r:+.3f} — the 40 "
        f"admitted losers leaked into it")
    assert established.key == "INTRADAY/ORB"


def test_admitted_trades_get_their_own_prior_not_the_established_one():
    """The other half: an admitted proposal must see the ADMITTED record,
    not established's, even though established has plenty of history."""
    from allocation.scoring import intraday_priors
    from allocation.allocator import Allocator

    rows = ([_row("ORB", 2.0, "TAKEN", population="bench") for _ in range(40)]
            + [_row("ORB", -2.0, "TAKEN", population="population_b") for _ in range(40)])
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        priors = intraday_priors(_SB(rows))
        alloc = Allocator.__new__(Allocator)
        alloc._priors = priors
        admitted = alloc._prior_for(_proposal("ORB", population="population_b"))

    assert admitted.mean_r < -1.5, (
        f"admitted ORB prior read {admitted.mean_r:+.3f} — it borrowed "
        f"established's +2R record instead of its own -2R one")
    assert admitted.key == "INTRADAY/ORB/ADMITTED"


def test_an_admitted_proposal_with_no_admitted_history_reaches_neutral_not_established():
    """Cold start must stay permissive (this module's own rule), but it must
    ALSO stay isolated: an admitted engine with zero admitted-population
    samples must not silently inherit established's prior, however good it
    is — that is the exact contamination this split exists to prevent, one
    step removed."""
    from allocation.scoring import intraday_priors
    from allocation.allocator import Allocator

    rows = [_row("ORB", 2.0, "TAKEN", population="bench") for _ in range(40)]
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        priors = intraday_priors(_SB(rows))
        alloc = Allocator.__new__(Allocator)
        alloc._priors = priors
        admitted = alloc._prior_for(_proposal("ORB", population="population_c_kite"))

    assert admitted.mean_r == 0.0, (
        f"admitted ORB prior read {admitted.mean_r:+.3f}, not neutral — it "
        f"must have borrowed established's record with no admitted "
        f"observations of its own")
    assert admitted.key == "INTRADAY/NONE/ADMITTED", (
        f"got {admitted.key!r} — must be the ADMITTED cold-start key, not "
        f"a borrowed established one")


def test_admitted_short_isolated_from_admitted_long_and_from_established():
    """Three populations that must never average together: established
    long, admitted long, admitted short."""
    from allocation.scoring import intraday_priors
    from allocation.allocator import Allocator

    rows = ([_row("SDN", 2.0, "TAKEN", population="bench") for _ in range(40)]
            + [_row("SDN", -1.0, "TAKEN", direction="SHORT", population="population_b")
              for _ in range(40)])
    with cfg_ctx({"priors_min_sample_intraday": "30",
                  "priors_intraday_taken_only": "true"}):
        priors = intraday_priors(_SB(rows))
        alloc = Allocator.__new__(Allocator)
        alloc._priors = priors
        admitted_short = alloc._prior_for(
            _proposal("SDN", population="population_b", direction="SHORT"))

    assert admitted_short.mean_r < 0, (
        f"admitted SHORT prior read {admitted_short.mean_r:+.3f} — did not "
        f"find its own -1R record")
    assert admitted_short.key == "INTRADAY/SDN/ADMITTED/SHORT"


def test_population_class_defaults_absent_meta_to_established():
    """A row with no `meta` at all (every row written before this stage, or
    any non-intraday path) must classify as established, not admitted — the
    split must be additive, never silently reclassifying old data."""
    from allocation.scoring import _population_class
    assert _population_class({"strategy": "ORB"}) == "established"


def test_population_class_reads_meta_stored_as_a_json_string():
    """Same defensive read _engine_of() needs, per its own 20-Aug docstring
    finding: meta stored as a JSON string, not object, in some historical
    rows."""
    from allocation.scoring import _population_class
    import json as _json
    row = {"meta": _json.dumps({"universe_population": "population_c_ipo"})}
    assert _population_class(row) == "admitted"


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
    ("admitted trades do not move the established prior",
     test_admitted_trades_do_not_move_the_established_prior),
    ("admitted trades get their own prior, not the established one",
     test_admitted_trades_get_their_own_prior_not_the_established_one),
    ("an admitted proposal with no admitted history reaches neutral, not established",
     test_an_admitted_proposal_with_no_admitted_history_reaches_neutral_not_established),
    ("admitted short isolated from admitted long and from established",
     test_admitted_short_isolated_from_admitted_long_and_from_established),
    ("population_class defaults absent meta to established",
     test_population_class_defaults_absent_meta_to_established),
    ("population_class reads meta stored as a JSON string",
     test_population_class_reads_meta_stored_as_a_json_string),
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


# ── THE COLLAPSE MUST HAPPEN IN R, NOT IN PERCENT — 16-Aug-2026 ─────────────
#
# The dedup above is right about the POPULATION and was wrong about the
# ARITHMETIC. It averaged `outcome_pct` across the group and then divided that
# one mean by `src[0]`'s risk — so the denominator came from whichever row the
# paged read happened to sort first, and the numerator came from all of them.
#
# A group is not one price level. `_setup_is_new` re-records a setup precisely
# WHEN its entry has drifted past `intraday_setup_dedup_pct`, so by construction
# the rows in a group have different entries and different stops, and therefore
# different risk. GODREJCP/SDN on 14-Aug is 10 rows carrying 7 distinct entries
# and risk between 0.141% and 0.603% — a 4.3x spread in the denominator that the
# old form applied one value of to all ten outcomes.
#
# R is the unit the whole allocator is denominated in, and R is a RATIO. The
# mean of ratios is not the ratio of means unless every denominator is equal,
# which is the one thing a dedup group guarantees is false.

def test_group_r_uses_each_rows_own_risk_not_the_first_rows():
    """The defect, at its smallest: two rows, one group, opposite verdicts.

    Row A risks 2.0% and loses all of it (-1R). Row B risks 0.2% and makes 1.0%
    (+5R). The setup was, on this day, a good one that the first detection
    happened to catch at the worst level.

    Collapsing in percent: mean(-2.0, +1.0) = -0.5, divided by A's 2.0% risk,
    = -0.25R. Collapsing in R: mean(-1.0, +5.0) = +2.0R. Not a precision
    difference — a sign."""
    from allocation.scoring import intraday_priors
    grp = [{**_row("VCE", -2.0, "TAKEN", entry=100.0, stop=98.0),
            "symbol": "X", "trade_date": "2026-08-05"},
           {**_row("VCE", 1.0, "TAKEN", entry=100.0, stop=99.8),
            "symbol": "X", "trade_date": "2026-08-05"}]
    with cfg_ctx({"priors_min_sample_intraday": "1",
                  "priors_intraday_taken_only": "true",
                  "priors_intraday_dedup": "true"}):
        p = intraday_priors(_SB(grp))["INTRADAY/VCE"]
    assert p.n == 1, f"n {p.n} — the group is one observation"
    assert abs(p.mean_r - 2.0) < 1e-9, (
        f"mean_r {p.mean_r:+.4f} — expected +2.0R, the mean of each row's own "
        f"R. -0.25 means the group's outcomes were divided by the FIRST row's "
        f"risk")


def test_dedup_is_invariant_to_row_order_within_a_group():
    """The property, stated directly: no engine's prior may depend on which row
    of a group the paged read sorted first.

    That is the whole defect. `base = dict(src[0])` took entry, stop and
    direction from row zero, so the same group under a different `order_by` —
    or the same rows arriving in a different sequence from a replay — produced a
    different R. An estimator whose answer moves when nothing about the
    evidence moves is not measuring the evidence."""
    from allocation.scoring import intraday_priors
    grp = [{**_row("RNG", -2.0, "TAKEN", entry=100.0, stop=98.0),
            "symbol": "X", "trade_date": "2026-08-05"},
           {**_row("RNG", 1.0, "TAKEN", entry=100.0, stop=99.8),
            "symbol": "X", "trade_date": "2026-08-05"},
           {**_row("RNG", 0.5, "TAKEN", entry=100.0, stop=99.5),
            "symbol": "X", "trade_date": "2026-08-05"}]
    means = []
    for first in range(len(grp)):
        rotated = grp[first:] + grp[:first]
        with cfg_ctx({"priors_min_sample_intraday": "1",
                      "priors_intraday_taken_only": "true",
                      "priors_intraday_dedup": "true"}):
            means.append(intraday_priors(_SB(rotated))["INTRADAY/RNG"].mean_r)
    assert max(means) - min(means) < 1e-9, (
        f"the same three rows gave {['%+.4f' % m for m in means]} depending on "
        f"which one came first")


def test_a_group_of_one_level_is_unchanged():
    """Where every row of a group shares one entry and stop, the two forms are
    algebraically identical, and the fix must be a no-op there. Most groups on
    the live table are exactly this, so a change that moved them would be
    changing far more than the defect."""
    from allocation.scoring import intraday_priors
    grp = [{**_row("ORB", pct, "TAKEN", entry=100.0, stop=99.0),
            "symbol": "X", "trade_date": "2026-08-05"}
           for pct in (-1.0, 0.5, 2.4)]
    with cfg_ctx({"priors_min_sample_intraday": "1",
                  "priors_intraday_taken_only": "true",
                  "priors_intraday_dedup": "true"}):
        p = intraday_priors(_SB(grp))["INTRADAY/ORB"]
    # risk_pct is 1.0%, so R == outcome_pct: mean(-1.0, 0.5, 2.4) = +0.6333
    assert abs(p.mean_r - (1.9 / 3.0)) < 1e-9, f"mean_r {p.mean_r:+.6f}"


def test_the_cost_add_back_survives_the_collapse():
    """Gross reconstruction happens per row, so a group mixing charged rows
    (TAKEN/REJECTED_COST carry a real cost_pct) with uncharged ones (every other
    verdict passes a literal 0.0) must add each row's OWN cost back — not a
    group average of costs to a group average of outcomes.

    GODREJCP/SDN is exactly that mixture: 4 rows at cost_pct 0.206, 6 at 0.

    The two rows carry DIFFERENT risk (1.0% and 2.0%) on purpose. With equal
    risk, spreading the group's mean cost over both rows is self-cancelling and
    the check passes while still being wrong; unequal risk is what makes a cost
    that crossed rows show up in the mean.

    Neither row is TAKEN, which is also GODREJCP/SDN's shape — `src = taken or
    grp` means one TAKEN row in a group discards the rest, so a mixed-cost group
    only exists among refused rows in the first place."""
    from allocation.scoring import intraday_priors
    # Both rows are +1.0R gross at their own level; one was recorded net of 0.21.
    grp = [{**_row("PBK", 0.79, "REJECTED_COST", entry=100.0, stop=99.0),
            "symbol": "X", "trade_date": "2026-08-05", "cost_pct": 0.21},
           {**_row("PBK", 2.0, "BLOCKED_STRUCTURE", entry=200.0, stop=196.0),
            "symbol": "X", "trade_date": "2026-08-05", "cost_pct": 0}]
    with cfg_ctx({"priors_min_sample_intraday": "1",
                  "priors_intraday_taken_only": "false",
                  "priors_intraday_dedup": "true"}):
        p = intraday_priors(_SB(grp))["INTRADAY/PBK"]
    assert abs(p.mean_r - 1.0) < 1e-9, (
        f"mean_r {p.mean_r:+.6f} — both rows are +1.0R gross at their own "
        f"levels; anything else means cost or risk crossed rows")


TESTS += [
    ("group R uses each row's own risk, not the first row's",
     test_group_r_uses_each_rows_own_risk_not_the_first_rows),
    ("dedup is invariant to row order within a group",
     test_dedup_is_invariant_to_row_order_within_a_group),
    ("a group at one price level is unchanged",
     test_a_group_of_one_level_is_unchanged),
    ("the cost add-back survives the collapse",
     test_the_cost_add_back_survives_the_collapse),
]
