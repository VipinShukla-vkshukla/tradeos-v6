"""
TradeOS v7 — Brain Engine v2: LLM Synthesizer
===============================================
Uses your existing ai_router with the full dataset. The LLM sees
ALL fields from ALL tables — no pre-filtering by us.
Anti-hallucination is enforced at both prompt and validation layers.
"""

import json
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from swing.brain.brain_prompt import get_system_prompt

VALID_TYPES = {
    "THRESHOLD_CHANGE", "ENGINE_WEIGHT", "REGIME_WEIGHT", "SCORE_WEIGHT_CHANGE",
    "SCRIPT_PATCH", "CODE_SUGGESTION", "INSIGHT",
}


def _df_summary(df: pd.DataFrame, name: str, max_rows: int = 5) -> dict:
    """
    Convert a DataFrame to a brain-readable summary.
    Includes: schema, null rates, sample rows, numeric stats.
    The LLM sees the actual field names and real data — no abstraction.
    """
    if df is None or df.empty:
        return {"table": name, "rows": 0, "columns": [], "sample": []}

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    stats = {}
    for col in numeric_cols[:20]:
        s = df[col].dropna()
        if not s.empty:
            stats[col] = {
                "mean":   round(float(s.mean()), 3),
                "median": round(float(s.median()), 3),
                "std":    round(float(s.std()), 3),
                "min":    round(float(s.min()), 3),
                "max":    round(float(s.max()), 3),
                "null_pct": round(df[col].isna().mean() * 100, 1),
            }

    # Sample rows — convert to JSON-safe dicts
    try:
        sample = df.tail(max_rows).fillna("null").infer_objects(copy=False).to_dict(orient="records")
    except Exception:
        sample = []

    return {
        "table":       name,
        "rows":        len(df),
        "columns":     list(df.columns),
        "numeric_stats": stats,
        "sample_rows": sample,
    }


def _build_synthesis_prompt(quant_findings: dict, dataset: dict,
                              script_scan_results: list,
                              registry=None) -> str:
    """
    Build the full synthesis prompt. The LLM receives:
      - Full quant findings (all analyses)
      - Schema + stats for every loaded table
      - Script scanner findings (hardcoded values)
      - Current config state
      - Performance history (brain's own track record)
      - Past applied proposals (what has already been tried)
    """
    system = get_system_prompt("brain")
    config = dataset.get("config", {})

    # Table summaries
    table_data = {}
    for tname in ["signals", "msl_history", "ai_context", "lessons",
                   "open_positions", "closed_positions", "perf_history",
                   "brain_run_history", "past_proposals", "signal_summary"]:
        df = dataset.get(tname)
        if df is not None:
            table_data[tname] = _df_summary(df, tname)

    # Outcome coverage context
    n_signals  = dataset.get("signals_analyzed", 0)
    n_outcomes = dataset.get("signals_with_outcomes", 0)
    coverage   = dataset.get("coverage_pct", 0)
    period     = dataset.get("period_days", 90)

    # Improvement notes from AI (recurring patterns)
    improvement_notes = dataset.get("improvement_notes", [])[:10]

    # Current config thresholds (full, not filtered)
    threshold_cfg = {k: v["value"] for k, v in config.items()
                     if k.startswith("signal_threshold_")}
    other_cfg     = {k: v["value"] for k, v in config.items()
                     if not k.startswith("signal_threshold_") and not k.startswith("brain_")}

    # Script scanner findings
    script_tunable = []
    for r in (script_scan_results or []):
        hv = r.get("hardcoded_values") or []
        if isinstance(hv, list):
            tunable = [h for h in hv if h.get("tunable")]
            if tunable:
                script_tunable.append({
                    "script": r.get("script_path"),
                    "coverage": r.get("brain_coverage"),
                    "tunable_values": tunable[:5],
                })

    # Past brain proposals that were applied (track record)
    past_proposals = dataset.get("past_proposals")
    past_applied   = []
    if past_proposals is not None and not past_proposals.empty:
        for _, row in past_proposals.head(10).iterrows():
            bt = row.get("backtest_result") or {}
            if isinstance(bt, str):
                try: bt = json.loads(bt)
                except: bt = {}
            past_applied.append({
                "key":       row.get("target_key"),
                "from":      row.get("rollback_value"),
                "to":        row.get("proposed_value"),
                "rationale": str(row.get("rationale",""))[:100],
                "wr_delta":  bt.get("wr_delta"),
                "applied":   str(row.get("applied_at",""))[:10],
            })

    prompt = f"""
{system}

════════════════════════════════════════════════
ANALYSIS CONTEXT
════════════════════════════════════════════════
Analysis period: {period} days
Signals in scope: {n_signals}
Signals with confirmed outcomes: {n_outcomes} ({coverage:.1f}% coverage)
Win threshold: {dataset.get('win_threshold', 3.0)}% forward return
Stop threshold: {dataset.get('stop_threshold', 5.0)}% drawdown
Eval horizons: {dataset.get('eval_horizons', [5,10,15,20])} trading days

{"⚠️ LOW COVERAGE WARNING: outcome coverage < 25%. Limit proposals to INSIGHT type only." if coverage < 25 else ""}

════════════════════════════════════════════════
QUANTITATIVE FINDINGS (from quant_analyzer)
════════════════════════════════════════════════
{json.dumps(quant_findings, indent=2, default=str)[:5000]}

════════════════════════════════════════════════
FULL TABLE DATA (schema + stats + samples)
════════════════════════════════════════════════
{json.dumps(table_data, indent=2, default=str)[:6000]}

════════════════════════════════════════════════
CURRENT SIGNAL THRESHOLDS (all system_config signal_threshold_* keys)
════════════════════════════════════════════════
{json.dumps(threshold_cfg, indent=2)[:2000]}

════════════════════════════════════════════════
OTHER SYSTEM CONFIG (weights, modes, limits)
════════════════════════════════════════════════
{json.dumps(other_cfg, indent=2)[:1000]}

════════════════════════════════════════════════
SCRIPT SCANNER: HARDCODED VALUES FOUND
════════════════════════════════════════════════
{json.dumps(script_tunable, indent=2, default=str)[:2000]}

════════════════════════════════════════════════
PAST APPLIED PROPOSALS (brain track record)
════════════════════════════════════════════════
{json.dumps(past_applied, indent=2, default=str)[:1500]}

════════════════════════════════════════════════
RECURRING AI SELF-IMPROVEMENT NOTES
════════════════════════════════════════════════
{json.dumps(improvement_notes[:8], indent=2, default=str)[:1500]}

════════════════════════════════════════════════
YOUR OUTPUT FORMAT
════════════════════════════════════════════════
Return a single valid JSON object with exactly these keys:

{{
  "proposals": [
    {{
      "type": "THRESHOLD_CHANGE|ENGINE_WEIGHT|REGIME_WEIGHT|SCORE_WEIGHT_CHANGE|SCRIPT_PATCH|CODE_SUGGESTION|INSIGHT",
      "target_key": "<system_config key, script path, or descriptive name>",
      "current_value": "<exact current value as string>",
      "proposed_value": "<exact proposed value as string or JSON>",
      "reasoning_chain": [
        "Step 1: Data shows X (cite field, value, n=N, wr=W%)",
        "Step 2: This implies Y about current system behaviour",
        "Step 3: Changing Z from A to B should improve outcome because...",
        "Step 4: This would be falsified if..."
      ],
      "rationale": "<2-3 sentence human-readable summary>",
      "confidence": <0.0-1.0 per confidence rules in system prompt>,
      "priority": <1-10, 1=highest>,
      "regime_specific": "<null or regime name if change only applies in specific regime>",
      "market_logic_check": "<does this make trading sense beyond statistics? explain>",
      "low_signal_risk": <true if proposed change generates <5 signals/week>,
      "cross_variable_insight": "<pattern visible only by combining multiple data points>"
    }}
  ],
  "narrative": {{
    "coverage_statement": "Based on N signals with X% outcome coverage:",
    "system_health": "<2-3 sentences on overall signal quality trend>",
    "top_finding": "<single most important insight this cycle>",
    "underperforming_elements": "<what is dragging down win rate and why>",
    "novel_patterns": "<patterns not in quant findings, cross-variable>",
    "script_recommendations": "<which scripts most need cfg() migration and why>",
    "next_cycle_watch": "<what to monitor in next analysis cycle>"
  }},
  "script_patch_proposals": [
    {{
      "script_path": "<relative path from backend/>",
      "hardcoded_variable": "<variable name>",
      "current_value": "<current hardcoded value>",
      "proposed_config_key": "<new system_config key name>",
      "rationale": "<why this variable should be tunable>"
    }}
  ]
}}

STRICT CONSTRAINTS:
- proposals list: maximum 10 items
- script_patch_proposals: maximum 5 items (most impactful only)
- Do NOT propose changes to keys not shown in CURRENT SIGNAL THRESHOLDS above
- Do NOT invent field names not present in TABLE DATA above
- If coverage < 20%, proposals list must be empty — only narrative and script_patch_proposals
- Every THRESHOLD_CHANGE must cite exact (n, win_rate_bucket) from quant findings
- Never propose the same change as an already-applied proposal unless contradicting evidence
- Respond with ONLY the JSON object — no markdown, no preamble, no explanation outside JSON
"""
    return prompt


def _validate_proposal(p: dict, config: dict) -> Optional[dict]:
    """Validate a single LLM proposal. Returns None to reject."""
    ptype = p.get("type", "")
    if ptype not in VALID_TYPES:
        return None

    # Must have reasoning chain
    rc = p.get("reasoning_chain", [])
    if not isinstance(rc, list) or len(rc) < 2:
        logger.debug(f"  Rejected {p.get('target_key')}: missing reasoning_chain")
        return None

    # Confidence bounds by type
    conf = float(p.get("confidence", 0))
    if conf < 0.40:
        return None

    # Safety: cap threshold changes
    if ptype == "THRESHOLD_CHANGE":
        target = p.get("target_key", "")
        cv_str = config.get(target, {}).get("value")
        pv_str = p.get("proposed_value")
        if cv_str and pv_str:
            try:
                cv, pv = float(cv_str), float(pv_str)
                if cv != 0 and abs(pv - cv) / abs(cv) > 0.30:
                    logger.debug(f"  Rejected {target}: change > 30%")
                    return None
            except (TypeError, ValueError):
                pass

    # Warn but don't reject on low_signal_risk
    if p.get("low_signal_risk"):
        logger.warning(f"  Proposal {p.get('target_key')} flagged LOW_SIGNAL_RISK")

    result = {
        "type":             ptype,
        "target_key":       p.get("target_key"),
        "current_value":    str(p.get("current_value", "")),
        "proposed_value":   str(p.get("proposed_value", "")),
        "rationale":        str(p.get("rationale", ""))[:600],
        "confidence":       round(conf, 2),
        "priority":         int(p.get("priority", 5)),
        "source":           "llm",
        "regime_specific":  p.get("regime_specific"),
        "evidence": {
            "reasoning_chain":       p.get("reasoning_chain", []),
            "cross_variable":        p.get("cross_variable_insight", ""),
            "market_logic_check":    p.get("market_logic_check", ""),
            "low_signal_risk":       p.get("low_signal_risk", False),
        },
    }
    if ptype not in ("THRESHOLD_CHANGE", "ENGINE_WEIGHT"):
        # Review-only types never check wr_delta/auto-apply, so this stub is
        # harmless here — it just means "no mechanical backtest applies".
        # THRESHOLD_CHANGE/ENGINE_WEIGHT deliberately get NO backtest_result
        # here — prepare_for_backtest() routes them through the same real
        # backtester quant findings use, so auto-apply only fires on actual
        # evidence, never on an LLM's self-reported confidence alone.
        result["backtest_result"] = {
            "valid": True, "passes": True,
            "summary": "No mechanical backtest applies to this proposal type.",
        }
    return result


def prepare_for_backtest(llm_proposals: list, registry=None) -> tuple[list, list]:
    """
    Splits validated LLM proposals into:
      - backtestable: THRESHOLD_CHANGE/ENGINE_WEIGHT proposals reshaped into the
        raw finding format run_backtests() expects (key/field/current/proposed/
        direction, or regime/engine/direction) — these get run through the
        EXACT SAME backtester quant findings use, so they can only auto-apply
        on real, measured evidence, never on the LLM's self-reported confidence.
      - passthrough: everything else, including backtestable-type proposals
        whose target_key couldn't be resolved to a real field/engine — those
        get an honest "couldn't mechanically verify" backtest_result instead
        of the old blanket "always passes" stamp, so they land PENDING with a
        clear reason rather than silently being treated as validated.
    """
    backtestable, passthrough = [], []
    threshold_map = getattr(registry, "threshold_map", {}) if registry else {}

    for p in llm_proposals:
        ptype = p.get("type", "")
        tkey  = p.get("target_key", "") or ""

        if ptype == "THRESHOLD_CHANGE":
            mapping = threshold_map.get(tkey)
            current = proposed = None
            try:
                current  = float(p.get("current_value"))
                proposed = float(p.get("proposed_value"))
            except (TypeError, ValueError):
                pass

            if mapping and current is not None and proposed is not None:
                field, _group, direction = mapping
                backtestable.append({
                    **p, "key": tkey, "field": field,
                    "current": current, "proposed": proposed,
                    "direction": direction,
                })
            else:
                reason = ("current/proposed value not numeric" if current is None or proposed is None
                          else f"'{tkey}' not in registry.threshold_map")
                p = dict(p)
                p["backtest_result"] = {"valid": False, "passes": False,
                                         "summary": f"Could not mechanically backtest: {reason}. "
                                                     f"Review manually."}
                passthrough.append(p)
            continue

        if ptype == "ENGINE_WEIGHT":
            current = proposed = None
            try:
                current  = float(p.get("current_value"))
                proposed = float(p.get("proposed_value"))
            except (TypeError, ValueError):
                pass

            if ":" in tkey and current is not None and proposed is not None:
                regime, engine = tkey.split(":", 1)
                direction = "OUTPERFORMING" if proposed > current else "UNDERPERFORMING"
                backtestable.append({
                    **p, "regime": regime.strip(), "engine": engine.strip(),
                    "direction": direction,
                })
            else:
                p = dict(p)
                p["backtest_result"] = {"valid": False, "passes": False,
                                         "summary": "Could not mechanically backtest: target_key must "
                                                     "be 'REGIME:ENGINE' with numeric values. Review manually."}
                passthrough.append(p)
            continue

        passthrough.append(p)

    return backtestable, passthrough


def synthesize(quant_findings: dict, dataset: dict,
               script_scan_results: list = None,
               max_proposals: int = 10,
               registry=None) -> tuple[list[dict], dict]:
    """
    Call the active AI provider to synthesize proposals and narrative.
    Returns: (proposals, narrative_dict)
    """
    try:
        from ai.ai_router import raw_completion, is_ai_available
        if not is_ai_available():
            logger.info("  LLM synthesis skipped (no provider configured)")
            return [], {}
    except ImportError:
        logger.warning("  ai_router not importable — LLM synthesis skipped")
        return [], {}

    config = dataset.get("config", {})
    prompt = _build_synthesis_prompt(quant_findings, dataset, script_scan_results or [], registry=registry)

    logger.info("  Calling LLM for synthesis (full data access)...")
    try:
        raw = raw_completion(prompt, max_tokens=3000, call_site="llm_synthesizer")
    except Exception as e:
        logger.warning(f"  LLM call failed: {e}")
        return [], {}

    # Parse response
    clean = re.sub(r'```(?:json)?', '', raw).strip()
    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', clean, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except Exception:
                logger.warning("  Could not parse LLM response as JSON")
                return [], {}
        else:
            logger.warning("  No JSON found in LLM response")
            return [], {}

    # Validate proposals
    raw_proposals = data.get("proposals", [])
    validated     = []
    for p in raw_proposals[:max_proposals]:
        v = _validate_proposal(p, config)
        if v:
            validated.append(v)

    narrative = data.get("narrative", {})

    # Build SCRIPT_PATCH proposals from scanner results (only source of actual diffs).
    # Build SCRIPT_PATCH proposals from scanner results (only source of actual diffs).
    # LLM script_patch_proposals provides rationale text only — never the diff.
    llm_rationale = {
        sp.get("script_path", "").replace("\\", "/"): sp.get("rationale", "")
        for sp in data.get("script_patch_proposals", [])
    }

    # Filter to only scripts that actually have tunable values and a diff, THEN take top 5
    patch_candidates = []
    for r in (script_scan_results or []):
        hv      = r.get("hardcoded_values") or []
        tunable = [h for h in hv if isinstance(hv, list) and h.get("tunable")] \
                  if isinstance(hv, list) else []
        diff    = r.get("diff", "")
        if tunable and diff.strip():
            patch_candidates.append((r, tunable))
    logger.debug(f"  Script patch candidates: {len(patch_candidates)} scripts with tunable values + diffs")

    for r, tunable in patch_candidates[:5]:
        script_path = r.get("script_path", "").replace("\\", "/")
        diff        = r.get("diff", "")
        validated.append({
            "type":          "SCRIPT_PATCH",
            "target_key":    script_path,
            "current_value": str(tunable[0].get("value", "")),
            "proposed_value":tunable[0].get("proposed_key", ""),
            "rationale":     llm_rationale.get(script_path,
                             f"Migrate {len(tunable)} hardcoded value(s) to system_config.")[:400],
            "confidence":    0.70,
            "priority":      7,
            "source":        "llm",
            "evidence":      {"type": "cfg_migration"},
            "script_diff":   diff,
            "hardcoded_values":  tunable,
            "backtest_result": {
                "valid": True, "passes": True, "high_impact": False,
                "summary": "Script migration proposal — manual approval required.",
            },
        })

    logger.info(f"  LLM synthesis: {len(raw_proposals)} raw proposals, "
                f"{len(validated)} validated")
    return validated, narrative