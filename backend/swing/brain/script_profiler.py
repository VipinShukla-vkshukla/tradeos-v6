"""
TradeOS v6 — Brain Engine v2: Script Profiler
===============================================
The script_scanner module answers "what hardcoded values could become config?".
This module answers a different question: "what does this script actually DO,
what does it assume about the data it touches, and is anything about it
concretely wrong?"

Runs only on scripts that are new or whose last_modified changed since the
last scan (diffed against brain_script_registry BEFORE script_scanner's own
scan_all() overwrites that row) — so a 60-file repo costs one LLM call per
changed file per week, not 60 calls every week regardless of churn.

Output, per script, written to brain_script_registry:
  - behavioral_summary : 2-3 sentence plain-English description
  - assumptions         : [{"resource": "<table.column|config_key>", "expects": "..."}]
  - flagged_issues      : [{"severity": "...", "line": int|null, "issue": "..."}]

flagged_issues become CODE_SUGGESTION proposals (always REVIEW_ONLY — this
module only ever describes and suggests, never writes to the script itself).
assumptions feed consistency_checker.py, which runs immediately after this
in the same weekly cycle.
"""

import json
import sys
import threading
import datetime as _dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from loguru import logger
from ai.ai_router import raw_completion, is_ai_available

MAX_SOURCE_CHARS = 12_000  # keep per-file prompt cost/latency bounded
LLM_TIMEOUT_SEC  = 90      # one slow/stuck call must not block the whole run


def _call_with_timeout(fn, args, timeout):
    """
    Runs fn(*args) in a daemon thread with a hard timeout. daemon=True is the
    part that matters: a plain ThreadPoolExecutor's worker threads are NOT
    daemon threads, and Python's atexit machinery joins every outstanding
    executor thread before the interpreter is allowed to fully exit — tested
    this directly, shutdown(wait=False) does NOT prevent that join. The net
    effect would have been moving a hung call's block from "mid-run" to "the
    very end of the run", not removing it. A daemon thread is genuinely
    abandoned on process exit, no join, no block, confirmed by the same test.
    """
    result = {}
    def _target():
        try:
            result["value"] = fn(*args)
        except Exception as e:
            result["error"] = e
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        raise TimeoutError(f"call exceeded {timeout}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")

_SEVERITY_TO_PRIORITY = {"high": 2, "medium": 4, "low": 7}
_SEVERITY_TO_CONF      = {"high": 0.65, "medium": 0.50, "low": 0.35}

_PROFILE_PROMPT = """You are auditing ONE Python script from a live algorithmic trading \
system (real capital deployed) as part of a software architecture review. This is \
analysis only — you are not being asked to fix anything, and nothing you say is \
auto-applied to the file.

SCRIPT PATH: {rel_path}
EXISTING DOCSTRING/PURPOSE (may be stale or absent): {purpose}
TABLES READ: {tables_read}
TABLES WRITTEN: {tables_written}
CONFIG KEYS USED: {config_keys}

SOURCE (may be truncated at {max_chars} chars):
---
{source}
---

Return ONLY valid JSON. No markdown fences, no preamble, no text outside the JSON object. \
Keep it SHORT — this has a hard output limit, an unterminated/truncated response is worse \
than a brief one. Max 3 assumptions, max 3 flagged_issues, ranked by what matters most.

{{
  "behavioral_summary": "<2-3 sentences on what this script ACTUALLY does, based on \
reading the code — not just restating the docstring>",
  "assumptions": [
    {{"resource": "<table.column or config_key this script reads/writes>",
      "expects": "<concrete: format, range, type, units, or shape this script assumes \
about that resource. One sentence.>"}}
  ],
  "flagged_issues": [
    {{"severity": "high|medium|low", "line": <int or null>,
      "issue": "<a SPECIFIC, concrete problem you can point to: dead/unreachable code, \
missing error handling around a real failure mode, logic that contradicts this script's \
own docstring or comments, an off-by-one, a comparison that looks backwards, a value that \
silently falls through to a default it shouldn't, etc. One or two sentences.>"}}
  ]
}}

Be conservative. Only list an assumption if you can point to where in the code it matters. \
Only flag an issue if you can point to the specific line/mechanism — never flag generic \
style preferences (naming, formatting, line length). Empty lists are a valid, expected, and \
common answer — do not invent assumptions or issues to fill the response."""


def _load_source(repo_root: Path, rel_path: str) -> str:
    try:
        text = (repo_root / rel_path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"  Profiler couldn't read {rel_path}: {e}")
        return ""
    if len(text.strip()) < 20:  # effectively empty (most __init__.py files)
        logger.debug(f"  Profiler: {rel_path} is empty/near-empty, nothing to profile")
        return ""
    if len(text) > MAX_SOURCE_CHARS:
        return text[:MAX_SOURCE_CHARS] + "\n# ... [truncated for profiling] ..."
    return text


def _parse_llm_json(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw.strip())
    except Exception as e:
        logger.warning(f"  Profiler: malformed LLM JSON ({e})")
        return None


def profile_one_script(repo_root: Path, scan_result: dict) -> dict | None:
    """
    Profile a single script. Returns the fields to upsert into
    brain_script_registry, or None if profiling couldn't run (no AI
    available, file unreadable, malformed response).
    """
    rel_path = scan_result.get("script_path", "")
    source   = _load_source(repo_root, rel_path)
    if not source:
        return None

    prompt = _PROFILE_PROMPT.format(
        rel_path     = rel_path,
        purpose      = scan_result.get("purpose") or "(none found)",
        tables_read  = scan_result.get("tables_read") or [],
        tables_written = scan_result.get("tables_written") or [],
        config_keys  = scan_result.get("config_keys_used") or [],
        max_chars    = MAX_SOURCE_CHARS,
        source       = source,
    )

    try:
        raw = _call_with_timeout(raw_completion, (prompt, 1800), LLM_TIMEOUT_SEC)
    except TimeoutError:
        logger.warning(f"  Profiler: LLM call timed out after {LLM_TIMEOUT_SEC}s for {rel_path} — skipping")
        return None
    except Exception as e:
        logger.warning(f"  Profiler LLM call failed for {rel_path}: {e}")
        return None

    parsed = _parse_llm_json(raw)
    if not isinstance(parsed, dict):
        return None

    return {
        "script_path":        rel_path,
        "behavioral_summary": str(parsed.get("behavioral_summary", ""))[:1000],
        "assumptions":        parsed.get("assumptions") if isinstance(parsed.get("assumptions"), list) else [],
        "flagged_issues":     parsed.get("flagged_issues") if isinstance(parsed.get("flagged_issues"), list) else [],
    }


def profile_changed_scripts(scan_results: list[dict],
                             previous_registry: dict,
                             repo_root: Path,
                             sb) -> list[dict]:
    """
    Profiles only scripts whose last_modified is newer than their
    last_profiled timestamp (or have never been profiled at all).

    previous_registry: {script_path: {"last_modified": ..., "last_profiled": ...}}
    snapshotted BEFORE scan_and_register() runs. Deliberately NOT comparing
    against last_modified alone — that column gets overwritten unconditionally
    by the hardcode scanner every single run regardless of whether profiling
    ever reaches that file. Tracking last_profiled separately means an
    interrupted run (timeout, Ctrl+C, job killed) only skips files it hasn't
    gotten to YET — it doesn't silently and permanently stop retrying them.
    """
    if not is_ai_available():
        logger.info("  Profiler: no AI provider available, skipping behavioral pass")
        return []

    to_profile = []
    for result in scan_results:
        rel_path = result.get("script_path", "")
        prev     = previous_registry.get(rel_path, {})
        last_modified = result.get("last_modified")
        last_profiled = prev.get("last_profiled")
        if last_profiled is None or (last_modified and last_modified != prev.get("last_modified")):
            to_profile.append(result)

    if not to_profile:
        logger.info("  Profiler: no new/changed scripts since last successful profile")
        return []

    logger.info(f"  Profiler: {len(to_profile)} script(s) need profiling "
                f"(of {len(scan_results)} scanned)")

    proposals = []
    profiled  = 0

    for i, result in enumerate(to_profile, 1):
        rel_path = result.get("script_path", "")
        logger.info(f"  Profiling {i}/{len(to_profile)}: {rel_path}...")

        profile = profile_one_script(repo_root, result)
        if not profile:
            continue
        profiled += 1

        try:
            sb.table("brain_script_registry").update({
                "behavioral_summary": profile["behavioral_summary"],
                "assumptions":        json.dumps(profile["assumptions"]),
                "flagged_issues":     json.dumps(profile["flagged_issues"]),
                "last_profiled":      _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }).eq("script_path", rel_path).execute()
        except Exception as e:
            logger.error(f"  Profiler: registry update failed for {rel_path}: {e}")
            continue  # don't generate proposals for a write that didn't persist

        for issue in profile["flagged_issues"]:
            severity = str(issue.get("severity", "low")).lower()
            line     = issue.get("line")
            key_part = f"L{line}" if line else str(abs(hash(issue.get("issue",""))) % 10_000)
            proposals.append({
                "type":           "CODE_SUGGESTION",
                "target_key":     f"code_suggestion:{rel_path}:{key_part}",
                "current_value":  f"{rel_path}" + (f" line {line}" if line else ""),
                "proposed_value": str(issue.get("issue", ""))[:500],
                "rationale":      str(issue.get("issue", ""))[:1000],
                "confidence":     _SEVERITY_TO_CONF.get(severity, 0.35),
                "priority":       _SEVERITY_TO_PRIORITY.get(severity, 7),
                "source":         "script_profiler",
            })

    logger.info(f"  Profiler: {profiled}/{len(to_profile)} script(s) profiled, "
                f"{len(proposals)} CODE_SUGGESTION proposal(s) generated")
    return proposals
