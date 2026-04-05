"""
TradeOS v6 — Phase 2: ML Regime Classifier  (v2 — schema-corrected)
====================================================================
CHANGES vs v1 (schema audit against live Supabase, 01-Apr-2026):
  FIX 1: nifty_total_market is an INDEX MEMBERSHIP table (symbol, nifty_200, nifty_500).
          It has NO close/advance/decline/breadth_pct columns.
          → _nifty_returns() now reads market_regime.nifty_5d_chg_pct / nifty_20d_chg_pct
            which are already pre-computed and available as direct columns.
          → _advance_decline() primary source unchanged (market_regime.advance_decline_ratio).
            nifty_total_market fallback REMOVED (those columns don't exist).

  FIX 2: sector_strength has NO strength_score column.
          Live schema: avg_rsi_daily, avg_rsi_weekly, breadth_sma50, rank, top4_flag, etc.
          → _sector_dispersion() now uses breadth_sma50 (% stocks above SMA50 per sector)
            std dev of breadth_sma50 across sectors is actually a better dispersion
            signal than a synthetic strength_score would be.

  FIX 3: regime_history uses date column, NOT snapshot_date.
          → build_training_data() queries updated to use 'date' column.

  FIX 4: fii_dii_flow.fii_net_5d and fii_net_20d are PRE-COMPUTED and stored directly.
          → _fii_net() now reads these columns directly instead of re-summing raw rows.
          Faster, consistent with what ingest_fii_dii.py already writes.

Relationship to ml_provider.py (stock-level model):
  These two models are independent and operate at different scopes:
  - ml_regime_classifier  → MARKET scope. Input: market breadth/FII/Nifty returns.
                             Output: predicted_regime written to market_regime.
  - ml_provider.py        → STOCK scope. Input: per-stock technicals from signal_log.
                             Output: conviction (HIGH/MEDIUM/LOW) per signal.
  Link: generate_signals._resolve_regime() reads predicted_regime → passes as
  regime_encoded into ml_provider's feature vector. One-way dependency only.

Why stock_data_daily / master_shortlist are NOT used here:
  The regime classifier answers a market-level question (what is the market doing?).
  Using 500 stock rows to answer it would be re-deriving breadth_pct and A/D ratio
  which we already have as direct columns in market_regime. stock_data_daily IS used
  by ml_provider.py indirectly — its computed fields flow into signal_log at signal
  time, and ml_provider trains on signal_log outcomes.

Feature sources (simplified — mostly from market_regime which is a daily denorm snapshot):
  nifty_ret_5d      → market_regime.nifty_5d_chg_pct      (direct, pre-computed)
  nifty_ret_20d     → market_regime.nifty_20d_chg_pct     (direct, pre-computed)
  advance_decline   → market_regime.advance_decline_ratio  (direct)
  breadth_pct       → market_regime.above_200dma_pct       (% stocks above 200dma)
  fii_net_5d        → fii_dii_flow.fii_net_5d              (direct, pre-computed)
  fii_net_20d       → fii_dii_flow.fii_net_20d             (direct, pre-computed)
  sector_dispersion → std dev of sector_strength.breadth_sma50 across sectors

Training labels:
  Primary:  regime_history.date + regime    (G14 fix: append_history writes daily)
  Fallback: market_regime.date + regime     (always available)

Tables written:
  market_regime  → predicted_regime, regime_confidence, regime_predicted_at
  ml_training_log → model_type, training_samples, accuracy, feature_names,
                    feature_importance, notes, trained_at
"""

import sys
import json
import pickle
import argparse
import math
from pathlib import Path
from datetime import datetime, timedelta

_BACKEND_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from config import (
    get_supabase, today_ist, IST,
    is_kill_switch_active, cfg_int, logger,
)

# ── Constants ─────────────────────────────────────────────────────────────────

MODEL_PATH = _BACKEND_ROOT / "models" / "ml_regime_model.pkl"
META_PATH  = _BACKEND_ROOT / "models" / "ml_regime_meta.json"

LABELS    = ["TRENDING", "NEUTRAL", "CAUTION", "RISK OFF"]
LABEL_IDX = {l: i for i, l in enumerate(LABELS)}
LABEL_TIER = LABEL_IDX

# 7 features — all sourced from pre-computed columns in market_regime / fii_dii_flow
# No manual rolling-window computation needed (FIX 1 + FIX 4)
FEATURES = [
    "nifty_ret_5d",       # market_regime.nifty_5d_chg_pct   (pre-computed)
    "nifty_ret_20d",      # market_regime.nifty_20d_chg_pct  (pre-computed)
    "advance_decline",    # market_regime.advance_decline_ratio
    "breadth_pct",        # market_regime.above_200dma_pct   (% stocks above 200dma)
    "fii_net_5d",         # fii_dii_flow.fii_net_5d           (pre-computed)
    "fii_net_20d",        # fii_dii_flow.fii_net_20d          (pre-computed)
    "sector_dispersion",  # std dev of sector_strength.breadth_sma50 across sectors
]

MIN_TRAINING_SAMPLES = 20   # 7 features × 2 classes × 1.5 safety = 21 theoretical minimum.
                             # 20 is the bootstrap floor — allows first training run while
                             # regime_history is still accumulating (daily via append_history).
                             # Accuracy at 20 rows is lower but meaningful. Improves each week.
MIN_PREDICT_SAMPLES  = 15
MODEL_STALE_DAYS     = 7
LOOKBACK_DAYS        = 365


# ── Data Health Check ─────────────────────────────────────────────────────────

def check_data_health(sb) -> dict:
    """
    Check row counts and latest dates for all tables the classifier touches,
    plus key pipeline tables for broader system health visibility.
    Non-destructive — just reads and logs.
    """
    results = {}
    checks = [
        # (label, table, date_col, min_rows, critical)
        ("regime_history",    "regime_history",    "date",       MIN_TRAINING_SAMPLES, True),
        ("market_regime",     "market_regime",     "date",       30,                   True),
        ("fii_dii_flow",      "fii_dii_flow",      "date",       20,                   True),
        ("sector_strength",   "sector_strength",   "date",       10,                   True),
        ("nifty_total_market","nifty_total_market","symbol",     100,                  False),  # membership table
        ("stock_data_daily",  "stock_data_daily",  "date",       100,                  False),
        ("master_shortlist",  "master_shortlist",  "created_at", 1,                    False),
    ]
    for label, table, date_col, min_rows, critical in checks:
        try:
            count_result = sb.table(table).select("*", count="exact").limit(1).execute()
            row_count    = count_result.count or 0
            latest_result = (
                sb.table(table).select(date_col)
                .order(date_col, desc=True).limit(1).execute().data
            )
            latest = latest_result[0].get(date_col) if latest_result else None
            ok     = row_count >= min_rows
            results[label] = {
                "rows": row_count, "latest": str(latest)[:10] if latest else "—",
                "min": min_rows, "ok": ok, "critical": critical,
            }
            status = "✓" if ok else ("⚠ CRITICAL" if critical else "⚠ context")
            logger.info(
                f"  [{status}] {label:<22} {row_count:>6} rows  "
                f"latest={str(latest)[:10] if latest else '—'}  (need {min_rows})"
            )
        except Exception as e:
            results[label] = {"rows": 0, "latest": "—", "min": min_rows,
                               "ok": False, "critical": critical}
            logger.warning(f"  [ERROR] {label}: {e}")

    critical_failures = [k for k, v in results.items() if v["critical"] and not v["ok"]]
    if critical_failures:
        logger.warning(
            f"\nCritical tables below threshold: {critical_failures}\n"
            "  → regime_history: needs G14 fix (append_history.py running daily)\n"
            "  → fii_dii_flow: needs ingest_fii_dii.py running (Phase 1)\n"
            "  → sector_strength: needs compute_indicators.py running (Phase 2)\n"
            "  → market_regime: needs ingest_sheets.py running (Phase 0)\n"
        )
    else:
        logger.success("All critical tables meet minimum data thresholds.")
    return results


# ── Feature Engineering ───────────────────────────────────────────────────────

def _regime_features_for_date(sb, as_of_date: str) -> dict:
    """
    Read pre-computed market features from market_regime for a given date.

    FIX 1 + FIX 4: market_regime already stores nifty_5d_chg_pct, nifty_20d_chg_pct,
    and advance_decline_ratio as direct columns. No rolling computation needed.
    Reading pre-computed values is also more consistent with what ingest_sheets/
    compute_indicators writes as ground truth for each day.

    Returns dict with keys:
      nifty_ret_5d, nifty_ret_20d, advance_decline, breadth_pct
    or None if no row available for date.
    """
    rows = (
        sb.table("market_regime")
        .select(
            "nifty_5d_chg_pct,nifty_20d_chg_pct,"
            "advance_decline_ratio,above_200dma_pct,avg_sector_breadth"
        )
        .lte("date", as_of_date)
        .order("date", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return None
    r = rows[0]

    # nifty_5d_chg_pct / nifty_20d_chg_pct: written by ingest_sheets (from Sheet formula)
    # and will be written by compute_indicators (Phase 2) from nifty_total_market price history
    nifty_5d  = r.get("nifty_5d_chg_pct")
    nifty_20d = r.get("nifty_20d_chg_pct")
    # Both 5d and 20d are essential — if either is NULL (early data before Sheet
    # populated these columns), skip this date entirely rather than crash.
    if nifty_5d is None or nifty_20d is None:
        return None

    # advance_decline_ratio: advance/decline ratio (advances/declines)
    ad = r.get("advance_decline_ratio") or 1.0

    # breadth: above_200dma_pct preferred (% stocks above 200dma — structural health)
    # fallback: avg_sector_breadth (average sector breadth score)
    breadth = r.get("above_200dma_pct") or r.get("avg_sector_breadth") or 0.0

    return {
        "nifty_ret_5d":    float(nifty_5d),
        "nifty_ret_20d":   float(nifty_20d),
        "advance_decline": float(ad),
        "breadth_pct":     float(breadth),
    }


def _fii_features_for_date(sb, as_of_date: str) -> tuple:
    """
    Read pre-computed FII rolling sums from fii_dii_flow.

    FIX 4: fii_net_5d and fii_net_20d are already computed by ingest_fii_dii.py
    and stored as direct columns. Reading them directly is faster and consistent.

    Returns (fii_net_5d, fii_net_20d). Defaults to 0.0 if not found.
    """
    rows = (
        sb.table("fii_dii_flow")
        .select("fii_net_5d,fii_net_20d")
        .lte("date", as_of_date)
        .order("date", desc=True)
        .limit(1)
        .execute()
        .data
    )
    if not rows:
        return 0.0, 0.0
    r = rows[0]
    return float(r.get("fii_net_5d") or 0), float(r.get("fii_net_20d") or 0)


def _sector_dispersion_for_date(sb, as_of_date: str):
    """
    Compute std dev of sector_strength.breadth_sma50 across sectors as of date.

    FIX 2: sector_strength has NO strength_score column.
    Live schema columns: avg_rsi_daily, avg_rsi_weekly, avg_rsi_monthly,
                         avg_ret_6m, breadth_sma50, rank, top4_flag, sector_state.
    → Use breadth_sma50 (% of sector stocks above SMA50) as the per-sector signal.
      Std dev of breadth_sma50 across sectors = how spread out sector health is.
      High dispersion → sectors diverging → market is rotational / non-trending.
      Low dispersion  → sectors aligned   → trending market (all rising or all falling).
    This is actually a better feature than strength_score would have been.

    Filters to exact as_of_date, falls back to most recent available.
    Returns None if fewer than 3 sectors available.
    """
    rows = (
        sb.table("sector_strength")
        .select("breadth_sma50")
        .eq("date", as_of_date)
        .limit(30)
        .execute()
        .data
    )
    if not rows:
        rows = (
            sb.table("sector_strength")
            .select("breadth_sma50")
            .lte("date", as_of_date)
            .order("date", desc=True)
            .limit(30)
            .execute()
            .data
        )

    scores = [float(r["breadth_sma50"]) for r in rows if r.get("breadth_sma50") is not None]
    if len(scores) < 3:
        return None

    n    = len(scores)
    mean = sum(scores) / n
    return round(math.sqrt(sum((s - mean) ** 2 for s in scores) / n), 4)


def _build_feature_row(sb, as_of_date: str):
    """
    Build a 7-feature vector for the given date.

    All features now sourced from pre-computed columns — no manual rolling windows.
    Returns None if essential market_regime data (nifty_5d_chg_pct) is missing.
    Falls back gracefully for optional features (fii, sector_dispersion → 0.0).
    """
    market_features = _regime_features_for_date(sb, as_of_date)
    if market_features is None:
        logger.debug(
            f"_build_feature_row: no market_regime data with nifty_5d_chg_pct for {as_of_date}. "
            "Ensure ingest_sheets.py has run and market_regime table is populated."
        )
        return None

    fii_5, fii_20   = _fii_features_for_date(sb, as_of_date)
    sector_disp     = _sector_dispersion_for_date(sb, as_of_date)

    return [
        market_features["nifty_ret_5d"],
        market_features["nifty_ret_20d"],
        market_features["advance_decline"],
        market_features["breadth_pct"],
        fii_5,
        fii_20,
        sector_disp if sector_disp is not None else 0.0,
    ]


# ── Label Normalisation ───────────────────────────────────────────────────────

def _normalise_regime(raw: str) -> str:
    if not raw:
        return ""
    cleaned = raw.strip().replace("_", " ").upper()
    if cleaned in ("RISK ON",):
        return "TRENDING"
    return cleaned


# ── Training Data ─────────────────────────────────────────────────────────────

def build_training_data(sb) -> tuple:
    """
    Build (X, y) from labelled regime history.

    FIX 3: regime_history uses 'date' column, NOT 'snapshot_date'.
    Live schema confirmed: date, regime, nifty_price, regime_score, etc.

    Primary:  regime_history.date + regime  (G14 fix: append_history writes daily)
    Fallback: market_regime.date + regime   (manual regime, always available)
    """
    cutoff = str(today_ist() - timedelta(days=LOOKBACK_DAYS))

    # FIX 3: use 'date' not 'snapshot_date'
    history = (
        sb.table("regime_history")
        .select("date,regime")
        .gte("date", cutoff)
        .not_.is_("regime", "null")
        .order("date")
        .execute()
        .data
    )

    if len(history) < MIN_TRAINING_SAMPLES:
        logger.warning(
            f"regime_history: {len(history)} rows (need {MIN_TRAINING_SAMPLES}). "
            "G14 fix (append_history.py) required for daily snapshots. "
            "Supplementing from market_regime."
        )
        extra = (
            sb.table("market_regime")
            .select("date,regime")
            .gte("date", cutoff)
            .not_.is_("regime", "null")
            .order("date")
            .execute()
            .data
        )
        existing_dates = {r["date"] for r in history}
        for r in extra:
            if r["date"] not in existing_dates:
                history.append({"date": r["date"], "regime": r["regime"]})

    logger.info(f"Training candidates: {len(history)} labelled rows (cutoff={cutoff})")

    X, y    = [], []
    skipped = 0
    for row in history:
        date   = row.get("date")
        regime = _normalise_regime(row.get("regime") or "")

        if regime not in LABEL_IDX:
            skipped += 1
            continue

        features = _build_feature_row(sb, date)
        if features is None:
            skipped += 1
            continue

        X.append(features)
        y.append(LABEL_IDX[regime])

    logger.info(f"Training rows built: {len(X)} usable, {skipped} skipped")
    return X, y


# ── Training ──────────────────────────────────────────────────────────────────

def train(sb):
    """
    Train RandomForest regime classifier.
    200 trees, max_depth=6, class_weight=balanced, StratifiedKFold CV.
    Saves model to models/ml_regime_model.pkl + metadata JSON.
    Logs to ml_training_log (requires sql_ml_regime_classifier_columns.sql migration).
    """
    logger.info("=" * 60)
    logger.info("ML Regime Classifier — Training (schema-corrected v2)")
    logger.info("=" * 60)
    logger.info("\nData health check:")
    check_data_health(sb)
    logger.info("")

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score, StratifiedKFold
        import numpy as np
    except ImportError:
        logger.error("scikit-learn not installed. Run: pip install scikit-learn")
        return False

    X, y = build_training_data(sb)

    if len(X) < MIN_TRAINING_SAMPLES:
        logger.warning(
            f"Insufficient training data: {len(X)} rows (need {MIN_TRAINING_SAMPLES}).\n"
            "Market regime features come from market_regime table — ensure:\n"
            "  1. ingest_sheets.py has been running (nifty_5d_chg_pct, advance_decline_ratio)\n"
            "  2. regime_history has ≥30 rows (append_history.py G14 fix running daily)\n"
            "  3. fii_dii_flow.fii_net_5d/20d populated (ingest_fii_dii.py Phase 1)\n"
            "  4. sector_strength.breadth_sma50 populated (compute_indicators.py Phase 2)"
        )
        return False

    X_arr = __import__("numpy").array(X, dtype=float)
    y_arr = __import__("numpy").array(y, dtype=int)
    import numpy as np

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=3,
        class_weight="balanced",
        random_state=42,
    )

    n_folds   = min(5, max(2, len(X) // max(MIN_TRAINING_SAMPLES // 5, 1)))
    cv        = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_arr, y_arr, cv=cv, scoring="accuracy")
    cv_mean   = float(np.mean(cv_scores))
    cv_std    = float(np.std(cv_scores))

    model.fit(X_arr, y_arr)
    importances = {f: round(float(v), 4) for f, v in zip(FEATURES, model.feature_importances_)}

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as fh:
        pickle.dump(model, fh)

    label_dist = {LABELS[i]: int(np.sum(y_arr == i)) for i in range(len(LABELS))}
    meta = {
        "trained_at":         datetime.now(IST).isoformat(),
        "sample_count":       len(X),
        "cv_accuracy":        cv_mean,
        "cv_std":             cv_std,
        "n_folds":            n_folds,
        "label_distribution": label_dist,
        "feature_importance": importances,
        "features":           FEATURES,
        "schema_version":     2,   # tracks fix applied
    }
    with open(META_PATH, "w") as fh:
        json.dump(meta, fh, indent=2)

    try:
        top3     = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:3]
        top3_str = " > ".join(f"{f}({v:.2f})" for f, v in top3)
        sb.table("ml_training_log").insert({
            "model_type":         "regime_classifier_v2",
            "training_samples":   len(X),
            "accuracy":           cv_mean,
            "feature_names":      json.dumps(FEATURES),
            "feature_importance": json.dumps(importances),
            "notes": (
                f"CV={n_folds}-fold±{cv_std:.3f} labels={json.dumps(label_dist)} "
                f"top3={top3_str} "
                f"sources=market_regime(5d/20d/ad/breadth)+fii_dii_flow(5d/20d)+sector_strength(breadth_sma50)"
            ),
            "trained_at": datetime.now(IST).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"ml_training_log write failed (non-fatal): {e}")

    top3_log = " > ".join(f"{f}({v:.3f})" for f, v in
                          sorted(importances.items(), key=lambda x: x[1], reverse=True)[:3])
    logger.success(
        f"\n✓ Regime classifier v2 trained: {len(X)} samples, "
        f"CV accuracy={cv_mean:.1%} ±{cv_std:.1%}\n"
        f"  Label distribution: {label_dist}\n"
        f"  Top features: {top3_log}\n"
        f"  Model saved → {MODEL_PATH}"
    )
    return True


# ── Prediction ────────────────────────────────────────────────────────────────

def predict_today(sb):
    """
    Load saved model, build today's feature vector from pre-computed columns,
    write predicted_regime + regime_confidence + regime_predicted_at to market_regime.

    FIRST RUN: model file absent → returns None silently. generate_signals falls back
    to manual regime. Run --train or wait for Sunday evolution_weekly.yml W2.
    """
    phase = cfg_int("autonomy_phase", 1)
    if phase < 2:
        logger.info(f"ml_regime_classifier: autonomy_phase={phase} < 2 — skipped (Phase 2 gate)")
        return None

    if not MODEL_PATH.exists():
        logger.warning(
            f"Model not found at {MODEL_PATH}.\n"
            "First run: run --train (or wait for Sunday evolution_weekly.yml W2).\n"
            "Manual regime from ingest_sheets controls until model exists."
        )
        return None

    with open(MODEL_PATH, "rb") as fh:
        model = pickle.load(fh)

    if META_PATH.exists():
        with open(META_PATH) as fh:
            meta = json.load(fh)
        sample_count = meta.get("sample_count", 0)
        if sample_count < MIN_PREDICT_SAMPLES:
            logger.warning(
                f"Model trained on only {sample_count} samples "
                f"(min {MIN_PREDICT_SAMPLES}) — unreliable. Skipping."
            )
            return None
        logger.info(
            f"Model v{meta.get('schema_version','1')}: {sample_count} samples, "
            f"CV={meta.get('cv_accuracy', 0):.1%}, trained={str(meta.get('trained_at','?'))[:10]}"
        )

    today    = str(today_ist())
    features = _build_feature_row(sb, today)
    if features is None:
        logger.warning(
            f"Cannot build feature vector for {today}.\n"
            "market_regime must have today's row with nifty_5d_chg_pct populated.\n"
            "Ensure ingest_sheets.py (or compute_indicators in Phase 2) has run first."
        )
        return None

    try:
        import numpy as np
    except ImportError:
        logger.error("numpy required — pip install numpy")
        return None

    proba      = model.predict_proba([features])[0]
    pred_idx   = int(np.argmax(proba))
    predicted  = LABELS[pred_idx]
    confidence = round(float(proba[pred_idx]), 4)
    proba_dist = {LABELS[i]: round(float(proba[i]), 4) for i in range(len(LABELS))}
    logger.debug(f"Regime probability distribution: {proba_dist}")

    manual_row = (
        sb.table("market_regime")
        .select("regime,id")
        .eq("date", today)
        .limit(1)
        .execute()
        .data
    )
    manual = "UNKNOWN"
    if manual_row:
        manual = _normalise_regime(manual_row[0].get("regime") or "UNKNOWN")

    update_payload = {
        "predicted_regime":    predicted,
        "regime_confidence":   confidence,
        "regime_predicted_at": datetime.now(IST).isoformat(),
    }
    try:
        if manual_row:
            sb.table("market_regime").update(update_payload).eq("date", today).execute()
        else:
            sb.table("market_regime").upsert(
                {"date": today, **update_payload}, on_conflict="date"
            ).execute()
        logger.info(
            f"market_regime updated: predicted_regime={predicted}, "
            f"confidence={confidence:.1%}"
        )
    except Exception as e:
        logger.warning(
            f"Failed to write predicted_regime: {e}\n"
            "Ensure sql_ml_regime_classifier_columns.sql migration has been run."
        )

    # ── GAP-1 FIX: denormalise predicted_regime → stock_data_daily ──────────
    # stock_data_daily.predicted_regime column exists in schema but nothing wrote it.
    # ml_provider_v2 uses regime_encoded from signal_log (which comes from
    # generate_signals reading market_regime), so this column is the bridge that
    # lets any per-stock query join regime state without joining market_regime.
    #
    # One bulk UPDATE for today's date — single query, no per-row loop.
    # Non-fatal: if stock_data_daily doesn't have today's rows yet (pipeline
    # ordering issue), this will update 0 rows and log a warning.
    _denormalise_predicted_regime(sb, today, predicted)

    result = {
        "predicted": predicted, "confidence": confidence,
        "manual": manual, "date": today, "proba_dist": proba_dist,
    }
    logger.success(
        f"Regime prediction: {predicted} (conf={confidence:.1%}) | manual={manual}"
    )
    _check_regime_disagreement(sb, predicted, manual, confidence, today)
    return result


def _denormalise_predicted_regime(sb, today: str, predicted: str) -> None:
    """
    GAP-1 FIX: Copy predicted_regime from market_regime → stock_data_daily.

    stock_data_daily.predicted_regime exists in schema (added in Phase 2 SQL
    migrations) but no script was ever writing to it. This creates a stale NULL
    for every stock row, meaning:
      - Any per-stock query wanting regime context has to join market_regime
      - ml_provider_v2 feature vector cannot read it directly from signal_log context

    Fix: single bulk UPDATE after ml_regime_classifier writes to market_regime.
    Pipeline order guarantees this runs after compute_indicators (step 10) wrote
    today's stock_data_daily rows, and before generate_signals (step 12) reads them.

    Supabase JS client doesn't expose raw SQL UPDATE ... WHERE date = X, so we
    use the REST pattern: .update({predicted_regime: value}).eq("date", today).
    This is equivalent to:
      UPDATE stock_data_daily SET predicted_regime = predicted WHERE date = today;

    Non-fatal. Logs how many rows were updated (should match today's stock count ~500).
    If 0 rows updated: stock_data_daily not yet populated for today (not a problem —
    generate_signals reads predicted_regime from market_regime directly anyway).
    """
    try:
        result = (
            sb.table("stock_data_daily")
            .update({"predicted_regime": predicted})
            .eq("date", today)
            .execute()
        )
        # Supabase returns updated rows in result.data
        n_updated = len(result.data) if result.data else 0
        if n_updated > 0:
            logger.info(
                f"stock_data_daily.predicted_regime={predicted} "
                f"denormalised to {n_updated} rows for {today}"
            )
        else:
            logger.debug(
                f"stock_data_daily.predicted_regime denormalise: 0 rows updated for {today}. "
                f"Normal if stock_data_daily not yet populated today (compute_indicators runs first)."
            )
    except Exception as e:
        logger.warning(f"stock_data_daily predicted_regime denormalise failed (non-fatal): {e}")


def _check_regime_disagreement(sb, predicted, manual, confidence, today):
    pred_tier   = LABEL_TIER.get(predicted, -1)
    manual_tier = LABEL_TIER.get(manual,    -1)
    if pred_tier < 0 or manual_tier < 0:
        return
    diff = abs(pred_tier - manual_tier)
    if diff <= 1:
        return
    severity = "ERROR" if diff >= 3 else "WARN"
    message  = (
        f"Regime disagreement: ML={predicted} ({confidence:.0%}) vs Manual={manual}. "
        f"Tier diff={diff}. "
        f"{'Review required — opposite ends of spectrum.' if diff >= 3 else 'Monitor.'}"
    )
    logger.warning(message)
    try:
        sb.table("data_anomalies").insert({
            "date": today, "check_name": "regime_ml_vs_manual", "severity": severity,
            "value": f"ML={predicted}({confidence:.0%}),Manual={manual},diff={diff}",
            "message": message, "affected": str(["market_regime"]),
            "created_at": datetime.now(IST).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"data_anomalies write failed (non-fatal): {e}")


def _should_retrain() -> bool:
    if not MODEL_PATH.exists() or not META_PATH.exists():
        return True
    try:
        with open(META_PATH) as fh:
            meta = json.load(fh)
        trained_at = datetime.fromisoformat(meta.get("trained_at", "2000-01-01"))
        return (datetime.now(IST) - trained_at).days > MODEL_STALE_DAYS
    except Exception:
        return True


def main():
    parser = argparse.ArgumentParser(description="TradeOS v6 — ML Regime Classifier v2")
    parser.add_argument("--train",   action="store_true")
    parser.add_argument("--predict", action="store_true")
    parser.add_argument("--status",  action="store_true")
    args = parser.parse_args()

    if is_kill_switch_active():
        logger.warning("Kill switch active — ml_regime_classifier skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb     = get_supabase()
    result = {}

    if args.status:
        logger.info("ML Regime Classifier v2 — Data Health Check")
        check_data_health(sb)
        if MODEL_PATH.exists() and META_PATH.exists():
            with open(META_PATH) as fh:
                meta = json.load(fh)
            logger.info(
                f"\nModel: schema_version={meta.get('schema_version','1')}\n"
                f"  Trained:  {str(meta.get('trained_at','?'))[:19]}\n"
                f"  Samples:  {meta.get('sample_count','?')}\n"
                f"  CV Acc:   {meta.get('cv_accuracy',0):.1%}\n"
                f"  Features: {meta.get('features',[])}\n"
                f"  Labels:   {meta.get('label_distribution',{})}"
            )
        else:
            logger.warning(f"\nNo model file at {MODEL_PATH}. Run --train first.")
        return result

    if args.train:
        result["trained"] = train(sb)

    smart_mode = not args.train and not args.predict
    if smart_mode and _should_retrain():
        logger.info("Smart mode: model absent or stale — training now.")
        result["trained"] = train(sb)

    if args.predict or args.train or smart_mode:
        pred = predict_today(sb)
        result["prediction"] = pred

    return result


if __name__ == "__main__":
    out  = main()
    pred = out.get("prediction")
    if pred:
        print(
            f"\npredicted_regime={pred['predicted']} "
            f"(conf={pred['confidence']:.0%}) written to market_regime"
        )
        print(f"manual regime:   {pred['manual']}")
        print(f"proba dist:      {pred.get('proba_dist',{})}")
    elif out.get("status") != "skipped" and "--predict" in sys.argv:
        print("\nNo prediction written — check logs above.")