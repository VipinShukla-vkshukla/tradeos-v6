"""
TradeOS v7 — Base AI Provider Interface
All providers must implement this interface.
ConvictionResult is the shared output struct.
"""
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import Optional


@dataclass
class ConvictionResult:
    conviction: str              # HIGH / MEDIUM / LOW / UNKNOWN
    conviction_reason: str
    risks: list
    catalyst: str
    suggested_action: str        # ENTER / WAIT / AVOID
    strategy_validation: str     # matches rule engine? any conflicts?
    conflicts: str               # any disagreements
    ai_note: str                 # historical context
    provider: str                # which provider generated this
    fallback_used: bool = False
    confidence: float = 0.5      # 0.0 - 1.0

    def to_dict(self) -> dict:
        return {
            "ai_conviction": self.conviction,
            "ai_conviction_reason": self.conviction_reason,
            "ai_risks": self.risks,
            "ai_catalyst": self.catalyst,
            "ai_suggested_action": self.suggested_action,
            "ai_strategy_validation": self.strategy_validation,
            "ai_conflicts": self.conflicts,
            "ai_note": self.ai_note,
            "ai_provider": self.provider,
            "ai_fallback_used": self.fallback_used,
            "ai_confidence": self.confidence,
        }


UNKNOWN_RESULT = ConvictionResult(
    conviction="UNKNOWN", conviction_reason="AI not configured",
    risks=[], catalyst="", suggested_action="MANUAL_REVIEW",
    strategy_validation="", conflicts="", ai_note="",
    provider="none", fallback_used=False, confidence=0.0
)


class BaseProvider(ABC):
    """Every AI provider must implement this interface."""

    @abstractmethod
    def analyze_signal(self, stock_data: dict, context: dict) -> ConvictionResult:
        """
        stock_data: full row from stock_data_daily + master_shortlist
        context: regime, events, lessons, fii_flag
        Returns: ConvictionResult
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """True if API key configured and reachable."""
        pass

    def build_prompt(self, stock_data: dict, context: dict) -> str:
        """
        Standard prompt used by all LLM providers.

        PATCHED: Reads new ai_context structure from ai_enrich.py (G6/G13/G17/G18 patches).
          - context["market_regime"]    dict  (was: context["regime"] string)
          - context["fii_dii"]          dict  (was: context["fii_flag"] string)
          - context["upcoming_events"]  list  (was: context["active_events"])
          - context["global_cues"]      dict  (NEW)
          - context["sector_context"]   dict  (NEW — G13)
          - context["industry_context"] dict  (NEW — G13)
          - context["portfolio"]        dict  (NEW — G18)
          - context["relevant_lessons"] list  (unchanged)

        G3: Zero-data guard — warns AI when Phase 2 computed indicators are absent.
        """
        sym      = stock_data.get("symbol", "")
        sector   = stock_data.get("sector", "")
        industry = stock_data.get("industry", "")
        score    = stock_data.get("final_score", 0)
        price    = stock_data.get("current_price", 0)
        rsi_d    = stock_data.get("rsi_daily", 0)
        rsi_w    = stock_data.get("rsi_weekly", 0)
        adx      = stock_data.get("adx", 0)
        vol_r    = stock_data.get("vol_ratio", 0)
        del_p    = stock_data.get("delivery_pct", 0)
        atr_p    = stock_data.get("atr_pct", 0)
        ret_6m   = stock_data.get("ret_6m", 0)
        lifecycle   = stock_data.get("lifecycle", "")
        eap_action  = stock_data.get("eap_action", "NO_CHANGE")
        signal_type = stock_data.get("signal_type", "BUY_CANDIDATE")

        # ── G3: Zero-data guard ────────────────────────────────────────────────
        computed_missing = all(
            v == 0 or v is None
            for v in [vol_r, adx, ret_6m, atr_p]
        )
        computed_note = (
            "\nNOTE: Computed technical indicators (vol_ratio, adx, ret_6m, atr_pct) are "
            "not yet populated — compute_indicators.py (Phase 2) not yet deployed. "
            "Base your conviction on RSI, sector/industry context, FII flow, and events only. "
            "Do NOT return empty JSON or UNKNOWN — use the available fields."
        ) if computed_missing else ""

        # ── Market regime (G17: full object) ──────────────────────────────────
        regime_obj    = context.get("market_regime", {})
        regime_str    = regime_obj.get("regime", "NEUTRAL") if isinstance(regime_obj, dict) else str(regime_obj)
        breadth_pct   = regime_obj.get("breadth_pct",     "") if isinstance(regime_obj, dict) else ""
        regime_score  = regime_obj.get("regime_score",    "") if isinstance(regime_obj, dict) else ""
        pred_regime   = regime_obj.get("predicted_regime","") if isinstance(regime_obj, dict) else ""

        # ── FII/DII (G17: full object from fii_dii_flow) ─────────────────────
        fii_obj       = context.get("fii_dii", {})
        fii_flag      = fii_obj.get("fii_flag",    "NEUTRAL") if isinstance(fii_obj, dict) else "NEUTRAL"
        fii_net       = fii_obj.get("fii_net",     "")        if isinstance(fii_obj, dict) else ""
        fii_net_5d    = fii_obj.get("fii_net_5d",  "")        if isinstance(fii_obj, dict) else ""
        fii_net_20d   = fii_obj.get("fii_net_20d", "")        if isinstance(fii_obj, dict) else ""

        # ── Global cues (G17) ─────────────────────────────────────────────────
        gcues         = context.get("global_cues", {})
        gift_nifty    = gcues.get("gift_nifty_chg_pct", "") if isinstance(gcues, dict) else ""
        gap_signal    = gcues.get("gap_signal",          "") if isinstance(gcues, dict) else ""
        dow_chg       = gcues.get("us_dow_chg_pct",      "") if isinstance(gcues, dict) else ""
        nq_chg        = gcues.get("us_nasdaq_chg_pct",   "") if isinstance(gcues, dict) else ""
        sec_impacts   = gcues.get("sector_impacts",       "") if isinstance(gcues, dict) else ""

        # ── Events (G6) ───────────────────────────────────────────────────────
        events        = context.get("upcoming_events", context.get("active_events", []))
        events_txt    = "; ".join(str(e) for e in events[:4]) if events else "None"

        # ── Sector + industry (G13) ───────────────────────────────────────────
        sec_ctx       = context.get("sector_context", {})
        sec_rank      = sec_ctx.get("rank",           "") if isinstance(sec_ctx, dict) else ""
        sec_trend     = sec_ctx.get("trend",          "") if isinstance(sec_ctx, dict) else ""
        sec_strength  = sec_ctx.get("strength_score", "") if isinstance(sec_ctx, dict) else ""

        ind_ctx       = context.get("industry_context", {})
        ind_rank      = ind_ctx.get("rank",           "") if isinstance(ind_ctx, dict) else ""
        ind_state     = ind_ctx.get("industry_state", "") if isinstance(ind_ctx, dict) else ""
        ind_rsi       = ind_ctx.get("avg_rsi_daily",  "") if isinstance(ind_ctx, dict) else ""
        ind_top5      = ind_ctx.get("top5_flag",      "") if isinstance(ind_ctx, dict) else ""

        # ── Portfolio context (G18) ───────────────────────────────────────────
        port          = context.get("portfolio", {})
        total_open    = port.get("total_open",        0)  if isinstance(port, dict) else 0
        already_held  = port.get("already_held",      False) if isinstance(port, dict) else False
        sec_exposure  = port.get("this_sector_count", 0)  if isinstance(port, dict) else 0
        port_note     = port.get("note",              "") if isinstance(port, dict) else ""

        # ── Lessons ───────────────────────────────────────────────────────────
        lessons       = context.get("relevant_lessons", [])
        lessons_txt   = "\n".join(f"  - {l}" for l in lessons[:3]) if lessons else "  None"

        return f"""You are an expert Indian equity swing trader with 15+ years of NSE experience.
Analyze this trade setup and return ONLY valid JSON.{computed_note}

SIGNAL: {sym} | {signal_type} | Sector: {sector} | Industry: {industry}
Score: {score:.1f} | Price: ₹{price} | Lifecycle: {lifecycle}

TECHNICALS:
RSI Daily/Weekly: {rsi_d:.0f}/{rsi_w:.0f} | ADX: {adx:.0f}
Volume Ratio: {vol_r:.1f}x | Delivery%: {del_p:.0f}% | ATR%: {atr_p:.1f}%
6M Return: {ret_6m:.1f}%

MARKET REGIME:
Regime: {regime_str}{f" (ML predicted: {pred_regime})" if pred_regime and pred_regime != regime_str else ""}
Breadth: {breadth_pct} | Score: {regime_score}
EAP Signal: {eap_action}

GLOBAL CUES (today):
Gift Nifty: {gift_nifty}% | Gap: {gap_signal} | DOW: {dow_chg}% | NASDAQ: {nq_chg}%
Sector impacts: {sec_impacts}

FII/DII FLOWS:
Today: ₹{fii_net}Cr | 5-day: ₹{fii_net_5d}Cr | 20-day: ₹{fii_net_20d}Cr | Flag: {fii_flag}

SECTOR & INDUSTRY:
Sector rank: {sec_rank} | Trend: {sec_trend} | Strength: {sec_strength}
Industry: {industry} | Rank: {ind_rank} | State: {ind_state} | Avg RSI: {ind_rsi} | Top5: {ind_top5}

PORTFOLIO:
Open positions: {total_open} | Already holding: {already_held}
This sector exposure: {sec_exposure} positions | {port_note}

UPCOMING EVENTS (next 14 days):
{events_txt}

LESSONS FROM HISTORY:
{lessons_txt}

Return ONLY this JSON (no markdown, no explanation):
{{
  "conviction": "HIGH|MEDIUM|LOW",
  "conviction_reason": "one sentence — specific to this stock's setup",
  "risks": ["risk1", "risk2"],
  "catalyst": "one sentence",
  "suggested_action": "ENTER|WAIT|AVOID",
  "strategy_validation": "one sentence about whether rule engine signal is sound",
  "conflicts": "any disagreements between signals or NONE",
  "ai_note": "relevant historical pattern, lesson reference, or portfolio context",
  "confidence": 0.0
}}"""