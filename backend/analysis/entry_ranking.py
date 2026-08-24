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

from loguru import logger

from config import cfg_bool, cfg_float

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
    # Carried alongside the decision and auditable against outcomes, but with
    # zero weight on the rank. This is what "annotation, not an input" means in
    # practice: visible, recorded, scoreable later — and unable to move capital
    # until it has been scored.
    annotations: dict = field(default_factory=dict)

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


def live_ranking_input(p: dict, rr_live: float | None) -> dict:
    """
    Override a plan's `implied_rr` with the truly live figure before
    ranking it, when one is available.

    Track E, Stage E5, 24-Aug-2026. `score_plan()`'s own R:R term claims
    "implied_rr is the live figure... a plan that has already run is a
    worse trade than it was when written, and only implied_rr knows
    that" — but `implied_rr` is written ONLY by the evening pipeline
    (final_snapshot.py / generate_signals.py) and nothing refreshes it
    between then and a live entry decision. `analysis.trade_decision.
    decide()` computes the real thing — `rr_live`, reward:risk AT the
    live price — and it was going unused at BOTH places that rank
    candidates for a scarce entry slot: `intraday/engine.py::
    _maybe_enter_swing` and `tools/simulate.py::simulate_swing_entries`.
    Factored here once both had independently grown the identical
    override, rather than after a THIRD copy drifts from the other two —
    the exact shape `tools/simulate.py`'s incomplete exit-policy dict
    (F-71 §3) already cost a session once.

    HAL, 21-Aug-2026: zone_low drifted 4779 -> 4808 across three prior
    signal snapshots while stop/target stayed fixed. rr_at_zone_low
    ranged 7.63-14.09 depending which snapshot you read; rr_live at the
    actual fill (5010.20) was 1.17. Ranking on whichever stale
    `implied_rr` a candidate dict still carried, instead of 1.17, is
    exactly the gap this closes.

    `rr_live=None` leaves `p` untouched — a plan can legitimately have no
    live figure (e.g. a CHASE_LIMIT proposal priced off the limit rather
    than ltp, or `decide()` never having run), and the stale pipeline
    value is a better fallback than a fabricated zero.
    """
    return {**p, "implied_rr": rr_live} if rr_live is not None else p


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
    annot: dict = {}
    reasons: list[str] = []

    # ── base: the screener's own composite ──────────────────────────────────
    #
    # WEIGHTED AND CENTERED 10-Aug-2026, end to end.
    #
    # Every component below this one is a delta from a neutral midpoint,
    # scaled into single digits, gated by its own cfg_float weight.
    # final_score alone was added at its full 0-100 magnitude with NO weight
    # at all, so a screener score of 80 contributed +80 points by itself —
    # more than every other component in this function combined — regardless
    # of what R:R, timing or the AI review said about the same plan. That
    # was never "the screener matters most", it was an accounting error: the
    # term was never put on the same scale as its peers.
    #
    # The KB's own tercile measurement (knowledge_base/KNOWLEDGE_BASE.md,
    # 6-Aug) found final_score flat against forward R — mean R by tercile
    # 0.516 / 0.491 / 0.511, no monotonic separation, n=125 resolved
    # CONTINUATION plans (CTL/SEC/TPO/SBS/VBD/RSB/IAD). Centered here at 50
    # — the same mid-band boundary compute_msl itself scores against — and
    # weighted low by default: enough to still break a tie between two
    # otherwise-equal plans, not enough to let a screener score outrank a
    # live R:R or an AI tier the way the unweighted raw value used to.
    #
    # Medium confidence, single family, per the KB's own caveat. Raise
    # rank_weight_screener toward 1.0 only if a later tercile re-run (KB:
    # "re-run python -m allocation.scoring --tercile as the resolved sample
    # grows") finds separation; drop it to 0.0 if it stays flat.
    base = _f(p.get("final_score"), _f(p.get("score"), 50.0))
    bp = (base - 50.0) / 5.0 * cfg_float("rank_weight_screener", 0.3)
    comp["screener"] = bp
    reasons.append(f"screener {base:.0f} {bp:+.0f}")

    # ── the conviction layer: ANNOTATION, not a ranking input ───────────────
    #
    # DEMOTED 04-Aug-2026 (Stage 7), pending validation.
    #
    # ai_tier and ai_conviction sat at the top of the decision stack and moved
    # the rank of every plan, and no tier-by-tier forward return had ever been
    # produced from the unbiased record. An unmeasured component deciding which
    # plan gets scarce capital is unpriced risk: if the tiers carry no signal it
    # is noise weighted at 20 points, and if they carry negative signal it is
    # actively destructive — and nothing in the system could tell those apart.
    #
    # The gate for restoring it is stated and is not a matter of taste:
    # tier-by-tier forward returns from signal_output_daily's resolved outcomes,
    # which Stage 4's resolver began producing on 04-Aug-2026. Until that exists
    # the values are carried alongside the decision and audited against
    # outcomes, exactly as the architecture requires of enrichment output —
    # "never an unaudited input to ranking".
    #
    # Both weights default to 0.0. The arithmetic is preserved rather than
    # deleted so that restoring the layer is a config change and the historical
    # scores stay reconstructable.
    tier = str(p.get("ai_tier") or "").upper()
    tp = _TIER_POINTS.get(tier, 0.0) * cfg_float("rank_weight_tier", 0.0)
    if tier:
        annot["ai_tier"] = tier
    if tp:
        comp["ai_tier"] = tp
        reasons.append(f"{tier} +{tp:.0f}")

    conv = _f(p.get("ai_conviction"))
    if conv:
        # Stored 0-1 or 0-100 depending on the model run; normalise both.
        conv01 = conv / 100.0 if conv > 1.0 else conv
        annot["ai_conviction"] = round(conv01, 3)
        cp = (conv01 - 0.5) * 20.0 * cfg_float("rank_weight_conviction", 0.0)
        if cp:
            comp["ai_conviction"] = cp
            reasons.append(f"AI conviction {conv01:.0%} {cp:+.0f}")

    # ── reward per unit of risk, measured at TODAY's price ──────────────────
    # implied_rr is the live figure; expected_r is the plan's own estimate.
    # Preferring the live one matters because a plan that has already run is a
    # worse trade than it was when written, and only implied_rr knows that.
    #
    # WEIGHT REDUCED 1.0 -> 0.4, 11-Aug-2026, on the same kind of evidence
    # that moved rank_weight_screener (migration 060): this term was the
    # single largest-magnitude component left in the function once
    # final_score was rescaled, and had never itself been tested against
    # forward R. allocation.scoring.rr_tercile_report() ran the identical
    # tercile methodology tercile_report() used for final_score, on the
    # exact fallback chain this line reads (implied_rr, else expected_r):
    #
    #   n=188 resolved CONTINUATION plans (larger than final_score's n=125)
    #   LOW rr tercile:  mean R +0.561 (n=63)
    #   MID rr tercile:  mean R +0.439 (n=79)
    #   HIGH rr tercile: mean R +0.448 (n=46)
    #
    # No monotonic positive separation — if anything the LOW tercile
    # outperformed HIGH, the opposite of what full weight on this term
    # assumes. The gap (LOW-HIGH ≈ 0.11) is within roughly one combined
    # standard error, so this is NOT strong evidence the relationship is
    # truly inverted — but it is a second, independent failure (after
    # final_score) of a component to show the positive relationship its
    # weight implied, and a plausible mechanism for why: every plan reaching
    # this ranking already cleared generate_signals.py's own minimum-R:R
    # entry gate, so the residual variation ABOVE that gate is exactly the
    # KIMS 19.67 kind of noise the rr_cap below already exists to contain —
    # more of it is not obviously more edge.
    #
    # Reduced, not zeroed: unlike final_score's opaque composite, reward:risk
    # has first-principles grounding this measurement did not disprove, only
    # failed to confirm at this sample size. Medium confidence — re-run
    # `python -m allocation.scoring --rr-tercile` as the resolved sample
    # grows, the same standing instruction final_score carries in the KB.
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
        rp = (min(rr, rr_cap) - 1.5) * 8.0 * cfg_float("rank_weight_rr", 0.4)
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

    # ── THE AI'S OWN WARNING, WHICH USED TO COST NOTHING — 18-Aug-2026 ──────
    #
    # `eap_action` and `ai_risks` have been written to every plan row since the
    # AI review was built, and NEITHER reached this function. On GABRIEL the
    # 29-Jul review said "Extended RSvN could lead to mean reversion" and the
    # 03-Aug one returned eap_action = AVOID_ENTRY. The trade was taken on
    # 06-Aug at rank 91, the highest in the book, and closed at -7.86%.
    #
    # AVOID_ENTRY is a REFUSAL and is not handled here — see entry_refusals(),
    # because this function is a ranking and "nothing here is a gate" is a
    # property worth keeping. What lands here is the softer case: a review that
    # named a risk without asking for a refusal. That is worth points, not a
    # veto, and the weight is deliberately smaller than the screener term it
    # argues against so it colours the order rather than deciding it.
    if str(p.get("ai_risks") or "").strip():
        w = cfg_float("rank_w_ai_risk", 6.0)
        comp["ai_risk"] = -w
        reasons.append(f"AI flagged risk -{w:.0f}")

    total = round(sum(comp.values()), 2)
    # Largest absolute contributions first, so `why` leads with what decided it.
    reasons.sort(key=lambda r: -abs(comp.get(r.split()[0].lower(), 0.0)))
    return Ranked(symbol=p.get("symbol") or "?", total=total, annotations=annot,
                  components=comp, reasons=reasons)


def entry_refusals(p: dict, rr_live: float | None = None,
                   rr_at_zone_low: float | None = None) -> list[str]:
    """
    Reasons this plan must NOT be entered, regardless of how it ranks.

    Separate from score_plan() on purpose. That function is a ranking and its
    docstring promises "nothing here is a gate" — a promise worth keeping,
    because a refusal buried in an additive score can be outvoted by any other
    term that happens to be large that day, which is exactly how GABRIEL's
    screener score of 82 drowned out everything else about it.

    Pure and no I/O (`rr_live`/`rr_at_zone_low` are decide()'s own already-
    computed numbers, passed in rather than recomputed here), so `tools.
    simulate` and `tools.verify` reach the same verdict as the live daemon
    rather than approximating it.

    Returns an empty list when the plan may proceed.
    """
    out: list[str] = []

    # eap_action is the AI review's verdict on ENTERING, as distinct from
    # ai_tier which is its view of the OPPORTUNITY. GABRIEL carried
    # AVOID_ENTRY on 03-Aug 2026 and was bought three sessions later; nothing
    # in the entry path had ever read this column. Note the direction of the
    # asymmetry: a refusal is honoured, a recommendation is not — the AI can
    # veto a trade here but can never promote one, which is the same
    # "annotation, never promotion" rule the conviction score already follows.
    if cfg_bool("entry_rank_respect_ai_avoid", False):
        if str(p.get("eap_action") or "").strip().upper() == "AVOID_ENTRY":
            note = str(p.get("ai_note") or p.get("ai_risks") or "").strip()
            out.append("AI review returned AVOID_ENTRY"
                       + (f" — {note[:110]}" if note else ""))

    # The plan's own quality gate already refused this row tonight. GABRIEL
    # carried filter_reason `insufficient_rr_0.78x` on 03, 04 AND 05 August and
    # was bought on the 6th; the daemon never looked at the column. Only
    # refusals are honoured — `holding`, `lifecycle_reduce` and the like are
    # states, not vetoes.
    if cfg_bool("entry_respect_filter_reason", False):
        fr = str(p.get("filter_reason") or "").strip().lower()
        if fr.startswith(("insufficient_rr", "blocked", "rejected", "veto")):
            out.append(f"the evening pipeline refused this plan: {fr}")

    # ── R:R RETENTION — Track E, Stage E5 piece 2 (shadow), 24-Aug-2026 ──────
    # F-74's own finding: HAL's reward:risk collapsed from 7.63-14.09 (at the
    # zone low, stop/target fixed) to 1.17 at the actual fill — not because
    # the raw price drifted far (0.94%), but because the stop/target never
    # moved while price ran most of the way to target before the trade was
    # ever taken. F-75 investigated turning this into a hard refusal and
    # found the evidence too thin (n=16 closed positions) to set a
    # confident floor without risking a threshold no real winner clears
    # (AIIL won at 0.134 retention; TRAVELFOOD lost at 0.057 — a floor
    # anywhere between them is a guess). Shipped as a SHADOW ONLY —
    # entry_refuse_low_rr_retention stays off; this logs what a refusal
    # would have caught so the next quantify pass has real data to set
    # entry_rr_retention_floor from, rather than staying blocked on n=16
    # forever.
    if rr_live is not None and rr_at_zone_low and rr_at_zone_low > 0:
        retention = rr_live / rr_at_zone_low
        floor = cfg_float("entry_rr_retention_floor", 0.20)
        if retention < floor:
            msg = (f"R:R has retained only {retention:.0%} of its zone-low "
                   f"value ({rr_live:.2f} vs {rr_at_zone_low:.2f}) — below "
                   f"the {floor:.0%} floor")
            if cfg_bool("entry_refuse_low_rr_retention", False):
                out.append(msg)
            else:
                logger.info(f"  {p.get('symbol')}: R:R-retention shadow — "
                           f"{msg} — entry_refuse_low_rr_retention is off")

    # ── BROKEN TREND AT ENTRY — Track E, Stage E5 piece 3 (shadow),
    # 24-Aug-2026. `control.exit_rules.assess_trend()` already reads the
    # SAME pipeline evidence (structure, momentum, RS, sector) this plan
    # dict carries, and this session's own F-75 fixed its weekly_structure
    # vocabulary bug — but it had only ever been called on an ALREADY-HELD
    # position (deterioration_check, the 3R runner decision, the Stage E4
    # early-invalidation rung). Nothing ever asked it about a CANDIDATE. A
    # plan whose own trend evidence already reads BROKEN — the same bar
    # the exit-side rules already trust to cut a LOSING position — is at
    # minimum worth flagging before spending a scarce entry slot on it.
    # Reuses the existing function rather than a second, narrower
    # weekly-structure-only check — "decision reuse is load-bearing".
    # Shadow only: same thin-evidence reasoning as the R:R check above,
    # entry_refusals() has never been exercised against ENTRY candidates
    # before, so there is no track record yet to set a confident bar from.
    try:
        from control.exit_rules import assess_trend
        tq = assess_trend(p)
        if tq.verdict == "BROKEN" and tq.has_evidence:
            msg = (f"trend evidence already reads BROKEN at entry "
                   f"({tq.score:.0%}, {tq.checks} checks) — "
                   f"{'; '.join(tq.against[:3])}")
            if cfg_bool("entry_refuse_broken_trend", False):
                out.append(msg)
            else:
                logger.info(f"  {p.get('symbol')}: broken-trend-at-entry "
                           f"shadow — {msg} — entry_refuse_broken_trend "
                           f"is off")
    except Exception as e:
        logger.debug(f"  {p.get('symbol')}: trend assessment at entry "
                    f"unavailable — {e}")

    return out


def rank(plans: list[dict]) -> list[Ranked]:
    """Best first."""
    return sorted((score_plan(p) for p in plans), key=lambda r: -r.total)
