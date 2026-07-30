"""
Which of today's buyable plans deserves the day's limited entries.

WHY final_score ALONE WAS NOT ENOUGH
------------------------------------
The pipeline computes 114 columns per plan across 27 steps — an AI tier and its
conviction, an expected R, a live implied R:R, a validity score, entry timing,
institutional flow, sector rank. Auto-entry then sorted on final_score and threw
the rest away.

final_score is the SCREENER's verdict: is this a good stock in a good setup. It
is computed before the AI reviews it, before the R:R is measured at today's
price, and before entry timing is classified. So two plans can share a score of
80 while one is a TIER_1 at 2.4R with OPTIMAL timing and the other a
WATCH_CLOSELY at 0.9R that the AI declined to promote. Ranking them equal, then
taking whichever sorts first, discards the most expensive analysis in the system.

WHAT THIS DOES NOT DO
---------------------
It does not decide whether a plan is takeable — decide() does that, and nothing
here can promote a WAIT into a BUY. This only orders the ones already cleared,
because the daily cap means the question is never "is this good" but "is this
the best use of the two entries I have".

EVERY SCORE CARRIES ITS REASONING
---------------------------------
rank() returns why, in words, for each component. A ranking you cannot explain
is one you cannot correct, and the reasoning belongs on the dashboard next to
the trade rather than in a log nobody reads.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg_float

# AI tier is a bucket, not a number. These are the weights it contributes.
# WATCH_CLOSELY is the model's default when it does not promote a name, so it
# earns nothing rather than being penalised — most plans carry it, and treating
# the common case as a demerit would just re-rank on the AI's willingness to
# commit rather than on the plan.
_TIER_POINTS = {
    "TIER_1": 20.0,
    "TIER_2": 12.0,
    "TIER_3": 4.0,
    "WATCH_CLOSELY": 0.0,
}

_TIMING_POINTS = {
    "OPTIMAL": 8.0,
    "EARLY": 2.0,
    "LATE": -6.0,       # chasing costs more than waiting
    "EXTENDED": -10.0,
}


@dataclass
class Ranked:
    symbol: str
    total: float
    components: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    def why(self) -> str:
        """One line, ordered by contribution — the largest reason first."""
        return " · ".join(self.reasons[:4]) if self.reasons else "base score only"


def _f(v, d=0.0) -> float:
    try:
        if v is None:
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def score_plan(p: dict) -> Ranked:
    """
    Rank one plan. Higher is better. Nothing here is a gate.

    Deliberately additive and flat rather than multiplicative: a plan missing a
    field should lose that field's contribution, not have its whole score
    zeroed. Several of these columns are genuinely absent on some rows — an AI
    tier is only written when the AI ran — and a ranking that collapses on a
    NULL would silently reorder the book on a day the AI timed out.
    """
    comp: dict[str, float] = {}
    reasons: list[str] = []

    # ── base: the screener's own composite ──────────────────────────────────
    base = _f(p.get("final_score"), _f(p.get("score"), 50.0))
    comp["screener"] = base
    reasons.append(f"screener {base:.0f}")

    # ── the AI's verdict, which was being discarded entirely ────────────────
    tier = str(p.get("ai_tier") or "").upper()
    tp = _TIER_POINTS.get(tier, 0.0) * cfg_float("rank_weight_tier", 1.0)
    if tp:
        comp["ai_tier"] = tp
        reasons.append(f"{tier} +{tp:.0f}")

    conv = _f(p.get("ai_conviction"))
    if conv:
        # Stored 0-1 or 0-100 depending on the model run; normalise both.
        conv01 = conv / 100.0 if conv > 1.0 else conv
        cp = (conv01 - 0.5) * 20.0 * cfg_float("rank_weight_conviction", 1.0)
        comp["ai_conviction"] = cp
        reasons.append(f"AI conviction {conv01:.0%} {cp:+.0f}")

    # ── reward per unit of risk, measured at TODAY's price ──────────────────
    # implied_rr is the live figure; expected_r is the plan's own estimate.
    # Preferring the live one matters because a plan that has already run is a
    # worse trade than it was when written, and only implied_rr knows that.
    rr = _f(p.get("implied_rr")) or _f(p.get("expected_r"))
    if rr:
        # CLAMPED, because an unbounded reward term lets one implausible number
        # decide where the day's capital goes.
        #
        # KIMS came through with implied_rr 19.67, worth +145 points — more than
        # the entire screener score — and ranked first on that alone. A 19x
        # reward-to-risk on a swing plan is not a great trade, it is a stop
        # computed too close or a target computed too far, and either way it is a
        # data fault being rewarded as if it were an edge.
        #
        # Above the cap the difference stops mattering anyway: 4R and 19R are
        # both "more than enough", and what separates them is noise in the stop.
        rr_cap = cfg_float("rank_rr_cap", 4.0)
        rp = (min(rr, rr_cap) - 1.5) * 8.0 * cfg_float("rank_weight_rr", 1.0)
        comp["rr"] = rp
        reasons.append(f"R:R {rr:.2f}{'(capped)' if rr > rr_cap else ''} {rp:+.0f}")

    # ── entry timing: how much of the move is already gone ──────────────────
    timing = str(p.get("entry_timing_type") or "").upper()
    tmp = _TIMING_POINTS.get(timing, 0.0) * cfg_float("rank_weight_timing", 1.0)
    if tmp:
        comp["timing"] = tmp
        reasons.append(f"{timing} timing {tmp:+.0f}")

    # ── is the setup still valid, and is the sector working ─────────────────
    val = _f(p.get("validity_score"))
    if val:
        vp = (val - 60.0) / 5.0 * cfg_float("rank_weight_validity", 1.0)
        comp["validity"] = vp
        if abs(vp) >= 1:
            reasons.append(f"validity {val:.0f} {vp:+.0f}")

    srank = _f(p.get("sector_rank_at_entry"))
    if srank:
        # Rank 1 is the strongest sector. Only the top few earn anything.
        sp = max(0.0, (6.0 - srank)) * cfg_float("rank_weight_sector", 1.0)
        if sp:
            comp["sector"] = sp
            reasons.append(f"sector #{srank:.0f} +{sp:.0f}")

    inst = _f(p.get("institutional_score"))
    if inst:
        ip = (inst - 50.0) / 10.0 * cfg_float("rank_weight_institutional", 1.0)
        comp["institutional"] = ip
        if abs(ip) >= 1:
            reasons.append(f"institutional {inst:.0f} {ip:+.0f}")

    # ── penalties for things that make a good plan a bad trade today ────────
    if str(p.get("pre_results_flag") or "").upper() in ("1", "TRUE", "YES"):
        comp["event"] = -15.0
        reasons.append("results imminent -15")

    days = _f(p.get("upcoming_event_days"), 99)
    if 0 < days <= 2:
        comp["event_soon"] = -10.0
        reasons.append(f"event in {days:.0f}d -10")

    if str(p.get("asm_flag") or "").upper() not in ("", "NONE", "NULL", "FALSE"):
        comp["asm"] = -12.0
        reasons.append("ASM/GSM -12")

    total = round(sum(comp.values()), 2)
    # Largest absolute contributions first, so `why` leads with what decided it.
    reasons.sort(key=lambda r: -abs(comp.get(r.split()[0].lower(), 0.0)))
    return Ranked(symbol=p.get("symbol") or "?", total=total,
                  components=comp, reasons=reasons)


def rank(plans: list[dict]) -> list[Ranked]:
    """Best first."""
    return sorted((score_plan(p) for p in plans), key=lambda r: -r.total)
