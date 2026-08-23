"""
Select, recheck the basket, buffer the verdicts. Never place an order.

    select(proposals, context) -> list[verdict]

THE STRUCTURAL PROHIBITION
--------------------------
This module does not import `execution`, and must never be made to. It is not a
convention — it is the mechanism that makes shadow mode safe to run against a
live book. A component that cannot import the thing that places orders cannot
place one by accident, however wrong its arithmetic, however confused its
priors, however badly someone wires its call-site. `tools/health` asserts it by
inspection.

Everything here returns data. The caller decides what to do with a TAKE, and the
caller is the only thing that touches execution.

WHAT MAKES THIS DIFFERENT IN KIND FROM WHAT CAME BEFORE
--------------------------------------------------------
Every verdict is recorded, including DECLINE. Phase 3 recorded what it did;
Phase 4 records what it decided, which includes the fifty-four proposals it
turned down. That record is the entire reason the allocator can ever be scored:
"the allocator beat greedy" is a claim about the trades it did not take.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_bool, cfg_float, cfg_int, get_supabase

from allocation import hurdle as H
from allocation import policies as P
from allocation import scoring as S
from allocation.proposal import Proposal

TAKE, DEFER, DECLINE = P.TAKE, P.DEFER, P.DECLINE


def _hold_days_for_proposal(framework: str, source: str, book_days: float, book_n: int,
                            swing_by_family: dict[str, tuple[float, int]]
                            ) -> tuple[float, int]:
    """
    Which (days, n) a proposal's edge divides by. SWING ONLY branches away
    from the book-pooled figure — see allocation/swing_hold_days.py.

    INTRADAY always returns (book_days, book_n) unchanged, regardless of
    what `swing_by_family` holds — this is the one line that must never
    move for that book, and it is proven never to by
    test_swing_hold_days.py::test_intraday_is_never_affected_by_the_swing_
    family_dict.
    """
    if framework != "SWING":
        return book_days, book_n
    return swing_by_family.get(source, (book_days, book_n))


class Allocator:
    """Stateless per cycle apart from the write buffer and DEFER book."""

    def __init__(self, sb=None):
        self.sb = sb or get_supabase()
        self._buffer: list[dict] = []
        self._deferred: dict[tuple, dict] = {}
        self._priors: dict | None = None
        # {engine: {feature: {category, ...}}} — VALIDATED, favourable
        # categorical findings only. See refresh_priority_criteria().
        self._priority_criteria: dict | None = None
        self._hold_days: dict[str, tuple[float, int]] = {}
        # SWING ONLY — see allocation/swing_hold_days.py. Empty dict means
        # "nothing measured yet", and select()'s lookup already falls back to
        # the book-pooled self._hold_days["SWING"] whenever a family is
        # absent, so an empty dict here behaves exactly as if this feature
        # did not exist — the same fail-open shape as self._priors being None.
        self._swing_hold_days_by_family: dict[str, tuple[float, int]] = {}

    # ── priors, refreshed on the slow timer rather than per cycle ──────────
    def refresh_priors(self) -> None:
        try:
            self._priors = {**S.intraday_priors(self.sb), **S.swing_priors(self.sb)}
            for fw in ("SWING", "INTRADAY"):
                self._hold_days[fw] = S.expected_hold_days(self.sb, fw)
            # SWING ONLY. A failure here must not take down the book-pooled
            # figure refreshed just above — caught separately so a broken
            # per-family query degrades to the existing behaviour rather than
            # losing this cycle's whole prior refresh over a swing-only add-on.
            try:
                from allocation.swing_hold_days import expected_hold_days_by_family
                self._swing_hold_days_by_family = expected_hold_days_by_family(self.sb)
            except Exception as e:
                logger.warning(f"  allocator: swing per-family hold_days failed "
                               f"({e}) — using the book-pooled figure for every family")
        except Exception as e:
            logger.warning(f"  allocator: prior refresh failed ({e}) — keeping previous")

    def refresh_priority_criteria(self) -> None:
        """
        Load VALIDATED, categorical `FEATURE_FILTER` findings from
        `brain_proposals` into the same-shape cache `_confirmation_key`
        reads — 22-Aug-2026, F-50.

        Same refresh discipline as `refresh_priors()`: read on the slow
        timer (`intraday/run.py`'s 300s tick), never inside the 15s
        decision loop, so a Supabase read never sits on the hot path a
        "Pure." function is documented to never touch.

        FAVOURABLE FINDINGS ONLY. `feature_edge_study.py`'s categorical
        splits can validate in either direction — a category the data
        prefers (SDN/auto, 85% win) or one it avoids (GAP/i.t., 6% win).
        This cache holds only the first kind. The operator's own
        instruction was explicit: "add it as priority criteria and not
        the hard filter to block everything" — de-prioritising a category
        is functionally a soft block by another name, a materially
        different and riskier mechanic than "prefer this when there is a
        choice", and was not asked for. Avoid-findings stay visible via
        `tradeos learn show`, for a human to act on directly.

        NUMERIC FINDINGS ARE NOT READ HERE, DELIBERATELY. A validated
        numeric split (e.g. GAP/atr_pct_daily) needs the tercile boundary
        that produced it to be matched consistently against a live
        candidate; that boundary exists today only inside a human-readable
        `evidence` string, not a structured field. Reading it back
        correctly is a real, separate piece of work — see this function's
        own test file for the explicit refusal to guess at parsing it.
        """
        try:
            rows = (self.sb.table("brain_proposals").select("target_key,current_value")
                     .eq("status", "VALIDATED").eq("proposal_type", "FEATURE_FILTER")
                     .eq("current_value", "favourable")
                     .order("id").execute().data or [])
        except Exception as e:
            logger.warning(f"  allocator: priority-criteria refresh failed ({e}) — keeping previous")
            return
        self._priority_criteria = P.build_priority_criteria(rows)

    def score_hypothetical(self, symbol: str, entry: float, stop: float, target: float,
                           qty: int, product: str, source: str,
                           native_rank: float = 0.0, direction: str = "LONG") -> float | None:
        """
        Edge for a plan that has not triggered yet, same pipeline as a live one.

        Feeds swing_assignment()'s reservation field: an untriggered candidate
        needs to be compared against the bar in the same units as a triggered
        proposal, which means the same prior lookup and the same cost model,
        not an approximation.
        """
        pri = self._prior_for(Proposal(symbol=symbol, framework="SWING", product=product,
                                       entry=entry, stop=stop, target=target, quantity=qty,
                                       source=source, native_rank=native_rank, direction=direction))
        days, _ = self._hold_days.get("SWING", (1.0, 0))
        return S.score(entry, stop, target, qty, product, pri, days,
                       direction=direction).get("edge")

    def expected_r_for(self, p: Proposal) -> float | None:
        """
        The prior mean R this proposal WOULD be priced on, or None when no
        rung of the ladder has a usable sample.

        Exists so a caller outside this module can ask "which of these
        competing setups has the better measured record" without building a
        second lookup. `intraday/engine.py` uses it to arbitrate between two
        engines firing on the SAME symbol, a choice that was previously made
        on raw `confidence` — a number measured 19-Aug-2026 to be inverted for
        SDN/PDL/VWR and noise for ORB at n=1030, and which in any case means
        something different in every engine that computes it.

        Returns None rather than 0.0 for an absent prior, and the distinction
        is the whole point: 0.0 is a MEASURED flat expectation, absence is no
        opinion, and a caller that collapsed them would rank an unmeasured
        engine ahead of one measured slightly negative. Same rule as
        `_row_gross_r` and `confidence_band`.
        """
        pri = self._prior_for(p)
        return pri.mean_r if (pri is not None and pri.usable) else None

    def _prior_for(self, p: Proposal):
        if self._priors is None:
            self.refresh_priors()
        pri = self._priors or {}

        # A BELOW-FLOOR PRIOR IS STILL A `Prior` OBJECT, AND OBJECTS ARE
        # TRUTHY. `pri.get(key) or pri.get(fallback)` therefore never falls
        # through when `key` exists with even one observation — it falls
        # through only when the key is ABSENT. `swing_priors()` populates a
        # key for every `signal_type` with at least one row, below-floor or
        # not, so a thin-but-present class prior (the exact case the
        # docstring below describes as "a missing class prior falls back to
        # the book") could never reach that fallback. `planned_stop` was only
        # populated from 28-Jul-2026, so almost every swing engine bucket is
        # currently below the 30-sample floor: every one of them resolved to
        # a NEUTRAL prior (e_r = 0 - cost_r, always negative) instead of the
        # book's own usable distribution, which can never clear a positive
        # hurdle. `_usable()` checks the flag explicitly instead of relying
        # on truthiness.
        def _usable(key: str):
            got = pri.get(key)
            return got if got is not None and got.usable else None

        # SHORT PROPOSALS MUST NEVER FALL BACK TO THE LONG POPULATION.
        #
        # scoring.intraday_priors() keys a short engine's distribution as
        # "{framework}/{source}/SHORT" — a genuinely different population, per
        # its own docstring: different base rate, different failure mode, a
        # different tail. Before this branch existed every proposal was LONG,
        # so a two-step ladder (class, then book) was complete. It stopped
        # being complete the moment a Proposal could carry direction="SHORT":
        # unmodified, this ladder would look up "INTRADAY/SDN" — a key that
        # does not exist for the short-only SDN family — miss, and fall
        # through to "INTRADAY/ALL", which is now explicitly the LONG-only
        # book-level distribution. A short would be scored against a
        # population that excludes every short ever observed, which is the
        # exact cross-class borrowing this docstring's own next line forbids.
        # THE ENGINE'S OWN RECORD FIRST, THEN ITS FAMILY'S -- 18-Aug-2026.
        #
        # `p.source` is the FAMILY (proposal.from_intraday sets it from
        # family_of()). Pricing on the family alone means GAP is scored on
        # ORB's record: over 1,766 TAKEN-and-resolved rows, structural-stop
        # only, GAP is +0.587R (n=144) and ORB is -0.534R (n=186) -- and they
        # are the same family. Under `alloc_edge_absolute_floor` that is the
        # difference between a proposal that clears zero and one that never
        # can, decided by evidence belonging to another engine.
        #
        # The family remains the FALLBACK, which is what makes the merge
        # still worth having: a new or thin engine inherits its family's
        # sample rather than dropping to the whole book. `_usable()` gates
        # each rung on the sample floor, so an engine only prices on itself
        # once it has earned the observations.
        sub = str((p.meta or {}).get("sub_engine") or p.source or "").upper()

        # ── THE BAND RUNG — 19-Aug-2026 ─────────────────────────────────────
        #
        # `native_rank` is the setup's own confidence x100 (proposal.
        # from_intraday), so the confidence the engine assigned is recoverable
        # here without widening Proposal. Banded FIRST because it is the most
        # specific evidence available: "what this engine's setups at THIS
        # confidence level have actually returned", rather than that engine's
        # record pooled across confidence levels it does not treat alike.
        #
        # THE BAND IS COMPUTED BY THE SAME FUNCTION THAT BUILT THE KEY.
        # scoring.confidence_band() is called on both sides on purpose — see
        # its docstring for the two occasions this repository has already
        # shipped a builder and a consumer that disagreed about a key, each
        # time producing a feature that was entirely inert and silent about
        # it. `_usable()` still gates this rung on the sample floor, so a band
        # with too few observations falls straight through to the un-banded
        # engine key, which is exactly today's behaviour.
        #
        # When `alloc_intraday_confidence_bands` is false, band_edges() is
        # never consulted: confidence_band() returns None, no rung is
        # inserted, and this ladder is the one that shipped before.
        band = (S.confidence_band(p.native_rank / 100.0)
                if cfg_bool("alloc_intraday_confidence_bands", False)
                and p.native_rank is not None else None)
        banded = f"{sub}{S.BAND_SEP}{band}" if band else None

        # ── ESTABLISHED VS ADMITTED — Stage D2h, 24-Aug-2026 ─────────────────
        #
        # `p.meta["universe_population"]` (proposal.py::from_intraday(),
        # threaded from Setup.meta, stamped at detection by registry.py) —
        # "bench"/"population_a" for a name build_universe() or its own
        # live-ATR requalification vetted with real stock_data_daily
        # history; "population_b"/"population_c_*" for a name Track D's
        # wider universe admitted with NONE. scoring.py's own "ESTABLISHED
        # VS ADMITTED" section is the other half of this — it builds the
        # `/ADMITTED`-suffixed keys this ladder now tries first for one.
        #
        # ADMITTED NEVER FALLS THROUGH TO ESTABLISHED'S OWN NUMBERS. Same
        # rule the SHORT ladder above already enforces for direction — "a
        # missing OR below-floor class prior is NOT borrowed from a
        # neighbour" applies exactly as much across this axis. An admitted
        # proposal with no prior of its own yet reaches the SAME neutral
        # cold-start distribution the rest of this function reaches for
        # everyone — never established's prior, which would silently
        # re-introduce the contamination this split exists to prevent.
        pop = str((p.meta or {}).get("universe_population") or "bench")
        admitted = pop.startswith("population_b") or pop.startswith("population_c")

        if (p.direction or "LONG").upper() == "SHORT":
            if admitted:
                return (_usable(f"{p.framework}/{sub}/ADMITTED/SHORT")
                        or _usable(f"{p.framework}/ALL/ADMITTED/SHORT")
                        or S._dist(f"{p.framework}/NONE/ADMITTED/SHORT", [], floor=10**9))
            return ((_usable(f"{p.framework}/{banded}/SHORT") if banded else None)
                    or _usable(f"{p.framework}/{sub}/SHORT")
                    or _usable(f"{p.framework}/{p.source}/SHORT")
                    or _usable(f"{p.framework}/ALL/SHORT")
                    or S._dist(f"{p.framework}/NONE/SHORT", [], floor=10**9))

        if admitted:
            return (_usable(f"{p.framework}/{sub}/ADMITTED")
                    or _usable(f"{p.framework}/ALL/ADMITTED")
                    or S._dist(f"{p.framework}/NONE/ADMITTED", [], floor=10**9))

        # Most specific first, then the book, then neutral. A missing OR
        # below-floor class prior is NOT borrowed from a neighbour — it falls
        # back to the book's own distribution and is flagged, because an
        # invented prior is indistinguishable from a measured one downstream.
        return ((_usable(f"{p.framework}/{banded}") if banded else None)
                or _usable(f"{p.framework}/{sub}")
                or _usable(f"{p.framework}/{p.source}")
                or _usable(f"{p.framework}/ALL")
                or S._dist(f"{p.framework}/NONE", [], floor=10**9))

    # ── the decision ───────────────────────────────────────────────────────
    def select(self, proposals: list[Proposal], *, regime: str = "NEUTRAL",
               slots_left: int = 0, minutes_left: int = 0,
               open_positions: list[dict] | None = None,
               field: list[dict] | None = None,
               slots_by_framework: dict[str, int] | None = None,
               max_slots_by_framework: dict[str, int] | None = None) -> list[dict]:
        """
        One cycle. Pure arithmetic over in-memory data — microseconds, no I/O
        beyond the prior cache, and no synchronous write anywhere.

        SLOTS ARE PER BOOK. `slots_left` was one pooled number handed to both
        policies, and it was wrong in both directions at once: the caller
        computed it as `alloc_max_slots(2) - every position entered today in
        EITHER book`, so a single swing entry in the morning capped the intraday
        book — whose own governance allows four new positions a day — at one for
        the rest of the session, and two entries of any kind capped it at zero.
        Meanwhile the same number was passed to each policy independently, so
        the "pooled" budget was never enforced jointly either: each book could
        take up to it. Over-restrictive within a book, under-restrictive across
        them, and invisible in either book's own logs.

        Each book now brings its own budget, from its own configured cap.
        `slots_left` remains as the fallback for callers that have not been
        updated, so nothing silently loses its limit.
        """
        if not proposals:
            return []
        bucket = H.regime_bucket(regime)
        out: list[dict] = []

        for fw in ("SWING", "INTRADAY"):
            book = [p for p in proposals if p.framework == fw]
            if not book:
                continue
            fw_slots = (slots_by_framework or {}).get(fw, slots_left)
            fw_max   = (max_slots_by_framework or {}).get(fw)
            bar, inputs = H.hurdle(bucket, fw_slots, minutes_left, fw, self.sb,
                                   max_slots=fw_max)
            days, n_days = self._hold_days.get(fw, (1.0, 0))

            scored = []
            for p in book:
                pri = self._prior_for(p)
                # direction=p.direction: without it every SHORT proposal was
                # scored as LONG. scoring.score()'s own coherence check then
                # read a short's stop (above entry) as "incoherent", returned
                # edge=None, and policies.intraday_stopping declines a None
                # edge as "not scoreable" — every short refused before its
                # prior or its cost was ever weighed, silently, because the
                # DEFAULT direction absorbed the missing argument instead of
                # raising.
                # SWING ONLY — per-engine-family hold_days instead of the
                # book-pooled figure, so a family whose setups genuinely take
                # longer to resolve is not divided by the same divisor as one
                # that resolves quickly. See allocation/swing_hold_days.py for
                # the full reasoning, and _hold_days_for_proposal() above for
                # the proof that INTRADAY's line is never touched.
                p_days, p_n = _hold_days_for_proposal(
                    fw, p.source, days, n_days, self._swing_hold_days_by_family)
                # engine_family=p.source, market_state=regime — both optional,
                # both pass straight through to regime_fit_multiplier(),
                # which is 1.0 (no-op) unless intraday_regime_fit_weight is
                # deliberately raised above 0. `regime` here is whatever this
                # evaluate() call was given, which for the live intraday path
                # is mc.state (RISK_ON/NEUTRAL/CAUTION/RISK_OFF) — see
                # engine.py::_allocate_shadow. p.source is the engine FAMILY
                # for intraday proposals (from_intraday()) and the swing
                # family for swing ones; ENGINE_ARCHETYPE only recognises the
                # former, so a swing proposal here always reads "unclassified
                # — no opinion" and this is a genuine no-op for that book.
                sc = S.score(p.entry, p.stop, p.target, p.quantity, p.product,
                             pri, p_days, direction=p.direction,
                             engine_family=p.source, market_state=regime)
                scored.append({"proposal": p, "symbol": p.symbol, **sc,
                               "hold_days_n": p_n})

            policy = P.swing_assignment if fw == "SWING" else P.intraday_stopping
            # getattr, not self._priority_criteria directly — several tests
            # in this codebase build an Allocator via `Allocator.__new__
            # (Allocator)` and set only the attributes their scenario
            # needs, bypassing __init__ entirely (see
            # test_engine_fairness_and_bands.py). A missing attribute
            # there must read as "no criteria yet", the same fail-open
            # shape self._priors already has via `if self._priors is None`.
            verdicts = (policy(scored, bar, fw_slots, field) if fw == "SWING"
                        else policy(scored, bar, fw_slots,
                                    inputs.get("bar_before_floor"),
                                    getattr(self, "_priority_criteria", None)))
            label_bar = inputs.get("label_bar")
            for v in verdicts:
                v["hurdle"] = (None if bar in (float("inf"), float("-inf"))
                               else round(bar, 5))
                v["hurdle_inputs"] = inputs
                v["regime_bucket"] = bucket
                # ── THE PICK LABEL — 19-Aug-2026 ────────────────────────────
                #
                # Additive to the verdict already decided above; changes
                # nothing about TAKE vs DECLINE. `label_bar` is a STRICTER,
                # time-aware quantile of the same arrival population `bar`
                # itself is drawn from — see allocation/hurdle.py's own header
                # comment on why this exists as a label rather than a second
                # gate. Only meaningful for something actually TAKEN; a
                # DECLINE has no pick to label. `label_bar is None` covers both
                # "the switch is off" and "no edges population existed to
                # build one from" — in either case this stays silent rather
                # than guessing.
                if fw == "INTRADAY" and v.get("verdict") == P.TAKE and label_bar is not None:
                    edge = v.get("edge")
                    v["pick_label"] = ("TOP_PICK" if edge is not None and edge >= label_bar
                                       else "EXPLORATION")
            out += verdicts

        out = self._basket_recheck(out, open_positions or [])
        self._age_deferrals(out)
        self._buffer += [self._record(v) for v in out]
        return out

    # ── basket recheck ─────────────────────────────────────────────────────
    def _basket_recheck(self, verdicts: list[dict], open_positions: list[dict]) -> list[dict]:
        """
        Do the selections, TAKEN TOGETHER, breach a constraint?

        The existing checker compares ONE proposal against held positions.
        Nothing has ever compared two simultaneous selections against each
        other — so two names in the same sector could each pass a sector cap
        individually and breach it jointly, and the breach would appear one
        cycle later as a position that should not exist.

        Drops the weakest until the basket holds. Weakest by edge, not by
        arrival order: which two of three were chosen is a decision, and
        letting it fall out of dict ordering would make it unreconstructable.
        """
        if not cfg_bool("alloc_basket_recheck", True):
            return verdicts
        taken = [v for v in verdicts if v["verdict"] == TAKE]
        if len(taken) < 2:
            return verdicts

        cap = cfg_float("portfolio_sector_cap_pct", 35.0)
        from config import TOTAL_CAPITAL
        cap_value = TOTAL_CAPITAL * cap / 100.0

        by_sector: dict[str, float] = {}
        for pos in open_positions:
            sec = str(pos.get("sector") or "").lower()
            by_sector[sec] = by_sector.get(sec, 0.0) + float(pos.get("invested_value") or 0)

        taken.sort(key=lambda v: -(v.get("edge") or 0))
        kept = set()
        for v in taken:
            p: Proposal = v["proposal"]
            sec = str(p.meta.get("sector") or "").lower()
            after = by_sector.get(sec, 0.0) + p.value
            if sec and after > cap_value:
                v["verdict"] = DECLINE
                v["reason"] = (f"basket recheck: taking this together with the "
                               f"{len(kept)} already selected would put sector "
                               f"'{sec}' at Rs {after:,.0f} against a Rs {cap_value:,.0f} "
                               f"cap. Individually it passed; together it does not")
                continue
            by_sector[sec] = after
            kept.add(p.key())
        return verdicts

    # ── DEFER lifecycle ────────────────────────────────────────────────────
    def _age_deferrals(self, verdicts: list[dict]) -> None:
        """
        DEFER has a defined lifecycle, not an implicit one.

        Re-arms on a cadence, is invalidated by a bounded drift from the price
        it was deferred at, and expires at a stated time. Without all three a
        deferral is a leak: the proposal is neither taken nor refused, it simply
        stops being anybody's problem, and it never resolves into evidence.
        """
        drift_pct = cfg_float("alloc_defer_max_drift_pct", 0.75)
        now = datetime.now(IST)
        for v in verdicts:
            if v["verdict"] != DEFER:
                continue
            p: Proposal = v["proposal"]
            prev = self._deferred.get(p.key())
            if prev:
                moved = abs(p.entry - prev["entry"]) / prev["entry"] * 100.0
                if moved > drift_pct:
                    v["verdict"] = DECLINE
                    v["reason"] = (f"deferred at {prev['entry']:.2f}, now {p.entry:.2f} "
                                   f"({moved:.2f}% away) — this is no longer the "
                                   f"proposal that was deferred")
                    self._deferred.pop(p.key(), None)
                    continue
            self._deferred[p.key()] = {"entry": p.entry, "at": now}

    # ── recording ──────────────────────────────────────────────────────────
    def _record(self, v: dict) -> dict:
        p: Proposal = v["proposal"]
        return {
            "decided_at": datetime.now(IST).isoformat(),
            "symbol": p.symbol, "framework": p.framework, "product": p.product,
            "direction": p.direction,
            "source": p.source, "verdict": v["verdict"], "reason": v.get("reason"),
            # `source` is the FAMILY (proposal.from_intraday sets it that
            # way), so GAP/PDL/ORB and PBK/VWR are indistinguishable in every
            # report built from this table — confirmed 20-Aug-2026 while
            # trying to compare GAP's own day against ORB's and finding no
            # way to. `sub_engine` is the same field the prior ladder and
            # this session's arbitration already key on
            # (allocator._prior_for), so this is read visibility, not new
            # plumbing: intraday_setups.meta.sub_engine, `p.meta` (from
            # from_intraday), copied through here.
            "sub_engine": (p.meta or {}).get("sub_engine"),
            "entry": p.entry, "stop": p.stop, "target": p.target,
            "quantity": p.quantity,
            "edge": v.get("edge"), "e_r": v.get("e_r"), "cost_r": v.get("cost_r"),
            "r_target": v.get("r_target"), "friction": v.get("friction"),
            "hold_days": v.get("hold_days"), "prior_n": v.get("prior_n"),
            "prior_below_floor": v.get("prior_floor"),
            "hurdle": v.get("hurdle"),
            # NATIVE DICT, NOT A JSON STRING — same fix as intraday/engine.py
            # ::_record_setup's meta field, 20-Aug-2026, and found the same
            # way: jsonb_typeof(hurdle_inputs)='string' on every row this
            # session ever wrote, confirmed live. hurdle_inputs is a jsonb
            # column; the client already serializes a dict into it natively.
            # json.dumps() first meant every row stored a JSON STRING inside
            # the jsonb column — hurdle_inputs->>'floor_only_rank' (this
            # session's own diagnostic queries) returned NULL on every row,
            # not because the rank was absent. round-tripped through loads
            # (default=str still sanitises non-JSON-native values) to land
            # on a plain dict instead of a second layer of string.
            "hurdle_inputs": json.loads(json.dumps(v.get("hurdle_inputs") or {}, default=str)),
            # The bucket the bar was drawn for, as a COLUMN rather than only
            # inside hurdle_inputs. _empirical_base segments the arrival
            # distribution on it, and a segmentation that has to parse JSON to
            # filter is one nobody will keep working.
            "regime_bucket": v.get("regime_bucket"),
            "native_rank": p.native_rank,
            "shadow": not cfg_bool(f"alloc_live_{p.framework.lower()}", False),
            "meta": json.dumps(p.meta, default=str),
        }

    def flush(self) -> int:
        """
        Write the buffer. CALLED ON THE SLOW TIMER, NEVER IN THE CYCLE.

        A synchronous write inside the decision loop puts a network round trip
        in front of exit evaluation on live positions. The catch-and-continue
        wrapper protects against a write FAILING; it does nothing about a write
        being SLOW, and slow is the failure that costs money here.
        """
        if not self._buffer:
            return 0
        rows, self._buffer = self._buffer, []
        try:
            self.sb.table("allocation_decisions").insert(rows).execute()
            return len(rows)
        except Exception as e:
            logger.error(f"  allocator: flush of {len(rows)} verdict(s) FAILED: {e}")
            # Loud, not swallowed. Buffered writes that vanish leave silent holes
            # in the promotion evidence, and the promotion gate is denominated in
            # exactly these rows.
            return 0
