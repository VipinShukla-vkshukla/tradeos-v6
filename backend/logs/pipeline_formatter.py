"""
TradeOS v7 — Evening Pipeline Log Formatter
============================================
Produces a traffic-light summary that replaces the raw step-by-step dump.
Designed as a drop-in replacement for run_pipeline.py's print_summary().
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from loguru import logger

from config import today_ist, IST
from datetime import datetime


# ── Step grouping for the summary ─────────────────────────────────────────────

STEP_GROUPS: Dict[str, List[str]] = {
    "INGESTION": [
        "01_calendar_prep", "04_market_news", "05_macro_indicators",
        "06_global_cues", "07_fetch_chartink", "08_ingest_bhavcopy",
        "09_fii_dii", "10_nse_events", "11_asm_gsm",
    ],
    "DERIVED": ["12_compute_indicators", "13_sector_strength", "14_compute_regime", "15_regime_predict"],
    "SELECTION": ["17_screen_stocks", "18_compute_msl"],
    "SIGNALS": ["19_quality_gate", "20_signals"],
    "AI": ["22_post_trade", "23_ai_market_intel", "24_ai_decision_engine"],
    "OUTPUT": ["25_signal_snapshot", "26_alerts", "27_quality_audit"],
    "LEARNING": ["28_resolve_outcomes", "28a_resolve_intraday", "28b_score_allocator"],
    "MAINTENANCE": ["29_storage_rolloff"],
}

GROUP_EMOJI = {
    "INGESTION": "📥",
    "DERIVED": "📊",
    "SELECTION": "🎯",
    "SIGNALS": "📡",
    "AI": "🤖",
    "OUTPUT": "📤",
    "LEARNING": "🧠",
    "MAINTENANCE": "🧹",
}


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_secs(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    rem = s % 60
    return f"{m}m {rem:.0f}s"


def _status_emoji(ok: bool) -> str:
    return "✅" if ok else "❌"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN FORMATTER
# ─────────────────────────────────────────────────────────────────────────────

def format_pipeline_summary(results: Dict[str, dict], total: float) -> str:
    """
    Build the traffic-light evening pipeline summary.

    `results` is the dict from run_pipeline.py:
        {step_name: {"ok": bool, "result": ..., "elapsed": float}}
    """
    ok_count = sum(1 for r in results.values() if r["ok"])
    err_count = sum(1 for r in results.values() if not r["ok"])
    total_steps = len(results)

    if err_count > 0:
        tl = "🔴"
    elif total_steps - ok_count - err_count > 0:
        tl = "🟡"
    else:
        tl = "🟢"

    lines: List[str] = []
    lines.append("")
    lines.append("═" * 72)
    lines.append(f"  TRADEOS v7 — EVENING PIPELINE | {today_ist()} IST")
    lines.append("═" * 72)
    lines.append("")

    # ── Pipeline health ─────────────────────────────────────────────────────
    lines.append("┌─────────────────────────────────────────────────────────────────────────┐")
    lines.append("│ 📊 PIPELINE HEALTH                                                      │")
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")
    lines.append(
        f"│  {tl} {ok_count}/{total_steps} steps OK   ⏱ {_fmt_secs(total)}   │"
    )
    lines.append("│                                                                         │")

    # Group steps
    for group_name, step_names in STEP_GROUPS.items():
        group_steps = [(n, r) for n, r in results.items() if n in step_names]
        if not group_steps:
            continue
        group_ok = sum(1 for _, r in group_steps if r["ok"])
        group_total = len(group_steps)
        group_emoji = GROUP_EMOJI.get(group_name, "●")
        status_chars = " ".join(_status_emoji(r["ok"]) for _, r in group_steps)
        lines.append(
            f"│  {group_emoji} {group_name:<14} {group_ok}/{group_total} OK   "
            f"{status_chars}   │"
        )

    lines.append("│                                                                         │")
    lines.append("└─────────────────────────────────────────────────────────────────────────┘")
    lines.append("")

    # ── Issues ──────────────────────────────────────────────────────────────
    issues = _extract_issues(results)
    if issues:
        lines.append("┌─────────────────────────────────────────────────────────────────────────┐")
        lines.append("│ ⚠️  NEEDS ATTENTION                                                     │")
        lines.append("├─────────────────────────────────────────────────────────────────────────┤")
        for issue in issues:
            lines.append(f"│  {issue:<73}│")
        lines.append("└─────────────────────────────────────────────────────────────────────────┘")
        lines.append("")

    # ── Step timeline ───────────────────────────────────────────────────────
    lines.append("┌─────────────────────────────────────────────────────────────────────────┐")
    lines.append("│ 📋 STEP TIMELINE                                                        │")
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")
    lines.append("│  Step                    Duration  Status                               │")
    lines.append("├─────────────────────────────────────────────────────────────────────────┤")
    for name, r in results.items():
        status = "✅ OK" if r["ok"] else "❌ FAIL"
        elapsed = r.get("elapsed", 0)
        lines.append(f"│  {name:<24} {elapsed:>7.1f}s   {status}   │")
    lines.append("└─────────────────────────────────────────────────────────────────────────┘")
    lines.append("")
    lines.append(
        f"  Total: {_fmt_secs(total)} | {tl} {ok_count} OK"
        + (f" | 🔴 {err_count} Failed" if err_count else " | ✅ All passed")
    )
    lines.append("═" * 72)
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# ISSUE EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def _extract_issues(results: Dict[str, dict]) -> List[str]:
    """Extract issues/warnings from step results."""
    import re

    issues: List[str] = []

    for name, r in results.items():
        if not r["ok"]:
            err = r.get("error", "unknown error")
            # Truncate long error messages
            err_short = err[:70] + ("…" if len(err) > 70 else "")
            issues.append(f"🔴 {name}: FAILED — {err_short}")
            continue

        # Check result metadata for warnings
        result = r.get("result")
        if isinstance(result, dict):
            for key in ("warning", "warn", "issue"):
                if key in result:
                    val = str(result[key])
                    val_short = val[:70] + ("…" if len(val) > 70 else "")
                    issues.append(f"🟡 {name}: {val_short}")
                    break

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# DROP-IN REPLACEMENT FOR print_summary()
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results: Dict[str, dict], total: float) -> None:
    """Drop-in replacement for run_pipeline.py's print_summary()."""
    logger.info(format_pipeline_summary(results, total))
