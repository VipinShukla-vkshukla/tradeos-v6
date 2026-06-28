"""
TradeOS v6 — Brain Engine v2: Consistency Checker
====================================================
script_profiler.py records what each script ASSUMES about every table/config
resource it touches. This module is the payoff: it groups those assumptions
by shared resource and flags genuine conflicts — e.g. script A writes a
column as a JSON list, script B (added last week) reads it as a
comma-separated string.

Runs after profiling in the same weekly cycle, reading directly from
brain_script_registry (no re-scanning, no re-reading source files).

Cost control: only resources where 2+ scripts have NON-IDENTICAL assumption
text get sent to the LLM, and they're all batched into a single call per run
— comparing short text snippets, not source code, so this stays cheap
regardless of repo size.
"""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from loguru import logger
from ai.ai_router import raw_completion, is_ai_available

LLM_TIMEOUT_SEC = 90  # must not block the run indefinitely on one bad call


def _call_with_timeout(fn, args, timeout):
    """Same fix as script_profiler.py — see that module's docstring on this
    function for why a plain ThreadPoolExecutor doesn't actually work here
    (its atexit machinery blocks process exit on an abandoned thread)."""
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

_CONFLICT_PROMPT = """You are checking for assumption conflicts across scripts in a live \
trading system. Each entry below lists one shared table column or config key, and what \
every script that touches it assumes about its format, range, type, or meaning.

For each entry, decide: do the listed assumptions genuinely CONFLICT — i.e. would cause a \
real bug if both scripts ran against the same data (one expects a JSON list, another a \
comma-separated string; one expects a 0-1 float, another a 0-100 percentage; one expects \
nulls to mean "unset", another treats null as zero) — or are they just worded differently \
while describing the same compatible thing?

INPUT:
{entries}

Return ONLY valid JSON, no markdown fences, no preamble:
[
  {{"resource": "<exact resource string from input>", "conflict": true|false,
    "explanation": "<one sentence, ONLY if conflict is true — be specific about the \
mismatch, e.g. 'script A writes list, script B parses as string'>"}}
]

Default to conflict=false when in doubt — only flag what you're confident would actually \
break something. Different wording for the same underlying assumption is NOT a conflict."""


def _normalize(text: str) -> str:
    return " ".join(str(text).lower().split())


def _collect_candidates(registry_rows: list[dict]) -> dict:
    """
    {resource: [{"script": path, "expects": text}, ...]} for resources
    touched by 2+ scripts with non-identical assumption text.
    """
    by_resource: dict[str, list[dict]] = {}
    for row in registry_rows:
        raw_assumptions = row.get("assumptions")
        if not raw_assumptions:
            continue
        try:
            assumptions = json.loads(raw_assumptions) if isinstance(raw_assumptions, str) else raw_assumptions
        except Exception:
            continue
        if not isinstance(assumptions, list):
            continue
        for a in assumptions:
            resource = a.get("resource")
            expects  = a.get("expects")
            if not resource or not expects:
                continue
            by_resource.setdefault(resource, []).append({
                "script": row.get("script_path", "?"),
                "expects": expects,
            })

    candidates = {}
    for resource, entries in by_resource.items():
        # de-dupe same script appearing twice for the same resource
        seen_scripts = {}
        for e in entries:
            seen_scripts[e["script"]] = e["expects"]
        if len(seen_scripts) < 2:
            continue
        distinct_texts = {_normalize(v) for v in seen_scripts.values()}
        if len(distinct_texts) < 2:
            continue  # everyone agrees, word-for-word (after normalizing) — skip
        candidates[resource] = [{"script": s, "expects": v} for s, v in seen_scripts.items()]
    return candidates


def check_consistency(sb) -> list[dict]:
    """
    Returns a list of CONSISTENCY_CONFLICT proposal dicts (unsaved).
    """
    rows = (sb.table("brain_script_registry")
              .select("script_path,assumptions")
              .execute().data or [])

    candidates = _collect_candidates(rows)
    if not candidates:
        logger.info("  Consistency checker: no overlapping resources with differing assumptions")
        return []

    if not is_ai_available():
        logger.info(f"  Consistency checker: {len(candidates)} candidate(s) found but no "
                     f"AI provider available to adjudicate — skipping rather than guessing")
        return []

    entries_payload = [
        {"resource": resource, "scripts": scripts}
        for resource, scripts in candidates.items()
    ]

    try:
        raw = _call_with_timeout(
            raw_completion,
            (_CONFLICT_PROMPT.format(entries=json.dumps(entries_payload, indent=2)), 1500),
            LLM_TIMEOUT_SEC,
        )
    except TimeoutError:
        logger.warning(f"  Consistency checker: LLM call timed out after {LLM_TIMEOUT_SEC}s")
        return []
    except Exception as e:
        logger.warning(f"  Consistency checker LLM call failed: {e}")
        return []

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        verdicts = json.loads(raw.strip())
    except Exception as e:
        logger.warning(f"  Consistency checker: malformed LLM JSON ({e})")
        return []
    if not isinstance(verdicts, list):
        return []

    proposals = []
    for v in verdicts:
        if not v.get("conflict"):
            continue
        resource = v.get("resource")
        scripts  = candidates.get(resource)
        if not scripts:
            continue
        summary = "; ".join(f"{s['script']}: {s['expects']}" for s in scripts)[:500]
        proposals.append({
            "type":           "CONSISTENCY_CONFLICT",
            "target_key":     f"consistency:{resource}",
            "current_value":  summary,
            "proposed_value": "Reconcile the conflicting assumption — see rationale",
            "rationale":      str(v.get("explanation", ""))[:1000],
            "confidence":     0.6,
            "priority":       3,
            "source":         "consistency_checker",
        })

    logger.info(f"  Consistency checker: {len(candidates)} candidate(s) checked, "
                f"{len(proposals)} real conflict(s) flagged")
    return proposals
