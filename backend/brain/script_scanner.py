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
  Zero manual steps. The scanner runs automatically on every brain cycle
  against all .py files under backend/. New scripts are auto-discovered,
  scanned, and registered. The brain will analyse them in the next cycle.

SCRIPT PATCHING (brain_script_patching_enabled = true):
  The brain can directly apply cfg() migrations to scripts:
  - Backs up original file content to script_change_log
  - Applies unified diff
  - Commits to git with a descriptive message
  - Stores git hash for rollback
  - Rollback: git revert + restore from script_change_log.backup_content
"""

import ast
import difflib
import json
import os
import re
import subprocess
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


class ScriptScanner:

    def __init__(self, backend_root: str = None):
        self.backend_root = Path(backend_root or Path(__file__).parent.parent)
        self.sb = get_supabase()

    # ─────────────────────────────────────────────────────────────────────
    # FILE DISCOVERY
    # ─────────────────────────────────────────────────────────────────────

    def discover_scripts(self) -> list[Path]:
        """Find all Python files under backend/, excluding test/venv/brain dirs."""
        skip_dirs = {".venv", "venv", "__pycache__", ".git", "tests",
                     "brain", "node_modules", "migrations"}
        scripts = []
        for path in self.backend_root.rglob("*.py"):
            if any(p in path.parts for p in skip_dirs):
                continue
            if path.name.startswith("test_"):
                continue
            scripts.append(path)
        logger.info(f"Discovered {len(scripts)} Python files for scanning")
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
                        module = script_path.stem
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
                            module   = script_path.stem
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
    # SCRIPT PATCHING (controlled write)
    # ─────────────────────────────────────────────────────────────────────

    def apply_patch(self, script_path: Path, diff_text: str,
                    proposal_id: int) -> bool:
        """
        Apply a unified diff to a script file.
        Backs up original, applies patch, commits to git.
        Full rollback via script_change_log.backup_content.
        """
        if not diff_text.strip():
            return False

        try:
            original = script_path.read_text(encoding="utf-8")
            rel_path = str(script_path.relative_to(self.backend_root))

            # Determine change type
            change_type = "HARDCODE_TO_CONFIG"

            # Apply via patch command
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".diff",
                                             delete=False) as tf:
                tf.write(diff_text)
                tf_path = tf.name

            result = subprocess.run(
                ["patch", "-p1", str(script_path), tf_path],
                capture_output=True, text=True,
                cwd=str(self.backend_root),
            )
            os.unlink(tf_path)

            if result.returncode != 0:
                logger.error(f"Patch failed for {rel_path}: {result.stderr}")
                return False

            # Git commit if enabled
            git_hash = None
            if cfg("brain_git_commit_patches", "true").lower() == "true":
                git_hash = self._git_commit(rel_path, proposal_id)

            # Log to script_change_log
            self.sb.table("script_change_log").insert({
                "proposal_id":    proposal_id,
                "script_path":    rel_path,
                "change_type":    change_type,
                "diff_text":      diff_text[:10000],
                "backup_content": original,
                "git_commit_hash": git_hash,
                "applied_by":     "brain_engine",
            }).execute()

            logger.success(f"  Patched {rel_path} (proposal {proposal_id}, git={git_hash})")
            return True

        except Exception as e:
            logger.error(f"  Patch error for {script_path}: {e}")
            return False

    def rollback_patch(self, script_change_id: int, reviewer: str = "manual") -> bool:
        """
        Rollback a script patch using backup_content from script_change_log.
        Also reverts git commit if available.
        """
        try:
            rows = (self.sb.table("script_change_log")
                          .select("*")
                          .eq("id", script_change_id)
                          .execute().data)
            if not rows:
                logger.error(f"script_change_log ID {script_change_id} not found")
                return False

            change   = rows[0]
            path     = self.backend_root / change["script_path"]
            backup   = change.get("backup_content", "")
            git_hash = change.get("git_commit_hash")

            if not backup:
                logger.error("No backup content — cannot rollback")
                return False

            # Restore file
            path.write_text(backup, encoding="utf-8")

            # Git revert if we have the hash
            rollback_hash = None
            if git_hash:
                rollback_hash = self._git_revert(git_hash)

            # Mark rolled back
            self.sb.table("script_change_log").update({
                "rolled_back_at":  datetime.now(timezone.utc).isoformat(),
                "rolled_back_by":  reviewer,
                "rollback_commit": rollback_hash,
            }).eq("id", script_change_id).execute()

            logger.success(f"  Rolled back {change['script_path']}")
            return True

        except Exception as e:
            logger.error(f"  Rollback error: {e}")
            return False

    def _git_commit(self, rel_path: str, proposal_id: int) -> Optional[str]:
        """Stage and commit a changed file. Returns commit hash."""
        try:
            subprocess.run(["git", "add", rel_path], cwd=str(self.backend_root), check=True)
            msg = f"brain[{proposal_id}]: migrate hardcoded values to cfg() in {rel_path}"
            subprocess.run(["git", "commit", "-m", msg],
                           cwd=str(self.backend_root), check=True)
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.backend_root), capture_output=True, text=True,
            )
            return result.stdout.strip()[:12]
        except Exception as e:
            logger.warning(f"  Git commit failed: {e}")
            return None

    def _git_revert(self, commit_hash: str) -> Optional[str]:
        """Revert a git commit. Returns new revert commit hash."""
        try:
            subprocess.run(
                ["git", "revert", "--no-edit", commit_hash],
                cwd=str(self.backend_root), check=True,
            )
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.backend_root), capture_output=True, text=True,
            )
            return result.stdout.strip()[:12]
        except Exception as e:
            logger.warning(f"  Git revert failed: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────
    # REGISTRY UPDATE
    # ─────────────────────────────────────────────────────────────────────

    def scan_and_register(self, script_path: Path) -> dict:
        """
        Full scan of one script. Upserts result into brain_script_registry.
        Returns the registry entry dict.
        """
        rel_path    = str(script_path.relative_to(self.backend_root))
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
        lines.append("To migrate a script: brain will auto-generate SCRIPT_PATCH proposals.")
        lines.append("Enable: set brain_script_patching_enabled=true in system_config.")
        lines.append(f"{'═'*60}\n")
        return "\n".join(lines)


def run_scan(backend_root: str = None) -> tuple[list[dict], str]:
    """Entry point: scan all scripts, return results + report."""
    scanner = ScriptScanner(backend_root)
    results = scanner.scan_all()
    report  = scanner.generate_scan_report(results)
    print(report)
    return results, report
