"""
TradeOS v6 — Engine Registry
=============================
Declarative hard-filter gates for all nine screener engines, plus engine
lifecycle state.

THE PROBLEM THIS SOLVES
-----------------------
Only four engines were configurable. strategy_config held CTL, SBS, TPO and
EAP; the six proprietary scanners — VBD, IAD, RSB, MOM, RVS, SEC — had every
threshold hardcoded in screen_stocks.py:

    if pct_chg < 3.0:              continue
    if vol_r   < 1.8:              continue
    if delivery < 35:              continue

Consequences:
  - Two thirds of the engines could not be tuned, disabled or retired without
    editing Python and redeploying.
  - The Brain could propose changes to CTL/SBS thresholds but was structurally
    blind to the six engines that actually produce most candidates (RVS alone
    contributed 65 of 124 on 2026-07-24).
  - There was no way to run an engine in shadow — you either shipped it live
    or deleted it.

GATES ARE DATA, SCORING IS CODE
-------------------------------
A deliberate split:

  GATES   binary include/exclude filters. Simple comparisons on one field.
          Safe to express as data and safe to expose to a UI slider, because
          the worst case is an empty or oversized candidate list.

  SCORING the non-linear banded logic (`if 4 <= pct_chg <= 7: score += 25`).
          Stays in Python. It encodes genuine strategy insight, it is not
          expressible as a threshold, and letting it be edited live would make
          historical scores incomparable — which would destroy the outcome
          attribution the whole system depends on.

LIFECYCLE
---------
  ACTIVE   contributes to composite_score and can produce candidates
  SHADOW   runs and records candidates, but its score is EXCLUDED from the
           composite. This is how a new engine earns its place: it accumulates
           screener_performance history with zero effect on live decisions.
  RETIRED  does not run at all

The SHADOW state is what makes "integrate new strategies" safe. A new engine
runs alongside the live ones for weeks, its forward returns are logged, and it
is promoted only once its hit rate justifies it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger
from config import get_supabase, cfg


LIFECYCLE_ACTIVE  = "ACTIVE"
LIFECYCLE_SHADOW  = "SHADOW"
LIFECYCLE_RETIRED = "RETIRED"

ENGINES = ["CTL", "SBS", "TPO", "VBD", "IAD", "RSB", "MOM", "RVS", "SEC"]


# ─────────────────────────────────────────────────────────────────────────────
# FAMILIES — the swing merge, which is not the same thing as a retirement
# ─────────────────────────────────────────────────────────────────────────────
#
# Merging keeps every detection and changes only who gets credit for it.
# Retiring stops the detection contributing at all. Stage 6's acceptance
# criterion — "detection rate per surviving engine >= 3x prior" — is reachable
# only by the first: you cannot raise a sample rate by deleting the samples.
#
# So the seven ACTIVE screeners keep running exactly as they do. Their hits are
# attributed to one CONTINUATION identity, which is what raises the per-engine
# sample rate and makes a quarterly verdict statistically possible. Measured
# over 4,747 resolved detections, the family carries ~4,600 of them against
# CTL's 1,739 alone.
#
# WHAT ACTUALLY CHANGES IS THE CONVERGENCE BONUS.
#
# Today a name found by CTL, SEC and MOM scores a convergence bonus for three
# engines agreeing. Measured Jaccard co-occurrence says CTL-SEC is 0.41 and
# CTL-MOM 0.25 — the only real overlap in the whole set — so that bonus was
# paid precisely where the agreement was least independent. Within a family,
# agreement is expected and earns nothing. Two DIFFERENT families agreeing
# still earns it, because that is two different burdens of proof being met.
#
# RVS and MOM are deliberately NOT in the family. They are shadowed on measured
# performance (RVS n=1,471 mean -0.49% win 42%; MOM n=1,090 mean +0.05%), which
# is a separate decision from the merge and stands on its own evidence. Keeping
# them as their own families means their forward returns stay separable for the
# shadow quarter rather than being absorbed into the survivors' record.
FAMILIES = {
    "CTL": "CONTINUATION",   # the core: consolidation/trend-alignment breakout
    "SEC": "CONTINUATION",   # sector-led continuation — 0.41 overlap with CTL
    "TPO": "CONTINUATION",
    "SBS": "CONTINUATION",
    "VBD": "CONTINUATION",
    "RSB": "CONTINUATION",
    "IAD": "CONTINUATION",
    "MOM": "MOM",            # shadowed; own family so its evidence stays separable
    "RVS": "RVS",            # shadowed; same
}


def engine_family(engine: str) -> str:
    """
    Which family an engine's detections are scored under.

    Config-overridable per engine so a family can be split back apart without a
    deploy. The burden of proof runs both ways: a merge that turns out to have
    hidden an independent signal must be reversible the same day.
    """
    e = (engine or "").upper()
    default = FAMILIES.get(e, e)
    return (cfg(f"screener_engine_{e.lower()}_family", default) or default).upper()

_registry_cache: dict | None = None


# ─────────────────────────────────────────────────────────────────────────────
# GATE EVALUATION
# ─────────────────────────────────────────────────────────────────────────────
#
# A gate spec maps a stock field to constraints:
#
#   {"rsi_daily":    {"min": 55}}                    field >= 55
#   {"pct_change":   {"min": 3.0, "max": 12.0}}      3.0 <= field <= 12.0
#   {"above_sma50":  {"eq": true}}                   field is truthy
#   {"dist_sma50":   {"abs_max": 4}}                 abs(field) <= 4
#   {"consol_range": {"max": 12, "or_field": "bb_squeeze"}}
#                                                    field <= 12 OR bb_squeeze
#   {"__any__wk":    {"any_of": ["wk_hi_high", "wk_hi_low"]}}
#                                                    at least one is truthy
#   {"__cmp__di":    {"gt_field": ["di_plus", "di_minus"]}}
#                                                    di_plus > di_minus
#
# `missing` controls what happens when the field is NULL. Default "block" —
# consistent with the null-as-pass fix in generate_signals, where treating an
# absent indicator as a pass caused partial data failures to produce MORE
# signals rather than fewer.

BB_SQUEEZE_MAX_WIDTH_PCT = 7.0   # must match run_sbs in screen_stocks.py


def derive_fields(stock: dict) -> dict:
    """
    Add fields the engines compute inline but which are NOT columns on
    stock_data_daily.

    bb_squeeze is the live case: run_sbs computes it from bb_upper/bb_lower/
    close rather than reading a column, so a gate referencing it directly would
    always evaluate as missing. Selecting it from Postgres fails outright
    ("column stock_data_daily.bb_squeeze does not exist"), which is how this
    surfaced. The same derivation is mirrored in the Control Room's
    /api/engines/universe route so the UI preview and the engine agree.
    """
    if "bb_squeeze" in stock:
        return stock
    up    = float(stock.get("bb_upper") or 0)
    lo    = float(stock.get("bb_lower") or 0)
    close = float(stock.get("close") or stock.get("current_price") or 0)
    width = ((up - lo) / close * 100) if (close > 0 and up > lo) else 999.0
    stock["bb_width_pct"] = round(width, 2) if width < 999 else None
    stock["bb_squeeze"]   = width < BB_SQUEEZE_MAX_WIDTH_PCT
    return stock


def _get(stock: dict, field: str):
    v = stock.get(field)
    if v is None or v == "":
        return None
    return v


def check_gate(stock: dict, field: str, spec: dict,
               sector_rank: dict | None = None) -> bool:
    """Evaluate one gate. Returns True when the stock passes."""
    missing_policy = spec.get("missing", "block")

    # ── Composite forms ──────────────────────────────────────────────────────
    if "any_of" in spec:
        return any(bool(_get(stock, f)) for f in spec["any_of"])

    if "gt_field" in spec:
        a, b = spec["gt_field"]
        va, vb = _get(stock, a), _get(stock, b)
        if va is None or vb is None:
            return missing_policy == "pass"
        return float(va) > float(vb)

    # ── sector_rank is derived, not a stock column ───────────────────────────
    if field == "sector_rank":
        sec = stock.get("sector") or ""
        rank = (sector_rank or {}).get(sec, 99)
        if "max" in spec and rank > spec["max"]:
            return False
        return True

    value = _get(stock, field)
    if value is None:
        # An OR-escape may still rescue it (consol_range OR bb_squeeze).
        if "or_field" in spec and bool(_get(stock, spec["or_field"])):
            return True
        return missing_policy == "pass"

    if "eq" in spec:
        return bool(value) == bool(spec["eq"])

    try:
        num = float(value)
    except (TypeError, ValueError):
        return missing_policy == "pass"

    if "abs_max" in spec and abs(num) > float(spec["abs_max"]):
        return False
    if "min" in spec and num < float(spec["min"]):
        if "or_field" in spec and bool(_get(stock, spec["or_field"])):
            return True
        return False
    if "max" in spec and num > float(spec["max"]):
        if "or_field" in spec and bool(_get(stock, spec["or_field"])):
            return True
        return False
    return True


def passes_gates(stock: dict, gates: dict,
                 sector_rank: dict | None = None) -> tuple[bool, str | None]:
    """
    Apply every gate. Returns (passed, first_failing_gate).

    The failing gate name is returned rather than a bare bool so the Control
    Room can show WHY a stock was excluded, and so the funnel diagnostic can
    report which gate is doing the filtering — that is how the SBS
    `vol_ratio >= 1.3` + `consol_range <= 12` tension became visible.
    """
    for field, spec in (gates or {}).items():
        if not check_gate(stock, field, spec, sector_rank):
            return False, field
    return True, None


def gate_funnel(stocks: dict, gates: dict,
                sector_rank: dict | None = None) -> list[dict]:
    """
    Cumulative survivor count per gate, in declaration order.

    Feeds the Control Room's live what-if preview: adjust a threshold and see
    exactly how many stocks each gate removes, before committing the change.
    """
    remaining = [derive_fields(dict(s)) for s in stocks.values()]
    out = []
    for field, spec in (gates or {}).items():
        before = len(remaining)
        remaining = [s for s in remaining if check_gate(s, field, spec, sector_rank)]
        out.append({
            "gate":    field,
            "spec":    spec,
            "before":  before,
            "after":   len(remaining),
            "removed": before - len(remaining),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# REGISTRY LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_registry(refresh: bool = False) -> dict:
    """
    {engine: {lifecycle, min_score, gates, scoring_params, ...}} for all nine.

    Falls back to DEFAULT_ENGINE_CONFIG so a database outage degrades to the
    behaviour compiled into the code rather than screening nothing.
    """
    global _registry_cache
    if _registry_cache is not None and not refresh:
        return _registry_cache

    cfg: dict = {}
    try:
        rows = get_supabase().table("strategy_config").select("*").execute().data
        for r in rows:
            name = r["strategy"]
            if name not in ENGINES:
                continue          # EAP is an overlay, not an engine
            from config import _coerce_params
            params = _coerce_params(name, r["params"])
            cfg[name] = {
                "lifecycle":      params.get("lifecycle",
                                             LIFECYCLE_ACTIVE if r.get("enabled", True)
                                             else LIFECYCLE_RETIRED),
                "min_score":      params.get("min_score"),
                "gates":          params.get("gates", {}),
                "params":         params,
                "enabled":        bool(r.get("enabled", True)),
            }
    except Exception as e:
        logger.warning(f"engine registry load failed: {e} — using code defaults")

    # Any engine absent from the DB falls back to its compiled default.
    for name in ENGINES:
        if name not in cfg or not cfg[name].get("gates"):
            d = DEFAULT_ENGINE_CONFIG.get(name, {})
            cfg.setdefault(name, {})
            cfg[name].setdefault("lifecycle", d.get("lifecycle", LIFECYCLE_ACTIVE))
            cfg[name].setdefault("enabled", True)
            if not cfg[name].get("gates"):
                cfg[name]["gates"] = d.get("gates", {})
            if cfg[name].get("min_score") is None:
                cfg[name]["min_score"] = d.get("min_score")
            cfg[name].setdefault("params", d)

    _registry_cache = cfg
    return cfg


def engine_lifecycle(engine: str) -> str:
    return load_registry().get(engine, {}).get("lifecycle", LIFECYCLE_ACTIVE)


def is_runnable(engine: str) -> bool:
    """RETIRED engines are skipped entirely; SHADOW still runs."""
    return engine_lifecycle(engine) != LIFECYCLE_RETIRED


def counts_toward_composite(engine: str) -> bool:
    """Only ACTIVE engines influence composite_score. SHADOW records only."""
    return engine_lifecycle(engine) == LIFECYCLE_ACTIVE


def engine_gates(engine: str) -> dict:
    return load_registry().get(engine, {}).get("gates", {})


def engine_min_score(engine: str, default: float) -> float:
    v = load_registry().get(engine, {}).get("min_score")
    return float(v) if v is not None else default


# ─────────────────────────────────────────────────────────────────────────────
# DEFAULTS — extracted verbatim from screen_stocks.py
# ─────────────────────────────────────────────────────────────────────────────
# These reproduce the hardcoded behaviour exactly, so seeding the registry is
# behaviour-neutral on day one. Every value here was previously a literal in
# the engine function of the same name.

DEFAULT_ENGINE_CONFIG = {
    "CTL": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": None,
        "label": "Core Trend Leaders",
        "gates": {
            "sector_rank":  {"max": 8},
            "rsi_monthly":  {"min": 58},
            "rsi_weekly":   {"min": 58},
            "ret_6m":       {"min": 0},
            "atr_pct":      {"max": 4.5},
            "market_cap":   {"min": 300},
            "above_sma50":  {"eq": True},
            "adx":          {"min": 12},
            "__di__":       {"gt_field": ["di_plus", "di_minus"]},
        },
    },
    "SBS": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": None,
        "label": "Structural Breakout Swing",
        "gates": {
            "sector_rank":  {"max": 10},
            "rsi_daily":    {"min": 52},
            "rsi_weekly":   {"min": 54},
            "atr_pct":      {"max": 5.5},
            "vol_ratio":    {"min": 1.1},
            "market_cap":   {"min": 300},
            # Tight base OR a volatility squeeze — the squeeze is the OR-escape.
            "consol_range": {"max": 15, "or_field": "bb_squeeze"},
            "above_sma50":  {"eq": True},
            "__wk__":       {"any_of": ["wk_hi_high", "wk_hi_low"]},
        },
    },
    "TPO": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": None,
        "label": "Trend Pullback Opportunities",
        "gates": {
            "dist_sma50":   {"abs_max": 4},
            "atr_pct":      {"max": 4},
            "above_sma50":  {"eq": True},
            "rsi_weekly":   {"min": 50},
        },
        "note": "RSI window is regime-dependent and applied in code, not as a gate.",
    },
    "VBD": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": 55,
        "label": "Velocity Burst Detector",
        "gates": {
            "pct_change":   {"min": 3.0, "max": 12.0},
            "vol_ratio":    {"min": 1.8},
            "delivery_pct": {"min": 35},
            "market_cap":   {"min": 300},
            "above_sma50":  {"eq": True},
            "sector_rank":  {"max": 12},
        },
    },
    "IAD": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": 55,
        "label": "Institutional Accumulation Detector",
        "gates": {
            "delivery_pct": {"min": 50},
            "vol_ratio":    {"min": 1.2},
            "market_cap":   {"min": 300},
            "above_sma50":  {"eq": True},
            "consol_range": {"max": 20},
            "sector_rank":  {"max": 14},
        },
    },
    "RSB": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": 55,
        "label": "Relative Strength Breakout",
        "gates": {
            "rs_vs_nifty":  {"min": 4},
            "consol_range": {"max": 12},
            "above_sma50":  {"eq": True},
            "market_cap":   {"min": 300},
            "rsi_daily":    {"min": 48},
            "sector_rank":  {"max": 12},
        },
    },
    "MOM": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": 45,
        "label": "Momentum Continuation",
        "gates": {
            "above_sma50":  {"eq": True},
            "sma50_gt_200": {"eq": True},
            "above_st":     {"eq": True},
            "adx":          {"min": 18},
            "__di__":       {"gt_field": ["di_plus", "di_minus"]},
            "rsi_daily":    {"min": 52, "max": 74},
            "rsi_weekly":   {"min": 54},
            "macd_hist":    {"min": 0.0001},
            "__wk__":       {"any_of": ["wk_hi_high", "wk_hi_low"]},
            "market_cap":   {"min": 300},
            "sector_rank":  {"max": 12},
        },
        "note": "Skipped entirely in RISK OFF; stricter ADX/RSI floors in RECOVERING.",
    },
    "RVS": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": 55,
        "label": "Reversal Setup",
        "gates": {
            "rsi_daily":    {"min": 36, "max": 54},
            "rsi_monthly":  {"min": 48},
            "sma50_gt_200": {"eq": True},
            "ret_6m":       {"min": 0},
            "dist_sma50":   {"min": -10},
            "market_cap":   {"min": 300},
            "sector_rank":  {"max": 12},
        },
        "note": "Sector gate auto-relaxed by +2 in RECOVERING / RISK OFF.",
    },
    "SEC": {
        "lifecycle": LIFECYCLE_ACTIVE, "min_score": 55,
        "label": "Sector Rotation",
        "gates": {
            "rsi_daily":    {"min": 48, "max": 72},
            "above_sma50":  {"eq": True},
            "market_cap":   {"min": 300},
            "__di__":       {"gt_field": ["di_plus", "di_minus"]},
        },
        "note": "Universe is restricted to the top N sectors before gates apply.",
    },
}
