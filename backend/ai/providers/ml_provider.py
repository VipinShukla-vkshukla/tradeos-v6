"""
TradeOS v7 — ML Provider v2 (conviction model)
===============================================
AI fallback #7 in ai_router.py chain. Predicts WIN probability per signal.
No API key, no cost. Local RandomForest trained on your closed trade history.

CHANGES vs v1 (Q1 answer — why more features, why only signal_log):
─────────────────────────────────────────────────────────────────────
WHY signal_log ONLY (not stock_data_daily or master_shortlist directly):
  Training requires POINT-IN-TIME correct feature values — the values that existed
  WHEN THE SIGNAL FIRED, not today's values. signal_log captures these at signal time.
  Reading stock_data_daily for a 3-month-old signal date would give the same value
  technically, but requires complex date-matched joins. signal_log is simpler and
  guaranteed to capture what the system actually saw when it decided to signal.
  generate_signals.py writes all features to signal_log — that's the design.

WHY NOT use all 80+ columns from stock_data_daily?
  1. Temporal leakage: many fields in stock_data_daily aren't captured at signal time
  2. Many fields are redundant (sma_10, sma_20, sma_50 all encode the same thing)
  3. RandomForest with 80 features needs 240+ samples to avoid overfitting
     (rule of thumb: 3x features × 2 classes = minimum samples)
  4. The 26 features below are information-dense non-redundant signals. Adding
     all 80 columns would add noise faster than it adds signal.

NEW FEATURES in v2 (26 total vs 19 in v1):
  + india_vix              — VIX at signal time. High VIX = high-risk environment
  + nifty_5d_chg_pct       — Nifty momentum at signal time. Signals in falling markets fail more
  + above_200dma_pct       — Market breadth at signal time. <40% breadth = avoid entries
  + fii_net_20d_ctx        — FII 20-day trend at signal time. Sustained selling → bad timing
  + score_adjusted         — Rule engine's own conviction AFTER all adjustments (+scanner, -caution)
  + breakout_setup         — Is stock in tight base approaching breakout?

FIX: regime_encoded now uses 4-class encoding:
  v1: RISK OFF=0, NEUTRAL=1, RISK ON=2  ← CAUTION silently mapped to NEUTRAL, TRENDING missing
  v2: RISK OFF=0, CAUTION=1, NEUTRAL=2, TRENDING=3  ← complete + meaningful ordering

FIX: min_trades raised from 60 to 90 (26 features × 2 classes × 1.75 safety margin)

Training source: signal_log (outcome = WIN or LOSS, all 26 feature columns)
Required SQL:    sql_signal_log_market_context.sql (adds 4 new columns to signal_log)
Required script: generate_signals_v2.py (writes the 4 new market-context columns)
"""
import json
import os
import sys
from pathlib import Path

# Resolve backend root FIRST, before any project-local imports (config, etc.),
# so this module works both when imported as part of the package
# (ai_router.py) and when run directly (python ml_provider.py from providers/).
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import cfg_float, cfg_int

try:
    # Normal import when used as a module inside the package (ai_router.py etc.)
    from .base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT
except ImportError:
    # Direct execution: python ml_provider.py — no parent package context.
    from ai.providers.base_provider import BaseProvider, ConvictionResult, UNKNOWN_RESULT

MODEL_PATH = Path(__file__).parent.parent.parent / "models" / "ml_conviction.pkl"

# 26 features — all sourced from signal_log at signal time (point-in-time correct)
FEATURES = [
    # ── Stock technicals (12) ─────────────────────────────────────────────
    "rsi_daily",      # RSI daily — primary momentum filter
    "rsi_weekly",     # RSI weekly — medium-term trend alignment
    "rsi_monthly",    # RSI monthly — long-term trend context (CC5)
    "adx",            # ADX — trend strength (>20 = meaningful trend)
    "vol_ratio",      # volume / avg_vol_20 — demand confirmation
    "delivery_pct",   # institutional delivery % — quality of volume
    "atr_pct",        # ATR / price — volatility / risk quantum
    "ret_6m",         # 6-month return — momentum quality
    "dist_sma50",     # distance from SMA50 — not extended / near support
    "rs_vs_nifty",    # relative strength vs Nifty (CC5) — outperforming market?
    "consol_range",   # base tightness % (CC5) — lower = tighter coil
    "breakout_setup", # Boolean: tight base + volume expanding (NEW v2)

    # ── Signal quality (3) ─────────────────────────────────────────
    # in_scanner removed: generate_signals hardcoded it to False on every
    # row, so as a feature it was constant across every training sample —
    # zero information, while still consuming a slot and diluting the
    # feature-importance ranking the Brain reads. Column dropped in
    # migration 008.──────
    "score_adjusted",  # Final score after all adjustments — rule engine conviction (NEW v2)
    "days_in_list",    # Days on master_shortlist — setup maturity
    "validity_score",  # Sheet's 0-100 entry quality gate (CC5)

    # ── Setup context (4) ────────────────────────────────────────────────
    "expected_r_msl",    # R multiple from MSL — reward/risk at entry (CC5)
    "trend_maturity_enc",# Fresh=3, Developing=2, Late=1, Exhausted=0 (CC5)
    "velocity_enc",      # Accelerating=2, Stable=1, Flat=0 (CC5)
    "eap_encoded",       # PRIORITISE=1, NO_CHANGE=0, AVOID_ENTRY=-1

    # ── Market context at signal time (6) ────────────────────────────────
    # FIX: these are captured in signal_log at signal time — not today's values
    "regime_encoded",    # FIX: 4-class: RISK OFF=0, CAUTION=1, NEUTRAL=2, TRENDING=3
    "sector_rank",       # Sector rank when signal fired (AG2 fix: sector_rank_at_entry)
    "india_vix",         # VIX at signal time (NEW v2) — high VIX = risk environment
    "nifty_5d_chg_pct",  # Nifty 5d momentum at signal time (NEW v2)
    "above_200dma_pct",  # % stocks above 200dma at signal time (NEW v2)
    "fii_net_20d_ctx",   # FII 20d net at signal time (NEW v2)
]

MIN_TRADES = cfg_float("ml_provider_min_trades", 90)


def _encode_regime(r: str) -> int:
    """
    FIX v2: 4-class encoding, matching actual regime labels used in generate_signals.
    v1 used 3-class (RISK ON=2) — CAUTION was silently mapped to NEUTRAL, TRENDING missing.
    """
    return {"RISK OFF": 0, "CAUTION": 1, "NEUTRAL": 2,
            "TRENDING": 3, "RISK ON": 3}.get((r or "").upper().strip(), 2)

def _encode_eap(e: str) -> int:
    return {"AVOID_ENTRY": -1, "NO_CHANGE": 0, "PRIORITISE": 1}.get(e or "", 0)

def _encode_trend_maturity(t: str) -> int:
    return {"Fresh": 3, "Developing": 2, "Late": 1, "Exhausted": 0}.get(str(t or ""), 2)

def _encode_velocity(v: str) -> int:
    return {"Accelerating": 2, "Stable": 1, "Flat": 0}.get(str(v or ""), 1)


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
                self._loaded = True

    def analyze_signal(self, stock_data: dict, context: dict) -> ConvictionResult:
        self._load()
        if self._model is None:
            return UNKNOWN_RESULT

        try:
            import numpy as np
            feat = [
                # Stock technicals (12)
                float(stock_data.get("rsi_daily") or 50),
                float(stock_data.get("rsi_weekly") or 50),
                float(stock_data.get("rsi_monthly") or 55),
                float(stock_data.get("adx") or 20),
                float(stock_data.get("vol_ratio") or 1.0),
                float(stock_data.get("delivery_pct") or 50),
                float(stock_data.get("atr_pct") or 2),
                float(stock_data.get("ret_6m") or 0),
                float(stock_data.get("dist_sma50") or 0),
                float(stock_data.get("rs_vs_nifty") or 0),
                float(stock_data.get("consol_range") or 8),
                float(1 if stock_data.get("breakout_setup") else 0),
                # Signal quality (4)
                float(stock_data.get("score_adjusted") or stock_data.get("score") or 50),
                float(stock_data.get("days_in_list") or 0),
                float(stock_data.get("validity_score") or 60),
                # Setup context (4)
                float(stock_data.get("expected_r_msl") or 1.5),
                float(_encode_trend_maturity(stock_data.get("trend_maturity"))),
                float(_encode_velocity(stock_data.get("velocity_state"))),
                float(_encode_eap(stock_data.get("eap_action"))),
                # Market context at signal time (6) — FIX: 4-class regime
                float(_encode_regime(context.get("regime"))),
                float(context.get("sector_rank") or 5),
                float(context.get("india_vix") or 16),      # neutral VIX default
                float(context.get("nifty_5d_chg_pct") or 0),
                float(context.get("above_200dma_pct") or 50),
                float(context.get("fii_net_20d") or 0),
            ]

            proba = self._model.predict_proba([feat])[0]
            prob  = proba[1] if len(proba) > 1 else (1.0 if self._model.classes_[0] == 1 else 0.0)

            if prob >= 0.70:
                conviction = "HIGH"
            elif prob >= 0.45:
                conviction = "MEDIUM"
            else:
                conviction = "LOW"

            regime     = context.get("regime", "UNKNOWN")
            n_trained  = context.get("trades_trained", 0)
            vix        = context.get("india_vix")
            vix_note   = f" | VIX={vix:.1f}" if vix else ""

            return ConvictionResult(
                conviction=conviction,
                conviction_reason=(
                    f"ML v2 ({n_trained} trades): {prob:.0%} win probability. "
                    f"Regime={regime}, sector_rank={context.get('sector_rank','?')}{vix_note}"
                ),
                risks=["ML model has no news/event awareness", "Needs 90+ trades for reliability"],
                catalyst="Pattern match from historical trade data (26 features)",
                suggested_action="ENTER" if conviction == "HIGH" else ("WAIT" if conviction == "MEDIUM" else "AVOID"),
                strategy_validation=(
                    "ML + rule engine aligned" if stock_data.get("in_rule_engine")
                    else "ML sees signal not in rule engine"
                ),
                conflicts="NONE",
                ai_note=(
                    f"Win prob: {prob:.1%} | 26 features | "
                    f"score_adj={stock_data.get('score_adjusted','?')}"
                ),
                provider="ml_model_v2",
                fallback_used=True,
                confidence=prob,
            )
        except Exception as e:
            from loguru import logger; logger.warning(f"ML provider v2 error: {e}")
            return UNKNOWN_RESULT


def train_model():
    """
    Train ML conviction model from signal_log outcomes.

    26 features — all from signal_log (point-in-time correct at signal time).
    Minimum 90 labelled rows (26 features × 2 classes × 1.75 safety margin).
    Uses StratifiedKFold CV to preserve WIN/LOSS class balance across folds.
    Saves to models/ml_conviction.pkl (same path as v1 — drop-in replacement).
    """
    from loguru import logger
    try:
        import numpy as np
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import StratifiedKFold, cross_val_score
        import joblib
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from config import get_supabase

        sb      = get_supabase()
        closed  = sb.table("closed_positions").select("id", count="exact").limit(1).execute()
        n_closed = closed.count or 0

        if n_closed < MIN_TRADES:
            logger.warning(
                f"Only {n_closed} closed trades — need {MIN_TRADES} for 26-feature model.\n"
                f"  Current features × 2 classes × 1.75 = {MIN_TRADES} minimum.\n"
                f"  Signal score from generate_signals is your deterministic backup until then."
            )
            return False

        # All signal_log rows with outcomes (WIN or LOSS)
        signals = (
            sb.table("signal_log")
            .select("*")
            .not_.is_("outcome", "null")
            .in_("outcome", ["WIN", "LOSS"])
            .execute()
            .data
        )

        if not signals:
            logger.warning("No labelled signal outcomes yet — run post_trade_analysis first")
            return False

        rows = []
        for s in signals:
            row = {
                # Stock technicals
                "rsi_daily":      float(s.get("rsi_daily") or 50),
                "rsi_weekly":     float(s.get("rsi_weekly") or 50),
                "rsi_monthly":    float(s.get("rsi_monthly") or 55),
                "adx":            float(s.get("adx") or 20),
                "vol_ratio":      float(s.get("vol_ratio") or 1),
                "delivery_pct":   float(s.get("delivery_pct") or 50),
                "atr_pct":        float(s.get("atr_pct") or 2),
                "ret_6m":         float(s.get("ret_6m") or 0),
                "dist_sma50":     float(s.get("dist_sma50") or 0),
                "rs_vs_nifty":    float(s.get("rs_vs_nifty") or 0),
                "consol_range":   float(s.get("consol_range") or 8),
                "breakout_setup": float(1 if s.get("breakout_setup") else 0),
                # Signal quality
                "score_adjusted": float(s.get("score_adjusted") or s.get("score") or 50),
                "days_in_list":   float(s.get("days_in_list") or 0),
                "validity_score": float(s.get("validity_score") or 60),
                # Setup context
                "expected_r_msl":     float(s.get("expected_r_msl") or 1.5),
                "trend_maturity_enc": float(_encode_trend_maturity(s.get("trend_maturity"))),
                "velocity_enc":       float(_encode_velocity(s.get("velocity_state"))),
                "eap_encoded":        float(_encode_eap(s.get("eap_action"))),
                # Market context at signal time (FIX: 4-class regime, new market features)
                "regime_encoded":   float(_encode_regime(s.get("regime"))),
                "sector_rank":      float(s.get("sector_rank_at_entry") or 5),
                "india_vix":        float(s.get("india_vix") or 16),
                "nifty_5d_chg_pct": float(s.get("nifty_5d_chg_pct") or 0),
                "above_200dma_pct": float(s.get("above_200dma_pct") or 50),
                "fii_net_20d_ctx":  float(s.get("fii_net_20d_ctx") or 0),
                # Label
                "win": 1 if s.get("outcome") == "WIN" else 0,
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        X  = df[FEATURES].values
        y  = df["win"].values

        win_rate = y.mean()
        logger.info(f"Training data: {len(rows)} labelled trades | win rate: {win_rate:.1%}")

        model = RandomForestClassifier(
            n_estimators=200,
            max_depth=6,
            min_samples_leaf=3,
            class_weight="balanced",  # handles WIN/LOSS imbalance
            random_state=42,
        )

        n_folds   = min(5, max(2, len(rows) // 20))
        cv        = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
        cv_scores = cross_val_score(model, X, y, cv=cv, scoring="accuracy")
        model.fit(X, y)

        # Feature importance
        importances = sorted(
            zip(FEATURES, model.feature_importances_),
            key=lambda x: x[1], reverse=True
        )
        top3_str = " > ".join(f"{f}({v:.2f})" for f, v in importances[:3])
        logger.info(f"Top 3 features: {top3_str}")

        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(model, MODEL_PATH)
        logger.success(
            f"✓ ML conviction model v2 trained: {len(rows)} trades, "
            f"CV accuracy={cv_scores.mean():.1%} ±{cv_scores.std():.1%}\n"
            f"  Win rate in training: {win_rate:.1%} | Features: {len(FEATURES)}\n"
            f"  Model saved → {MODEL_PATH}"
        )

        sb.table("ml_training_log").insert({
            "trades_used":   len(rows),
            "features_used": json.dumps(FEATURES),
            "accuracy":      float(cv_scores.mean()),
            "model_version": "rf_v2_26feat",
            "notes":         f"CV={n_folds}-fold win_rate={win_rate:.1%} top3={top3_str}",
        }).execute()
        return True

    except Exception as e:
        from loguru import logger
        logger.error(f"ML training v2 failed: {e}")
        return False


if __name__ == "__main__":
    # try/except at module top already resolved the import — just run training.
    train_model()