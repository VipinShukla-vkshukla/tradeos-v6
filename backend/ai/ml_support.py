"""
TradeOS v6 — ML Support Layer
==============================
Puts the trained conviction model to work in the two ways it should be:
as a SECOND OPINION alongside the LLM, and as a FALLBACK when the LLM is
unavailable.

WHY THIS IS NEW
---------------
models/ml_conviction.pkl has existed since March and has never once run.
MLProvider was reachable only through ai_router.analyze(), and nothing in the
codebase calls analyze() — the pipeline imports raw_completion and
is_ai_available from that module and nothing else. So the entire per-stock
provider chain, ML fallback included, was dead code.

Meanwhile ai_decision_engine.call_ai() returns None when no AI provider
answers, and step 19 logs "step 19 skipped" and produces NOTHING. On a day the
LLM is down or out of credits, a working pipeline generates signals, scores
them, ranks them — and then emits no tiering at all, because the only component
that could rank them was unavailable and had no substitute.

TWO ROLES, DELIBERATELY SEPARATE
--------------------------------
  SUPPORT   ml_win_probability() runs BEFORE the LLM call and its output is
            written into the prompt. The LLM sees a number derived from
            realised outcomes rather than reasoning purely from the narrative
            in front of it. Where the two disagree, that disagreement is
            itself information and the LLM is told to explain it.

  FALLBACK  rank_by_ml() produces the same ranked_candidates shape the LLM
            would have, so downstream steps (final_snapshot, send_alerts)
            work unchanged. Tiering degrades from "reasoned" to "scored",
            which is a large drop in nuance and a small one in usefulness —
            and infinitely better than nothing.

HONESTY ABOUT THE MODEL
-----------------------
The conviction model trains on closed_positions outcomes. Only one closed
trade is currently attributable to a signal, so the model is trained on the
pre-automation era and its probabilities should be read as weak priors, not
verdicts. is_ml_trustworthy() reports that explicitly rather than letting a
confident-looking 0.73 imply evidence that does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import get_supabase, cfg_bool, cfg_int


_provider = None


def _get_provider():
    global _provider
    if _provider is None:
        try:
            from ai.providers.ml_provider import MLProvider
            _provider = MLProvider()
        except Exception as e:
            logger.warning(f"  ML provider unavailable: {e}")
            _provider = False
    return _provider or None


def is_ml_available() -> bool:
    p = _get_provider()
    return bool(p and p.is_available())


def is_ml_trustworthy(sb=None) -> tuple[bool, str]:
    """
    Whether the conviction model's output should carry weight yet.

    Returns (trustworthy, reason). A model trained on 70 trades of which 69
    predate the signal engine is describing a different system; saying so is
    more useful than a probability that looks authoritative.
    """
    if not is_ml_available():
        return False, "no trained model at models/ml_conviction.pkl"

    # Feature-count check FIRST. A model whose input width no longer matches
    # the feature list cannot make a single prediction, so how much training
    # data exists is irrelevant. ml_conviction.pkl was fitted on 12 features
    # while ml_provider now assembles 24 — every call raises inside the
    # provider and returns UNKNOWN.
    try:
        p = _get_provider()
        p._load()
        model = getattr(p, "_model", None)
        expected = getattr(model, "n_features_in_", None)
        if expected is not None:
            from ai.providers.ml_provider import FEATURES
            if expected != len(FEATURES):
                return False, (f"saved model expects {expected} features but the code "
                               f"assembles {len(FEATURES)} — retrain: "
                               f"python -c \"from ai.providers.ml_provider import train_model; train_model()\"")
    except Exception:
        pass

    try:
        sb = sb or get_supabase()
        # head=True returns no rows and this client reports count=None for it,
        # which `or 0` then turned into a confident "0 of 0 closed trades".
        # Select a single cheap column instead so the count is real.
        total = len(sb.table("closed_positions").select("id").execute().data or [])
        attributed = len(sb.table("closed_positions").select("id")
                           .not_.is_("signal_date", "null").execute().data or [])
        need = cfg_int("ml_min_attributed_trades", 30)
        if attributed < need:
            return False, (f"only {attributed} of {total} closed trades are signal-attributed "
                           f"(need {need}); the model reflects the pre-automation era")
        return True, f"{attributed} attributed trades"
    except Exception as e:
        return False, f"could not assess training data: {e}"


def ml_win_probability(candidates: list[dict]) -> dict[str, dict]:
    """
    {symbol: {win_prob, conviction, confidence}} for each candidate.

    Never raises — a failure here must degrade the prompt, not break step 19.
    """
    p = _get_provider()
    if not p or not p.is_available() or not candidates:
        return {}

    out: dict[str, dict] = {}
    failed = 0
    for c in candidates:
        sym = c.get("symbol")
        if not sym:
            continue
        try:
            r = p.analyze_signal(c, {})
            # MLProvider swallows its own errors and returns
            # ConvictionResult(conviction="UNKNOWN", confidence=0.0). Treating
            # that as a real prediction would inject "win_prob=0.00" for every
            # candidate into the LLM prompt — telling the model that every
            # setup has a zero percent chance, which is far worse than telling
            # it nothing. A failed prediction is not a pessimistic one.
            if (r.conviction or "").upper() == "UNKNOWN":
                failed += 1
                continue
            out[sym] = {
                "win_prob":   round(float(r.confidence or 0), 3),
                "conviction": r.conviction,
                "reason":     r.conviction_reason,
            }
        except Exception as e:
            failed += 1
            logger.debug(f"  ML scoring failed for {sym}: {e}")

    if failed and not out:
        logger.warning(
            f"  ML model produced no usable prediction for any of {len(candidates)} "
            f"candidates. Most likely the saved model is stale — check that "
            f"len(FEATURES) in ml_provider matches what ml_conviction.pkl was "
            f"trained on, and retrain if not."
        )
    elif out:
        logger.info(f"  ML scored {len(out)}/{len(candidates)} candidates"
                    + (f" ({failed} failed)" if failed else ""))
    return out


def format_for_prompt(ml_scores: dict[str, dict], trustworthy: bool, reason: str) -> str:
    """
    The block injected into the LLM prompt.

    Framed as a second opinion with its provenance and its limits stated, so
    the model weighs it rather than deferring to it. A number presented without
    its confidence invites false precision.
    """
    if not ml_scores:
        return ""
    lines = [
        "",
        "═══ ML SECOND OPINION (gradient-boosted model on realised outcomes) ═══",
    ]
    if trustworthy:
        lines.append(f"  Model status: USABLE — {reason}.")
        lines.append("  Weigh these against your own read. Where you disagree, say why.")
    else:
        lines.append(f"  Model status: WEAK PRIOR — {reason}.")
        lines.append("  Treat as a tiebreaker only. Do NOT let it override your analysis.")
    lines.append("")
    for sym, s in sorted(ml_scores.items(), key=lambda kv: -kv[1]["win_prob"]):
        lines.append(f"    {sym:<14} win_prob={s['win_prob']:.2f}  ({s['conviction']})")
    lines.append("")
    return "\n".join(lines)


def rank_by_ml(candidates: list[dict], max_tier1: int = 3,
               max_tier2: int = 5) -> dict | None:
    """
    Produce the ranked_candidates structure the LLM would have, from ML scores.

    Used ONLY when no AI provider answered. Emits the same shape downstream
    expects so final_snapshot and send_alerts need no special case, and marks
    every row so the source of the tiering is never ambiguous after the fact.
    """
    scores = ml_win_probability(candidates)
    if not scores:
        return None

    trustworthy, reason = is_ml_trustworthy()
    ranked = []
    ordered = sorted(candidates,
                     key=lambda c: -scores.get(c.get("symbol", ""), {}).get("win_prob", 0))

    for i, c in enumerate(ordered):
        sym = c.get("symbol", "")
        s = scores.get(sym)
        if not s:
            continue
        tier = ("TIER_1" if i < max_tier1 else
                "TIER_2" if i < max_tier1 + max_tier2 else "TIER_3")
        # Only the model's own confidence maps to conviction — no narrative is
        # invented, because none exists.
        conv = "HIGH" if s["win_prob"] >= 0.65 else "MEDIUM" if s["win_prob"] >= 0.5 else "LOW"
        ranked.append({
            "symbol":     sym,
            "tier":       tier,
            "conviction": conv,
            "confidence": s["win_prob"],
            "action":     "ENTER_NOW" if tier == "TIER_1" else "WAIT_FOR_TRIGGER",
            "thesis": (f"ML fallback ranking — no AI provider was reachable. "
                       f"Model win probability {s['win_prob']:.2f}. {s['reason']}"),
            "risks": ["Ranked without LLM reasoning: no news, event or "
                      "correlation context was considered."]
                     + ([] if trustworthy else [f"Model is a weak prior — {reason}."]),
            "ranked_by": "ml_fallback",
        })

    logger.warning(
        f"  ML FALLBACK produced {len(ranked)} ranked candidates "
        f"({sum(1 for r in ranked if r['tier'] == 'TIER_1')} TIER_1). "
        f"Tiering is scored, not reasoned — no news or event context applied."
    )
    return {
        "ranked_candidates": ranked,
        "portfolio_guidance": {
            "sizing": "MINIMAL",
            "note": ("Generated by the ML fallback because no AI provider "
                     "responded. Size down until the LLM path is restored."),
        },
        "source": "ml_fallback",
    }
