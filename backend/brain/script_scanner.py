"""
TradeOS v6 — Brain Engine v2: Script Scanner
=============================================
Plug-and-play script analysis. Point it at any Python file and it:
  1. Extracts all hardcoded numeric/string values that are candidates
     for system_config migration (tunable parameters)
  2. Identifies which Supabase tables the script reads/writes
  3. Identifies which system_config keys the script already uses
  4. Generates a unified diff showing the proposed cfg() migration
  5. Registers everything in brain_script_registry

ADDING A NEW SCRIPT TO BRAIN SCOPE:
  Zero manual steps. The scanner walks every path listed as active in
  system_config.brain_scan_roots (default: just "backend"). New scripts
  under an active root are auto-discovered, scanned, and registered on
  the next brain cycle — no registration step.

SCRIPT PATCHING:
  SCRIPT_PATCH proposals (cfg() migration diffs) are always REVIEW_ONLY.
  There is no auto-commit path: a GitHub Actions runner is destroyed the
  moment the job ends, and committing a file inside that runner without a
  push doesn't persist the change anywhere — so this scanner only ever
  generates the diff for manual review/application, never attempts to
  write or commit it.
"""

import ast
import difflib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg

# Patterns that indicate a value is likely a tunable parameter (not a constant)
TUNABLE_PATTERNS = [
    r"^[A-Z_]+\s*=\s*[\d.]+",          # ALL_CAPS = number
    r"threshold",                         # anything with threshold in name
    r"min_|max_|_min|_max",              # min/max variables
    r"weight|score|ratio|pct|factor",    # score/weight variables
    r"period|window|lookback|horizon",   # time period variables
    r"confidence|conviction",             # confidence thresholds
]

# Supabase table names to detect read/write operations
_TABLES_FALLBACK = [
    "signal_log", "msl_history", "stock_data_daily", "ai_context",
    "system_config", "lessons", "open_positions", "closed_positions",
    "master_shortlist", "msl_computed", "performance_metrics",
    "brain_proposals", "config_change_log", "brain_analysis_log",
    "brain_script_registry",
]

# Ignore these patterns — they are not tunable
IGNORE_PATTERNS = [
    r"^\s*#",           # comments
    r"version\s*=",     # version strings
    r"__",              # dunder attributes
    r"port\s*=\s*\d+",  # ports
    r"chunk_size\s*=",  # implementation details
]


def _sanitize_module_name(stem: str) -> str:
    """
    A raw filename stem can contain anything the filesystem allows — spaces,
    hyphens, mixed case (e.g. a Windows "make a copy" duplicate like
    "ingest_sheets - Copy.py"). Config keys everywhere else in this system
    are lowercase, underscore-separated, no spaces or punctuation — without
    sanitizing, a stray file like that produces a key like
    "ingest_sheets - copy_threshold", which is a different kind of broken
    than what it's tunable-value-detection was trying to do.
    """
    s = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return s or "unnamed"


class ScriptScanner:

    def __init__(self, backend_root: str = None):
        # backend_root stays as the default/fallback resolution anchor — repo root
        # is one level up from it. Explicit backend_root param (tests, manual runs)
        # bypasses brain_scan_roots entirely, same as before.
        self._explicit_root = Path(backend_root) if backend_root else None
        self.backend_root   = Path(backend_root or Path(__file__).parent.parent)
        self.repo_root       = self.backend_root.parent
        self.sb = get_supabase()

    def _scan_roots(self) -> list[dict]:
        """
        Active scan roots from system_config.brain_scan_roots. Falls back to
        just "backend" (the original hardcoded behaviour) if unset — adding a
        new root, or enabling frontend once a TS adapter exists, is then a
        config edit, not a code change.
        """
        if self._explicit_root:
            return [{"path": str(self._explicit_root), "lang": "python", "active": True}]
        raw = cfg("brain_scan_roots", "")
        if not raw:
            return [{"path": "backend", "lang": "python", "active": True}]
        try:
            roots = json.loads(raw)
            if isinstance(roots, list):
                return roots
        except Exception as e:
            logger.warning(f"  brain_scan_roots malformed ({e}) — falling back to backend/")
        return [{"path": "backend", "lang": "python", "active": True}]

    # ─────────────────────────────────────────────────────────────────────
    # FILE DISCOVERY
    # ─────────────────────────────────────────────────────────────────────

    def discover_scripts(self) -> list[Path]:
        """Find all Python files under every active brain_scan_roots entry."""
        skip_dirs = {".venv", "venv", "__pycache__", ".git", "tests",
                     "brain", "node_modules", "migrations"}
        scripts = []
        for root_cfg in self._scan_roots():
            if not root_cfg.get("active", True):
                continue
            if root_cfg.get("lang", "python") != "python":
                logger.warning(f"  Scan root '{root_cfg.get('path')}' has lang="
                                f"'{root_cfg.get('lang')}' — no adapter for that "
                                f"language yet, skipping. Python only for now.")
                continue
            root_path = self.repo_root / root_cfg["path"] if self._explicit_root is None \
                        else Path(root_cfg["path"])
            if not root_path.exists():
                logger.warning(f"  Scan root does not exist, skipping: {root_path}")
                continue
            for path in root_path.rglob("*.py"):
                if any(p in path.parts for p in skip_dirs):
                    continue
                if path.name.startswith("test_"):
                    continue
                scripts.append(path)
        logger.info(f"Discovered {len(scripts)} Python files for scanning "
                    f"across {len(self._scan_roots())} configured root(s)")
        return sorted(scripts)

    # ─────────────────────────────────────────────────────────────────────
    # HARDCODED VALUE EXTRACTION
    # ─────────────────────────────────────────────────────────────────────

    def extract_hardcoded_values(self, script_path: Path) -> list[dict]:
        """
        Parse AST to find hardcoded numeric and string values that are
        candidates for system_config migration.
        Returns list of {line, name, value, context, tunable, proposed_key}.
        """
        try:
            source = script_path.read_text(encoding="utf-8")
        except Exception:
            return []

        findings = []
        lines    = source.splitlines()

        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            # Look for assignments: NAME = value
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = ""
                    if isinstance(target, ast.Name):
                        name = target.id
                    elif isinstance(target, ast.Attribute):
                        name = target.attr

                    if not name:
                        continue

                    # Skip already-cfg() calls
                    if isinstance(node.value, ast.Call):
                        func = node.value.func
                        func_name = (func.id if isinstance(func, ast.Name) else
                                    func.attr if isinstance(func, ast.Attribute) else "")
                        if func_name in ("cfg", "cfg_int", "cfg_float"):
                            continue

                    # Extract numeric constants
                    if isinstance(node.value, (ast.Constant, ast.Num)):
                        val = node.value.n if isinstance(node.value, ast.Num) else node.value.value
                        if not isinstance(val, (int, float)):
                            continue
                        if val in (0, 1, -1, True, False):  # skip trivial
                            continue

                        line_ctx = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                        is_ignore = any(re.search(p, line_ctx, re.IGNORECASE)
                                        for p in IGNORE_PATTERNS)
                        is_tunable = any(re.search(p, name, re.IGNORECASE)
                                         for p in TUNABLE_PATTERNS)

                        if is_ignore:
                            continue

                        # Derive proposed config key
                        module = _sanitize_module_name(script_path.stem)
                        proposed_key = f"{module}_{name.lower()}"

                        findings.append({
                            "line":         node.lineno,
                            "name":         name,
                            "value":        str(val),
                            "context":      line_ctx[:120],
                            "tunable":      is_tunable,
                            "proposed_key": proposed_key,
                            "type":         "numeric",
                        })

            # Look for dict literals with numeric values (e.g., WEIGHT_MAP = {...})
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
                        name = target.id
                        all_numeric = all(
                            isinstance(v, (ast.Constant, ast.Num)) and
                            isinstance((v.n if isinstance(v, ast.Num) else v.value), (int, float))
                            for v in node.value.values
                        )
                        if all_numeric and len(node.value.values) >= 3:
                            line_ctx = lines[node.lineno-1].strip() if node.lineno <= len(lines) else ""
                            module   = _sanitize_module_name(script_path.stem)
                            findings.append({
                                "line":         node.lineno,
                                "name":         name,
                                "value":        "dict",
                                "context":      line_ctx[:120],
                                "tunable":      True,
                                "proposed_key": f"{module}_{name.lower()}",
                                "type":         "dict",
                            })

        return findings

    def detect_table_usage(self, script_path: "Path") -> "tuple[list[str], list[str]]":
        """
        Detect which Supabase tables a script reads/writes.
        Table list is fetched live from Supabase — no manual maintenance.
        Falls back to _TABLES_FALLBACK if DB is unavailable.
        """
        try:
            source = script_path.read_text(encoding="utf-8")
        except Exception:
            return [], []
 
        reads  = set()
        writes = set()
 
        # Live discovery — automatically includes any table ever created
        try:
            from brain.dynamic_registry import discover_all_tables
            tables_to_check = discover_all_tables(self.sb)
        except Exception:
            tables_to_check = _TABLES_FALLBACK
 
        for table in tables_to_check:
            if table not in source:
                continue
            # Write patterns
            for pattern in [
                rf'\.table\("{table}"\)\s*\.\s*(insert|upsert|update|delete)',
                rf'\.table\(\'{table}\'\)\s*\.\s*(insert|upsert|update|delete)',
            ]:
                if re.search(pattern, source):
                    writes.add(table)
                    break
            # Read patterns
            for pattern in [
                rf'\.table\("{table}"\)\s*\.\s*select',
                rf'\.table\(\'{table}\'\)\s*\.\s*select',
            ]:
                if re.search(pattern, source):
                    reads.add(table)
                    break
 
        return sorted(reads), sorted(writes)
    
    def detect_config_keys(self, script_path: Path) -> list[str]:
        """Detect which system_config keys a script already reads via cfg()."""
        try:
            source = script_path.read_text(encoding="utf-8")
        except Exception:
            return []

        keys = re.findall(r'cfg[_a-z]*\(["\']([a-z_]+)["\']', source)
        return sorted(set(keys))

    def extract_docstring_purpose(self, script_path: Path) -> str:
        """Extract the first docstring from a Python file as purpose description."""
        try:
            source = script_path.read_text(encoding="utf-8")
            tree   = ast.parse(source)
            if (tree.body and isinstance(tree.body[0], ast.Expr)
                    and isinstance(tree.body[0].value, ast.Constant)):
                doc = str(tree.body[0].value.value)
                return doc.strip()[:300]
        except Exception:
            pass
        return ""

    # ─────────────────────────────────────────────────────────────────────
    # DIFF GENERATION
    # ─────────────────────────────────────────────────────────────────────

    def generate_cfg_migration_diff(self, script_path: Path,
                                     findings: list[dict]) -> str:
        """
        Generate a unified diff showing how to migrate hardcoded values to cfg().
        This is the SCRIPT_PATCH proposal content — fully reversible.
        """
        try:
            original = script_path.read_text(encoding="utf-8")
        except Exception:
            return ""

        lines    = original.splitlines(keepends=True)
        patched  = list(lines)

        # Process from bottom to top so line numbers stay valid
        for finding in sorted(findings, key=lambda x: x["line"], reverse=True):
            if not finding.get("tunable") or finding["type"] != "numeric":
                continue
            line_idx  = finding["line"] - 1
            if line_idx >= len(patched):
                continue
            old_line  = patched[line_idx]
            val_str   = finding["value"]
            key       = finding["proposed_key"]
            name      = finding["name"]
            # Replace value with cfg() call, preserving indentation
            indent    = len(old_line) - len(old_line.lstrip())
            spaces    = " " * indent
            new_line  = f"{spaces}{name} = cfg_float(\"{key}\", {val_str})\n"
            patched[line_idx] = new_line

        diff = list(difflib.unified_diff(
            lines, patched,
            fromfile=f"a/{script_path.name}",
            tofile=f"b/{script_path.name}",
            lineterm="\n",
        ))
        return "".join(diff)

    # ─────────────────────────────────────────────────────────────────────
    # REGISTRY UPDATE
    # ─────────────────────────────────────────────────────────────────────

    def scan_and_register(self, script_path: Path) -> dict:
        """
        Full scan of one script. Upserts result into brain_script_registry.
        Returns the registry entry dict.
        """
        rel_path    = str(script_path.relative_to(self.repo_root))
        hardcoded   = self.extract_hardcoded_values(script_path)
        reads, writes = self.detect_table_usage(script_path)
        config_keys = self.detect_config_keys(script_path)
        purpose     = self.extract_docstring_purpose(script_path)
        diff        = self.generate_cfg_migration_diff(script_path, hardcoded)

        tunable_count = sum(1 for h in hardcoded if h.get("tunable"))
        coverage = ("FULL"    if tunable_count == 0 else
                    "PARTIAL" if len(config_keys) > 0 else
                    "NONE")

        entry = {
            "script_path":      rel_path,
            "module_name":      rel_path.replace("/", ".").replace(".py", ""),
            "purpose":          purpose[:300] if purpose else None,
            "tables_read":      reads,
            "tables_written":   writes,
            "config_keys_used": config_keys,
            "hardcoded_values": json.dumps([h for h in hardcoded if h.get("tunable")])[:5000],
            "brain_coverage":   coverage,
            "last_scanned":     datetime.now(timezone.utc).isoformat(),
            "last_modified":    datetime.fromtimestamp(
                script_path.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        }

        try:
            self.sb.table("brain_script_registry").upsert(
                entry, on_conflict="script_path"
            ).execute()
        except Exception as e:
            logger.warning(f"  Registry upsert failed for {rel_path}: {e}")

        return {**entry, "hardcoded_values": hardcoded, "diff": diff}

    def scan_all(self) -> list[dict]:
        """Scan all discovered scripts and return registry entries."""
        scripts = self.discover_scripts()
        results = []
        for script in scripts:
            try:
                result = self.scan_and_register(script)
                results.append(result)
            except Exception as e:
                logger.warning(f"  Scan failed for {script}: {e}")
        logger.info(f"Script scan complete: {len(results)} scripts registered")
        return results

    def generate_scan_report(self, results: list[dict]) -> str:
        """Generate a human-readable report of script scan findings."""
        total         = len(results)
        full_coverage = sum(1 for r in results if r.get("brain_coverage") == "FULL")
        partial       = sum(1 for r in results if r.get("brain_coverage") == "PARTIAL")
        none_cov      = sum(1 for r in results if r.get("brain_coverage") == "NONE")

        lines = [
            f"\n{'═'*60}",
            "SCRIPT SCANNER REPORT",
            f"{'═'*60}",
            f"Scripts discovered: {total}",
            f"  Full cfg() coverage:    {full_coverage}",
            f"  Partial cfg() coverage: {partial}",
            f"  No cfg() coverage:      {none_cov}",
            "",
            "Scripts with tunable hardcoded values:",
        ]

        for r in sorted(results, key=lambda x: len(x.get("hardcoded_values") or []), reverse=True):
            hv = r.get("hardcoded_values") or []
            if not isinstance(hv, list):
                continue
            tunable = [h for h in hv if h.get("tunable")]
            if not tunable:
                continue
            lines.append(f"\n  {r['script_path']} [{r.get('brain_coverage','?')}]")
            lines.append(f"    Reads: {r.get('tables_read',[])} | Writes: {r.get('tables_written',[])}")
            for h in tunable[:5]:
                lines.append(f"    L{h['line']}: {h['name']} = {h['value']}  → cfg key: {h['proposed_key']}")
            if len(tunable) > 5:
                lines.append(f"    ... and {len(tunable)-5} more")

        lines.append(f"\n{'═'*60}")
        lines.append("SCRIPT_PATCH proposals are review-only — cfg() migration diffs")
        lines.append("are generated for you to apply manually, never auto-committed.")
        lines.append(f"{'═'*60}\n")
        return "\n".join(lines)


def run_scan(backend_root: str = None) -> tuple[list[dict], str, list[dict]]:
    """
    Entry point for the weekly scan cycle:
      1. Hardcode/table/config scan — every script, every run (cheap, AST-based)
      2. Behavioral profiling — only new/changed scripts (LLM-based)
      3. Cross-script consistency check — reads what profiling just wrote

    Returns (scan_results, report, new_proposals). Caller (brain_engine.py)
    decides whether to save_proposals()/send_telegram_digest() with the third
    element — run_scan() itself never writes to brain_proposals.
    """
    scanner = ScriptScanner(backend_root)
    sb      = scanner.sb

    # Snapshot BEFORE scan_all() overwrites last_modified — last_profiled is
    # tracked separately precisely so an interrupted profiling pass doesn't
    # silently lose coverage of whichever files it didn't reach (see
    # script_profiler.py docstring for why last_modified alone isn't safe here).
    previous_registry = {
        r["script_path"]: {"last_modified": r.get("last_modified"),
                            "last_profiled": r.get("last_profiled")}
        for r in (sb.table("brain_script_registry")
                    .select("script_path,last_modified,last_profiled")
                    .execute().data or [])
    }

    results = scanner.scan_all()
    report  = scanner.generate_scan_report(results)
    print(report)

    new_proposals = []
    try:
        from brain.script_profiler import profile_changed_scripts
        new_proposals += profile_changed_scripts(
            results, previous_registry, scanner.repo_root, sb)
    except Exception as e:
        logger.error(f"  Profiling step failed (scan results still saved): {e}")

    try:
        from brain.consistency_checker import check_consistency
        new_proposals += check_consistency(sb)
    except Exception as e:
        logger.error(f"  Consistency check failed (scan results still saved): {e}")

    return results, report, new_proposals
