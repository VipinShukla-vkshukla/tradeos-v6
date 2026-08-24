"""
Stage D6 — templated shadow engines instantiated from an approved
tools.discover_engines candidate, no human writing code. docs/TRADEOS_
ROADMAP.md, Track D, Stage D6. Branch feat/intraday-evolution.

WHAT THIS DOES AND DOES NOT DO
--------------------------------
tools/discover_engines.py::moved_but_unseen() (Pass B) measures, from
end-of-day summary data (stock_data_daily, one row per symbol-day),
whether a prior-day condition preceded a big intraday move that no engine
caught. That measurement identifies a POPULATION worth testing — it is
NOT, on its own, an intraday entry rule. GDB (intraday/strategies/
gap_down_bounce.py) is the one instance of a human turning such a finding
into a real engine, and its own docstring is explicit that doing so
needed genuine judgment: which existing mechanism to reuse (VWR's VWAP-
reclaim), how to bound the gap, where to place the stop. A template
cannot invent that judgment — it can only reuse the SAME already-proven
mechanism GDB reused, pointed at whichever population a candidate names.

So every templated candidate shares ONE fixed, generic shape:

  FILTER    the discovered daily-bar condition, translated onto live
            SymbolContext fields (FEATURE_TRANSLATORS below) — the SAME
            predicate discover_engines.py itself tested, not a re-
            derived approximation. `None` means the field has not been
            captured yet, distinct from the condition being false — the
            same "no opinion" rule every gate in this codebase applies to
            missing data.
  TRIGGER   a single-bar VWAP reclaim — the identical, already-proven
            live confirmation GDB reused from vwap_reclaim.py, including
            its 10-Aug-2026 single-bar-crossing fix. Most of the 11
            discovered conditions (ADX, delivery%, RS-vs-NIFTY, ATR,
            volume ratio) describe YESTERDAY and do not change intraday —
            without a live trigger the condition would fire on every
            evaluation of every qualifying name all session, which is a
            static filter, not a detection.
  STOP      structural: the swing low made while price sat below VWAP
            today, via risk_from_structure() — GDB's own stop mechanic,
            reused verbatim, including its refuse-rather-than-tighten
            contract.
  TARGET    a fixed R-multiple (candidate_target_r). Deliberately simpler
            than any hand-tuned engine's target logic — GDB's day-high-
            aware target is GDB-specific tuning, not something a template
            can honestly claim to reuse. The point of shadow here is
            testing whether auto-generated code runs and detects
            sensibly, a lower bar than testing whether it trades well
            (see docs/TRADEOS_ROADMAP.md Stage D6's own "why shadow"
            paragraph).

LONG ONLY, DELIBERATELY. The raw feature name never specifies direction —
GDB's own docstring warns about this explicitly ("gap down" names the
condition, not the trade). A population that made a big move and closed
strong (`closed_strong_rate` in a candidate's own structured evidence) is
real evidence for testing long-side follow-through; stock_data_daily's
one-row-per-day shape cannot see an intraday reversal, so a SHORT read
would rest on an inference the data genuinely cannot confirm. Raised with
the operator directly (24-Aug-2026) rather than assumed.

"gap down > 1%" IS EXCLUDED, DELIBERATELY — GDB already covers exactly
this condition (see gap_down_bounce.py's own docstring for its origin,
brain_proposals#190). Templating a second shadow engine for the identical
population GDB already trades would test nothing new; `_ALREADY_COVERED`
below is where any future such overlap gets named.

APPROVAL IS A SEPARATE, EXPLICIT HUMAN ACTION — tools/approve_candidate.py,
which calls the EXISTING swing/brain/backtester_and_change_manager.py::
approve_proposal(), not a new status invented for this stage.
tools/proposal_backtest.py's own docstring already establishes why
ENGINE_CANDIDATE proposals never reach status=VALIDATED through the
existing automated out-of-sample re-check ("proposes a pattern with NO
engine built yet — nothing exists to replay") — but `status=APPROVED`
already IS this proposal type's real, precedented human-approval
mechanism (verified live: proposals #188/#190 became GDB this way), safe
specifically because ENGINE_CANDIDATE sits in that module's own
REVIEW_ONLY set — apply_proposal() acknowledges and returns for anything
in it, never reaching the system_config/strategy_config write path.
`from_proposal()` below does not itself check status — it only asks "can
this row's evidence be templated at all"; `intraday/candidate_shadow.py::
load_active_candidates()` is what filters to `status=APPROVED` rows.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

from intraday.session import OPENING, PRIME
from intraday.strategies.base import Setup, SymbolContext, risk_from_structure
from config import cfg_float, cfg_int


def _gap_pct(ctx: SymbolContext) -> float | None:
    """(day_open - prev_close) / prev_close * 100 — the exact quantity
    discover_engines.py's own `m["gap"]` computes from stock_data_daily,
    here read live. Shared by all three gap-based translators below."""
    if not ctx.day_open or not ctx.prev_close:
        return None
    return (ctx.day_open - ctx.prev_close) / ctx.prev_close * 100.0


# Same 11 keys as tools/discover_engines.py's own `feats` dict, same
# semantics, read from live SymbolContext instead of a stock_data_daily
# row + move-outcome dict. `move`/`closed_strong` (discover_engines.py's
# OUTCOME variables, computed AFTER the day closes) have no live analogue
# and are not translated here — they describe what already happened,
# never a condition a live trigger could fire on.
FEATURE_TRANSLATORS: dict[str, Callable[[SymbolContext], bool | None]] = {
    "gap down > 1%":
        lambda c: (lambda g: None if g is None else g <= -1.0)(_gap_pct(c)),
    "flat open +/-0.3%":
        lambda c: (lambda g: None if g is None else abs(g) <= 0.3)(_gap_pct(c)),
    "gap up > 1%":
        lambda c: (lambda g: None if g is None else g >= 1.0)(_gap_pct(c)),
    "prior volume > 1.5x":
        lambda c: None if c.vol_ratio_daily is None else c.vol_ratio_daily > 1.5,
    "prior volume < 0.8x":
        lambda c: None if c.vol_ratio_daily is None else 0 < c.vol_ratio_daily < 0.8,
    "ADX > 25 (trending)":
        lambda c: None if c.adx_daily is None else c.adx_daily > 25,
    "ADX < 18 (choppy)":
        lambda c: None if c.adx_daily is None else 0 < c.adx_daily < 18,
    "ATR > 3% (volatile)":
        lambda c: None if c.atr_pct_daily is None else c.atr_pct_daily > 3.0,
    "delivery > 60%":
        lambda c: None if c.delivery_pct_daily is None else c.delivery_pct_daily > 60,
    "RS vs NIFTY > 5":
        lambda c: None if c.rs_vs_nifty_daily is None else c.rs_vs_nifty_daily > 5,
    "extended > 8% o/50MA":
        lambda c: None if c.dist_sma50_daily is None else c.dist_sma50_daily > 8,
}

# Already has a hand-built engine — see this module's own docstring.
_ALREADY_COVERED = {"gap down > 1%": "GDB (intraday/strategies/gap_down_bounce.py)"}


@dataclass(frozen=True)
class TemplatedCandidate:
    """One approved discover_engines.py Pass B finding, wired to fire."""
    proposal_id: int
    feature_name: str
    avg_move_pct: float
    confidence: float
    lift: float
    n_miss: int

    @property
    def name(self) -> str:
        return f"CAND{self.proposal_id}"


def from_proposal(row: dict) -> tuple[TemplatedCandidate | None, str]:
    """
    Build a TemplatedCandidate from one brain_proposals row, or (None,
    reason) when it cannot — NEVER guesses at a shape the row does not
    have. The reason string is returned in both cases so a caller can log
    why a row was skipped, matching this project's "I could not compute
    this is a required finding" rule rather than a silent no-op.
    """
    target_key = row.get("target_key") or ""
    if not target_key.startswith("UNSEEN/"):
        return None, f"{target_key}: not a Pass B (UNSEEN/*) candidate"
    feature_name = target_key[len("UNSEEN/"):]

    if feature_name in _ALREADY_COVERED:
        return None, (f"{target_key}: already covered by "
                      f"{_ALREADY_COVERED[feature_name]}, skipped")

    if feature_name not in FEATURE_TRANSLATORS:
        return None, f"{target_key}: unrecognised feature name, skipped"

    evidence = row.get("evidence")
    if not isinstance(evidence, dict) or "avg_move_pct" not in evidence:
        return None, (f"{target_key}: evidence is not the structured shape "
                      f"this template reads (pre-Stage-D6 row?), skipped")

    proposal_id = row.get("id")
    if proposal_id is None:
        return None, f"{target_key}: no proposal id, skipped"

    return TemplatedCandidate(
        proposal_id=int(proposal_id),
        feature_name=feature_name,
        avg_move_pct=float(evidence.get("avg_move_pct") or 0),
        confidence=float(row.get("confidence") or 0.4),
        lift=float(evidence.get("lift") or 0),
        n_miss=int(evidence.get("n_miss") or 0),
    ), "ok"


def evaluate(candidate: TemplatedCandidate, ctx: SymbolContext, phase: str) -> Setup | None:
    """
    Generic detect: the candidate's own filter, gated by a live VWAP
    reclaim — see this module's own docstring for why every templated
    candidate shares this one trigger shape. Pure, like every other
    engine's evaluate() — no I/O, safe to call from a hot loop or a test.
    """
    if phase not in (OPENING, PRIME):
        return None
    passes = FEATURE_TRANSLATORS[candidate.feature_name](ctx)
    if not passes:                      # False or None (unknown) both skip
        return None
    if not (ctx.bars and ctx.vwap and ctx.ltp):
        return None

    lookback = cfg_int("candidate_lookback_bars", 12)
    recent = ctx.bars[-lookback:]
    if len(recent) < 4:
        return None

    below = [b for b in recent if b.close < ctx.vwap]
    min_below = cfg_int("candidate_min_bars_below", 2)
    if len(below) < min_below or ctx.ltp <= ctx.vwap:
        return None
    if not (recent[-1].close > ctx.vwap and recent[-2].close < ctx.vwap):
        return None                      # not the single-bar crossing

    swing_low = min(b.low for b in below)
    frame = risk_from_structure(
        ctx.ltp,
        swing_low * (1 - cfg_float("candidate_stop_buffer_pct", 0.10) / 100.0),
        "LONG", max_risk_pct=cfg_float("candidate_max_risk_pct", 1.10))
    if frame is None:
        return None

    target = ctx.ltp + frame.risk * cfg_float("candidate_target_r", 2.0)
    if target <= ctx.ltp:
        return None

    return Setup(
        symbol=ctx.symbol, strategy=candidate.name, direction="LONG",
        entry=round(ctx.ltp, 2), stop=round(frame.stop, 2), target=round(target, 2),
        confidence=round(candidate.confidence, 2),
        rationale=(f"templated from brain_proposals#{candidate.proposal_id} "
                   f"('{candidate.feature_name}', {candidate.lift:.1f}x lift, "
                   f"n_miss={candidate.n_miss}); VWAP reclaim after "
                   f"{len(below)} bars below"),
        invalidation=f"a close back under VWAP {ctx.vwap:.2f}",
        valid_phases=(OPENING, PRIME),
        meta={**frame.meta(), "vwap": round(ctx.vwap, 2), "swing_low": round(swing_low, 2),
              "source_proposal": f"brain_proposals#{candidate.proposal_id}",
              "feature_name": candidate.feature_name, "template": True},
    )
