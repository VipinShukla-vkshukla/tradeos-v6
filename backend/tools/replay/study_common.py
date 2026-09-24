"""
Shared machinery for the newer IGN replay studies (short side, forming candle).

The long-side study's own files (ign_feature_stats.py / ign_feature_study.py) are
pre-registered and stay byte-identical, so this re-uses their statistics (imported, not
copied) and adds only what those files hard-code for LONG: a train stage over an arbitrary
hypothesis list, an "armable" filter that respects which SIGN a gate check can act on, a
gate verdict with a pluggable verdict function, and a one-look holdout guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from tools.replay import ign_feature_stats as S


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd or Path(__file__).resolve().parent,
                          capture_output=True, text=True, check=False).stdout.strip()


def preflight(files: list[Path], results_dir: Path, prefix: str) -> tuple[bool, str, str]:
    """(ok, reason, sha12). The holdout may only be revealed for COMMITTED, unmodified
    copies of every study file, and once per set of file versions."""
    root = Path(_git("rev-parse", "--show-toplevel")).resolve()
    shas = []
    for f in files:
        f = Path(f).resolve()
        rel = f.relative_to(root).as_posix()
        if _git("status", "--porcelain", "--", str(f)):
            return False, f"{f.name} has uncommitted changes — commit it first (R2)", ""
        head, disk = _git("rev-parse", f"HEAD:{rel}"), _git("hash-object", str(f))
        if not head or head != disk:
            return False, f"{f.name} on disk is not the committed version (R2)", ""
        shas.append(disk)
    sha12 = "".join(s[:6] for s in shas)
    if (results_dir / f"{prefix}_{sha12}.json").exists():
        return False, (f"the holdout was already revealed for these exact file versions "
                       f"({prefix}_{sha12}.json) — one look only (R3)"), sha12
    return True, "", sha12


def train_stage(train: list[S.Row], hypotheses) -> dict:
    """Pre-registered features tested on TRAIN only; Holm across the whole list."""
    tests = [S.test_feature(train, f, s, with_ci=False) for f, s in hypotheses]
    for t, a in zip(tests, S.holm([t["p"] for t in tests])):
        t["p_holm"] = a
        t["candidate"] = bool(a == a and a <= S.HOLM_ALPHA and S.sign_agrees(t["rho"], t["sign"]))
    return {"tests": tests, "candidates": [t["feature"] for t in tests if t["candidate"]]}


def armable(holdout_tests: list[dict], gate_sign: dict[str, int]) -> list[str]:
    """Confirmed features whose holdout rho has the sign the gate check ACTS on. A gate
    can only say "require above the SuperTrend" or "cap -DI"; a feature confirmed with
    the opposite sign is a finding, not something that can be armed."""
    out = []
    for t in holdout_tests:
        need = gate_sign.get(t["feature"])
        if t.get("confirmed") and need and t["rho"] == t["rho"] and (t["rho"] > 0) == (need > 0):
            out.append(t["feature"])
    return out


def gate_verdict(hold: list[S.Row], features: list[str], *, gate_map: dict, off_cfg: dict,
                 verdict_fn) -> dict:
    """ign_feature_stats.gate_verdict's decision rule (size floor, kept-fraction floor,
    bootstrap CI above zero) with the verdict function and check map supplied, so the
    SHORT gate is judged by trend_verdict_short and not by the long one."""
    usable = [f for f in features if f in gate_map]
    if not usable:
        return {"arm": False, "why": "no confirmed signal has a gate check"}
    cfg = dict(off_cfg)
    for f in usable:
        key, val = gate_map[f]
        cfg[key] = val
    part = {"pass": [], "refuse": [], "abstain": []}
    for r in hold:
        part[verdict_fn(r.feats, **cfg)[0]].append(r)
    kept, dropped = part["pass"] + part["abstain"], part["refuse"]
    if not dropped or not kept:
        return {"arm": False, "cfg": cfg, "why": "gate separates nothing on the holdout",
                "kept": len(kept), "dropped": len(dropped)}
    kr = np.asarray([r.r for r in kept])
    dr = np.asarray([r.r for r in dropped])
    diff = float(kr.mean() - dr.mean())
    frac = len(kept) / len(hold)
    rng = np.random.default_rng(7)
    boots = [kr[rng.integers(0, len(kr), len(kr))].mean()
             - dr[rng.integers(0, len(dr), len(dr))].mean() for _ in range(S.N_BOOT)]
    lo = float(np.quantile(boots, (1 - S.CI_LEVEL) / 2))
    ok = bool(diff >= S.MIN_IMPROVEMENT_R and frac >= S.MIN_KEPT_FRAC and lo > 0)
    return {"arm": ok, "cfg": cfg, "features": usable, "kept": len(kept),
            "dropped": len(dropped), "kept_frac": frac, "kept_mean_r": float(kr.mean()),
            "dropped_mean_r": float(dr.mean()), "diff": diff, "diff_ci_lo": lo,
            "why": "arm" if ok else
            f"diff {diff:+.3f}R (need >= {S.MIN_IMPROVEMENT_R}), kept {frac:.0%} "
            f"(need >= {S.MIN_KEPT_FRAC:.0%}), CI lo {lo:+.3f} (need > 0)"}


def fmt_p(p: float) -> str:
    return "  nan" if p != p else f"{p:.3f}"


def print_train(out: dict) -> None:
    print(f"  {'feature':<16}{'exp':>4}{'n':>6}{'rho':>8}{'p':>8}{'p_holm':>8}  candidate")
    for t in out["tests"]:
        exp = {1: "+", -1: "-", 0: "+/-"}[t["sign"]]
        rho = "  nan" if t["rho"] != t["rho"] else f"{t['rho']:+.3f}"
        print(f"  {t['feature']:<16}{exp:>4}{t['n']:>6}{rho:>8}{fmt_p(t['p']):>8}"
              f"{fmt_p(t['p_holm']):>8}  {'YES' if t['candidate'] else '-'}")
