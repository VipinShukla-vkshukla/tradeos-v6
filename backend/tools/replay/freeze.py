"""
Freezing parameters — REPLAY_DESIGN §8. The honesty mechanism.

WHAT GOES WRONG WITHOUT IT
---------------------------
Every function the harness imports reads `system_config` through `cfg()` AT CALL
TIME. `base.confirmation_pct()` does; so do all seven of `squeeze.py`'s
thresholds and the sixty-odd others the engines carry. A replay that lets that
happen is applying tonight's switches to March's bars, and — worse — is not
reproducible tomorrow, because the number it produced depended on a mutable row
nobody recorded.

The 2026-08-14 verification ran without this and said so. That was defensible
only because the session was two days old; it was measured afterwards and no
engine key had changed (0 of 89 with `updated_at` past the session). For March
the same assumption is worth nothing.

THE SEAM, AND WHY IT IS THE RIGHT ONE
--------------------------------------
`cfg()`, `cfg_bool()`, `cfg_int()` and `cfg_float()` all resolve through one
module-level dict, `config._sys_config` (`config.py:369-401`). Freezing is
therefore a substitution of that dict, not a patch of four functions — the
engines are not modified, not wrapped, and cannot tell the difference. Nothing
in `intraday/` or `control/` changes.

TWO SOURCES OF A VALUE, AND BOTH ARE PINNED
---------------------------------------------
A key that is absent from `system_config` is not absent from the system: `cfg`
returns the CALLER's literal default, and 18 of the 107 keys the engines read
resolve that way today. Recording only the table would leave those 18 free to
move whenever someone edits a default in an engine — a "frozen" replay that
silently changes. So the frozen file records the RESOLVED value for every key,
tagged with where it came from, and `apply()` installs all of them. Editing a
literal default in an engine then cannot alter a frozen run; it shows up as a
`freeze --check` mismatch, which is the point.

CODE IDENTITY
--------------
Parameters alone do not identify a run. The same `frozen.json` against a changed
`squeeze.py` is a different experiment wearing the same label, so the file also
carries the git object SHA of every path whose content decides a detection — the
five §8 names. These are content hashes from HEAD, so they change if and only if
the content changes, and they are meaningless on a dirty tree. That is why the
holdout runner refuses one.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

import config as _config
from config import IST

REPO = Path(__file__).resolve().parents[3]
BACKEND = Path(__file__).resolve().parents[2]
PARAMS_DIR = Path(__file__).resolve().parent / "params"

# Every path whose content decides a detection, an exit or a universe.
# REPLAY_DESIGN §8 names exactly these five.
CODE_PATHS = (
    "backend/intraday/strategies",
    "backend/intraday/exit_policy.py",
    "backend/control/position_lifecycle.py",
    "backend/analysis/risk_model.py",
    "backend/intraday/scanner.py",
)

# Where the keys are read from. Scanned statically for `cfg*("name", default)`.
# `intraday/strategies/` is a directory and every module in it is scanned.
CONFIG_SOURCES = (
    "intraday/strategies",
    "intraday/session.py",
    "intraday/market_context.py",
    "intraday/scanner.py",
    "intraday/exit_policy.py",
    "intraday/cost_model.py",
    "control/position_lifecycle.py",
    "analysis/risk_model.py",
)

_CALL = re.compile(
    r"\bcfg(_bool|_int|_float)?\(\s*[\"']([a-zA-Z0-9_]+)[\"']\s*(?:,\s*([^)]*?))?\s*\)")


# ── git ─────────────────────────────────────────────────────────────────────
def _git(*args: str) -> str:
    out = subprocess.run(("git", *args), cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def tree_is_clean() -> tuple[bool, str]:
    """`git status --porcelain` empty. Returns (clean, the dirt) so callers can print it."""
    dirt = subprocess.run(("git", "status", "--porcelain"), cwd=REPO,
                          capture_output=True, text=True).stdout.strip()
    return (not dirt), dirt


def code_shas() -> dict[str, str]:
    """
    The git object SHA of each §8 path, from HEAD.

    A missing path RAISES rather than returning a placeholder. A freeze that
    silently omits `squeeze.py` because someone moved it is a freeze that
    identifies the wrong experiment, and every later comparison against it is
    then a comparison against nothing.
    """
    out: dict[str, str] = {}
    for p in CODE_PATHS:
        line = _git("rev-parse", f"HEAD:{p}")
        if not line:
            raise RuntimeError(f"freeze: no git object for {p}")
        out[p] = line
    return out


# ── key discovery ───────────────────────────────────────────────────────────
def _source_files() -> list[Path]:
    files: list[Path] = []
    for rel in CONFIG_SOURCES:
        p = BACKEND / rel
        if p.is_dir():
            files.extend(sorted(p.glob("*.py")))
        elif p.exists():
            files.append(p)
        else:
            # Loud, not skipped — see code_shas().
            raise RuntimeError(f"freeze: config source {rel} does not exist")
    return files


def discover_keys() -> dict[str, dict]:
    """
    Every `cfg*` key the replayed code reads, with the literal default beside it.

    Static extraction, deliberately. Importing the modules and watching which
    keys they ask for would only find the keys hit on the paths that happened to
    run, and a threshold inside a branch no bar reached is exactly the parameter
    a later window will trip over.
    """
    found: dict[str, dict] = {}
    for f in _source_files():
        rel = str(f.relative_to(BACKEND)).replace("\\", "/")
        for kind, key, default in _CALL.findall(f.read_text(encoding="utf-8")):
            rec = found.setdefault(key, {"reader": f"cfg{kind}", "defaults": {},
                                         "read_by": []})
            if rel not in rec["read_by"]:
                rec["read_by"].append(rel)
            d = (default or "").strip()
            if d:
                rec["defaults"][rel] = d
    return dict(sorted(found.items()))


def _resolve(key: str, rec: dict, table: dict) -> tuple[str, str]:
    """(value, source) — the table wins, then the literal default, then empty."""
    if key in table and table[key] is not None:
        return str(table[key]), "system_config"
    for d in rec["defaults"].values():
        # The literal as written, normalised the way `cfg` would see it. A bare
        # True/False becomes the lower-case string `cfg_bool` tests against.
        lit = d.strip().strip("\"'")
        if lit in ("True", "False"):
            lit = lit.lower()
        return lit, "source_default"
    return "", "absent"


# ── the file ────────────────────────────────────────────────────────────────
@dataclass
class FrozenParams:
    label: str
    created_at: str
    code: dict[str, str]
    values: dict[str, str]
    provenance: dict[str, dict]
    sha: str = ""
    notes: dict = field(default_factory=dict)

    def body(self) -> dict:
        """Everything the SHA is taken over. `sha` itself is excluded."""
        return {"label": self.label, "code": self.code, "values": self.values,
                "provenance": self.provenance}

    def compute_sha(self) -> str:
        return hashlib.sha256(
            json.dumps(self.body(), sort_keys=True, separators=(",", ":")
                       ).encode("utf-8")).hexdigest()

    def to_json(self) -> str:
        return json.dumps({**self.body(), "created_at": self.created_at,
                           "notes": self.notes, "sha": self.sha},
                          indent=2, sort_keys=True) + "\n"


def build(label: str = "frozen") -> FrozenParams:
    """
    Read the config ONCE and describe the code beside it.

    This is the only place in the harness that touches `system_config`, and it
    touches it exactly once per freeze.
    """
    from config import get_supabase
    keys = discover_keys()
    table = _config.get_system_config(refresh=True)

    # F-19 GUARD. `config.get_system_config()` is an UNPAGED
    # `.select("key,value")`, so PostgREST caps it at 1000 rows and says
    # nothing. 510 rows today, but a freeze silently missing 400 keys would
    # resolve every one of them to a source default and look completely normal —
    # the exact shape of the defect that cost this project 91% of its price
    # history. Counted against the server, not assumed.
    from config import get_supabase
    server = (get_supabase().table("system_config").select("key", count="exact")
              .limit(1).execute().count)
    if server is not None and len(table) < server:
        raise RuntimeError(
            f"system_config read returned {len(table)} of {server} rows — the "
            f"unpaged select in config.get_system_config() is truncating, and "
            f"every missing key would freeze to a source default with no "
            f"symptom. Page the read before freezing anything.")
    # A SECRET MUST NEVER REACH THIS FILE. The frozen params are committed to
    # git by design (§8, R2), and the repository's root .gitignore blanket-
    # ignores `*.json` precisely to keep credentials out of the history — the
    # replay package carries a narrow exemption so R2 can be satisfied at all.
    # That exemption is only defensible paired with this refusal.
    secret = {r["key"] for r in
              (get_supabase().table("system_config").select("key,is_secret")
               .eq("is_secret", True).execute().data or [])}
    leaked = sorted(secret & set(keys))
    if leaked:
        raise RuntimeError(
            f"refusing to freeze {len(leaked)} key(s) marked is_secret in "
            f"system_config: {leaked}. This file is committed to git.")

    values: dict[str, str] = {}
    prov: dict[str, dict] = {}
    for key, rec in keys.items():
        val, src = _resolve(key, rec, table)
        values[key] = val
        prov[key] = {"source": src, "reader": rec["reader"],
                     "read_by": rec["read_by"]}
    counts: dict[str, int] = {}
    for p in prov.values():
        counts[p["source"]] = counts.get(p["source"], 0) + 1

    fp = FrozenParams(
        label=label,
        created_at=datetime.now(IST).isoformat(),
        code=code_shas(),
        values=values,
        provenance=prov,
        notes={"keys": len(values), "by_source": counts,
               "config_sources": list(CONFIG_SOURCES)})
    fp.sha = fp.compute_sha()
    return fp


def path_for(label: str = "frozen") -> Path:
    return PARAMS_DIR / f"{label}.json"


def write(fp: FrozenParams) -> Path:
    PARAMS_DIR.mkdir(parents=True, exist_ok=True)
    p = path_for(fp.label)
    p.write_text(fp.to_json(), encoding="utf-8")
    return p


def load(label: str = "frozen") -> FrozenParams:
    p = path_for(label)
    if not p.exists():
        raise FileNotFoundError(
            f"no frozen parameters at {p} — run `python -m tools.replay.freeze "
            f"--label {label}` and COMMIT the result before scoring a window")
    raw = json.loads(p.read_text(encoding="utf-8"))
    fp = FrozenParams(label=raw["label"], created_at=raw.get("created_at", ""),
                      code=raw["code"], values=raw["values"],
                      provenance=raw["provenance"], sha=raw.get("sha", ""),
                      notes=raw.get("notes", {}))
    recomputed = fp.compute_sha()
    if fp.sha and fp.sha != recomputed:
        raise ValueError(
            f"{p.name}: recorded sha {fp.sha[:12]} does not match its own "
            f"contents ({recomputed[:12]}) — the file was edited by hand")
    fp.sha = recomputed
    return fp


# ── application ─────────────────────────────────────────────────────────────
class frozen_config:
    """
    Run a block with `cfg*` resolving from the frozen file and nowhere else.

    A context manager rather than a global switch, and it restores the previous
    dict on the way out, because `config._sys_config` is process-wide — the same
    property that makes `cfg_ctx()` necessary in the test suite. One replay
    leaking its parameters into the next would produce two windows scored under
    one set of switches and no record of which.

    Entering it also proves the code has not moved underneath the file: the §8
    SHAs are recompared against HEAD and a mismatch RAISES. A frozen parameter
    set applied to different code is a different experiment.
    """

    def __init__(self, fp: FrozenParams, check_code: bool = True):
        self.fp = fp
        self.check_code = check_code
        self._prev = None

    def __enter__(self) -> FrozenParams:
        if self.check_code:
            now = code_shas()
            drift = {p: (self.fp.code.get(p), now.get(p))
                     for p in now if self.fp.code.get(p) != now.get(p)}
            if drift:
                raise RuntimeError(
                    "frozen parameters were captured against different code:\n  "
                    + "\n  ".join(f"{p}: frozen {a} -> HEAD {b}"
                                  for p, (a, b) in drift.items()))
        self._prev = _config._sys_config
        _config._sys_config = dict(self.fp.values)
        return self.fp

    def __exit__(self, *exc) -> bool:
        _config._sys_config = self._prev
        return False


# ── CLI ─────────────────────────────────────────────────────────────────────
def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default="frozen")
    ap.add_argument("--check", action="store_true",
                    help="compare the committed file against the live config "
                         "and HEAD, change nothing, and exit non-zero on drift")
    args = ap.parse_args()

    if args.check:
        try:
            have = load(args.label)
        except (FileNotFoundError, ValueError) as e:
            print(f"FAIL: {e}")
            return 1
        fresh = build(args.label)
        vdrift = {k: (have.values.get(k), fresh.values.get(k))
                  for k in set(have.values) | set(fresh.values)
                  if have.values.get(k) != fresh.values.get(k)}
        cdrift = {k: (have.code.get(k), fresh.code.get(k))
                  for k in set(have.code) | set(fresh.code)
                  if have.code.get(k) != fresh.code.get(k)}
        print(f"frozen : {path_for(args.label)}  sha {have.sha[:12]}  "
              f"{len(have.values)} keys")
        for k, (a, b) in sorted(vdrift.items()):
            print(f"  VALUE DRIFT  {k}: frozen {a!r} -> live {b!r}")
        for k, (a, b) in sorted(cdrift.items()):
            print(f"  CODE  DRIFT  {k}: frozen {a} -> HEAD {b}")
        if not vdrift and not cdrift:
            print("OK: the frozen file still describes the live config and HEAD")
            return 0
        print(f"FAIL: {len(vdrift)} value drift(s), {len(cdrift)} code drift(s)")
        return 1

    clean, dirt = tree_is_clean()
    fp = build(args.label)
    p = write(fp)
    print(f"wrote {p}")
    print(f"  sha        : {fp.sha}")
    print(f"  keys       : {fp.notes['keys']}  {fp.notes['by_source']}")
    for k, v in fp.code.items():
        print(f"  {k:<40} {v}")
    if not clean:
        # A warning here and a REFUSAL in the holdout runner. Freezing from a
        # dirty tree is a normal thing to do while iterating; SCORING a holdout
        # from one is not, because the SHAs above then describe HEAD rather than
        # the code that actually ran.
        print(f"\nWARNING: working tree is dirty — the code SHAs above are "
              f"HEAD's, not what is on disk. Commit before scoring a holdout.")
    print("\nNEXT: commit this file. The holdout runner refuses to start until "
          "it resolves to a committed git object.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
