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


class Allocator:
    """Stateless per cycle apart from the write buffer and DEFER book."""

    def __init__(self, sb=None):
        self.sb = sb or get_supabase()
        self._buffer: list[dict] = []
        self._deferred: dict[tuple, dict] = {}
        self._priors: dict | None = None
        self._hold_days: dict[str, tuple[float, int]] = {}

    # ── priors, refreshed on the slow timer rather than per cycle ──────────
    def refresh_priors(self) -> None:
        try:
            self._priors = {**S.intraday_priors(self.sb), **S.swing_priors(self.sb)}
            for fw in ("SWING", "INTRADAY"):
                self._hold_days[fw] = S.expected_hold_days(self.sb, fw)
        except Exception as e:
            logger.warning(f"  allocator: prior refresh failed ({e}) — keeping previous")

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
        if (p.direction or "LONG").upper() == "SHORT":
            return (_usable(f"{p.framework}/{p.source}/SHORT")
                    or _usable(f"{p.framework}/ALL/SHORT")
                    or S._dist(f"{p.framework}/NONE/SHORT", [], floor=10**9))

        # Most specific first, then the book, then neutral. A missing OR
        # below-floor class prior is NOT borrowed from a neighbour — it falls
        # back to the book's own distribution and is flagged, because an
        # invented prior is indistinguishable from a measured one downstream.
        return (_usable(f"{p.framework}/{p.source}")
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
                sc = S.score(p.entry, p.stop, p.target, p.quantity, p.product,
                             pri, days, direction=p.direction)
                scored.append({"proposal": p, "symbol": p.symbol, **sc,
                               "hold_days_n": n_days})

            policy = P.swing_assignment if fw == "SWING" else P.intraday_stopping
            verdicts = (policy(scored, bar, fw_slots, field) if fw == "SWING"
                        else policy(scored, bar, fw_slots))
            for v in verdicts:
                v["hurdle"] = (None if bar in (float("inf"), float("-inf"))
                               else round(bar, 5))
                v["hurdle_inputs"] = inputs
                v["regime_bucket"] = bucket
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
            "entry": p.entry, "stop": p.stop, "target": p.target,
            "quantity": p.quantity,
            "edge": v.get("edge"), "e_r": v.get("e_r"), "cost_r": v.get("cost_r"),
            "r_target": v.get("r_target"), "friction": v.get("friction"),
            "hold_days": v.get("hold_days"), "prior_n": v.get("prior_n"),
            "prior_below_floor": v.get("prior_floor"),
            "hurdle": v.get("hurdle"),
            "hurdle_inputs": json.dumps(v.get("hurdle_inputs") or {}, default=str),
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
