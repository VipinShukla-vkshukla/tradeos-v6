"""
ML Provider — Free local model trained on your closed trades.
Uses scikit-learn RandomForest. No API key needed.
Gets better as more trades accumulate.
"""
import json
from pathlib import Path
from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT

MODEL_PATH = Path(__file__).parent.parent.parent / "models" / "ml_conviction.pkl"

FEATURES = [
    "rsi_daily","rsi_weekly","adx","vol_ratio","delivery_pct",
    "atr_pct","ret_6m","dist_sma50","days_in_list",
    "regime_encoded","sector_rank","eap_encoded"
]

def _encode_regime(r: str) -> int:
    return {"RISK OFF": 0, "NEUTRAL": 1, "RISK ON": 2}.get(r or "", 1)

def _encode_eap(e: str) -> int:
    return {"AVOID_ENTRY": -1, "NO_CHANGE": 0, "PRIORITISE": 1}.get(e or "", 0)


class MLProvider(BaseProvider):
    def __init__(self):
        self._model  = None
        self._loaded = False

    def is_available(self) -> bool:
        return MODEL_PATH.exists()

    def _load(self):
        if not self._loaded:
            try:
                import joblib
                self._model  = joblib.load(MODEL_PATH)
                self._loaded = True
            except Exception:
                self._loaded = True  # prevent repeated attempts

    def analyze_signal(self, stock_data: dict, context: dict) -> ConvictionResult:
        self._load()
        if self._model is None:
            return UNKNOWN_RESULT

        try:
            import numpy as np
            feat = [
                float(stock_data.get("rsi_daily") or 50),
                float(stock_data.get("rsi_weekly") or 50),
                float(stock_data.get("adx") or 20),
                float(stock_data.get("vol_ratio") or 1.0),
                float(stock_data.get("delivery_pct") or 50),
                float(stock_data.get("atr_pct") or 2),
                float(stock_data.get("ret_6m") or 0),
                float(stock_data.get("dist_sma50") or 0),
                float(stock_data.get("days_in_list") or 0),
                _encode_regime(context.get("regime")),
                float(context.get("sector_rank") or 5),
                _encode_eap(stock_data.get("eap_action")),
            ]
            prob = self._model.predict_proba([feat])[0][1]  # P(win)
            if prob >= 0.7:
                conviction = "HIGH"
            elif prob >= 0.45:
                conviction = "MEDIUM"
            else:
                conviction = "LOW"

            return ConvictionResult(
                conviction=conviction,
                conviction_reason=f"ML model confidence: {prob:.0%} based on {context.get('trades_trained',0)} historical trades",
                risks=["ML model has no news/event awareness"],
                catalyst="Pattern match from historical trade data",
                suggested_action="ENTER" if conviction == "HIGH" else ("WAIT" if conviction == "MEDIUM" else "AVOID"),
                strategy_validation="ML agrees with rule engine" if stock_data.get("in_rule_engine") else "ML sees signal not in rule engine",
                conflicts="NONE",
                ai_note=f"Win probability: {prob:.1%}",
                provider="ml_model",
                fallback_used=True,
                confidence=prob,
            )
        except Exception as e:
            from loguru import logger; logger.warning(f"ML provider error: {e}")
            return UNKNOWN_RESULT


def train_model():
    """Train ML model from closed_positions. Run every Sunday."""
    from loguru import logger
    try:
        import numpy as np
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
        import joblib

        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from config import get_supabase

        sb = get_supabase()
        closed = sb.table("closed_positions").select("*").execute().data
        signals = sb.table("signal_log").select("*").not_.is_("outcome", "null").execute().data

        if len(closed) < 30:
            logger.warning(f"Only {len(closed)} closed trades — need 30 minimum to train ML model")
            return False

        # Build training data from signal_log (has features + outcomes)
        rows = []
        for s in signals:
            if not s.get("outcome"):
                continue
            row = {
                "rsi_daily": float(s.get("rsi_daily") or 50),
                "rsi_weekly": float(s.get("rsi_weekly") or 50),
                "adx": float(s.get("adx") or 20),
                "vol_ratio": float(s.get("vol_ratio") or 1),
                "delivery_pct": float(s.get("delivery_pct") or 50),
                "atr_pct": float(s.get("atr_pct") or 2),
                "ret_6m": float(s.get("ret_6m") or 0),
                "dist_sma50": float(s.get("dist_sma50") or 0),
                "days_in_list": float(s.get("days_in_list") or 0),
                "regime_encoded": _encode_regime(s.get("regime")),
                "sector_rank": 5.0,
                "eap_encoded": _encode_eap(s.get("eap_action")),
                "win": 1 if s.get("outcome") == "WIN" else 0,
            }
            rows.append(row)

        if not rows:
            logger.warning("No labelled signal outcomes yet — skipping ML training")
            return False

        df = pd.DataFrame(rows)
        X  = df[FEATURES].values
        y  = df["win"].values

        model = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
        cv_scores = cross_val_score(model, X, y, cv=min(5, len(rows)//10 + 1), scoring="accuracy")
        model.fit(X, y)

        MODEL_PATH.parent.mkdir(exist_ok=True)
        joblib.dump(model, MODEL_PATH)

        logger.info(f"✓ ML model trained on {len(rows)} labelled trades | CV accuracy: {cv_scores.mean():.2%}")

        sb.table("ml_training_log").insert({
            "trades_used": len(rows),
            "features_used": json.dumps(FEATURES),
            "accuracy": float(cv_scores.mean()),
            "model_version": "rf_v1",
        }).execute()
        return True

    except Exception as e:
        from loguru import logger; logger.error(f"ML training failed: {e}")
        return False


if __name__ == "__main__":
    train_model()
