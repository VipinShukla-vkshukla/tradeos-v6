"""
AI and empirical priors for intraday — advisory, never in the hot path.

THE LATENCY CONSTRAINT THAT SHAPES EVERYTHING HERE
---------------------------------------------------
Step 19's DeepSeek call took 88.6 seconds, measured. The intraday decision loop
runs every 15 seconds. An LLM cannot sit inside that loop under any
circumstances — by the time it answered, the setup it was asked about would be
six cycles stale, and a stop that needed acting on at 11:02:15 would be acted on
at 11:03:44.

So the AI runs OUT OF BAND, on the slow timer, over the setups already detected.
It produces a per-symbol verdict that the fast loop merely LOOKS UP. The
mechanical engines stay authoritative for what a setup is and where its levels
are; the AI only ever answers "given everything else happening today, is this
one worth your limited capital".

TWO INPUTS, DELIBERATELY DIFFERENT IN KIND
-------------------------------------------
  EMPIRICAL   v_intraday_engine_scorecard — how each engine has ACTUALLY
              resolved, net of costs. This is the ML component, and it is
              deliberately a simple empirical prior rather than a fitted model:
              the swing system's gradient-boosted model is untrainable because
              it needs 90 samples and has 70, and a per-engine hit rate needs no
              such threshold. It starts neutral and sharpens as outcomes accrue,
              so there is no cold-start problem to wait out.

  JUDGEMENT   DeepSeek, given the day's setups, the index state and the events.
              Asked to rank and to VETO, never to invent a setup — mechanical
              rules beat an LLM at pattern recognition on price, and the LLM
              beats them at noticing that four of today's five setups are the
              same sector bet wearing different tickers.

Both are advisory. Neither can create a setup, and neither can override the cost
model — a trade that does not clear its round trip is not tradeable no matter
how confident anything is about it.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, get_supabase, cfg_bool, cfg_float, cfg_int, today_ist


@dataclass
class Advice:
    symbol: str
    strategy: str
    verdict: str            # PREFER | NEUTRAL | AVOID
    confidence_delta: float # applied to the engine's own confidence
    reason: str
    source: str             # ai | prior | blend
    at: datetime = field(default_factory=lambda: datetime.now(IST))


# ── Empirical prior ─────────────────────────────────────────────────────────

def engine_priors(sb=None, min_samples: int | None = None) -> dict[str, dict]:
    """
    Per-engine hit rate and net expectancy from resolved setups.

    Returns {} until an engine has enough resolved samples to say anything —
    reporting a 100% hit rate off two setups would be worse than silence,
    because it reads as evidence and is noise.
    """
    sb = sb or get_supabase()
    need = min_samples if min_samples is not None else cfg_int("intraday_prior_min_samples", 15)
    try:
        rows = (sb.table("v_intraday_engine_scorecard")
                  .select("strategy,setups,wins,losses,hit_rate_pct,avg_net_pct")
                  .execute().data or [])
    except Exception as e:
        logger.debug(f"  priors unavailable: {e}")
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        resolved = int(r.get("wins") or 0) + int(r.get("losses") or 0)
        if resolved < need:
            continue
        out[r["strategy"]] = {
            "hit_rate": float(r.get("hit_rate_pct") or 0),
            "avg_net": float(r.get("avg_net_pct") or 0),
            "samples": resolved,
        }
    return out


def prior_adjustment(strategy: str, priors: dict[str, dict]) -> tuple[float, str]:
    """
    Confidence delta from an engine's realised record.

    Bounded hard. A strong record should tilt a decision, never dominate it —
    intraday regimes change, and an engine that worked for three weeks is not
    guaranteed to work today. The bound is what stops this becoming
    performance-chasing.
    """
    p = priors.get(strategy)
    if not p:
        return 0.0, ""
    cap = cfg_float("intraday_prior_max_delta", 0.12)
    # Centre on the break-even the cost model implies rather than on 50%: a 50%
    # hit rate at 2R is good, and treating it as neutral would understate it.
    edge = (p["avg_net"] / max(cfg_float("intraday_prior_net_scale", 0.5), 0.01)) * cap
    edge = max(-cap, min(cap, edge))
    return round(edge, 3), (f"{strategy} has resolved {p['samples']} setups at "
                            f"{p['hit_rate']:.0f}% hit, {p['avg_net']:+.2f}% net")


# ── LLM judgement ───────────────────────────────────────────────────────────

_PROMPT = """You are an experienced Indian intraday equity trader reviewing setups a
mechanical system has already validated. The levels, stops and targets are FIXED and
correct — do not restate or recompute them.

Your job is only to rank and to veto, using the context the price-based engines cannot see:
- concentration (several setups that are really one sector bet)
- market state (index direction and whether these fight it)
- event risk already flagged
- whether a setup is redundant with an open position

Capital is limited: at most {max_new} new positions today, and {open_count} are already open.

MARKET: {market}
OPEN POSITIONS: {open_syms}

SETUPS:
{setups}

Reply ONLY with JSON, no prose:
{{"ranked": [{{"symbol": "X", "strategy": "ORB", "verdict": "PREFER|NEUTRAL|AVOID",
"reason": "<15 words max>"}}], "note": "<one sentence on today's overall picture>"}}"""


def _call_ai(payload: str) -> dict | None:
    try:
        from ai.ai_router import raw_completion
        raw = raw_completion(payload, max_tokens=cfg_int("intraday_ai_max_tokens", 2000),
                             call_site="intraday_ai_advisor", framework="INTRADAY")
        if not raw:
            return None
        t = raw.strip()
        if t.startswith("```"):
            t = t.split("```")[1]
            t = t[4:] if t.lower().startswith("json") else t
        i, j = t.find("{"), t.rfind("}")
        return json.loads(t[i:j + 1]) if i >= 0 and j > i else None
    except Exception as e:
        logger.warning(f"  intraday AI call failed: {e}")
        return None


def review(setups: list, market_state: str, open_symbols: list[str],
           sb=None) -> dict[str, Advice]:
    """
    Rank today's setups. Called on the SLOW timer, never per cycle.

    Returns {symbol: Advice}. An empty dict is a perfectly good result and means
    the fast loop proceeds on mechanics alone — which is the correct degradation,
    since the engines were never dependent on this.
    """
    if not setups or not cfg_bool("intraday_ai_enabled", True):
        return {}

    sb = sb or get_supabase()
    priors = engine_priors(sb)
    out: dict[str, Advice] = {}

    # Priors first — they cost nothing and work even when the LLM does not.
    for s in setups:
        delta, why = prior_adjustment(s.strategy, priors)
        if delta:
            out[s.symbol] = Advice(s.symbol, s.strategy, "NEUTRAL", delta, why, "prior")

    # ── SEND EACH IDEA ONCE — 12-Aug-2026 ───────────────────────────────────
    #
    # `_pending_review` accumulates every Setup DETECTED since the last slow
    # tick. The fast loop runs every 15s, so one 5-minute window re-detects the
    # same (symbol, strategy) up to twenty times and every copy became its own
    # prompt line. Measured in production on 12-Aug: single calls of 764, 712,
    # 702 and 696 setups — against 1,527 DISTINCT detections recorded across
    # the entire session. The model was being asked to re-read the same idea
    # twenty times in one prompt and reliably vetoed 2-5 of them.
    #
    # Dedup keeps the highest-confidence instance of each idea, which is also
    # the most recent meaningful one: confidence moves with volume and RS, so
    # the best copy is the strongest the setup ever looked.
    #
    # The cap then bounds the tail. The model's job here is to veto crowding
    # and correlation among the candidates that could actually be funded, and
    # `intraday_max_new_positions` is 2 — ranking the 40 best against each
    # other answers that; ranking 700 does not answer it any better, it just
    # costs more and buries the signal.
    #
    # The PRIORS loop above deliberately stays over the full list: it is local
    # arithmetic, costs nothing, and works when the LLM is unavailable.
    best_by_idea: dict[tuple, object] = {}
    for s in setups:
        k = (s.symbol, s.strategy)
        prev = best_by_idea.get(k)
        if prev is None or s.confidence > prev.confidence:
            best_by_idea[k] = s
    ranked = sorted(best_by_idea.values(), key=lambda x: -x.confidence)
    cap = cfg_int("intraday_ai_max_setups", 40)
    sent = ranked[:cap] if cap > 0 else ranked
    if len(sent) < len(setups):
        logger.info(f"  intraday AI: {len(setups)} detections -> {len(best_by_idea)} "
                    f"distinct -> {len(sent)} sent "
                    f"({100 * (1 - len(sent) / max(1, len(setups))):.0f}% fewer prompt lines)")

    lines = []
    for s in sent:
        lines.append(
            f"- {s.symbol} [{s.strategy}] entry {s.entry:.2f} stop {s.stop:.2f} "
            f"target {s.target:.2f} R:R {s.rr:.1f} conf {s.confidence} "
            f"| {s.rationale[:90]}"
        )

    body = _PROMPT.format(
        max_new=cfg_int("intraday_max_new_positions", 2),
        open_count=len(open_symbols),
        market=market_state,
        open_syms=", ".join(open_symbols) or "none",
        setups="\n".join(lines),
    )

    data = _call_ai(body)
    if not data:
        if out:
            logger.info(f"  intraday AI unavailable — using empirical priors for "
                        f"{len(out)} setup(s)")
        return out

    for r in (data.get("ranked") or []):
        sym = r.get("symbol")
        if not sym:
            continue
        verdict = (r.get("verdict") or "NEUTRAL").upper()
        delta = {"PREFER": cfg_float("intraday_ai_prefer_delta", 0.10),
                 "AVOID": -cfg_float("intraday_ai_avoid_delta", 0.25)}.get(verdict, 0.0)
        prior = out.get(sym)
        if prior:
            delta += prior.confidence_delta
            reason = f"{r.get('reason', '')} · {prior.reason}"
            src = "blend"
        else:
            reason, src = r.get("reason", ""), "ai"
        out[sym] = Advice(sym, r.get("strategy", ""), verdict,
                          round(delta, 3), reason, src)

    note = data.get("note")
    if note:
        logger.info(f"  intraday AI: {note}")
    logger.info(f"  intraday AI reviewed {len(sent)} setup(s), "
                f"{sum(1 for a in out.values() if a.verdict == 'AVOID')} vetoed")

    try:
        sb.table("ai_context").upsert({
            "date": today_ist().isoformat(),
            "symbol": "__INTRADAY_REVIEW__",
            "conviction_reason": json.dumps(data),
            "ai_note": note or "",
            "provider": "deepseek",
            "fallback_used": False,
        }, on_conflict="date,symbol").execute()
    except Exception as e:
        logger.debug(f"  intraday AI context write skipped: {e}")

    return out


def apply(setup, advice: dict[str, Advice]) -> tuple[bool, float, str]:
    """
    Apply advice to one setup. Returns (allow, adjusted_confidence, note).

    AVOID no longer hard-blocks — measured 29-Aug-2026 against 24 sessions
    (17,545 resolved detections): what AI vetoed resolved at -0.034% net
    while what it approved and got TAKEN resolved at -0.302% net, inverted
    in both sub-periods and within every engine checked (docs/FINDINGS.md).
    AVOID now applies its own negative confidence_delta like PREFER does,
    and the existing confidence floor / gates decide from there.
    `intraday_ai_avoid_hard_block` restores the old block for instant rollback.
    """
    a = advice.get(setup.symbol)
    if not a:
        return True, setup.confidence, ""
    if a.verdict == "AVOID" and cfg_bool("intraday_ai_avoid_hard_block", False):
        return False, setup.confidence, f"AI veto: {a.reason}"
    adj = max(0.05, min(0.99, setup.confidence + a.confidence_delta))
    return True, round(adj, 2), (f"{a.source}: {a.reason}" if a.reason else "")
