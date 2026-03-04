"""
TradeOS v6 — Base AI Provider Interface
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
        """Standard prompt used by all LLM providers."""
        sym    = stock_data.get("symbol", "")
        sector = stock_data.get("sector", "")
        score  = stock_data.get("final_score", 0)
        price  = stock_data.get("current_price", 0)
        rsi_d  = stock_data.get("rsi_daily", 0)
        rsi_w  = stock_data.get("rsi_weekly", 0)
        adx    = stock_data.get("adx", 0)
        vol_r  = stock_data.get("vol_ratio", 0)
        del_p  = stock_data.get("delivery_pct", 0)
        atr_p  = stock_data.get("atr_pct", 0)
        ret_6m = stock_data.get("ret_6m", 0)
        lifecycle = stock_data.get("lifecycle", "")
        eap_action = stock_data.get("eap_action", "NO_CHANGE")
        regime = context.get("regime", "NEUTRAL")
        fii_flag = context.get("fii_flag", "NEUTRAL")
        events = context.get("active_events", [])
        lessons = context.get("relevant_lessons", [])

        events_txt  = "; ".join(events[:3]) if events else "None"
        lessons_txt = "; ".join(lessons[:2]) if lessons else "None"

        return f"""You are an expert Indian equity swing trader with 15+ years of NSE experience.
Analyze this trade setup and return ONLY valid JSON.

STOCK: {sym} | Sector: {sector} | Strategy Score: {score:.1f}
Price: ₹{price} | RSI(D/W): {rsi_d:.0f}/{rsi_w:.0f} | ADX: {adx:.0f}
Volume Ratio: {vol_r:.1f}x | Delivery%: {del_p:.0f}% | ATR%: {atr_p:.1f}%
6M Return: {ret_6m:.1f}% | Lifecycle: {lifecycle}

MARKET CONTEXT:
Regime: {regime} | FII Flow: {fii_flag}
Active Events: {events_txt}
EAP Signal: {eap_action}

RELEVANT LESSONS FROM HISTORY:
{lessons_txt}

Return ONLY this JSON (no markdown, no explanation):
{{
  "conviction": "HIGH|MEDIUM|LOW",
  "conviction_reason": "one sentence",
  "risks": ["risk1", "risk2"],
  "catalyst": "one sentence",
  "suggested_action": "ENTER|WAIT|AVOID",
  "strategy_validation": "one sentence about rule engine agreement",
  "conflicts": "any disagreements or NONE",
  "ai_note": "historical pattern or lesson reference",
  "confidence": 0.0-1.0
}}"""
