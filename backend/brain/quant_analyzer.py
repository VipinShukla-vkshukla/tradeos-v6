"""
TradeOS v6 — Brain Engine v2: Quantitative Analyzer
=====================================================
Full-field statistical analysis. Receives complete DataFrames (SELECT *)
and decides internally what correlates with outcomes.

ANALYSES:
  1.  Threshold sensitivity       — all signal_threshold_* keys
  2.  Engine performance          — all 10 engines vs baseline
  3.  Score correlation           — ALL numeric fields vs forward return
  4.  Categorical field analysis  — bb_context, vwap_alignment, lifecycle, etc.
  5.  Signal type quality         — PRIME/BREAKOUT/STAGED/REENTRY win rates
  6.  Regime accuracy             — does regime classification predict success?
  7.  Lesson reliability          — which lessons are proven vs noise
  8.  Self-improvement note mining — recurring AI observations
  9.  Regime × Engine interaction — engine performance WITHIN each regime
  10. Multi-field combination     — find 2-field combinations that outperform
  11. Temporal decay              — does signal quality degrade over time?
  12. AI conviction accuracy      — does AI HIGH/MEDIUM/LOW actually differentiate?
  13. Brain impact measurement    — did past proposals improve win rates?
"""

import json
import re
from collections import defaultdict
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

MIN_SAMPLES  = 15   # minimum for any statistic
MIN_PROPOSAL = 10   # minimum for a proposal to be generated
# Dynamic lists — resolved at runtime from DynamicRegistry.
# DO NOT add hardcoded values here. Instead:
#   New engine:    add to system_config.regime_engine_weights JSON
#   New cat field: add column to signal_log table in Supabase
#   New threshold: add signal_threshold_* key to system_config
# All three are auto-discovered on next brain run with zero code changes.
 
# Kept as emergency fallback only — used when registry discovery fails
# and only to avoid a crash, never for normal operation.
_ENGINES_FALLBACK     = ["CTL","SBS","TPO","VBD","IAD","RSB","MOM","RVS","SEC","EAP"]
_CAT_FIELDS_FALLBACK  = ["bb_context","vwap_alignment","momentum_phase","velocity_state",
                          "lifecycle","weekly_structure","active_regime","regime","sector"]

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _win_rate(df: pd.DataFrame) -> Optional[float]:
    if df.empty or "outcome_win" not in df.columns:
        return None
    v = df["outcome_win"].dropna()
    return round(float(v.mean()) * 100, 1) if len(v) >= MIN_SAMPLES else None


def _pearson(a: pd.Series, b: pd.Series) -> Optional[float]:
    both = pd.DataFrame({"a": a, "b": b}).dropna()
    if len(both) < MIN_SAMPLES:
        return None
    if both["a"].nunique() < 2 or both["b"].nunique() < 2:
        return None  # zero-variance column -> corr() returns nan, not None;
                      # catch it here so every caller gets this fix for free
    r = both["a"].corr(both["b"])
    return round(float(r), 3) if pd.notna(r) else None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _evaluable(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty or "outcome_label" not in signals.columns:
        return pd.DataFrame()
    return signals[signals["outcome_label"].notna()].copy()


# ─────────────────────────────────────────────────────────────────────────────
# 1. THRESHOLD SENSITIVITY
# ─────────────────────────────────────────────────────────────────────────────

def analyze_threshold_sensitivity(signals: pd.DataFrame, config: dict,
                                   registry=None) -> list[dict]:
    ev = _evaluable(signals)
    if len(ev) < MIN_SAMPLES * 2:
        return []
 
    # Use live threshold map from registry; fall back to deriving from config keys
    threshold_map = (registry.threshold_map if registry
                     else _derive_threshold_map_from_config(config, list(ev.columns)))
 
    findings = []
    for cfg_key, (field, signal_group, direction) in threshold_map.items():
        if field not in ev.columns:
            continue
        current_val = _safe_float(config.get(cfg_key, {}).get("value"))
        if current_val is None:
            continue

        type_col = next((c for c in ["signal_subtype","signal_type"] if c in ev.columns), None)
        if signal_group.startswith("regime:"):
            regime_val = signal_group.split(":", 1)[1]
            regime_col = next((c for c in ["regime","active_regime"] if c in ev.columns), None)
            if regime_col:
                mask = ev[regime_col] == regime_val
                subset = ev[mask][[field,"outcome_win","max_fwd_return"]].dropna(subset=[field])
            else:
                subset = pd.DataFrame()
        elif type_col:
            mask = ev[type_col].str.lower().str.contains(signal_group, na=False)
            subset = ev[mask][[field,"outcome_win","max_fwd_return"]].dropna(subset=[field])
        else:
            subset = ev[[field,"outcome_win","max_fwd_return"]].dropna(subset=[field])

        if len(subset) < MIN_PROPOSAL:
            continue

        try:
            subset = subset.copy()
            subset["_q"] = pd.qcut(subset[field], q=5, duplicates="drop", labels=False)
        except Exception:
            continue

        bstats = subset.groupby("_q").agg(
            n=("outcome_win","count"),
            wr=("outcome_win","mean"),
            mean_val=(field,"mean"),
            avg_ret=("max_fwd_return","mean"),
        ).reset_index()

        if len(bstats) < 3:
            continue

        bstats["wr"] *= 100

        if direction == "floor":
            low_wr, high_wr = bstats.iloc[0]["wr"], bstats.iloc[-1]["wr"]
            low_n,  low_v   = bstats.iloc[0]["n"],  bstats.iloc[0]["mean_val"]
            if low_wr < 40 and (high_wr - low_wr) > 10 and low_n >= 5:
                proposed = round(current_val * 1.10, 2)
                findings.append({
                    "type":      "THRESHOLD_CHANGE",
                    "key":       cfg_key,
                    "field":     field,
                    "current":   current_val,
                    "proposed":  proposed,
                    "direction": "increase_floor",
                    "low_bucket_wr":  round(low_wr, 1),
                    "high_bucket_wr": round(high_wr, 1),
                    "delta_wr":  round(high_wr - low_wr, 1),
                    "sample_size": int(len(subset)),
                    "low_n":     int(low_n),
                    "low_val":   round(float(low_v), 2),
                    "evidence_summary": (
                        f"Signals with {field}≈{low_v:.1f} win {low_wr:.0f}% (n={low_n}) "
                        f"vs {high_wr:.0f}% for higher values. "
                        f"Raising {cfg_key}: {current_val}→{proposed} eliminates weakest cohort."
                    ),
                    "confidence": min(0.87, 0.50 + (high_wr-low_wr)/100 + len(subset)/600),
                    "source": "quant",
                    "priority": 3,
                })

        elif direction == "ceiling":
            high_wr, low_wr = bstats.iloc[-1]["wr"], bstats.iloc[0]["wr"]
            high_n, high_v  = bstats.iloc[-1]["n"],  bstats.iloc[-1]["mean_val"]
            if high_wr < 40 and (low_wr - high_wr) > 10 and high_n >= 5:
                proposed = round(current_val * 0.90, 2)
                findings.append({
                    "type":      "THRESHOLD_CHANGE",
                    "key":       cfg_key,
                    "field":     field,
                    "current":   current_val,
                    "proposed":  proposed,
                    "direction": "decrease_ceiling",
                    "high_bucket_wr": round(high_wr, 1),
                    "low_bucket_wr":  round(low_wr, 1),
                    "delta_wr":  round(low_wr - high_wr, 1),
                    "sample_size": int(len(subset)),
                    "high_n":    int(high_n),
                    "high_val":  round(float(high_v), 2),
                    "evidence_summary": (
                        f"Signals with {field}≈{high_v:.1f} win only {high_wr:.0f}% (n={high_n}). "
                        f"Lowering ceiling {cfg_key}: {current_val}→{proposed} removes overextended cohort."
                    ),
                    "confidence": min(0.87, 0.50 + (low_wr-high_wr)/100 + len(subset)/600),
                    "source": "quant",
                    "priority": 3,
                })

    logger.info(f"  Threshold sensitivity: {len(findings)} proposals")
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# 1b. SCORE COMPONENT SENSITIVITY
# ─────────────────────────────────────────────────────────────────────────────

def analyze_score_component_sensitivity(signals: pd.DataFrame, config: dict,
                                          registry=None) -> list[dict]:
    """
    Additive score bonus/penalty magnitudes (score_bonus_*, score_penalty_*,
    industry_bonus_*) tied to a condition — e.g. "+2 if psar_dual_confirmed".

    Different mechanism from threshold sensitivity above: these don't filter
    which signals exist, they nudge score on signals that already qualified.
    Evidence is the win-rate gap between signals where the condition held vs
    didn't; the proposal scales the magnitude toward that gap. This does NOT
    simulate the downstream effect of a magnitude change on ranking/selection
    (that would mean re-running the screener counterfactually) — it validates
    that the underlying evidence is real and the current magnitude is roughly
    sized to it, nothing more. SCORE_WEIGHT_CHANGE defaults to review-only
    in the auto-apply policy for exactly this reason.
    """
    ev = _evaluable(signals)
    if len(ev) < MIN_SAMPLES * 2:
        return []

    comp_map = registry.score_component_map if registry else {}
    findings = []

    for cfg_key, spec in comp_map.items():
        field = spec["field"]
        if field not in ev.columns:
            continue
        current_val = _safe_float(config.get(cfg_key, {}).get("value"))
        if current_val is None:
            continue

        op  = spec["op"]
        col = ev[field]

        try:
            if op == "eq":
                mask = col == spec["value"]
            elif op == "gte":
                mask = pd.to_numeric(col, errors="coerce") >= spec["value"]
            elif op == "gte_cfg":
                cutoff = _safe_float(config.get(spec["value"], {}).get("value"))
                if cutoff is None:
                    continue
                mask = pd.to_numeric(col, errors="coerce") >= cutoff
            elif op == "range":
                lo, hi = spec["value"]
                numeric = pd.to_numeric(col, errors="coerce")
                mask = numeric >= lo
                if hi is None:
                    cutoff = _safe_float(config.get("risk_penalty_threshold", {}).get("value"))
                    mask = mask & (numeric < cutoff) if cutoff is not None else mask & False
                else:
                    mask = mask & (numeric < hi)
            else:
                continue
        except Exception:
            continue

        mask = mask.fillna(False)
        with_cond, without_cond = ev[mask], ev[~mask]
        n_with, n_without = len(with_cond), len(without_cond)
        if n_with < MIN_PROPOSAL or n_without < MIN_PROPOSAL:
            continue

        wr_with, wr_without = _win_rate(with_cond), _win_rate(without_cond)
        if wr_with is None or wr_without is None:
            continue

        gap        = wr_with - wr_without
        is_penalty = current_val < 0

        if not is_penalty:
            if gap > 12:
                proposed, direction = round(current_val * 1.10, 2), "increase_bonus"
            elif gap < 3:
                proposed, direction = round(current_val * 0.90, 2), "decrease_bonus"
            else:
                continue
        else:
            if gap < -12:
                proposed, direction = round(current_val * 1.10, 2), "increase_penalty"
            elif gap > -3:
                proposed, direction = round(current_val * 0.90, 2), "decrease_penalty"
            else:
                continue

        # Don't trust a magnitude read off a sliver of the population either way
        pop_share   = n_with / (n_with + n_without) * 100
        high_impact = pop_share < 5 or pop_share > 95

        findings.append({
            "type":      "SCORE_WEIGHT_CHANGE",
            "key":       cfg_key,
            "field":     field,
            "current":   current_val,
            "proposed":  proposed,
            "direction": direction,
            "wr_with":   wr_with,
            "wr_without": wr_without,
            "delta_wr":  round(gap, 1),
            "sample_size": int(n_with + n_without),
            "condition_share_pct": round(pop_share, 1),
            "high_impact": high_impact,
            "evidence_summary": (
                f"Signals where {cfg_key}'s condition holds ({field}, n={n_with}, "
                f"{pop_share:.0f}% of pool) win {wr_with:.0f}% vs {wr_without:.0f}% "
                f"otherwise ({gap:+.1f}pp). Current magnitude {current_val} → {proposed}."
            ),
            "confidence": min(0.70, 0.45 + abs(gap)/100 + min(n_with, n_without)/400),
            "source": "quant",
            "priority": 4,
        })

    logger.info(f"  Score component sensitivity: {len(findings)} proposals")
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# 2. ENGINE PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────

def analyze_engine_performance(signals: pd.DataFrame, registry=None) -> list[dict]:
    ev = _evaluable(signals)
    if len(ev) < MIN_SAMPLES:
        return []

    pat_col = next((c for c in ["scanner_patterns","engines_list"] if c in ev.columns), None)
    if not pat_col:
        return []

    baseline = _win_rate(ev) or 50.0
    findings = []

    engines = registry.engines if registry else _ENGINES_FALLBACK
    for engine in engines:
        mask   = ev[pat_col].str.contains(engine, na=False, case=False)
        subset = ev[mask]
        if len(subset) < MIN_PROPOSAL:
            continue
        wr     = _win_rate(subset)
        if wr is None:
            continue
        delta  = round(wr - baseline, 1)
        if abs(delta) < 8:
            continue

        avg_ret = round(float(subset["max_fwd_return"].dropna().mean()), 2) \
                  if "max_fwd_return" in subset.columns else None

        findings.append({
            "type":      "ENGINE_PERFORMANCE",
            "engine":    engine,
            "win_rate":  wr,
            "baseline":  round(baseline, 1),
            "delta_wr":  delta,
            "count":     int(len(subset)),
            "avg_ret":   avg_ret,
            "direction": "OUTPERFORMING" if delta > 0 else "UNDERPERFORMING",
            "evidence_summary": (
                f"Engine {engine}: {wr:.0f}% WR ({delta:+.0f}pp vs {baseline:.0f}% baseline, "
                f"n={len(subset)}, avg_ret={avg_ret}%)"
            ),
            "confidence": min(0.82, 0.50 + abs(delta)/100 + len(subset)/400),
            "source": "quant",
            "priority": 4,
        })

    logger.info(f"  Engine performance: {len(findings)} divergences (baseline {baseline:.1f}%)")
    return findings


# ─────────────────────────────────────────────────────────────────────────────
# 3. SCORE CORRELATION — ALL NUMERIC FIELDS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_score_correlation(signals: pd.DataFrame) -> dict:
    """Pearson correlation of EVERY numeric column vs forward return."""
    ev = _evaluable(signals)
    if ev.empty or "max_fwd_return" not in ev.columns:
        return {}

    numeric_cols = ev.select_dtypes(include="number").columns.tolist()
    exclude      = {"outcome_win","outcome_loss","id","signal_id",
                    "max_fwd_return","min_fwd_return","ret_fwd_5d",
                    "ret_fwd_10d","ret_fwd_15d","ret_fwd_20d"}
    correlations = {}

    for col in numeric_cols:
        if col in exclude or col.startswith("ret_fwd_"):
            continue
        r = _pearson(ev[col], ev["max_fwd_return"])
        if r is not None:
            correlations[col] = r

    if correlations:
        ranked = sorted(correlations.items(), key=lambda x: abs(x[1]), reverse=True)
        logger.info(f"  Score correlations: {len(correlations)} fields. "
                    f"Top: {ranked[0][0]}={ranked[0][1]:.3f}, "
                    f"Bottom: {ranked[-1][0]}={ranked[-1][1]:.3f}")
    return correlations


# ─────────────────────────────────────────────────────────────────────────────
# 4. CATEGORICAL FIELD ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_categorical_fields(signals: pd.DataFrame, registry=None) -> dict:
    """
    For every categorical field, compute win rate per category value.
    Flags high-performing and low-performing categories.
    This is what tells the brain: "bb_context=SQUEEZE wins 71%, UPPER_BAND wins 38%"
    """
    ev = _evaluable(signals)
    if ev.empty:
        return {}

    results = {}
    baseline = _win_rate(ev) or 50.0

    cat_fields = registry.categorical_fields if registry else _CAT_FIELDS_FALLBACK
    for field in cat_fields:
        if field not in ev.columns:
            continue
        col = ev[field].astype(str).str.strip().str.upper()
        col = col.replace({"NAN":"", "NONE":"", "NULL":""})
        col = col[col != ""]

        if col.nunique() < 2 or col.nunique() > 20:
            continue

        field_result = {"baseline": round(baseline, 1), "categories": {}}
        for cat_val, grp in ev.groupby(col):
            if len(grp) < 5:
                continue
            wr = _win_rate(grp)
            if wr is not None:
                field_result["categories"][str(cat_val)] = {
                    "wr":    wr,
                    "n":     int(len(grp)),
                    "delta": round(wr - baseline, 1),
                }

        if len(field_result["categories"]) >= 2:
            wrs = [v["wr"] for v in field_result["categories"].values()]
            field_result["spread"] = round(max(wrs) - min(wrs), 1)
            if field_result["spread"] >= 10:  # only useful if there's meaningful spread
                results[field] = field_result

    logger.info(f"  Categorical analysis: {len(results)} fields with meaningful spread")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 5. SIGNAL TYPE QUALITY
# ─────────────────────────────────────────────────────────────────────────────

def analyze_signal_type_quality(signals: pd.DataFrame) -> dict:
    ev = _evaluable(signals)
    if ev.empty:
        return {}
    col = next((c for c in ["signal_subtype","signal_type"] if c in ev.columns), None)
    if not col:
        return {}

    results = {}
    for stype, grp in ev.groupby(col):
        if len(grp) < MIN_SAMPLES:
            continue
        results[str(stype)] = {
            "win_rate":  _win_rate(grp),
            "count":     int(len(grp)),
            "avg_score": round(float(grp["score_adjusted"].dropna().mean()), 1)
                         if "score_adjusted" in grp.columns else None,
            "avg_ret":   round(float(grp["max_fwd_return"].dropna().mean()), 2)
                         if "max_fwd_return" in grp.columns else None,
        }
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 6. REGIME ACCURACY
# ─────────────────────────────────────────────────────────────────────────────

def analyze_regime_accuracy(signals: pd.DataFrame) -> dict:
    ev  = _evaluable(signals)
    col = next((c for c in ["active_regime","regime"] if c in ev.columns), None)
    if not col or ev.empty:
        return {}

    results = {}
    for regime, grp in ev.groupby(col):
        if len(grp) < MIN_SAMPLES:
            continue
        results[str(regime)] = {
            "win_rate": _win_rate(grp),
            "count":    int(len(grp)),
            "avg_ret":  round(float(grp["max_fwd_return"].dropna().mean()), 2)
                        if "max_fwd_return" in grp.columns else None,
        }
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 7. REGIME × ENGINE INTERACTION
# ─────────────────────────────────────────────────────────────────────────────

def analyze_regime_engine_interaction(signals: pd.DataFrame, registry=None) -> list[dict]:
    """
    Finds engine win rates within each specific regime.
    Critical for calibrating regime_engine_weights in system_config.
    Example: "IAD engine wins 74% in RECOVERING but only 41% in RISK ON"
    """
    ev = _evaluable(signals)
    if len(ev) < MIN_SAMPLES * 2:
        return []

    regime_col = next((c for c in ["active_regime","regime"] if c in ev.columns), None)
    pat_col    = next((c for c in ["scanner_patterns","engines_list"] if c in ev.columns), None)
    if not regime_col or not pat_col:
        return []

    interactions = []
    overall_wr   = _win_rate(ev) or 50.0

    engines = registry.engines if registry else _ENGINES_FALLBACK
    for regime, regime_grp in ev.groupby(regime_col):
        for engine in engines:
            mask   = regime_grp[pat_col].str.contains(engine, na=False, case=False)
            subset = regime_grp[mask]
            if len(subset) < 8:
                continue
            wr = _win_rate(subset)
            if wr is None:
                continue
            delta = round(wr - overall_wr, 1)
            if abs(delta) < 12:  # only flag strong regime-engine interactions
                continue
            interactions.append({
                "regime":    str(regime),
                "engine":    engine,
                "win_rate":  wr,
                "baseline":  round(overall_wr, 1),
                "delta_wr":  delta,
                "count":     int(len(subset)),
                "direction": "BOOST" if delta > 0 else "DRAG",
                "evidence_summary": (
                    f"In {regime} regime, {engine} wins {wr:.0f}% "
                    f"({delta:+.0f}pp vs {overall_wr:.0f}% overall, n={len(subset)}). "
                    f"regime_engine_weights[{regime}][{engine}] may need adjustment."
                ),
                "confidence": min(0.80, 0.50 + abs(delta)/100 + len(subset)/200),
                "source": "quant",
                "priority": 3,
                "type": "ENGINE_WEIGHT",
            })

    logger.info(f"  Regime×Engine interactions: {len(interactions)} strong interactions")
    return interactions


# ─────────────────────────────────────────────────────────────────────────────
# 8. MULTI-FIELD COMBINATION ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

def analyze_field_combinations(signals: pd.DataFrame) -> list[dict]:
    """
    Find 2-field combinations that significantly outperform either field alone.
    Limited to top-10 correlated numeric fields to keep compute reasonable.
    """
    ev = _evaluable(signals)
    if len(ev) < MIN_SAMPLES * 3:
        return []

    # Get top correlated numeric fields
    numeric = ev.select_dtypes(include="number").columns.tolist()
    exclude = {"outcome_win","outcome_loss","id","max_fwd_return","min_fwd_return"}
    numeric = [c for c in numeric if c not in exclude and not c.startswith("ret_fwd_")]

    if len(numeric) < 2 or "max_fwd_return" not in ev.columns:
        return []

    # Pre-rank by |correlation|
    corrs = {}
    for col in numeric[:30]:
        r = _pearson(ev[col], ev["max_fwd_return"])
        if r is not None:
            corrs[col] = abs(r)
    top_fields = sorted(corrs, key=corrs.get, reverse=True)[:8]

    baseline = _win_rate(ev) or 50.0
    combos   = []

    for f1, f2 in combinations(top_fields, 2):
        sub = ev[[f1, f2, "outcome_win", "max_fwd_return"]].dropna()
        if len(sub) < MIN_PROPOSAL:
            continue

        # Both above median simultaneously
        try:
            m1, m2 = sub[f1].median(), sub[f2].median()
            both_high = sub[(sub[f1] >= m1) & (sub[f2] >= m2)]
            if len(both_high) < 8:
                continue
            combo_wr = _win_rate(both_high)
            if combo_wr is None:
                continue
            delta = round(combo_wr - baseline, 1)
            if delta < 12:  # only flag meaningful combinations
                continue
            combos.append({
                "fields":    [f1, f2],
                "condition": f"{f1}≥median AND {f2}≥median",
                "win_rate":  combo_wr,
                "baseline":  round(baseline, 1),
                "delta_wr":  delta,
                "count":     int(len(both_high)),
                "evidence_summary": (
                    f"When both {f1}≥{m1:.1f} AND {f2}≥{m2:.1f}: "
                    f"win rate {combo_wr:.0f}% (+{delta:.0f}pp vs {baseline:.0f}%, n={len(both_high)}). "
                    f"Combined filter is more predictive than either field alone."
                ),
                "confidence": min(0.75, 0.50 + delta/100 + len(both_high)/300),
                "type":      "INSIGHT",
                "source":    "quant",
                "priority":  5,
            })
        except Exception:
            continue

    combos.sort(key=lambda x: x["delta_wr"], reverse=True)
    logger.info(f"  Multi-field combinations: {len(combos)} meaningful combinations found")
    return combos[:5]  # top 5 only


# ─────────────────────────────────────────────────────────────────────────────
# 9. AI CONVICTION ACCURACY
# ─────────────────────────────────────────────────────────────────────────────

def analyze_ai_conviction_accuracy(signals: pd.DataFrame) -> dict:
    ev = _evaluable(signals)
    if ev.empty or "ai_conviction" not in ev.columns:
        return {}

    results = {}
    baseline = _win_rate(ev) or 50.0

    for conviction in ["HIGH","MEDIUM","LOW","UNKNOWN"]:
        mask = ev["ai_conviction"].str.upper() == conviction
        grp  = ev[mask]
        if len(grp) < 5:
            continue
        wr = _win_rate(grp)
        if wr is not None:
            results[conviction] = {
                "win_rate": wr,
                "count":    int(len(grp)),
                "delta":    round(wr - baseline, 1),
            }

    # Check if HIGH > MEDIUM > LOW (correct calibration)
    if all(c in results for c in ["HIGH","MEDIUM","LOW"]):
        calibrated = (results["HIGH"]["win_rate"] > results["MEDIUM"]["win_rate"] >
                      results["LOW"]["win_rate"])
        results["_calibrated"]        = calibrated
        results["_spread"]            = round(
            results["HIGH"]["win_rate"] - results["LOW"]["win_rate"], 1
        )
        results["_calibration_note"]  = (
            "AI conviction correctly differentiates outcomes" if calibrated else
            "WARNING: AI conviction is NOT differentiating — HIGH does not outperform LOW"
        )

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 10. BRAIN IMPACT MEASUREMENT
# ─────────────────────────────────────────────────────────────────────────────

def analyze_brain_impact(signals: pd.DataFrame, past_proposals: pd.DataFrame) -> list[dict]:
    """
    For each previously applied proposal, compare win rates before vs after.
    This is the core measurement of whether the brain is actually helping.
    """
    if past_proposals is None or past_proposals.empty:
        return []
    if signals.empty or "outcome_label" not in signals.columns:
        return []

    applied = past_proposals[past_proposals.get("status", pd.Series()) == "APPLIED"] \
              if "status" in past_proposals.columns else past_proposals

    from datetime import timedelta
    results = []

    for _, proposal in applied.iterrows():
        applied_at  = proposal.get("applied_at")
        target_key  = proposal.get("target_key")
        bt          = proposal.get("backtest_result") or {}
        if isinstance(bt, str):
            try: bt = json.loads(bt)
            except: bt = {}

        if applied_at is None:
            continue

        try:
            apply_date = pd.to_datetime(applied_at).date()
        except Exception:
            continue

        from datetime import date as dt_date
        window = 30
        before_mask = (signals["date"] >= apply_date - timedelta(days=window)) & \
                      (signals["date"] < apply_date)
        after_mask  = (signals["date"] >= apply_date) & \
                      (signals["date"] < apply_date + timedelta(days=window))

        before_grp = signals[before_mask]
        after_grp  = signals[after_mask]

        wr_before = _win_rate(before_grp)
        wr_after  = _win_rate(after_grp)

        if wr_before is None or wr_after is None:
            continue

        actual_delta    = round(wr_after - wr_before, 1)
        predicted_delta = _safe_float(bt.get("wr_delta"))

        results.append({
            "proposal_id":      int(proposal.get("id", 0)),
            "target_key":       target_key,
            "applied_at":       str(apply_date),
            "wr_before":        wr_before,
            "wr_after":         wr_after,
            "actual_delta":     actual_delta,
            "predicted_delta":  predicted_delta,
            "prediction_error": round(actual_delta - predicted_delta, 1)
                                if predicted_delta is not None else None,
            "n_before":         int(len(before_grp)),
            "n_after":          int(len(after_grp)),
            "verdict": (
                "CONFIRMED" if actual_delta > 0 else
                ("NEGATIVE" if actual_delta < -3 else "NEUTRAL")
            ),
        })

    if results:
        confirmed = sum(1 for r in results if r["verdict"] == "CONFIRMED")
        logger.info(f"  Brain impact: {confirmed}/{len(results)} applied proposals confirmed positive")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 11. LESSON RELIABILITY
# ─────────────────────────────────────────────────────────────────────────────

def analyze_lesson_reliability(lessons: pd.DataFrame) -> dict:
    if lessons is None or lessons.empty:
        return {}

    proven, unproven, retire = [], [], []
    for _, row in lessons.iterrows():
        times = int(row.get("times_applied") or 0)
        conf  = float(row.get("confidence") or 0)
        entry = {
            "rule":    str(row.get("corrective_rule",""))[:100],
            "strategy":str(row.get("strategy",""))[:40],
            "scenario":str(row.get("scenario_type",""))[:40],
            "times":   times,
            "conf":    round(conf, 2),
        }
        if times >= 10 and conf >= 0.70:
            proven.append(entry)
        elif times >= 10 and conf < 0.30:
            retire.append(entry)
        elif times < 5:
            unproven.append(entry)

    logger.info(f"  Lessons: {len(proven)} proven, {len(retire)} retire candidates")
    return {"proven": proven, "unproven": unproven, "retire_candidates": retire}


# ─────────────────────────────────────────────────────────────────────────────
# 12. SELF-IMPROVEMENT NOTE MINING
# ─────────────────────────────────────────────────────────────────────────────

def mine_improvement_notes(notes: list[dict]) -> list[dict]:
    if not notes:
        return []

    groups = defaultdict(list)
    for note in notes:
        rule = note.get("suggested_rule","").strip()
        if not rule:
            continue
        key = re.sub(r'\s+', ' ', rule[:45].lower())
        groups[key].append(note)

    aggregated = []
    conf_map   = {"HIGH": 0.85, "MEDIUM": 0.65, "LOW": 0.45}

    for key, group in groups.items():
        if len(group) < 2:
            continue
        confs = [conf_map.get(n.get("confidence","MEDIUM"), 0.65) for n in group]
        aggregated.append({
            "type":             "INSIGHT",
            "source":           "llm_observation",
            "recurrence_count": len(group),
            "suggested_rule":   group[0].get("suggested_rule",""),
            "applies_to_sectors": list({s for n in group
                                        for s in n.get("applies_to_sectors",[])}),
            "avg_confidence":   round(sum(confs)/len(confs), 2),
            "first_seen":       str(min(n.get("date","") for n in group)),
            "last_seen":        str(max(n.get("date","") for n in group)),
            "evidence_summary": (
                f"Recurring AI observation ({len(group)}x over analysis window): "
                f"'{group[0].get('suggested_rule','')[:80]}'"
            ),
            "confidence": round(min(0.80, 0.50 + len(group)*0.06), 2),
            "priority":   4,
        })

    logger.info(f"  Improvement notes: {len(aggregated)} recurring patterns")
    return aggregated


# ─────────────────────────────────────────────────────────────────────────────
# MASTER RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def _derive_threshold_map_from_config(config: dict, signal_cols: list) -> dict:
    """
    Lightweight fallback: derive threshold map from config keys using naming
    convention without needing a DB connection.
    Delegates to dynamic_registry.discover_threshold_field_map if available.
    """
    try:
        from brain.dynamic_registry import discover_threshold_field_map
        return discover_threshold_field_map(config, signal_cols)
    except ImportError:
        # Ultra-minimal fallback: return empty dict — analysis still runs,
        # threshold proposals will be skipped for this cycle.
        logger.warning("  dynamic_registry not importable — threshold analysis skipped")
        return {}

def run_analysis(dataset: dict, registry=None) -> dict:
    signals       = dataset.get("signals",       pd.DataFrame())
    lessons       = dataset.get("lessons",       pd.DataFrame())
    past_proposals= dataset.get("past_proposals",pd.DataFrame())
    config        = dataset.get("config",        {})
    sim_notes     = dataset.get("improvement_notes", [])
 
    # Build registry if not passed in (allows standalone testing)
    if registry is None:
        try:
            from brain.dynamic_registry import DynamicRegistry
            registry = DynamicRegistry(dataset)
            logger.info(f"  {registry.summary()}")
        except Exception as e:
            logger.warning(f"  DynamicRegistry unavailable ({e}) — using fallbacks")
 
    logger.info("Running quantitative analysis (all fields)...")
 
    findings = {
        "threshold_proposals":    analyze_threshold_sensitivity(signals, config, registry),
        "score_component_findings": analyze_score_component_sensitivity(signals, config, registry),
        "engine_performance":     analyze_engine_performance(signals, registry),
        "score_correlations":     analyze_score_correlation(signals),
        "categorical_analysis":   analyze_categorical_fields(signals, registry),
        "signal_type_quality":    analyze_signal_type_quality(signals),
        "regime_accuracy":        analyze_regime_accuracy(signals),
        "regime_engine_interactions": analyze_regime_engine_interaction(signals, registry),
        "field_combinations":     analyze_field_combinations(signals),
        "ai_conviction_accuracy": analyze_ai_conviction_accuracy(signals),
        "brain_impact":           analyze_brain_impact(signals, past_proposals),
        "lesson_reliability":     analyze_lesson_reliability(lessons),
        "improvement_insights":   mine_improvement_notes(sim_notes),
        "metadata": {
            "signals_analyzed":      dataset.get("signals_analyzed", 0),
            "signals_with_outcomes": dataset.get("signals_with_outcomes", 0),
            "coverage_pct":          dataset.get("coverage_pct", 0),
            "fields_analyzed":       len(signals.columns) if not signals.empty else 0,
        },
    }

    total = (len(findings["threshold_proposals"])
             + len(findings["score_component_findings"])
             + len(findings["engine_performance"])
             + len(findings["regime_engine_interactions"])
             + len(findings["improvement_insights"]))
    logger.info(f"Quant analysis complete: {total} actionable findings across "
                f"{findings['metadata']['fields_analyzed']} fields")
    return findings