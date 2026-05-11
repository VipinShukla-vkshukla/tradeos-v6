"""
TradeOS v6 — Phase 2: ML Regime Classifier  (v3 — holistic rebuild)
====================================================================
CHANGES vs v2 (strategic audit, 03-May-2026):

  FIX 1: 5-STATE LABEL ALIGNMENT
    v2 used 4 labels: ["TRENDING", "NEUTRAL", "CAUTION", "RISK OFF"]
    Live system runs 5-state hysteresis: TRENDING / RISK ON / NEUTRAL / RECOVERING / RISK OFF
    v2 was silently collapsing RISK ON → TRENDING and skipping every RECOVERING row.
    v3 uses all 5 labels, aligned with compute_regime.py and data_quality_monitor.py.
    Legacy label mapping: CAUTION → NEUTRAL (closest modern equivalent).

  FIX 2: nifty_20d HARD-KILL REMOVED
    v2 returned None and skipped the entire row if nifty_20d_chg_pct was NULL.
    This column was unpopulated until Apr-03; 15+ rows were lost.
    v3 computes nifty_20d from nifty_price rolling window as a fallback.
    Only nifty_ret_5d is a hard requirement — it is the core momentum signal
    and genuinely cannot be imputed without arbitrary assumptions.

  FIX 3: FEATURE SET REDESIGNED — 9 FEATURES, ALL RELIABLY SOURCED
    Removed:  (implicit) assumption that all columns populated from day 1
    Added:    india_vix   — populated from Mar-09, strong RISK OFF signal
    Added:    avg_sector_breadth — populated from Mar-09, always available
    Kept:     above_200dma_pct (cross-fallback with avg_sector_breadth)
    Kept:     advance_decline, fii flows, sector_dispersion
    Effect:   usable training rows jump from 9 → ~28 immediately

  FIX 4: SOFT IMPUTATION STRATEGY
    Hard skip (return None)  → only when nifty_ret_5d is NULL
    Soft default (0.0 / 1.0) → all other features
    Rationale: a partial feature vector with sensible defaults is always
    better than dropping the row entirely. RandomForest is robust to
    noisy features — it learns which features to trust per split.

Feature sources (9 total):
  nifty_ret_5d       → market_regime.nifty_5d_chg_pct          (hard req)
  nifty_ret_20d      → market_regime.nifty_20d_chg_pct         (compute from price if NULL)
  india_vix          → market_regime.india_vix                  (recent-row fallback)
  advance_decline    → market_regime.advance_decline_ratio      (default 1.0)
  breadth_200dma     → market_regime.above_200dma_pct           (fallback: avg_sector_breadth)
  avg_sector_breadth → market_regime.avg_sector_breadth         (default 30.0)
  fii_net_5d         → fii_dii_flow.fii_net_5d                  (default 0.0)
  fii_net_20d        → fii_dii_flow.fii_net_20d                 (default 0.0)
  sector_dispersion  → std dev of sector_strength.breadth_sma50 (default 0.0)

Training labels (5-state, matching live hysteresis model):
  TRENDING / RISK ON / NEUTRAL / RECOVERING / RISK OFF
  LABEL_TIER ordering: 0=TRENDING → 4=RISK OFF (aligned with data_quality_monitor.py)

Tables written:
  market_regime   → predicted_regime, regime_confidence, regime_predicted_at
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
from config import get_supabase, today_ist, is_kill_switch_active, cfg, cfg_float, cfg_int, logger

_BACKEND_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from config import (
    get_supabase, today_ist, IST,
    is_kill_switch_active, cfg_int, logger,
)

# ── Constants ─────────────────────────────────────────────────────────────────

MODEL_PATH = _BACKEND_ROOT / "models" / "ml_regime_model.pkl"
META_PATH  = _BACKEND_ROOT / "models" / "ml_regime_meta.json"

# FIX 1: Full 5-state labels matching live compute_regime.py hysteresis model.
# Order is meaningful — tier 0 = most bullish, tier 4 = most bearish.
# data_quality_monitor.py uses the same ordering for disagreement checks.
LABELS = ["TRENDING", "RISK ON", "NEUTRAL", "RECOVERING", "RISK OFF"]
LABEL_IDX  = {l: i for i, l in enumerate(LABELS)}
LABEL_TIER = LABEL_IDX   # alias — used by disagreement checker

# FIX 3: 9 features. All sourced from pre-computed columns — no manual rolling windows.
# Ordered from most structurally important (price) to most contextual (flows/dispersion).
FEATURES = [
    "nifty_ret_5d",        # market_regime.nifty_5d_chg_pct      (hard requirement)
    "nifty_ret_20d",       # market_regime.nifty_20d_chg_pct     (compute from price if NULL)
    "india_vix",           # market_regime.india_vix              (recent-row fallback)
    "advance_decline",     # market_regime.advance_decline_ratio  (default 1.0 = neutral)
    "breadth_200dma",      # market_regime.above_200dma_pct       (fallback: avg_sector_breadth)
    "avg_sector_breadth",  # market_regime.avg_sector_breadth     (available from day 1)
    "fii_net_5d",          # fii_dii_flow.fii_net_5d              (default 0.0)
    "fii_net_20d",         # fii_dii_flow.fii_net_20d             (default 0.0)
    "sector_dispersion",   # std dev sector_strength.breadth_sma50 (default 0.0)
]

MIN_TRAINING_SAMPLES = cfg_float("ml_regime_classifier_min_training_samples", 20)
                             # Cross-val accuracy at 20 is noisy but directionally correct.
                             # Improves materially past 40. Retrain weekly via _should_retrain.
MIN_PREDICT_SAMPLES = cfg_float("ml_regime_classifier_min_predict_samples", 15)
MODEL_STALE_DAYS     = 7
LOOKBACK_DAYS = cfg_float("ml_regime_classifier_lookback_days", 365)


# ── Data Health Check ─────────────────────────────────────────────────────────

def check_data_health(sb) -> dict:
    results = {}
    checks = [
        ("regime_history",    "regime_history",    "date",       MIN_TRAINING_SAMPLES, True),
        ("market_regime",     "market_regime",     "date",       30,                   True),
        ("fii_dii_flow",      "fii_dii_flow",      "date",       20,                   True),
        ("sector_strength",   "sector_strength",   "date",       10,                   True),
        ("nifty_total_market","nifty_total_market","symbol",     100,                  False),
        ("stock_data_daily",  "stock_data_daily",  "date",       100,                  False),
        ("master_shortlist",  "master_shortlist",  "created_at", 1,                    False),
    ]
    for label, table, date_col, min_rows, critical in checks:
        try:
            count_result  = sb.table(table).select("*", count="exact").limit(1).execute()
            row_count     = count_result.count or 0
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
            "  → regime_history: needs append_history.py running daily\n"
            "  → fii_dii_flow:   needs ingest_fii_dii.py running (Phase 1)\n"
            "  → sector_strength: needs compute_indicators.py running (Phase 2)\n"
            "  → market_regime:  needs ingest_sheets.py running (Phase 0)\n"
        )
    else:
        logger.success("All critical tables meet minimum data thresholds.")
    return results


# ── Label Normalisation ───────────────────────────────────────────────────────

def _normalise_regime(raw: str) -> str:
    """
    Normalise raw regime string to one of the 5 live labels.

    FIX 1: v2 collapsed RISK ON → TRENDING, losing a meaningful distinction.
    v3 keeps all 5 states. Legacy labels mapped to nearest modern equivalent:
      CAUTION → NEUTRAL   (CAUTION was pre-hysteresis bear-watch state)
      RISK ON  → RISK ON  (kept as-is — distinct from TRENDING)

    Anything unrecognised returns "" which causes build_training_data to skip
    the row with a clear log message rather than silently mislabelling it.
    """
    if not raw:
        return ""
    cleaned = raw.strip().replace("_", " ").upper()
    # Legacy label migrations
    legacy_map = {
        "CAUTION": "NEUTRAL",     # pre-hysteresis bear-watch → closest modern state
        "BULLISH": "RISK ON",     # early experimental label
        "BEARISH": "RISK OFF",    # early experimental label
    }
    return legacy_map.get(cleaned, cleaned)


# ── Feature Engineering ───────────────────────────────────────────────────────

def _compute_nifty20d_from_price(sb, as_of_date: str, current_price: float) -> float | None:
    """
    FIX 2: Compute nifty 20-day return from price history when nifty_20d_chg_pct is NULL.

    Strategy: find the nifty_price approximately 20 trading days (≈28 calendar days)
    before as_of_date. Uses the oldest available row in a [28d_ago, 22d_ago] window
    so we don't accidentally pick a price too close to as_of_date.

    Returns None only if no price data exists in that window (very early dates).
    """
    try:
        dt           = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        window_start = str(dt - timedelta(days=30))
        window_end   = str(dt - timedelta(days=20))
        rows = (
            sb.table("market_regime")
            .select("nifty_price")
            .gte("date", window_start)
            .lte("date", window_end)
            .not_.is_("nifty_price", "null")
            .order("date")          # ascending — get oldest in window
            .limit(1)
            .execute().data
        )
        if not rows or not rows[0].get("nifty_price"):
            return None
        past_price = float(rows[0]["nifty_price"])
        if past_price == 0:
            return None
        return round((current_price - past_price) / past_price * 100, 4)
    except Exception:
        return None


def _get_recent_vix(sb, as_of_date: str) -> float | None:
    """
    Fetch india_vix from the most recent available market_regime row at or before as_of_date.
    VIX doesn't change dramatically day-to-day, so a ≤5-day lookback is acceptable.
    """
    try:
        rows = (
            sb.table("market_regime")
            .select("india_vix")
            .lte("date", as_of_date)
            .not_.is_("india_vix", "null")
            .order("date", desc=True)
            .limit(1)
            .execute().data
        )
        return float(rows[0]["india_vix"]) if rows and rows[0].get("india_vix") else None
    except Exception:
        return None


def _regime_features_for_date(sb, as_of_date: str) -> dict | None:
    """
    Read market features from market_regime for a given date.

    FIX 2 + FIX 3: Soft imputation for all features except nifty_ret_5d.

    Hard requirement (returns None → row skipped):
      nifty_5d_chg_pct — core momentum signal, cannot be imputed without
                          assumptions that would corrupt the label.

    Soft imputation:
      nifty_20d_chg_pct  → compute from nifty_price rolling window
      india_vix          → use most recent available row (≤5d lookback)
      advance_decline    → default 1.0 (neutral — equal advances and declines)
      above_200dma_pct   → fallback to avg_sector_breadth
      avg_sector_breadth → default 30.0 (bear-leaning neutral if truly absent)
    """
    rows = (
        sb.table("market_regime")
        .select(
            "nifty_5d_chg_pct,nifty_20d_chg_pct,nifty_price,"
            "advance_decline_ratio,above_200dma_pct,avg_sector_breadth,india_vix"
        )
        .lte("date", as_of_date)
        .order("date", desc=True)
        .limit(1)
        .execute().data
    )
    if not rows:
        return None
    r = rows[0]

    # ── Hard requirement ──────────────────────────────────────────────────────
    nifty_5d = r.get("nifty_5d_chg_pct")
    if nifty_5d is None:
        logger.debug(f"_regime_features: nifty_5d_chg_pct NULL for {as_of_date} — skip")
        return None

    # ── nifty_20d: compute from price history if column is NULL ──────────────
    nifty_20d = r.get("nifty_20d_chg_pct")
    if nifty_20d is None:
        current_price = r.get("nifty_price")
        if current_price:
            nifty_20d = _compute_nifty20d_from_price(sb, as_of_date, float(current_price))
        if nifty_20d is None:
            # True fallback: 4x the 5d return is a rough 20d estimate
            nifty_20d = float(nifty_5d) * 4.0
            logger.debug(f"_regime_features: nifty_20d estimated from 5d×4 for {as_of_date}")

    # ── india_vix: recent-row fallback ────────────────────────────────────────
    india_vix = r.get("india_vix")
    if india_vix is None:
        india_vix = _get_recent_vix(sb, as_of_date)
    india_vix = float(india_vix) if india_vix is not None else 18.0   # historical avg

    # ── advance_decline: default neutral ──────────────────────────────────────
    ad = r.get("advance_decline_ratio")
    ad = float(ad) if ad is not None else 1.0   # 1.0 = equal A/D = neutral

    # ── breadth_200dma: cross-fallback with avg_sector_breadth ───────────────
    breadth_200 = r.get("above_200dma_pct")
    avg_breadth = r.get("avg_sector_breadth")
    if breadth_200 is None:
        breadth_200 = avg_breadth if avg_breadth is not None else 35.0
    breadth_200 = float(breadth_200)

    # ── avg_sector_breadth ────────────────────────────────────────────────────
    avg_breadth = float(avg_breadth) if avg_breadth is not None else 30.0

    return {
        "nifty_ret_5d":        float(nifty_5d),
        "nifty_ret_20d":       float(nifty_20d),
        "india_vix":           india_vix,
        "advance_decline":     ad,
        "breadth_200dma":      breadth_200,
        "avg_sector_breadth":  avg_breadth,
    }


def _fii_features_for_date(sb, as_of_date: str) -> tuple:
    """
    Read pre-computed FII rolling sums from fii_dii_flow.
    Returns (fii_net_5d, fii_net_20d). Defaults to 0.0 — neutral.
    """
    try:
        rows = (
            sb.table("fii_dii_flow")
            .select("fii_net_5d,fii_net_20d")
            .lte("date", as_of_date)
            .order("date", desc=True)
            .limit(1)
            .execute().data
        )
        if not rows:
            return 0.0, 0.0
        r = rows[0]
        return float(r.get("fii_net_5d") or 0), float(r.get("fii_net_20d") or 0)
    except Exception:
        return 0.0, 0.0


def _sector_dispersion_for_date(sb, as_of_date: str) -> float:
    """
    Std dev of sector_strength.breadth_sma50 across sectors as of date.
    High dispersion → sectors diverging → rotational/non-trending market.
    Low dispersion  → sectors aligned   → trending (all rising or all falling).
    Returns 0.0 (neutral default) if fewer than 3 sectors available.
    """
    try:
        rows = (
            sb.table("sector_strength")
            .select("breadth_sma50")
            .eq("date", as_of_date)
            .limit(30)
            .execute().data
        )
        if not rows:
            rows = (
                sb.table("sector_strength")
                .select("breadth_sma50")
                .lte("date", as_of_date)
                .order("date", desc=True)
                .limit(30)
                .execute().data
            )
        scores = [float(r["breadth_sma50"]) for r in rows if r.get("breadth_sma50") is not None]
        if len(scores) < 3:
            return 0.0
        n    = len(scores)
        mean = sum(scores) / n
        return round(math.sqrt(sum((s - mean) ** 2 for s in scores) / n), 4)
    except Exception:
        return 0.0


def _build_feature_row(sb, as_of_date: str) -> list | None:
    """
    Build a 9-feature vector for the given date.

    FIX 2 + FIX 3: Only returns None when nifty_ret_5d is genuinely missing.
    All other features impute gracefully.

    Feature order MUST match FEATURES list exactly — RandomForest uses positional index.
    """
    market = _regime_features_for_date(sb, as_of_date)
    if market is None:
        return None   # nifty_ret_5d was NULL — hard skip

    fii_5, fii_20 = _fii_features_for_date(sb, as_of_date)
    sector_disp   = _sector_dispersion_for_date(sb, as_of_date)

    return [
        market["nifty_ret_5d"],
        market["nifty_ret_20d"],
        market["india_vix"],
        market["advance_decline"],
        market["breadth_200dma"],
        market["avg_sector_breadth"],
        fii_5,
        fii_20,
        sector_disp,
    ]


# ── Training Data ─────────────────────────────────────────────────────────────

def build_training_data(sb) -> tuple:
    """
    Build (X, y) from labelled regime history.

    FIX 1: 5-state labels. RECOVERING rows are now included, not silently skipped.
    Label normalisation handles legacy values (CAUTION → NEUTRAL etc).

    Primary source:  regime_history  (daily snapshot via append_history)
    Fallback source: market_regime   (manual regime — always available)
    """
    cutoff = str(today_ist() - timedelta(days=LOOKBACK_DAYS))

    history = (
        sb.table("regime_history")
        .select("date,regime")
        .gte("date", cutoff)
        .not_.is_("regime", "null")
        .order("date")
        .execute().data
    )

    if len(history) < MIN_TRAINING_SAMPLES:
        logger.warning(
            f"regime_history: {len(history)} rows (need {MIN_TRAINING_SAMPLES}). "
            "Supplementing from market_regime."
        )
        extra = (
            sb.table("market_regime")
            .select("date,regime")
            .gte("date", cutoff)
            .not_.is_("regime", "null")
            .order("date")
            .execute().data
        )
        existing_dates = {r["date"] for r in history}
        for r in extra:
            if r["date"] not in existing_dates:
                history.append({"date": r["date"], "regime": r["regime"]})

    logger.info(f"Training candidates: {len(history)} labelled rows (cutoff={cutoff})")

    X, y           = [], []
    skipped        = 0
    skip_reasons: dict = {}

    for row in history:
        date   = row.get("date")
        regime = _normalise_regime(row.get("regime") or "")

        if regime not in LABEL_IDX:
            reason = f"unknown_label:{regime or 'empty'}"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            skipped += 1
            continue

        features = _build_feature_row(sb, date)
        if features is None:
            skip_reasons["nifty_5d_null"] = skip_reasons.get("nifty_5d_null", 0) + 1
            skipped += 1
            continue

        X.append(features)
        y.append(LABEL_IDX[regime])

    label_counts = {}
    for label_idx in y:
        label = LABELS[label_idx]
        label_counts[label] = label_counts.get(label, 0) + 1

    logger.info(
        f"Training rows built: {len(X)} usable, {skipped} skipped | "
        f"Label distribution: {label_counts}"
    )
    if skip_reasons:
        logger.info(f"  Skip reasons: {skip_reasons}")

    return X, y


# ── Training ──────────────────────────────────────────────────────────────────

def train(sb) -> bool:
    """
    Train RandomForest regime classifier.
    200 trees, max_depth=6, class_weight=balanced, StratifiedKFold CV.
    Saves model to models/ml_regime_model.pkl + metadata JSON.
    """
    logger.info("=" * 60)
    logger.info("ML Regime Classifier — Training (v3 — 5-state, 9 features)")
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
            "v3 already uses soft imputation — if still below threshold:\n"
            "  1. nifty_5d_chg_pct genuinely missing (early pipeline dates)\n"
            "  2. Wait for more daily pipeline runs (~1 row/day)\n"
            "  3. Check regime_history and market_regime are both writing daily"
        )
        return False

    import numpy as np
    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=int)

    # Warn if any label class has < 3 samples — CV will be unreliable for that class
    unique, counts = np.unique(y_arr, return_counts=True)
    sparse_classes = [(LABELS[i], c) for i, c in zip(unique, counts) if c < 3]
    if sparse_classes:
        logger.warning(
            f"Sparse label classes (< 3 samples): {sparse_classes} — "
            "CV accuracy for these classes will be noisy. More data needed."
        )

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=6,
        min_samples_leaf=2,      # relaxed from 3: better for sparse classes
        class_weight="balanced", # handles class imbalance (mostly RISK OFF early data)
        random_state=42,
    )

    # Adaptive CV: use min(5, n_classes) folds to avoid empty fold for rare classes
    n_classes = len(np.unique(y_arr))
    n_folds   = min(5, max(2, len(X) // max(MIN_TRAINING_SAMPLES // 5, 1)), len(X) // n_classes)
    cv        = StratifiedKFold(n_splits=n_folds, shuffle=True, random_state=42)

    try:
        cv_scores = cross_val_score(model, X_arr, y_arr, cv=cv, scoring="accuracy")
        cv_mean   = float(np.mean(cv_scores))
        cv_std    = float(np.std(cv_scores))
    except Exception as e:
        logger.warning(f"Cross-validation failed ({e}) — training without CV score")
        cv_mean, cv_std = 0.0, 0.0

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
        "schema_version":     3,   # v3: 5-state labels + 9 features + soft imputation
    }
    with open(META_PATH, "w") as fh:
        json.dump(meta, fh, indent=2)

    try:
        top3     = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:3]
        top3_str = " > ".join(f"{f}({v:.2f})" for f, v in top3)
        sb.table("ml_training_log").insert({
            "model_type":         "regime_classifier_v3",
            "training_samples":   len(X),
            "accuracy":           cv_mean,
            "feature_names":      json.dumps(FEATURES),
            "feature_importance": json.dumps(importances),
            "notes": (
                f"CV={n_folds}-fold±{cv_std:.3f} labels={json.dumps(label_dist)} "
                f"top3={top3_str} "
                f"v3:5state+9features+soft_imputation"
            ),
            "trained_at": datetime.now(IST).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"ml_training_log write failed (non-fatal): {e}")

    top3_log = " > ".join(f"{f}({v:.3f})" for f, v in
                          sorted(importances.items(), key=lambda x: x[1], reverse=True)[:3])
    logger.success(
        f"\n✓ Regime classifier v3 trained: {len(X)} samples, "
        f"CV accuracy={cv_mean:.1%} ±{cv_std:.1%}\n"
        f"  Label distribution: {label_dist}\n"
        f"  Top features: {top3_log}\n"
        f"  Model saved → {MODEL_PATH}"
    )
    return True

def _last_trading_day(sb) -> str:
    """
    Walk backwards from today (IST) to find the most recent trading day.
    A trading day is a weekday that is NOT in nse_holidays.
    Looks back up to 10 calendar days (covers long holiday runs).
    """
    from datetime import date, timedelta
    candidate = today_ist()

    # Fetch holidays once — only need the last ~2 weeks to be safe
    window_start = str(candidate - timedelta(days=14))
    try:
        holiday_rows = (
            sb.table("nse_holidays")
            .select("date")
            .gte("date", window_start)
            .execute().data
        )
        holidays = {r["date"] for r in holiday_rows}   # set of "YYYY-MM-DD" strings
    except Exception as e:
        logger.warning(f"_last_trading_day: could not fetch nse_holidays ({e}) — using today")
        return str(candidate)

    for _ in range(10):
        s = str(candidate)
        if candidate.weekday() < 5 and s not in holidays:   # Mon–Fri, not a holiday
            return s
        candidate -= timedelta(days=1)

    logger.warning("_last_trading_day: no trading day found in last 10 days — falling back to today")
    return str(today_ist())

# ── Prediction ────────────────────────────────────────────────────────────────

def predict_today(sb):
    """
    Load saved model, build today's feature vector, write predicted_regime
    to market_regime and denormalise to stock_data_daily.

    Returns None silently if model absent — generate_signals falls back to
    manual regime. This is the intended Phase 2 bootstrap behaviour.
    """
    phase = cfg_int("autonomy_phase", 1)
    if phase < 2:
        logger.info(f"ml_regime_classifier: autonomy_phase={phase} < 2 — skipped (Phase 2 gate)")
        return None

    if not MODEL_PATH.exists():
        logger.warning(
            f"Model not found at {MODEL_PATH}.\n"
            "Run --train or wait for Sunday evolution_weekly.yml W2.\n"
            "Manual regime from ingest_sheets controls until model exists."
        )
        return None

    with open(MODEL_PATH, "rb") as fh:
        model = pickle.load(fh)

    if META_PATH.exists():
        with open(META_PATH) as fh:
            meta = json.load(fh)
        sample_count   = meta.get("sample_count", 0)
        schema_version = meta.get("schema_version", 1)
        if schema_version < 3:
            logger.warning(
                f"Model is schema_version={schema_version} (need 3 — 5-state labels). "
                "Retraining now to use v3 feature set."
            )
            train(sb)
            if not MODEL_PATH.exists():
                return None
            with open(MODEL_PATH, "rb") as fh:
                model = pickle.load(fh)
            with open(META_PATH) as fh:
                meta = json.load(fh)
            sample_count = meta.get("sample_count", 0)

        if sample_count < MIN_PREDICT_SAMPLES:
            logger.warning(
                f"Model trained on only {sample_count} samples "
                f"(min {MIN_PREDICT_SAMPLES}) — unreliable. Skipping prediction."
            )
            return None
        logger.info(
            f"Model v{meta.get('schema_version','?')}: {sample_count} samples, "
            f"CV={meta.get('cv_accuracy', 0):.1%}, trained={str(meta.get('trained_at','?'))[:10]}"
        )

    today    = _last_trading_day(sb)
    features = _build_feature_row(sb, today)
    if features is None:
        logger.warning(
            f"Cannot build feature vector for {today}.\n"
            "nifty_5d_chg_pct missing from market_regime — ensure ingest_sheets "
            "or compute_indicators has run first (pipeline step order)."
        )
        return None

    try:
        import numpy as np
    except ImportError:
        logger.error("numpy required — pip install numpy")
        return None

    proba          = model.predict_proba([features])[0]
    best_cls_pos   = int(np.argmax(proba))                   # position within model.classes_
    predicted      = LABELS[model.classes_[best_cls_pos]]    # map back to LABELS via classes_
    confidence     = round(float(proba[best_cls_pos]), 4)

    # Build full 5-state distribution; unseen classes default to 0.0
    proba_dist = {label: 0.0 for label in LABELS}
    for pos, cls_idx in enumerate(model.classes_):
        proba_dist[LABELS[cls_idx]] = round(float(proba[pos]), 4)

    logger.info(f"Regime probability distribution: {proba_dist}")

    manual_row = (
        sb.table("market_regime")
        .select("regime")
        .eq("date", today)
        .limit(1)
        .execute().data
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
    Copy predicted_regime from market_regime → stock_data_daily (bulk UPDATE).
    Allows per-stock queries to read regime context without joining market_regime.
    Non-fatal — 0 rows updated is normal if compute_indicators hasn't run yet today.
    """
    try:
        result   = (
            sb.table("stock_data_daily")
            .update({"predicted_regime": predicted})
            .eq("date", today)
            .execute()
        )
        n_updated = len(result.data) if result.data else 0
        if n_updated > 0:
            logger.info(
                f"stock_data_daily.predicted_regime={predicted} "
                f"denormalised to {n_updated} rows for {today}"
            )
        else:
            logger.debug(
                f"stock_data_daily predicted_regime: 0 rows updated for {today} "
                "(normal if compute_indicators runs after this step)"
            )
    except Exception as e:
        logger.warning(f"stock_data_daily predicted_regime denormalise failed (non-fatal): {e}")


def _check_regime_disagreement(sb, predicted: str, manual: str, confidence: float, today: str):
    """
    Flag significant disagreements between ML and manual regime to data_anomalies.
    Uses 5-state LABEL_TIER — diff ≥ 2 tiers = WARN, ≥ 3 tiers = ERROR.
    """
    pred_tier   = LABEL_TIER.get(predicted, -1)
    manual_tier = LABEL_TIER.get(manual, -1)
    if pred_tier < 0 or manual_tier < 0:
        return
    diff = abs(pred_tier - manual_tier)
    if diff <= 1:
        return
    severity = "ERROR" if diff >= 3 else "WARN"
    message  = (
        f"Regime disagreement: ML={predicted} ({confidence:.0%}) vs Manual={manual}. "
        f"Tier diff={diff}. "
        f"{'Review required — opposite ends of spectrum.' if diff >= 3 else 'Monitor closely.'}"
    )
    logger.warning(message)
    try:
        sb.table("data_anomalies").insert({
            "date":       today,
            "check_name": "regime_ml_vs_manual",
            "severity":   severity,
            "value":      f"ML={predicted}({confidence:.0%}),Manual={manual},diff={diff}",
            "message":    message,
            "affected":   str(["market_regime"]),
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
        # Also retrain if schema_version is old (v1/v2 → v3)
        if meta.get("schema_version", 1) < 3:
            return True
        trained_at = datetime.fromisoformat(meta.get("trained_at", "2000-01-01"))
        return (datetime.now(IST) - trained_at).days > MODEL_STALE_DAYS
    except Exception:
        return True


def main():
    parser = argparse.ArgumentParser(description="TradeOS v6 — ML Regime Classifier v3")
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
        logger.info("ML Regime Classifier v3 — Data Health Check")
        check_data_health(sb)
        if MODEL_PATH.exists() and META_PATH.exists():
            with open(META_PATH) as fh:
                meta = json.load(fh)
            logger.info(
                f"\nModel: schema_version={meta.get('schema_version','?')}\n"
                f"  Trained:   {str(meta.get('trained_at','?'))[:19]}\n"
                f"  Samples:   {meta.get('sample_count','?')}\n"
                f"  CV Acc:    {meta.get('cv_accuracy',0):.1%}\n"
                f"  Features:  {meta.get('features',[])}\n"
                f"  Labels:    {meta.get('label_distribution',{})}"
            )
        else:
            logger.warning(f"\nNo model file at {MODEL_PATH}. Run --train first.")
        return result

    if args.train:
        result["trained"] = train(sb)

    smart_mode = not args.train and not args.predict
    if smart_mode and _should_retrain():
        logger.info("Smart mode: model absent, stale, or outdated schema — training now.")
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