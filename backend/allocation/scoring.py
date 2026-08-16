"""
What a proposal is worth, per rupee-day, from evidence that was not selected.

    edge = E[R] / expected_hold_days

    R_target = (target - entry) / (entry - stop)
    risk_pct = (entry - stop) / entry
    cost_R   = full_round_trip(product, entry, qty) / risk_pct
    E[R]     = expectation over the EMPIRICAL R distribution of this class
               - cost_R

THREE RULES, ALL BINDING
------------------------
**`product` is mandatory.** Omitting it prices a delivery trade as intraday and
understates its cost roughly fivefold — measured on this account, CNC round
trips run 0.32-1.07% against MIS at a flat 0.11-0.21%. A scorer that defaulted
the product would systematically over-allocate to swing, which is the book
holding real money.

**Empirical distribution, never binary.** A target-or-stop model assigns a
runner the value of its target and nothing more, which systematically
undervalues exactly the trade class that pays for the losers. The distribution
keeps the right tail because it is made of realised outcomes, not of two points.

**Priors come from the FULL FIELD, never from executed trades.** Executed trades
inherit the old policy's selection: they are the plans that policy liked, scored
in the region that policy sampled, and using them to rank the plans it refused
is circular. The unbiased populations already exist —

    signal_output_daily   every daily plan's forward outcome, traded or not
    intraday_setups       every detection's resolution, taken or not

— and are tens of thousands of observations a year against the ~90 closed
trades this account has ever had.

NO ESTIMATE WITHOUT ITS n
-------------------------
Every number returned here carries the sample it came from and a standard error.
Below the floor the prior is NEUTRAL and flagged, never interpolated and never
borrowed from a neighbouring class. A fabricated prior is worse than no prior
because it is indistinguishable from a measured one downstream.
"""

from __future__ import annotations

import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (cfg_bool, cfg_float, cfg_int, get_supabase, today_ist,
                    fetch_all)


PAGE = 1000


@dataclass(frozen=True)
class Prior:
    """An empirical R distribution for one class, with its own uncertainty."""
    key:        str
    n:          int
    mean_r:     float
    median_r:   float
    stderr:     float
    p10:        float
    p90:        float
    trigger_rate: float | None = None    # swing only: how often the zone filled
    below_floor:  bool = False
    note:         str = ""

    @property
    def usable(self) -> bool:
        return not self.below_floor

    def describe(self) -> str:
        if self.below_floor:
            return f"{self.key}: NEUTRAL (n={self.n} below floor) — {self.note}"
        return (f"{self.key}: E[R]={self.mean_r:+.3f} ±{self.stderr:.3f} "
                f"(n={self.n}, median {self.median_r:+.3f}, p10 {self.p10:+.2f}, "
                f"p90 {self.p90:+.2f})")


def _dist(key: str, values: list[float], floor: int,
          trigger_rate: float | None = None, note: str = "") -> Prior:
    if len(values) < floor:
        return Prior(key, len(values), 0.0, 0.0, float("nan"), 0.0, 0.0,
                     trigger_rate, True,
                     f"needs {floor} observations to be trusted")
    s = sorted(values)
    return Prior(
        key      = key,
        n        = len(s),
        mean_r   = statistics.fmean(s),
        median_r = statistics.median(s),
        stderr   = statistics.stdev(s) / (len(s) ** 0.5) if len(s) > 1 else float("nan"),
        p10      = s[int(0.10 * (len(s) - 1))],
        p90      = s[int(0.90 * (len(s) - 1))],
        trigger_rate = trigger_rate,
        note     = note,
    )


def intraday_priors(sb, rows: list[dict] | None = None) -> dict[str, Prior]:
    """
    Per-engine R distributions from every detection, taken or refused.

    intraday_setups is the population the architecture calls this system's
    rarest asset: 595 detections on 04-Aug-2026, 595 resolved. `outcome_pct` is
    the realised move; dividing by the setup's own risk turns it into R so the
    two books share a scale.

    `rows` LETS A CALLER SUPPLY THE POPULATION INSTEAD OF FETCHING IT — the I/O
    is the only thing separating this function from a pure one, and separating
    them is what lets `tools/allocator_replay.py` walk forward through history
    while still calling THIS function rather than its own copy of it.

    That parameter exists because the copy was written and immediately caused
    the failure it was always going to cause: the replay tool reimplemented
    this logic in `_priors_from()`, the 10-Aug prior-deduplication fix landed
    here, and the tool's next run produced byte-identical output because it had
    never been reading this function at all. A tool that measures a change must
    execute the changed code. Same rule as `decide()` and `evaluate_exit()`:
    never reimplement a decision, import it.
    """
    floor = cfg_int("priors_min_sample_intraday", 30)
    if rows is not None:
        return _intraday_priors_from_rows(list(rows), floor)

    # ── THE PRIOR MUST BE ABLE TO FORGET — 12-Aug-2026 ──────────────────────
    #
    # This fetch had no time bound: every resolved row in intraday_setups,
    # ever. Two consequences, both compounding.
    #
    # The book cannot demonstrate that it improved. Every pre-fix trade sits in
    # the prior at equal weight with every post-fix one, permanently, so an
    # engine change can only move the mean by dilution — and the worse the
    # history, the more good trades are needed to shift it.
    #
    # And the absorbing state `engine.allocator_permits` works around has no
    # exit. A prior that has gone negative floors the bar, the floor declines
    # everything, zero trades write zero new TAKEN rows, and with no window to
    # age out of, the negative prior is permanent. That is why the paper
    # carve-out had to exist; bounding the window is what eventually makes it
    # unnecessary.
    #
    # 0 restores the unbounded read exactly, the rollback lever. A window short
    # enough to drop the sample below `priors_min_sample_intraday` does not
    # fail — it lands in `_cold_start`, which is deliberately PERMISSIVE,
    # because "no opinion" and "measured bad" must not give the same answer.
    #
    # SWING'S FETCH IS NOT TOUCHED. It is a different function (swing_priors)
    # over a different table with a different horizon, and this session's remit
    # is the intraday book.
    lookback = cfg_int("priors_intraday_lookback_days", 90)
    since = None
    if lookback > 0:
        from datetime import timedelta
        since = (today_ist() - timedelta(days=lookback)).isoformat()

    # SORTED PAGING, via the shared primitive. This loop paged on .range()
    # alone, and LIMIT/OFFSET without ORDER BY has no stable row order across
    # requests: pages can repeat rows and skip others. Measured on this table
    # 15-Aug-2026, 8324 matching rows came back as 8324 rows / 5000 distinct.
    # A prior is a MEAN over these rows, so duplicates silently reweight it
    # toward whichever observations the planner happened to repeat.
    def _build():
        q = (sb.table("intraday_setups")
                  # symbol and trade_date are NOT decoration: they are two of
                  # the three fields _intraday_priors_from_rows() dedups on.
                  # Omitted, r.get() returns None for both, every row of one
                  # engine collapses into ONE (None, strategy, None) group,
                  # and every prior in the system drops to n=1 -- below
                  # priors_min_sample_intraday, so NEUTRAL, so edge == -cost_r
                  # for every proposal, uniformly. Measured on the live book
                  # 11-Aug-2026: 3,066 resolved rows / 410 real opportunities
                  # became 7. The unit tests never saw it because they build
                  # their own rows WITH these keys; only the fetch was wrong.
                  # test_priors_survive_the_production_select_string() now
                  # runs this function through a fetch that honours the
                  # select string, which is the only place the two can be
                  # compared.
                  .select("symbol,trade_date,strategy,outcome,outcome_pct,"
                          "entry,stop,direction,cost_verdict,cost_pct")
                  .not_.is_("outcome_pct", "null"))
        return q.gte("trade_date", since) if since else q

    rows = fetch_all(_build, page=PAGE)
    return _intraday_priors_from_rows(rows, floor)


# Carrier for a deduplicated group's mean R. Not a database column and never
# written back — the leading underscore says so at every call site, and
# `_record_setup` would reject it if it ever reached PostgREST.
_GROUP_R = "_group_mean_r"


def _row_gross_r(r: dict) -> float | None:
    """Gross R for ONE detection, against THAT detection's own entry and stop.

    The single home of this arithmetic. It was previously written inline in the
    prior loop while the dedup block above did its own averaging in percent, and
    the two disagreed about which row a denominator belonged to. One function,
    one row, no group.

    Returns None — never 0.0 — for a row that cannot be turned into an R: no
    entry, a stop on the wrong side of it, or a missing outcome. Zero is a
    measured flat trade and would pull a mean toward it; absence must stay
    absent, per this module's cold-start rule.

    ── THE PRIOR MUST BE GROSS; score() IS WHAT CHARGES THE COST ─────────────

    `outcomes.resolve_day` writes `outcome_pct = gain_pct - cost_pct` — already
    NET of the round trip. `score()` then computes `e_r = prior.mean_r -
    cost_r`, subtracting the identical quantity a SECOND time (`cost_r =
    friction / (risk_pct * entry * qty)` is `cost_pct / risk_pct` in R units —
    same number, different spelling). This module's own header states the
    intended formula: E[R] = expectation over the EMPIRICAL R distribution -
    cost_R. That wants a GROSS distribution minus one charge.

    IT WAS NOT UNIFORM, WHICH IS WHY IT SURVIVED. `_record_setup` is passed
    `rt.pct_of_position` only on the TAKEN / REJECTED_COST / ALLOCATOR_DECLINED
    paths; every other verdict passes a literal 0.0. So refused rows were GROSS
    (charged once, correctly) and gate-passed rows were NET (charged twice) —
    and the old prior was dominated by refused rows, so the double charge stayed
    a minority effect that never showed up as an obvious constant offset.

    `priors_intraday_taken_only` selects exactly the gate-passed rows, so it
    would have turned that minority effect into a SYSTEMATIC one: every
    observation double-charged, every intraday edge pushed ~cost_r more
    negative, and with the absolute floor that means a book that refuses
    everything. The two changes had to land together.

    Reconstructed rather than backfilled: adding the row's own `cost_pct` back
    recovers the gross figure exactly, needs no migration over historical rows,
    and is a no-op on the rows that were already gross (cost_pct = 0). Because
    it is the ROW's own cost against the ROW's own risk, it stays correct inside
    a group that mixes charged and uncharged verdicts — GODREJCP/SDN on 14-Aug
    is 4 rows at cost_pct 0.206 and 6 at 0.
    """
    from intraday import direction as D

    if r.get("outcome_pct") is None:
        return None
    try:
        entry, stop = float(r.get("entry") or 0), float(r.get("stop") or 0)
        d = D.normalise(r.get("direction"))
        risk = D.risk_per_share(entry, stop, d) if entry else 0.0
        risk_pct = risk / entry * 100.0 if risk else 0.0
        if risk_pct <= 0:
            return None
        gross_pct = float(r["outcome_pct"]) + float(r.get("cost_pct") or 0)
        return gross_pct / risk_pct
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _intraday_priors_from_rows(rows: list[dict], floor: int) -> dict[str, Prior]:
    """The whole of intraday_priors() except the fetch. Pure, so a replay can
    hand it a walk-forward slice and get the identical arithmetic."""
    # SEGMENTED BY DIRECTION, NOT POOLED.
    #
    # `stop < entry` excluded every short row outright, so a short engine would
    # have accumulated detections forever and never acquired a prior — it would
    # have sat permanently on the NEUTRAL fallback and been scored as though it
    # had no history, however much it had.
    #
    # Pooling them instead would be worse than excluding them. A long and a
    # short in the same name are not two samples of one distribution: they have
    # different base rates (indices drift up, so the long side has a tailwind
    # the short side pays for), different failure modes (a short squeezes, a
    # long does not) and different tails. `outcome_pct` is also signed against
    # the LONG convention by the resolver, so a short's realised R is its
    # negation. Keyed as "ENGINE/SHORT" so the two never average together.
    from intraday import direction as D

    # ── THE PRIOR IS ABOUT TRADES, NOT ABOUT DETECTIONS — 10-Aug-2026 ───────
    #
    # `intraday_setups` holds EVERY detection with a resolved outcome,
    # including the ones the safety gates threw out: BLOCKED_STRUCTURE,
    # REJECTED_COST, VETOED_AI, BELOW_CONVICTION, BLOCKED_LIQUIDITY. On
    # 10-Aug-2026 that was 127 rows of which 15 were TAKEN — so the prior that
    # priced every new candidate was ~88% composed of trades the system had
    # deliberately REFUSED.
    #
    # That inverts the learning loop. Every gate that WORKS pushes more bad
    # outcomes into the prior, which lowers the expected R of every future
    # candidate, which lowers the allocator's bar (a percentile of that same
    # scored population), which admits worse trades. The better the gates get,
    # the more negative the system believes itself to be. Measured live: the
    # INTRADAY book's TAKE population went from E[edge] +0.1705 on 07-Aug to
    # -0.5458/-1.0935 on 10-Aug while the ENGINES were producing 2.5x more
    # candidates, and a -1.09-edge short was taken and lost 0.813R in 99s.
    #
    # The question a prior must answer is "if I take a trade from this engine,
    # what R should I expect" — so the sample is trades this system would
    # take, i.e. the ones that cleared every gate. `cost_verdict = 'TAKEN'` is
    # exactly that marker. A setup later refused by the ALLOCATOR still counts:
    # `_record_setup` writes a fresh row when a verdict changes, so its TAKEN
    # row remains, and allocator refusal is opportunity cost, not a safety
    # veto. Refused detections are NOT discarded from the database — they stay
    # the rarest asset this architecture has, for measuring what standing down
    # cost. They are simply not the population that prices a candidate which
    # has already passed every gate they failed.
    #
    # PER-ENGINE FALLBACK, NOT ALL-OR-NOTHING. A young engine with few TAKEN
    # rows falls back to its full detection history rather than losing its
    # prior entirely and dropping to NEUTRAL — a fabricated prior is worse
    # than no prior, but so is throwing away real evidence from an engine that
    # simply has not been funded often yet. The fallback is FLAGGED in the
    # Prior's note so `describe()` and the audit tools say which sample a
    # number came from, per this module's "never borrowed silently" rule.
    taken_only = cfg_bool("priors_intraday_taken_only", True)

    # ── ONE SETUP IS ONE OBSERVATION, NOT ONE PER 15s CYCLE — 10-Aug-2026 ────
    #
    # `intraday_setups` carries a row per (setup, evaluation cycle): a setup
    # lingering near its level is re-recorded whenever its entry drifts past
    # the 0.35% `intraday_setup_dedup_pct` threshold, and `_setup_is_new` does
    # NOT prevent this for a repeat TAKEN — it only suppresses an identical
    # restatement. Measured on the live table:
    #
    #     engine   rows the prior counted   distinct setups   meanR     deduped
    #     ORB               234                    23         +0.089    -0.215
    #     VWR               137                    30         +0.205    -0.040
    #     PDL                56                     8         -0.804    -0.094
    #     SDN                26                     9         +0.756    +0.187
    #     RNG                11                     1         -1.000    -1.000
    #
    # `n` is a claim about INDEPENDENT observations and every one of those was
    # false. RNG's entire "n=11" is one setup counted eleven times; PDL cleared
    # the 30-sample floor on eight real trades. There is no reading under which
    # that is correct — it corrupts the floor decision, the standard error, and
    # the mean simultaneously, because a setup that lingers longest gets the
    # most votes and lingering is not independent of outcome.
    #
    # WORST OF ALL, IT FLIPS SIGNS ON THE TWO HIGHEST-VOLUME ENGINES. ORB and
    # VWR both read positive inflated and negative deduplicated. That is what
    # made the per-engine key fix above look like a regression when replayed:
    # the fix correctly made per-engine priors REACHABLE for the first time,
    # and what it reached was duplicate-driven noise that ranked ORB and VWR
    # above a zero floor they do not actually clear. Before the key fix
    # everything fell through to the pooled book distribution, which averaged
    # across engines and partly washed the duplication out — so the bug was
    # masked by a second bug.
    #
    # Collapsed to the MEAN per (symbol, engine, day), the same key
    # `tools/allocator_replay.py::_dedupe_candidates` uses, so the prior and
    # any replay of it count the same population the same way.
    #
    # NOT applicable to `swing_priors()`: it reads `signal_output_daily`, which
    # the evening pipeline writes once per symbol per day by construction.
    # ── THE COLLAPSE HAPPENS IN R, NOT IN PERCENT — 16-Aug-2026 ─────────────
    #
    # This block was right about the POPULATION and wrong about the ARITHMETIC.
    # It averaged `outcome_pct` over the group, wrote that one mean onto a copy
    # of `src[0]`, and let the loop below divide it by `src[0]`'s risk. So the
    # NUMERATOR came from every row in the group and the DENOMINATOR came from
    # whichever row the paged read sorted first — `fetch_all`'s default
    # `order_by="id"`, i.e. the earliest detection, which is not a property of
    # the setup at all.
    #
    # A group is not one price level, and cannot be, BY THE MECHANISM THAT
    # CREATES IT: `_setup_is_new` re-records a setup precisely WHEN its entry
    # has drifted past `intraday_setup_dedup_pct`. Drift is the admission
    # criterion, so every multi-row group holds multiple entries and stops by
    # construction. GODREJCP/SDN on 14-Aug-2026 is 10 rows carrying 7 distinct
    # entries, with risk between 0.141% and 0.603% — a 4.3x spread, one value of
    # which was applied to all ten outcomes.
    #
    # R is a RATIO, and the mean of ratios is not the ratio of means unless
    # every denominator is equal — the one thing this grouping guarantees is
    # false. Measured over the full resolved table, the two forms disagree on
    # sign for RNG (+0.061 vs -0.137) and by 68% of the estimate for VCE.
    #
    # Each row is therefore reduced to R against ITS OWN entry and stop first,
    # and the group mean is taken of those. `_row_gross_r` is the single place
    # that arithmetic lives, so the deduplicated and non-deduplicated paths
    # cannot drift apart — the pre-fix code had the formula written out twice,
    # which is how a numerator and a denominator came from different rows
    # without either line looking wrong on its own.
    if cfg_bool("priors_intraday_dedup", True):
        collapsed: dict[tuple, list[dict]] = {}
        for r in rows:
            collapsed.setdefault(
                (r.get("symbol"), r.get("strategy"), r.get("trade_date")), []).append(r)
        deduped = []
        for grp in collapsed.values():
            # A group's verdict is TAKEN if the setup was EVER taken that day —
            # the trade happened, and the near-miss rows that preceded it are
            # restatements of the same decision, not separate refusals.
            taken = [g for g in grp if (g.get("cost_verdict") or "").upper() == "TAKEN"]
            src = taken or grp
            base = dict(src[0])
            vals = [v for v in (_row_gross_r(g) for g in src) if v is not None]
            if vals:
                # Carried as R, already gross and already divided by each row's
                # own risk. The loop below reads this instead of recomputing,
                # because `base`'s entry/stop are one member's, not the group's.
                base[_GROUP_R] = statistics.fmean(vals)
            deduped.append(base)
        rows = deduped

    by: dict[str, list[float]] = {}
    by_taken: dict[str, list[float]] = {}
    for r in rows:
        try:
            d = D.normalise(r.get("direction"))
            key = r["strategy"] or "?"
            if D.is_short(d):
                key = f"{key}/SHORT"
            # A deduplicated row already carries the group's mean R, computed
            # per member against that member's own risk. Recomputing it from
            # `base`'s entry/stop is exactly the defect fixed above.
            r_mult = r.get(_GROUP_R)
            if r_mult is None:
                r_mult = _row_gross_r(r)
            if r_mult is None:
                continue
            by.setdefault(key, []).append(r_mult)
            if (r.get("cost_verdict") or "").upper() == "TAKEN":
                by_taken.setdefault(key, []).append(r_mult)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    def _prior_for(key: str, all_vals: list[float]) -> Prior:
        if not taken_only:
            return _dist(f"INTRADAY/{key}", all_vals, floor)
        gated = by_taken.get(key, [])
        if len(gated) >= floor:
            return _dist(f"INTRADAY/{key}", gated, floor,
                         note=f"gate-passed sample ({len(gated)} TAKEN of "
                              f"{len(all_vals)} detections)")
        return _dist(f"INTRADAY/{key}", all_vals, floor,
                     note=f"FALLBACK to all detections — only {len(gated)} "
                          f"TAKEN row(s), under the {floor} floor")

    # ── THE DICT KEY MUST BE THE KEY THE ALLOCATOR LOOKS UP — 10-Aug-2026 ───
    #
    # This dict was built as `{k: _dist(f"INTRADAY/{k}", ...)}` — the Prior's
    # OWN `.key` field carried the "INTRADAY/" prefix, but the dictionary was
    # keyed on the bare engine name. `Allocator._prior_for()` looks up
    # `f"{p.framework}/{p.source}"`, i.e. "INTRADAY/ORB", which never matched
    # "ORB". The only two entries that DID match were `INTRADAY/ALL` and
    # `INTRADAY/ALL/SHORT`, because those two were written with the prefix.
    #
    # So EVERY intraday proposal ever scored — ORB, GAP, PDL, VCE, PBK, VWR,
    # RNG, SDN — fell through to the book-wide pooled distribution, and the
    # per-engine segmentation this function builds so carefully (and whose
    # docstring forbids "borrowing from a neighbouring class") never once
    # reached the allocator.
    #
    # This is what made the 10-Aug edge distribution degenerate. With one
    # shared prior, `edge = (prior.mean_r - cost_r) / max(hold_days, 0.5)`
    # varies only through `cost_r` — which moves only with position size and
    # stop distance — so 141 DECLINEd proposals averaged -1.0937 against 1
    # TAKEn at -1.0935. The allocator was not failing to discriminate between
    # engines; it had never been given the numbers with which to try.
    #
    # A below-floor prior still falls back to the book via `_usable()`, so this
    # only promotes engines that have genuinely earned a sample of their own.
    out = {f"INTRADAY/{k}": _prior_for(k, v) for k, v in by.items()}
    if rows:
        # The book-level fallback is ALSO split. `INTRADAY/ALL` is what a class
        # with too few observations falls back to, so pooling shorts into it
        # would contaminate the long fallback with a different distribution —
        # the exact borrowing this module's own header forbids ("never borrowed
        # from a neighbouring class"). A short with no prior falls back to the
        # SHORT book, not to the book as a whole.
        #
        # THE BOOK-LEVEL FALLBACKS TAKE THE GATED SAMPLE TOO — and this is the
        # pair that actually mattered on 10-Aug. SDN sits below
        # `priors_min_sample_intraday` (~6 legs against a floor of 30), so it
        # falls back to `INTRADAY/ALL/SHORT`, which means that pool — not SDN's
        # own record — is the number that priced DEVYANI at edge -1.0935.
        # Leaving these two on the raw detection population while every named
        # engine used the gated one would have put the contaminated prior back
        # underneath precisely the engines too young to have escaped it.
        longs  = [x for k, v in by.items() if not k.endswith("/SHORT") for x in v]
        shorts = [x for k, v in by.items() if k.endswith("/SHORT") for x in v]
        # _prior_for() reads by_taken[key], so the pooled keys need pooled
        # gated samples registered under the same names before it is called.
        by_taken["ALL"] = [x for k, v in by_taken.items()
                           if not k.endswith("/SHORT") and k != "ALL" for x in v]
        by_taken["ALL/SHORT"] = [x for k, v in by_taken.items()
                                 if k.endswith("/SHORT") for x in v]
        out["INTRADAY/ALL"] = _prior_for("ALL", longs)
        out["INTRADAY/ALL/SHORT"] = _prior_for("ALL/SHORT", shorts)
    return out


# Swing engine -> family, matching docs/0_SYSTEM_BLUEPRINT.md §4: CTL, SEC,
# TPO, SBS, VBD, RSB and IAD share family CONTINUATION; MOM, RVS, PEAD and ACC
# are each split into their own family because the blueprint's own reason for
# splitting them — a distinct evidence profile, tracked SHADOW/ACTIVE
# separately — is exactly what pooling them back into CONTINUATION would erase.
_SWING_FAMILY = {
    "CTL": "CONTINUATION", "SEC": "CONTINUATION", "TPO": "CONTINUATION",
    "SBS": "CONTINUATION", "VBD": "CONTINUATION", "RSB": "CONTINUATION",
    "IAD": "CONTINUATION",
    "MOM": "MOM", "RVS": "RVS", "PEAD": "PEAD", "ACC": "ACC",
}


def swing_family(strategy: str | None) -> str:
    """
    `strategy` (written as `strategy_source` earlier in the pipeline — see
    `screen_stocks.py::run_sector_rotation` and friends) is a '+'-joined combo
    of every engine that agreed on a name that day, e.g. "CTL+SEC" or
    "CTL+MOM+SEC" — confirmed empirically against `signal_output_daily`:
    dozens of distinct combo strings, most with single-digit counts, which is
    too fine a grain for a stable prior on its own.

    A combo stays CONTINUATION only if EVERY constituent is a CONTINUATION
    engine. If any constituent belongs to a family the blueprint deliberately
    isolated (MOM, RVS, PEAD, ACC), that family wins — a mixed signal must
    never be silently diluted back into the pool it was split out of. Ties
    among more than one non-CONTINUATION family (not observed in current
    data) resolve alphabetically, deterministic rather than order-of-arrival.

    Unrecognised or empty resolves to "ALL", the existing book-level fallback
    key — never invented, matching this module's own rule for the R
    distributions themselves.

    A trailing parenthetical annotation is stripped before the lookup —
    found 07-Aug-2026, closed_positions.strategy carrying "CTL (Legacy)" for
    21 of 75 historical SWING closes (89% of the sample together with plain
    "CTL"), which `_SWING_FAMILY.get()` cannot match as written and which
    resolved to "ALL" instead of CONTINUATION — a real family's engine
    silently falling out of its own bucket, the identical shape as the
    signal_type/strategy column-confusion bug this module's own swing_
    priors() docstring already documents. "CTL (Legacy)" is CTL; the
    parenthetical is metadata about WHEN it was labelled, not a different
    engine. Generic strip (any trailing "(...)"), not a hardcoded alias list,
    so a future "SEC (Legacy)" or similar is not a second silent miss.
    """
    strip_paren = lambda p: re.sub(r"\s*\([^)]*\)\s*$", "", p).strip()
    parts = [strip_paren(p) for p in (strategy or "").split("+") if p]
    parts = [p for p in parts if p]
    if not parts:
        return "ALL"
    families = {_SWING_FAMILY.get(p, "ALL") for p in parts}
    families.discard("CONTINUATION")
    if not families:
        return "CONTINUATION"
    if len(families) == 1:
        return next(iter(families))
    return sorted(families)[0]


def swing_priors(sb) -> dict[str, Prior]:
    """
    R distributions from every daily plan's forward outcome, traded or not.

    SEGMENTED BY ENGINE FAMILY (`swing_family(strategy)`), NOT `signal_type`.

    06-Aug-2026: this function keyed its buckets on `signal_type`, which reads
    as "which engine produced this" but is actually a WORKFLOW status —
    confirmed against real data, its values are WATCH, BUY_CANDIDATE,
    REENTRY_SETUP, AVOID_ENTRY_EVENT, MOMENTUM_CONTINUATION, never an engine
    name. The real engine identity lives in the `strategy` column (CTL, SEC,
    MOM, TPO, ... and their '+'-joined combos). This was invisible from the
    allocator's own logs because it compounded with a second, independent bug
    at the call site (`proposal.py::from_swing`, `intraday/engine.py`
    `_allocate_shadow`): every swing Proposal's `source` was ALSO always the
    literal fallback string "CONTINUATION" regardless of which engine fired,
    so the class lookup `pri.get(f"SWING/{p.source}")` was `pri.get(
    "SWING/CONTINUATION")` against a dict that never contained that key
    either way — always missing, always falling through to the pooled
    SWING/ALL prior, for every swing candidate, regardless of engine. Fixed
    together: `from_swing` now reads `strategy` (not the never-populated
    `strategy_source`) through `swing_family()`, the same function this uses
    to build the keys it looks up.

    SEGMENTED BY WHETHER THE ENTRY LEVEL WAS REACHED, NOT POOLED.

    A plan's forward outcome assumes it *could* have been entered at its
    recorded level. Where the zone was never touched that assumption is
    untested, so pooling triggered and untriggered plans into one mean answers a
    question nobody asked. The trigger rate is reported separately and the R
    distribution is built only from plans that actually filled.

    Plans without a recorded stop cannot be expressed in R at all — 1,246 of
    1,711 were written before planned_stop was populated. Their forward return
    is real and is reported as a percentage, but it is NOT converted to R
    against an invented denominator.
    """
    floor = cfg_int("priors_min_sample_swing", 30)
    rows, off = [], 0
    while True:
        page = (sb.table("signal_output_daily")
                  .select("strategy,outcome_category,outcome_return_pct,"
                          "outcome_entered,entry_zone_high,planned_stop")
                  .not_.is_("outcome_category", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    if not rows:
        return {}

    entered = [r for r in rows if r.get("outcome_entered")]
    trigger = len(entered) / len(rows)

    by: dict[str, list[float]] = {}
    for r in entered:
        entry, stop = r.get("entry_zone_high"), r.get("planned_stop")
        ret = r.get("outcome_return_pct")
        if None in (entry, stop, ret):
            continue                      # no stop → no R, and none is invented
        try:
            entry, stop = float(entry), float(stop)
            risk_pct = (entry - stop) / entry * 100.0
            if risk_pct <= 0:
                continue
            by.setdefault(swing_family(r.get("strategy")), []).append(
                float(ret) / risk_pct)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    # PREFIXED, for the same reason as intraday_priors() — see the block there.
    # `Allocator._prior_for()` looks up "SWING/CONTINUATION"; this dict was
    # keyed "CONTINUATION", so every swing family prior missed and fell through
    # to "SWING/ALL". That is the second half of the attribution bug recorded
    # for 06-Aug in docs/6_IMPLEMENTATION_STATUS.md: the buckets were fixed to
    # key on engine family instead of the workflow-status `signal_type` column,
    # but the allocator still could not reach the corrected buckets, so the
    # knowledge base's "it was previously conditioning on neither" stayed true
    # of engine identity even after that fix landed.
    out = {f"SWING/{k}": _dist(f"SWING/{k}", v, floor, trigger) for k, v in by.items()}
    allr = [x for v in by.values() for x in v]
    out["SWING/ALL"] = _dist("SWING/ALL", allr, floor, trigger)
    return out


def expected_hold_days(sb, framework: str) -> tuple[float, int]:
    """
    Measured, per book, from closed records. Never hardcoded.

    Returns (days, n) so the caller can see how thin the estimate is. With ~19
    intraday and ~72 swing closes the intraday figure is weak, and the readiness
    review says to report that uncertainty rather than present a point estimate.
    """
    rows = (sb.table("closed_positions").select("hold_days")
              .eq("framework", framework.upper())
              .not_.is_("hold_days", "null").limit(PAGE).execute().data) or []
    days = [max(float(r["hold_days"]), 0.5) for r in rows]   # same-day = half a day
    if not days:
        return 1.0, 0
    return statistics.fmean(days), len(days)


MOMENTUM       = "MOMENTUM"        # needs a trend to break INTO
MEAN_REVERSION = "MEAN_REVERSION"  # needs the ABSENCE of one

# STRUCTURAL, not statistical — each classification is drawn from the
# engine's OWN module docstring (intraday/strategies/*.py), not an outside
# judgment. See regime_fit_multiplier()'s docstring for why a per-engine-
# per-regime EMPIRICAL prior is not attempted instead.
ENGINE_ARCHETYPE = {
    "ORB": MOMENTUM,        # "Opening Range Breakout"
    "GAP": MOMENTUM,        # "gap up, hold, continue"
    "PDL": MOMENTUM,        # "previous-day high break and retest"
    "PBK": MOMENTUM,        # "the first pullback in a trend day" — needs an
                            # established trend to pull back FROM
    "VCE": MOMENTUM,        # "volatility contraction, then expansion" — a
                            # squeeze play, classic breakout precursor
    "SDN": MOMENTUM,        # short family — a breakdown/distribution
                            # pattern, structurally the same axis inverted
    "RNG": MEAN_REVERSION,  # "buy the low of an established range" — its
                            # OWN docstring: "the complement to every
                            # breakout engine in this package"
    "VWR": MEAN_REVERSION,  # "VWAP reclaim — the one thing that works in
                            # the midday DRIFT" — its own word for a
                            # non-trending regime
    # GDB WAS THE ONE FAMILY THAT ACTUALLY REACHES THIS LOOKUP AND WAS ABSENT
    # — 12-Aug-2026. regime_fit_multiplier() is keyed by `p.source`, and
    # from_intraday() sets `source` to the FAMILY (family_of()), so the keys
    # that ever reach here are the six FAMILIES {ORB, VWR, VCE, RNG, SDN, GDB}
    # — not the eight sub-engine names. GAP/PDL/PBK above are documentary:
    # real engines, but merged into the ORB/VWR families, so their key is
    # never looked up. GDB is a family of its own (registry.FAMILIES: GDB->GDB)
    # and was the sole family missing a row, so an unpromoted-then-promoted GDB
    # would read "unclassified — no opinion" (an exact 1.0 no-op) the day the
    # weight is raised, silently, exactly the "future regime_fit_report() run
    # drops them" failure this module's test docstring already names. Inert
    # today twice over — the weight defaults to 0.0 and GDB is SHADOW so no GDB
    # proposal reaches the allocator at all — but correct the moment either
    # changes. Its own module docstring is unambiguous ("This is a
    # mean-reversion LONG engine: buy the recovery off a gap-down open"), so
    # unlike the pooled VWR family there is no archetype ambiguity to resolve.
    "GDB": MEAN_REVERSION,  # gap-down bounce — buy the recovery, not the gap
}

# Bounded nudge, as a FRACTION of 1.0, before `intraday_regime_fit_weight`
# scales it down. RISK_OFF is 0.0 for every entry because market_context.
# classify() already blocks new longs outright in RISK_OFF (allow_longs is
# False), so a long-only engine's fit in that state is moot — this table
# only has an opinion where a proposal can actually reach it.
_REGIME_FIT_NUDGE = {
    (MOMENTUM, "RISK_ON"):        +0.15,   # a real trend to break into
    (MOMENTUM, "NEUTRAL"):        -0.10,   # false-breakout risk
    (MOMENTUM, "CAUTION"):        -0.10,
    (MOMENTUM, "RISK_OFF"):        0.0,
    (MEAN_REVERSION, "NEUTRAL"):  +0.15,   # RNG's own reason to exist
    (MEAN_REVERSION, "CAUTION"):  +0.05,
    (MEAN_REVERSION, "RISK_ON"):  -0.10,   # fading a real trend
    (MEAN_REVERSION, "RISK_OFF"):  0.0,
}


def regime_fit_multiplier(engine_family: str | None,
                          market_state: str | None) -> tuple[float, str]:
    """
    A bounded nudge to an engine's prior, based on whether its own archetype
    (momentum/breakout vs mean-reversion) structurally suits the CURRENT
    market state — not an empirical per-engine-per-regime prior.

    WHY NOT EMPIRICAL, GIVEN THAT'S THIS PROJECT'S USUAL STANDARD. Every
    other weight change in this session (rank_weight_screener,
    rank_weight_rr) was shipped on a direct tercile measurement. This one
    is not, on purpose: hurdle.py's own docstring already states the
    project's position on this exact question — "Per-regime fitting is
    Phase 5 and is gated on years of data, not on cleverness" — and that
    caution has already been paid for twice. The STRONG hurdle bucket was
    unreachable until 05-Aug-2026 for lack of history, then crossed its own
    40-sample floor on rows the allocator had written that same morning,
    pricing itself against its own newborn output. intraday_priors() has
    n=843 TAKEN rows total across ALL engines and ALL history combined (see
    the 11-Aug review); splitting that further by 4 regime states AND 8
    engines before it has even cleared 30 per ENGINE alone would recreate
    the identical trap at a finer grain.

    So this is a STRUCTURAL rule instead — each engine's classification
    comes from what the engine's own docstring says it needs (a trend to
    break into, or the absence of one), not from a backtest. That is
    weaker evidence than a tercile split, and the weight says so: default
    0.0, exactly mirroring how rank_weight_tier and rank_weight_conviction
    already sit at 0.0 pending validation (entry_ranking.py's own
    precedent). Restoring it is a config change once regime_at_detection
    (migration 068) has accumulated enough rows to run the SAME kind of
    measurement this session ran for final_score and implied_rr — see
    KNOWLEDGE_BASE.md's "Promising Hypotheses" entry for this exact item
    and what evidence would confirm or kill it.

    Returns (multiplier, reason). multiplier is 1.0 — an exact no-op — when
    the weight is 0, the engine is unclassified, or market_state is
    unknown; "no opinion" must be indistinguishable from "no adjustment",
    the same rule the cold-start floor already applies for the same reason.
    """
    weight = cfg_float("intraday_regime_fit_weight", 0.0)
    if weight <= 0 or not market_state:
        return 1.0, "regime fit off (weight 0 or no market state)"

    archetype = ENGINE_ARCHETYPE.get(engine_family or "")
    if archetype is None:
        return 1.0, f"{engine_family or '?'} unclassified — no opinion"

    nudge = _REGIME_FIT_NUDGE.get((archetype, market_state), 0.0)
    mult = 1.0 + nudge * weight
    return mult, (f"{engine_family}={archetype} in {market_state}: "
                  f"{nudge:+.0%} nudge x weight {weight:.2f}")


def score(entry: float, stop: float, target: float, qty: int, product: str,
          prior: Prior, hold_days: float, direction: str = "LONG",
          engine_family: str | None = None, market_state: str | None = None) -> dict:
    """
    One proposal, on the common scale. Pure arithmetic over in-memory data.

    `product` is a required positional argument on purpose — there is no default
    that is safe for both books.

    `direction` defaults to LONG because every caller predating shorts is one.
    The coherence test was `stop >= entry or target <= entry` — which is the
    definition of a correctly constructed SHORT. So every short proposal
    returned edge=None, and `policies.intraday_stopping` renders a None edge as
    "not scoreable — levels incoherent or no prior" and DECLINEs it. Shorts
    would have been refused by the allocator before any engine's opinion was
    consulted, and the refusal would have read like a data problem rather than
    a policy.

    `engine_family`/`market_state` are OPTIONAL and both default to None, in
    which case regime_fit_multiplier() returns an exact 1.0 no-op — a caller
    that does not pass them gets identical output to before this parameter
    pair existed. See regime_fit_multiplier() for what they do when passed
    and why the weight defaults to 0.0 regardless.
    """
    from intraday.cost_model import round_trip
    from intraday import direction as D

    if qty <= 0:
        return {"edge": None, "reason": "incoherent levels — no position"}
    ok, why = D.validate(entry, stop, target, direction)
    if not ok:
        return {"edge": None, "reason": f"incoherent levels — {why}"}

    risk  = D.risk_per_share(entry, stop, direction)
    risk_pct = risk / entry
    r_target = D.reward_per_share(entry, target, direction) / risk
    friction = round_trip(entry, qty, product=product).total
    cost_r   = friction / (risk_pct * entry * qty)

    regime_mult, regime_reason = regime_fit_multiplier(engine_family, market_state)

    if prior.usable:
        # The nudge scales the RETURN estimate (mean_r), not the cost — cost
        # is a broker fee schedule and has no opinion about market regime.
        adj_mean_r = prior.mean_r * regime_mult
        e_r, basis = adj_mean_r - cost_r, f"empirical n={prior.n}"
        if regime_mult != 1.0:
            basis += f" x{regime_mult:.3f} ({regime_reason})"
    else:
        # NEUTRAL means zero expected R, not "assume the target". Flagged so a
        # caller cannot mistake an absent prior for a measured one. 0.0 * any
        # multiplier is still 0.0 — the regime nudge is correctly a no-op on
        # an unmeasured engine, same as it would be on a measured one with
        # mean_r == 0.
        e_r, basis = 0.0 - cost_r, f"NEUTRAL prior (n={prior.n} below floor)"

    return {
        "edge":        e_r / max(hold_days, 0.5),
        "e_r":         e_r,
        "cost_r":      cost_r,
        "r_target":    r_target,
        "risk_pct":    risk_pct,
        "friction":    friction,
        "hold_days":   hold_days,
        "prior_n":     prior.n,
        "prior_floor": prior.below_floor,
        "basis":       basis,
        "regime_fit_mult": regime_mult,
    }


def _swing_bias_warning(sb) -> None:
    """
    Say out loud why the swing R prior is not yet trustworthy.

    IT LOOKS POSITIVE AND IT IS BIASED IN A KNOWN DIRECTION.

    A plan enters the R distribution only once it has resolved to TARGET or
    STOP, which requires a recorded stop AND a level being reached. Two things
    follow, both inflating the mean:

      · planned_stop was only populated from 28-Jul-2026, so the entire R
        sample is drawn from a few recent weeks and one market regime.
      · a plan that hits its target in three sessions resolves; one that grinds
        for fifteen is still pending. In a young dataset the fast winners have
        resolved and the slow outcomes have not — survivorship in TIME, not in
        selection, and it disappears only as windows close.

    Meanwhile the 625 plans with no recorded levels average **-0.86%** over the
    same horizon, and the expectancy ledger puts the realised swing book at
    **-Rs 12,489** across 72 closed trades. A +0.5R prior sitting next to those
    two facts is a warning, not a result.
    """
    try:
        rows = (sb.table("signal_output_daily")
                  .select("date,outcome_category,planned_stop")
                  .not_.is_("outcome_category", "null").limit(PAGE).execute().data) or []
        scored = [r for r in rows if r["outcome_category"] in ("TARGET", "STOP")]
        if not scored:
            return
        span = f"{min(str(r['date']) for r in scored)} to {max(str(r['date']) for r in scored)}"
        unscored = len(rows) - len(scored)
        logger.warning(f"  ⚠ the R distribution above rests on {len(scored)} plans, all "
                       f"signalled between {span}.")
        logger.warning(f"    {unscored} further resolved plans have NO recorded stop and are "
                       f"excluded from R — their mean forward return is negative.")
        logger.warning(f"    Fast outcomes resolve first, so a young sample over-represents "
                       f"quick winners. Do not promote anything on this prior until the "
                       f"15-session windows have closed across a full quarter.")
    except Exception:
        pass


def tercile_report(sb=None) -> int:
    """
    DIAGNOSTIC ONLY — not called from `swing_priors()`, not wired into the
    allocator, changes nothing. Run with `python -m allocation.scoring --tercile`.

    Council Break 2 observed, correctly, that priors discarded `final_score`
    entirely. Whether conditioning on it actually predicts anything is a
    separate, empirical question, and `docs/TRADING_METHODOLOGY_REVIEW.md`
    already warns that tercile-mining at this account's sample size produces
    spurious "material" splits by chance. This prints mean R for each
    `(swing_family(strategy), final_score tercile)` bucket next to the pooled
    per-family number, so the split can be read by eye before anyone
    conditions priors on it.

    MEASURED 06-Aug-2026, n=125 entered+resolved CONTINUATION plans (the
    dominant family): tercile means were 0.5156 / 0.4910 / 0.5112 — FLAT, no
    monotonic separation. `final_score`'s hand-set weights
    (`compute_msl.py::get_final_score_weights`,
    0.22/0.20/0.15/0.13/0.12/0.08/0.06/0.04) have never been validated
    against forward outcomes, and this is the first time they were tested
    against one: on this measurement they carry no information about forward
    R once a plan has already entered. MOM and RVS showed n too small (17 and
    5) to read anything into. Conclusion: the conditioning was NOT shipped.
    Re-run this as the sample grows — a flat result at n=125 is evidence, not
    a permanent verdict.
    """
    floor = cfg_int("priors_min_sample_swing", 30)
    sb = sb or get_supabase()
    rows, off = [], 0
    while True:
        page = (sb.table("signal_output_daily")
                  .select("strategy,outcome_return_pct,outcome_entered,"
                          "entry_zone_high,planned_stop,final_score,ai_tier")
                  .not_.is_("outcome_category", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    triples: list[tuple[str, float, float, str]] = []   # (family, final_score, R, ai_tier)
    for r in rows:
        if not r.get("outcome_entered"):
            continue
        entry, stop = r.get("entry_zone_high"), r.get("planned_stop")
        ret, fs = r.get("outcome_return_pct"), r.get("final_score")
        if None in (entry, stop, ret, fs):
            continue                      # no stop or no score → excluded, not invented
        try:
            entry, stop = float(entry), float(stop)
            risk_pct = (entry - stop) / entry * 100.0
            if risk_pct <= 0:
                continue
            triples.append((swing_family(r.get("strategy")), float(fs),
                            float(ret) / risk_pct, r.get("ai_tier") or "UNTIERED"))
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    logger.info("═" * 74)
    logger.info("TERCILE REPORT — mean R by (engine, final_score tercile). DIAGNOSTIC ONLY.")
    logger.info("Not wired into the live allocator or swing_priors(). Evidence for a decision,")
    logger.info("not the decision.")
    logger.info("═" * 74)

    if not triples:
        logger.error("  no entered plan has both a stop and a final_score — nothing to report")
        return 1

    by_engine: dict[str, list[tuple[float, float]]] = {}
    for eng, fs, r, _tier in triples:
        by_engine.setdefault(eng, []).append((fs, r))

    for eng in sorted(by_engine):
        pts = sorted(by_engine[eng])          # sorted by final_score
        n = len(pts)
        pooled = _dist(f"SWING/{eng}", [r for _, r in pts], floor)
        logger.info("")
        logger.info(f"── {eng} (n={n}) ──")
        logger.info(f"  pooled: {pooled.describe()}")
        if n < floor:
            logger.warning(f"  too few observations for a tercile split "
                           f"(n={n}, floor={floor} per bucket recommended)")
            continue
        t1, t2 = pts[n // 3][0], pts[(2 * n) // 3][0]
        for label, vals in (
            ("LOW",  [r for fs, r in pts if fs <= t1]),
            ("MID",  [r for fs, r in pts if t1 < fs <= t2]),
            ("HIGH", [r for fs, r in pts if fs > t2]),
        ):
            d = _dist(f"SWING/{eng}/{label}", vals, floor)
            logger.info(f"  {label:<4}: {d.describe()}")

    # Same population, sliced by ai_decision_engine's tier instead of
    # final_score. Pooled across engine families — splitting by both would
    # starve every bucket at this sample size.
    logger.info("")
    logger.info("── mean R by ai_tier, pooled across engines ──")
    by_tier: dict[str, list[float]] = {}
    for _eng, _fs, r, tier in triples:
        by_tier.setdefault(tier, []).append(r)
    for tier in sorted(by_tier):
        d = _dist(f"SWING/TIER/{tier}", by_tier[tier], floor)
        (logger.warning if d.below_floor else logger.info)(f"  {tier:<14}: {d.describe()}")

    return 0


def rr_tercile_report(sb=None) -> int:
    """
    DIAGNOSTIC ONLY — not called from `swing_priors()` or `entry_ranking.py`,
    changes nothing. Run with `python -m allocation.scoring --rr-tercile`.

    THE QUESTION THIS ANSWERS. Migration 060 (10-Aug-2026) rescaled
    entry_ranking.score_plan()'s `final_score` term from an unweighted
    0-100 magnitude down to a centered, weighted delta, on the strength of
    `tercile_report()`'s finding that final_score does not separate forward
    R (0.516/0.491/0.511, n=125). That rescale left `implied_rr` — the R:R
    term, `rr = _f(p.get("implied_rr")) or _f(p.get("expected_r"))` — as the
    single largest-magnitude component in the function by a wide margin
    (clamped at rank_rr_cap=4.0, weight 1.0 by default, so it swings roughly
    -12..+20 points against every other term's single digits). No forward-R
    measurement had ever been run on THAT term before this rescale shipped
    — the KB's own evidence base was silent on the thing the ranking was
    left dominated by. This is that measurement, using the exact same
    methodology and exact same population as `tercile_report()` so the two
    numbers are directly comparable.

    THE FALLBACK IS REPLICATED DELIBERATELY. entry_ranking.py does not read
    implied_rr alone — it falls back to expected_r when implied_rr is null —
    and testing implied_rr in isolation would measure a different quantity
    than what the ranking function actually consumes. `_rr_value()` below is
    the same fallback chain, so this measures the CODE PATH, not just one of
    its inputs.
    """
    floor = cfg_int("priors_min_sample_swing", 30)
    sb = sb or get_supabase()
    rows, off = [], 0
    while True:
        page = (sb.table("signal_output_daily")
                  .select("strategy,outcome_return_pct,outcome_entered,"
                          "entry_zone_high,planned_stop,implied_rr,expected_r,"
                          "ai_tier")
                  .not_.is_("outcome_category", "null")
                  .range(off, off + PAGE - 1).execute().data) or []
        rows += page
        if len(page) < PAGE:
            break
        off += PAGE

    def _rr_value(r: dict) -> float | None:
        # Mirrors analysis.entry_ranking.score_plan()'s own fallback chain
        # exactly — see that function's "reward per unit of risk" section.
        for key in ("implied_rr", "expected_r"):
            v = r.get(key)
            if v is not None:
                try:
                    fv = float(v)
                    if fv:
                        return fv
                except (TypeError, ValueError):
                    continue
        return None

    triples: list[tuple[str, float, float]] = []   # (family, rr, realised_R)
    skipped_no_rr = 0
    for r in rows:
        if not r.get("outcome_entered"):
            continue
        entry, stop = r.get("entry_zone_high"), r.get("planned_stop")
        ret = r.get("outcome_return_pct")
        rr = _rr_value(r)
        if rr is None:
            skipped_no_rr += 1
            continue
        if None in (entry, stop, ret):
            continue                      # no stop or no outcome → excluded, not invented
        try:
            entry, stop = float(entry), float(stop)
            risk_pct = (entry - stop) / entry * 100.0
            if risk_pct <= 0:
                continue
            triples.append((swing_family(r.get("strategy")), rr,
                            float(ret) / risk_pct))
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    logger.info("═" * 74)
    logger.info("RR TERCILE REPORT — mean R by (engine, implied_rr/expected_r tercile).")
    logger.info("DIAGNOSTIC ONLY. Not wired into the live allocator or entry_ranking.py.")
    logger.info("Evidence for a decision, not the decision.")
    logger.info("═" * 74)

    if not triples:
        logger.error("  no entered plan has both a stop and an implied_rr/expected_r "
                     "— nothing to report")
        return 1
    if skipped_no_rr:
        logger.info(f"  {skipped_no_rr} entered plan(s) had neither implied_rr nor "
                    f"expected_r — excluded, not treated as zero")

    by_engine: dict[str, list[tuple[float, float]]] = {}
    for eng, rr, r in triples:
        by_engine.setdefault(eng, []).append((rr, r))

    for eng in sorted(by_engine):
        pts = sorted(by_engine[eng])          # sorted by rr
        n = len(pts)
        pooled = _dist(f"SWING/{eng}/RR", [r for _, r in pts], floor)
        logger.info("")
        logger.info(f"── {eng} (n={n}) ──")
        logger.info(f"  pooled: {pooled.describe()}")
        if n < floor:
            logger.warning(f"  too few observations for a tercile split "
                           f"(n={n}, floor={floor} per bucket recommended)")
            continue
        t1, t2 = pts[n // 3][0], pts[(2 * n) // 3][0]
        for label, vals in (
            ("LOW",  [r for rr, r in pts if rr <= t1]),
            ("MID",  [r for rr, r in pts if t1 < rr <= t2]),
            ("HIGH", [r for rr, r in pts if rr > t2]),
        ):
            d = _dist(f"SWING/{eng}/RR/{label}", vals, floor)
            logger.info(f"  {label:<4}: {d.describe()}")

    return 0


def regime_fit_report(sb=None) -> int:
    """
    DIAGNOSTIC ONLY — not called from score() or the allocator. Run with
    `python -m allocation.scoring --regime-fit`.

    THE QUESTION THIS ANSWERS. regime_fit_multiplier() classifies each
    intraday engine as MOMENTUM or MEAN_REVERSION by what its own docstring
    says it needs, and nudges its edge by a small bounded amount depending
    on the CURRENT market_context state — shipped at weight 0.0 (an exact
    no-op) because that classification is structural, not measured. This
    report is what WOULD measure it, once regime_at_detection (migration
    068, 11-Aug-2026) has accumulated enough TAKEN rows: does MOMENTUM mean
    R actually run higher in RISK_ON than in NEUTRAL/CAUTION, and does
    MEAN_REVERSION mean R actually run higher in NEUTRAL/CAUTION than in
    RISK_ON? If both hold, the hypothesis is confirmed and the weight can
    be raised on evidence, the same way rank_weight_screener and
    rank_weight_rr were. If neither holds, the classification was wrong and
    the weight should stay at 0 regardless of how appealing the theory is.

    TAKEN ONLY, DELIBERATELY — the same discipline priors_intraday_
    taken_only enforces on intraday_priors() (see that switch and
    KNOWLEDGE_BASE.md's own account of what happened before it existed:
    88% of the population being refused detections inverted the entire
    learning loop). A regime-fit measurement built from every BLOCKED_* row
    alongside TAKEN ones would have the identical defect.

    RUN TODAY, THIS REPORTS "NO DATA YET" AND THAT IS THE CORRECT
    ANSWER — regime_at_detection is a brand new column with zero historical
    rows the day this shipped. This function exists to be re-run in a few
    weeks, not to justify anything today.
    """
    sb = sb or get_supabase()
    floor = cfg_int("priors_min_sample_intraday", 30)
    # Sorted paging — same reason as intraday_priors above.
    rows = fetch_all(lambda: sb.table("intraday_setups")
                     .select("strategy,outcome_pct,entry,stop,direction,cost_pct,"
                             "cost_verdict,regime_at_detection")
                     .not_.is_("outcome_pct", "null")
                     .eq("cost_verdict", "TAKEN")
                     .not_.is_("regime_at_detection", "null"), page=PAGE)

    logger.info("═" * 74)
    logger.info("REGIME FIT REPORT — mean R by (archetype, regime_at_detection).")
    logger.info("DIAGNOSTIC ONLY. Not wired into score() or the allocator.")
    logger.info("═" * 74)

    if not rows:
        logger.warning("  0 TAKEN rows carry regime_at_detection yet — expected "
                       "immediately after migration 068 ships. Re-run this "
                       "after a few weeks of live sessions have accumulated "
                       "data; see this function's own docstring.")
        return 0

    from intraday import direction as D
    by_cell: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        family = str(r.get("strategy") or "")
        archetype = ENGINE_ARCHETYPE.get(family)
        if archetype is None:
            continue
        entry, stop = r.get("entry"), r.get("stop")
        ret = r.get("outcome_pct")
        if None in (entry, stop, ret):
            continue
        try:
            risk_pct = D.risk_per_share(float(entry), float(stop),
                                        r.get("direction") or "LONG") / float(entry) * 100.0
            if risk_pct <= 0:
                continue
            # GROSS R, cost added back — matches _intraday_priors_from_rows'
            # exact convention (outcome_pct is stored NET of cost; score()
            # subtracts cost_r separately), so this report's numbers are
            # directly comparable to intraday_priors()'s.
            gross_pct = float(ret) + float(r.get("cost_pct") or 0)
            r_mult = gross_pct / risk_pct
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        key = (archetype, r["regime_at_detection"])
        by_cell.setdefault(key, []).append(r_mult)

    if not by_cell:
        logger.warning("  regime_at_detection rows exist but none matched a "
                       "classified engine family — nothing to report")
        return 0

    for archetype in (MOMENTUM, MEAN_REVERSION):
        logger.info("")
        logger.info(f"── {archetype} ──")
        for state in ("RISK_ON", "NEUTRAL", "CAUTION", "RISK_OFF"):
            vals = by_cell.get((archetype, state), [])
            d = _dist(f"{archetype}/{state}", vals, floor)
            (logger.warning if d.below_floor else logger.info)(f"  {state:<9}: {d.describe()}")

    logger.info("")
    logger.info("Confirms the hypothesis if MOMENTUM's RISK_ON mean exceeds its "
               "NEUTRAL/CAUTION means, AND MEAN_REVERSION's NEUTRAL/CAUTION "
               "means exceed its RISK_ON mean. Anything else argues for "
               "leaving intraday_regime_fit_weight at 0.")
    return 0


def report() -> int:
    """Print every prior the system can currently justify, with its n."""
    sb = get_supabase()
    logger.info("═" * 74)
    logger.info("EMPIRICAL PRIORS — from the full field, never from executed trades")
    logger.info("═" * 74)

    for name, fn in (("INTRADAY — every detection", intraday_priors),
                     ("SWING — every daily plan", swing_priors)):
        logger.info("")
        logger.info(f"── {name} ──")
        priors = fn(sb)
        if not priors:
            logger.error("  no resolved outcomes — this population is EMPTY")
            continue
        for k in sorted(priors):
            p = priors[k]
            (logger.warning if p.below_floor else logger.info)(f"  {p.describe()}")
        any_p = next(iter(priors.values()))
        if any_p.trigger_rate is not None:
            logger.info(f"  trigger rate (zone actually filled): {any_p.trigger_rate:.0%}")
            _swing_bias_warning(sb)

    logger.info("")
    logger.info("── Expected hold days, measured per book ───────────────────────────")
    for fw in ("SWING", "INTRADAY"):
        d, n = expected_hold_days(sb, fw)
        warn = "  ← thin, treat as provisional" if n < 30 else ""
        logger.info(f"  {fw:<9} {d:.2f} days from n={n} closed records{warn}")
    return 0


if __name__ == "__main__":
    if "--tercile" in sys.argv:
        sys.exit(tercile_report())
    elif "--rr-tercile" in sys.argv:
        sys.exit(rr_tercile_report())
    elif "--regime-fit" in sys.argv:
        sys.exit(regime_fit_report())
    else:
        sys.exit(report())
