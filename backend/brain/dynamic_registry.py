"""
TradeOS v6 — Brain Engine v2: Dynamic Registry
================================================
Replaces ALL hardcoded lists in the brain with live discovery from:
  - Supabase information_schema  → table names, column names, column types
  - system_config                → engine names (from regime_engine_weights JSON)
  - system_config key patterns   → threshold field mappings (derived, not hardcoded)

The three problems this solves:
  1. CATEGORICAL_FIELDS   — auto-discovered from signal_log column types + cardinality
  2. THRESHOLD_FIELD_MAP  — auto-derived from system_config key naming convention
  3. KNOWN_TABLES         — auto-discovered from Supabase public schema
  4. ENGINES list         — auto-read from regime_engine_weights in system_config

HOW TO ADD NEW DATA POINTS IN FUTURE:
  - New table:           add it to Supabase → discovered automatically next brain run
  - New signal_log col:  add to table → auto-included in categorical/numeric analyses
  - New engine:          add to regime_engine_weights JSON in system_config → auto-picked up
  - New threshold key:   add to system_config with prefix signal_threshold_* → auto-mapped
  - New config key:      add to system_config → brain reads it in next analysis

ZERO manual updates to brain code required for any of the above.

CACHING:
  Results are cached in brain_script_registry (tables) and in-process for the
  duration of a single brain run. Cache is refreshed at the start of each run.
"""

import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase


# ─────────────────────────────────────────────────────────────────────────────
# 1. TABLE DISCOVERY — replaces KNOWN_TABLES in script_scanner.py
# ─────────────────────────────────────────────────────────────────────────────

def discover_all_tables(sb=None, schema: str = "public") -> list[str]:
    """
    Query Supabase/Postgres information_schema to get every table in the
    public schema. No hardcoded list — new tables appear automatically.

    Returns sorted list of table names.
    """
    sb = sb or get_supabase()
    try:
        # Supabase exposes information_schema via RPC or direct query
        # Using a raw SQL approach via the REST API
        result = sb.rpc("get_public_tables", {}).execute()
        if result.data:
            tables = [r["table_name"] for r in result.data]
            logger.debug(f"  Discovered {len(tables)} tables via RPC")
            return sorted(tables)
    except Exception:
        pass

    # Fallback: query information_schema directly
    try:
        result = (sb.table("information_schema.tables")
                    .select("table_name")
                    .eq("table_schema", schema)
                    .eq("table_type", "BASE TABLE")
                    .execute())
        if result.data:
            tables = [r["table_name"] for r in result.data]
            logger.debug(f"  Discovered {len(tables)} tables via information_schema")
            return sorted(tables)
    except Exception:
        pass

    # Second fallback: use pg_tables via rpc
    try:
        result = sb.rpc("exec_sql", {
            "query": f"SELECT tablename FROM pg_tables WHERE schemaname='{schema}' ORDER BY tablename"
        }).execute()
        if result.data:
            tables = [r["tablename"] for r in result.data]
            return sorted(tables)
    except Exception:
        pass

    # Final fallback: known core tables (only used if all DB discovery fails)
    logger.warning("  Table discovery via DB failed — using cached registry fallback")
    return _get_tables_from_registry(sb)


def _get_tables_from_registry(sb) -> list[str]:
    """Read table names from brain_script_registry as a fallback."""
    try:
        rows = sb.table("brain_script_registry").select("tables_read,tables_written").execute().data
        tables = set()
        for row in rows:
            for field in ["tables_read", "tables_written"]:
                val = row.get(field) or []
                if isinstance(val, list):
                    tables.update(val)
                elif isinstance(val, str):
                    try:
                        tables.update(json.loads(val))
                    except Exception:
                        pass
        return sorted(tables)
    except Exception:
        return []


def discover_table_columns(sb, table: str) -> dict:
    """
    Get all column names and types for a given table.
    Returns {column_name: data_type} dict.
    Used by script_scanner and quant_analyzer.
    """
    sb = sb or get_supabase()
    try:
        result = (sb.table("information_schema.columns")
                    .select("column_name,data_type,is_nullable")
                    .eq("table_schema", "public")
                    .eq("table_name", table)
                    .execute())
        if result.data:
            return {r["column_name"]: r["data_type"] for r in result.data}
    except Exception:
        pass

    # Fallback: load one row and infer types from pandas
    try:
        row = sb.table(table).select("*").limit(1).execute().data
        if row:
            df = pd.DataFrame(row)
            return {col: str(df[col].dtype) for col in df.columns}
    except Exception:
        pass

    return {}


# ─────────────────────────────────────────────────────────────────────────────
# 2. ENGINE DISCOVERY — replaces hardcoded ENGINES list everywhere
# ─────────────────────────────────────────────────────────────────────────────

def discover_engines(sb=None, config: dict = None) -> list[str]:
    """
    Auto-discover engine names from regime_engine_weights in system_config.

    The JSON structure is: {"REGIME": {"ENGINE_NAME": weight, ...}, ...}
    Engine names are the inner keys — union across all regimes.

    If no engine weights found: falls back to scanning signal_log.scanner_patterns
    to discover whatever engines have been used historically.

    Adding a new engine: add it to regime_engine_weights JSON → auto-discovered.
    Removing an engine: remove from the JSON → stops appearing in analysis.
    """
    sb = sb or get_supabase()

    # Primary: read from system_config
    if config:
        raw = config.get("regime_engine_weights", {}).get("value", "{}")
    else:
        try:
            row = (sb.table("system_config")
                     .select("value")
                     .eq("key", "regime_engine_weights")
                     .execute().data)
            raw = row[0]["value"] if row else "{}"
        except Exception:
            raw = "{}"

    try:
        weights = json.loads(raw) if isinstance(raw, str) else raw
        engines = set()
        for regime_weights in weights.values():
            if isinstance(regime_weights, dict):
                engines.update(regime_weights.keys())
        if engines:
            logger.debug(f"  Discovered {len(engines)} engines from regime_engine_weights: {sorted(engines)}")
            return sorted(engines)
    except Exception:
        pass

    # Fallback: scan historical scanner_patterns in signal_log
    try:
        rows = (sb.table("signal_log")
                  .select("scanner_patterns")
                  .not_.is_("scanner_patterns", "null")
                  .limit(500)
                  .execute().data)
        engines = set()
        pattern = re.compile(r'\b([A-Z]{2,6})\b')  # 2-6 uppercase letter codes
        for row in rows:
            val = row.get("scanner_patterns", "") or ""
            engines.update(pattern.findall(val))
        # Filter to plausible engine names (2-6 chars, all caps)
        engines = {e for e in engines if 2 <= len(e) <= 6}
        if engines:
            logger.debug(f"  Discovered {len(engines)} engines from scanner_patterns: {sorted(engines)}")
            return sorted(engines)
    except Exception:
        pass

    # Fallback: import directly from screen_stocks (always in sync with actual engines)
    try:
        from signals.screen_stocks import _REGIME_ENGINE_WEIGHTS_DEFAULT
        engines = set()
        for regime_weights in _REGIME_ENGINE_WEIGHTS_DEFAULT.values():
            if isinstance(regime_weights, dict):
                engines.update(regime_weights.keys())
        if engines:
            logger.info(f"  Engine discovery: {len(engines)} engines from screen_stocks default "
                        f"(tip: add regime_engine_weights to system_config to override)")
            return sorted(engines)
    except Exception as e:
        logger.debug(f"  screen_stocks fallback failed: {e}")

    logger.warning("  Engine discovery failed — no engines found")
    return []


def discover_regime_names(sb=None, config: dict = None) -> list[str]:
    """
    Auto-discover all regime names from regime_engine_weights outer keys.
    Adapts automatically if new regimes are added.
    """
    sb = sb or get_supabase()

    if config:
        raw = config.get("regime_engine_weights", {}).get("value", "{}")
    else:
        try:
            row = (sb.table("system_config")
                     .select("value")
                     .eq("key", "regime_engine_weights")
                     .execute().data)
            raw = row[0]["value"] if row else "{}"
        except Exception:
            raw = "{}"

    try:
        weights = json.loads(raw) if isinstance(raw, str) else raw
        regimes = list(weights.keys())
        if regimes:
            return sorted(regimes)
    except Exception:
        pass

    # Fallback: distinct values from signal_log
    try:
        col = next(iter(["active_regime", "regime"]), None)
        rows = sb.table("signal_log").select(col).not_.is_(col, "null").limit(200).execute().data
        return sorted({r.get(col,"") for r in rows if r.get(col)})
    except Exception:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# 3. CATEGORICAL FIELD DISCOVERY — replaces hardcoded CATEGORICAL_FIELDS
# ─────────────────────────────────────────────────────────────────────────────

def discover_categorical_fields(signals: pd.DataFrame = None,
                                 sb=None, table: str = "signal_log") -> list[str]:
    """
    Dynamically discover categorical fields from signal_log (or any DataFrame).

    A field is categorical if:
      - dtype is object (string)
      - Has between 2 and 25 distinct non-null values
      - Not an ID, date, symbol, or free-text field
      - Has >30% non-null coverage

    This means new categorical fields added to signal_log are automatically
    included in categorical analysis on the next brain run. No code changes.
    """
    EXCLUDE_PATTERNS = [
        r"^id$", r"_id$", r"^date", r"^created", r"^updated",
        r"symbol", r"company_name", r"rationale", r"ai_note",
        r"conviction_reason", r"risks", r"catalyst", r"filter_reason",
        r"scanner_patterns", r"engines_list", r"upcoming_events",
        r"near_miss_data", r"run_id", r"telegram", r"notes$",
    ]

    def _is_excluded(col_name: str) -> bool:
        return any(re.search(p, col_name, re.IGNORECASE) for p in EXCLUDE_PATTERNS)

    if signals is not None and not signals.empty:
        # Discover from the actual loaded DataFrame (most accurate)
        candidates = []
        for col in signals.columns:
            if _is_excluded(col):
                continue
            series = signals[col]
            # Object columns with low cardinality
            if series.dtype == object:
                n_unique  = series.nunique()
                coverage  = series.notna().mean()
                if 2 <= n_unique <= 25 and coverage >= 0.10:
                    candidates.append(col)
            # Also include boolean columns (they're categorical)
            elif str(series.dtype) == "bool":
                candidates.append(col)
        logger.debug(f"  Discovered {len(candidates)} categorical fields from DataFrame")
        return sorted(candidates)

    # Fallback: discover from information_schema column types
    sb = sb or get_supabase()
    columns = discover_table_columns(sb, table)
    TEXT_TYPES = {"text", "character varying", "varchar", "character", "char",
                  "boolean", "bool"}
    candidates = []
    for col, dtype in columns.items():
        if _is_excluded(col):
            continue
        if dtype.lower() in TEXT_TYPES or "char" in dtype.lower():
            candidates.append(col)
    logger.debug(f"  Discovered {len(candidates)} categorical fields from schema")
    return sorted(candidates)


# ─────────────────────────────────────────────────────────────────────────────
# 4. THRESHOLD FIELD MAP DISCOVERY — replaces hardcoded THRESHOLD_FIELD_MAP
# ─────────────────────────────────────────────────────────────────────────────

# Naming convention mapping: system_config key suffix → signal_log field name
# This covers the translation between config key naming and actual DB column names.
# ADD NEW MAPPINGS HERE ONLY if your naming convention breaks the auto-derive rules.
FIELD_NAME_OVERRIDES = {
    "rsi_d":        "rsi_daily",
    "rsi_w":        "rsi_weekly",
    "rsi_m":        "rsi_monthly",
    "rsi_daily":    "rsi_daily",
    "rsi_weekly":   "rsi_weekly",
    "rsi_monthly":  "rsi_monthly",
    "del":          "delivery_pct",
    "vol":          "vol_ratio",
    "adx":          "adx",
    "consol":       "consol_range",
    "sma50":        "dist_sma50",
    "ret6m":        "ret_6m",
    "ret_6m":       "ret_6m",
    "ret_1m":       "ret_1m",
    "ret_3m":       "ret_3m",
    "atr":          "atr_pct",
    "rs":           "rs_vs_nifty",
    "dist_vwap":    "dist_vwap_20d_pct",
}

# Suffix patterns that indicate direction (floor = minimum, ceiling = maximum)
FLOOR_SUFFIXES   = {"_min", "_min_pct", "_min_d", "_min_w", "_min_m"}
CEILING_SUFFIXES = {"_max", "_max_pct", "_max_d", "_max_w", "_max_m"}

# ─────────────────────────────────────────────────────────────────────────────
# EXTENDED THRESHOLD KEYS — floor/ceiling gates on a real signal_log field that
# DON'T follow the signal_threshold_{type}_{field}_{min|max} convention closely
# enough to auto-derive (the key name doesn't embed the field name). Curated
# by hand, same spirit as FIELD_NAME_OVERRIDES above — these require reading
# the actual gating code to map correctly, a regex can't safely guess them.
#
# Format: cfg_key -> (field, signal_group, direction)
#   signal_group ""           -> applies across all signal types, no filter
#   signal_group "prime" etc. -> substring-matched against signal_type/subtype
#   signal_group "regime:X"   -> grouped by regime/active_regime column instead
#                                of signal type (exact match, not substring)
#
# min_rr_to_enter is a proxy: the live gate compares against an inline
# `implied_rr` computed from entry/stop/target prices that is never itself
# persisted to signal_log. expected_r_msl (the MSL-stage expected R, the
# primary input to that calculation) is the closest available stand-in —
# sensitivity findings on this family should be read as directional evidence,
# not a literal replay of the live gate.
EXTENDED_THRESHOLD_KEYS = {
    "risk_block_threshold":         ("risk_score",          "",         "ceiling"),
    "risk_penalty_threshold":       ("risk_score",          "",         "ceiling"),
    "holding_score_exit_threshold": ("holding_score",        "",         "floor"),
    "add_holding_min":              ("holding_score",        "",         "floor"),
    "prime_breakout_min":           ("breakout_readiness",   "prime",    "floor"),
    "min_rr_to_enter":              ("expected_r_msl",        "",         "floor"),
    "min_rr_prime":                 ("expected_r_msl",        "prime",    "floor"),
    "min_rr_breakout":              ("expected_r_msl",        "breakout", "floor"),
    "min_rr_reentry":                ("expected_r_msl",        "reentry",  "floor"),
    "min_rr_staged":                 ("expected_r_msl",        "staged",   "floor"),
    "min_rr_momentum_cont":          ("expected_r_msl",        "momentum", "floor"),
}


def _build_regime_rr_keys(regime_names: list) -> dict:
    """
    min_rr_to_enter_<REGIME> overrides, built from discovered regime names
    (DynamicRegistry.regime_names) rather than hardcoded — a new regime added
    to regime_engine_weights gets a corresponding RR sensitivity check for
    free, no code change.
    """
    out = {}
    for r in (regime_names or []):
        cfg_key = f"min_rr_to_enter_{str(r).replace(' ', '_')}"
        out[cfg_key] = ("expected_r_msl", f"regime:{r}", "floor")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# SCORE COMPONENT MAP — additive score bonus/penalty magnitudes tied to a
# boolean/categorical condition (NOT a floor/ceiling gate — these don't filter
# which signals exist, they adjust score on signals that already exist). Same
# hand-curated reasoning as above: the trigger condition lives in scoring code
# that has to be read to map correctly.
#
# Format: cfg_key -> {"field": <column>, "op": "eq"|"gte", "value": <match
#         value for eq, or the live cfg_key for a dynamic gte cutoff>}
SCORE_COMPONENT_MAP = {
    "score_bonus_momentum_high": {"field": "momentum_score",     "op": "gte", "value": 80},
    "score_bonus_momentum_mid":  {"field": "momentum_score",     "op": "range", "value": (70, 80)},
    "score_bonus_institutional": {"field": "institutional_score", "op": "gte", "value": 60},
    "score_bonus_breakout_ready": {"field": "breakout_readiness", "op": "gte", "value": 65},
    "score_bonus_psar_dual":     {"field": "psar_dual_confirmed", "op": "eq",  "value": True},
    "score_bonus_weekly_strong": {"field": "weekly_structure",    "op": "eq",  "value": "STRONG"},
    "score_bonus_prime_setup":   {"field": "signal_type",         "op": "eq",  "value": "PRIME_SETUP"},
    "score_bonus_breakout_setup": {"field": "signal_type",        "op": "eq",  "value": "BREAKOUT_SETUP"},
    "score_bonus_reentry_setup": {"field": "signal_type",         "op": "eq",  "value": "REENTRY_SETUP"},
    "industry_bonus_top5":       {"field": "industry_top5",       "op": "eq",  "value": True},
    "industry_bonus_strong":     {"field": "industry_state",      "op": "eq",  "value": "STRONG"},
    # risk penalties key off risk_score against a threshold that's itself a
    # cfg key (risk_penalty_threshold) rather than a fixed literal — handled
    # specially in analyze_score_component_sensitivity() since "value" here
    # needs a live cfg lookup, not a constant.
    "score_penalty_risk_high":   {"field": "risk_score", "op": "gte_cfg", "value": "risk_penalty_threshold"},
    "score_penalty_risk_mid":    {"field": "risk_score", "op": "range",   "value": (50, None)},  # upper bound = risk_penalty_threshold, resolved at runtime
}


def discover_score_component_map(config: dict, signal_columns: list = None) -> dict:
    """Filters SCORE_COMPONENT_MAP to entries whose cfg key exists in config
    and whose field exists in signal_log — same defensive pattern as
    discover_threshold_field_map, so a renamed/dropped column or key just
    quietly drops that one entry instead of crashing the run."""
    signal_cols = set(signal_columns or [])
    return {
        k: v for k, v in SCORE_COMPONENT_MAP.items()
        if k in config and (not signal_cols or v["field"] in signal_cols)
    }


def discover_threshold_field_map(config: dict,
                                  signal_columns: list = None,
                                  sb=None,
                                  regime_names: list = None) -> dict:
    """
    Auto-derive THRESHOLD_FIELD_MAP from system_config keys that follow the
    naming convention: signal_threshold_{signal_type}_{field}_{min|max}

    Adds new threshold mappings automatically when:
      - New signal_threshold_* keys are added to system_config
      - New threshold types are configured

    Returns:
        {config_key: (signal_log_field, signal_group, direction)}

    Example auto-derivation:
      "signal_threshold_prime_rsi_daily_min"
        → signal_group = "prime"
        → field_key    = "rsi_daily"
        → direction    = "floor"
        → field        = "rsi_daily" (from FIELD_NAME_OVERRIDES or direct match)
    """
    sb            = sb or get_supabase()
    signal_cols   = set(signal_columns or [])
    derived_map   = {}
    unresolved    = []

    threshold_keys = [k for k in config.keys()
                      if k.startswith("signal_threshold_")]

    for key in threshold_keys:
        # Strip prefix: "signal_threshold_prime_rsi_daily_min"
        #                                → "prime_rsi_daily_min"
        remainder = key.replace("signal_threshold_", "")

        # Determine direction from suffix
        direction = None
        field_part = remainder
        for suffix in FLOOR_SUFFIXES:
            if remainder.endswith(suffix):
                direction = "floor"
                field_part = remainder[:-len(suffix)]
                break
        if direction is None:
            for suffix in CEILING_SUFFIXES:
                if remainder.endswith(suffix):
                    direction = "ceiling"
                    field_part = remainder[:-len(suffix)]
                    break

        if direction is None:
            unresolved.append(key)
            continue

        # Determine signal group (first segment of field_part)
        # Convention: prime_rsi_daily → group=prime, field=rsi_daily
        parts  = field_part.split("_", 1)
        if len(parts) < 2:
            unresolved.append(key)
            continue

        signal_group = parts[0]   # "prime", "reentry", "staged", "prebreak"
        field_key    = parts[1]   # "rsi_daily", "adx", "vol", etc.

        # Resolve field_key to actual signal_log column
        field = FIELD_NAME_OVERRIDES.get(field_key)

        if field is None:
            # Try direct match against actual columns
            if field_key in signal_cols:
                field = field_key
            else:
                # Try partial match (e.g. "rsi" → find "rsi_daily")
                matches = [c for c in signal_cols if field_key in c]
                if len(matches) == 1:
                    field = matches[0]
                elif len(matches) > 1:
                    # Prefer exact over partial
                    exact = [c for c in matches if c == field_key]
                    field = exact[0] if exact else matches[0]

        if field is None:
            unresolved.append(key)
            continue

        derived_map[key] = (field, signal_group, direction)

    if unresolved:
        logger.debug(f"  Threshold map: {len(unresolved)} keys could not be auto-resolved: {unresolved}")
    logger.debug(f"  Threshold map: auto-derived {len(derived_map)} mappings from {len(threshold_keys)} config keys")

    # Merge in the hand-curated extended keys (don't follow the auto-derive
    # naming convention) — same existence checks as above, so a missing key
    # or renamed column just quietly drops that one entry.
    extended_added = 0
    for key, (field, group, direction) in EXTENDED_THRESHOLD_KEYS.items():
        if key in config and (not signal_cols or field in signal_cols) and key not in derived_map:
            derived_map[key] = (field, group, direction)
            extended_added += 1

    for key, (field, group, direction) in _build_regime_rr_keys(regime_names).items():
        if key in config and (not signal_cols or field in signal_cols) and key not in derived_map:
            derived_map[key] = (field, group, direction)
            extended_added += 1

    if extended_added:
        logger.debug(f"  Threshold map: +{extended_added} extended (non-auto-derivable) mappings")

    return derived_map


# ─────────────────────────────────────────────────────────────────────────────
# 5. FULL REGISTRY — single call to get everything
# ─────────────────────────────────────────────────────────────────────────────

class DynamicRegistry:
    """
    Cached registry of all dynamic system metadata for a brain run.
    Instantiated once per brain run in brain_engine.py and passed through.

    Usage:
        registry = DynamicRegistry(dataset)
        engines  = registry.engines        # ["CTL", "EAP", "IAD", ...]
        tables   = registry.tables         # ["ai_context", "brain_proposals", ...]
        cat_fields = registry.categorical_fields  # ["bb_context", "lifecycle", ...]
        thresh_map = registry.threshold_map       # {cfg_key: (field, group, dir)}
    """

    def __init__(self, dataset: dict, sb=None):
        self.sb      = sb or get_supabase()
        self.config  = dataset.get("config", {})
        self.signals = dataset.get("signals")
        self._cache  = {}

    @property
    def engines(self) -> list[str]:
        if "engines" not in self._cache:
            self._cache["engines"] = discover_engines(self.sb, self.config)
        return self._cache["engines"]

    @property
    def regime_names(self) -> list[str]:
        if "regimes" not in self._cache:
            self._cache["regimes"] = discover_regime_names(self.sb, self.config)
        return self._cache["regimes"]

    @property
    def tables(self) -> list[str]:
        if "tables" not in self._cache:
            self._cache["tables"] = discover_all_tables(self.sb)
        return self._cache["tables"]

    @property
    def categorical_fields(self) -> list[str]:
        if "cat_fields" not in self._cache:
            self._cache["cat_fields"] = discover_categorical_fields(self.signals, self.sb)
        return self._cache["cat_fields"]

    @property
    def signal_columns(self) -> list[str]:
        if "sig_cols" not in self._cache:
            if self.signals is not None and not self.signals.empty:
                self._cache["sig_cols"] = list(self.signals.columns)
            else:
                cols = discover_table_columns(self.sb, "signal_log")
                self._cache["sig_cols"] = list(cols.keys())
        return self._cache["sig_cols"]

    @property
    def threshold_map(self) -> dict:
        if "thresh_map" not in self._cache:
            self._cache["thresh_map"] = discover_threshold_field_map(
                self.config, self.signal_columns, self.sb, self.regime_names
            )
        return self._cache["thresh_map"]

    @property
    def score_component_map(self) -> dict:
        if "score_comp_map" not in self._cache:
            self._cache["score_comp_map"] = discover_score_component_map(
                self.config, self.signal_columns
            )
        return self._cache["score_comp_map"]

    def summary(self) -> str:
        return (
            f"DynamicRegistry — "
            f"engines={len(self.engines)}, "
            f"tables={len(self.tables)}, "
            f"categorical_fields={len(self.categorical_fields)}, "
            f"threshold_mappings={len(self.threshold_map)}, "
            f"score_components={len(self.score_component_map)}, "
            f"regimes={len(self.regime_names)}"
        )
