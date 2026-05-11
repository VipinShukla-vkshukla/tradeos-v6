"""
TradeOS v6 — Brain Engine v2: Backtester + Change Manager
===========================================================
"""

# ═══════════════════════════════════════════════════════════════════════
# BACKTESTER
# ═══════════════════════════════════════════════════════════════════════

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, cfg, cfg_float

MIN_BACKTEST = 10
MAX_CHANGE   = 0.30   # max 30% change per cycle
HIGH_IMPACT  = 0.50   # >50% signal reduction = HIGH_IMPACT


def _metrics(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"count": 0, "win_rate": None, "avg_ret": None}
    ow = "outcome_win" in df.columns and df["outcome_win"].notna().any()
    return {
        "count":   int(len(df)),
        "win_rate": round(float(df["outcome_win"].mean())*100, 1) if ow else None,
        "avg_ret": round(float(df["max_fwd_return"].dropna().mean()), 2)
                   if "max_fwd_return" in df.columns else None,
    }


def _threshold_filter(df, field, val, direction):
    col = pd.to_numeric(df[field], errors="coerce")
    if direction == "floor":
        return df[col >= val]
    elif direction == "ceiling":
        return df[col <= val]
    return df


def backtest_threshold(signals: pd.DataFrame, finding: dict) -> dict:
    field    = finding.get("field")
    current  = finding.get("current")
    proposed = finding.get("proposed")
    direction= finding.get("direction","increase_floor")

    if not field or current is None or proposed is None:
        return {"valid": False, "reason": "missing finding fields"}

    if current != 0 and abs(proposed - current) / abs(current) > MAX_CHANGE:
        return {"valid": False, "reason": f"change exceeds {MAX_CHANGE:.0%} safety cap"}

    ev = signals[signals["outcome_label"].notna()].copy() \
         if "outcome_label" in signals.columns else signals.copy()
    if ev.empty or field not in ev.columns:
        return {"valid": False, "reason": "no evaluable signals or field missing"}

    dir_key = "floor" if "floor" in direction else "ceiling"
    before  = _threshold_filter(ev, field, current,  dir_key)
    after   = _threshold_filter(ev, field, proposed, dir_key)

    bm = _metrics(before)
    am = _metrics(after)

    if bm["count"] < MIN_BACKTEST or am["count"] < MIN_BACKTEST:
        return {"valid": False, "reason": f"insufficient samples: before={bm['count']}, after={am['count']}"}

    wr_delta  = ((am["win_rate"] or 0) - (bm["win_rate"] or 0)
                 if am["win_rate"] is not None and bm["win_rate"] is not None else None)
    reduction = 1 - (am["count"] / bm["count"]) if bm["count"] > 0 else 0

    return {
        "valid":        True,
        "passes":       wr_delta is not None and wr_delta >= 0,
        "high_impact":  reduction > HIGH_IMPACT,
        "before":       bm,
        "after":        am,
        "wr_delta":     round(wr_delta, 1) if wr_delta is not None else None,
        "signal_reduction_pct": round(reduction * 100, 1),
        "summary": (
            f"Before: {bm['count']} signals @ {bm['win_rate']:.0f}% WR. "
            f"After: {am['count']} signals @ {am['win_rate']:.0f}% WR. "
            f"Δ={wr_delta:+.1f}pp, signal count {-reduction*100:.0f}%."
            + (" ⚠️ HIGH_IMPACT" if reduction > HIGH_IMPACT else "")
        ) if wr_delta is not None else "No outcome data available.",
    }


def backtest_engine_weight(signals: pd.DataFrame, finding: dict) -> dict:
    engine    = finding.get("engine")
    direction = finding.get("direction","")
    if not engine:
        return {"valid": False, "reason": "no engine"}

    ev = signals[signals["outcome_label"].notna()].copy() \
         if "outcome_label" in signals.columns else signals.copy()
    if ev.empty or "score_adjusted" not in ev.columns:
        return {"valid": False, "reason": "no evaluable signals with scores"}

    pat_col = next((c for c in ["scanner_patterns","engines_list"] if c in ev.columns), None)
    if not pat_col:
        return {"valid": False, "reason": "no pattern column"}

    ev["_has"] = ev[pat_col].str.contains(engine, na=False, case=False)
    ev["score_adjusted"] = pd.to_numeric(ev["score_adjusted"], errors="coerce")
    top_n = min(20, max(5, len(ev)//5))

    base_topn = ev.nlargest(top_n, "score_adjusted")
    base_wr   = float(base_topn["outcome_win"].mean())*100 \
                if base_topn["outcome_win"].notna().any() else None

    delta = 1.2 if direction == "OUTPERFORMING" else 0.8
    ev["_adj"] = ev.apply(
        lambda r: r["score_adjusted"] * delta if r["_has"] else r["score_adjusted"], axis=1
    )
    adj_topn = ev.nlargest(top_n, "_adj")
    adj_wr   = float(adj_topn["outcome_win"].mean())*100 \
               if adj_topn["outcome_win"].notna().any() else None

    wr_delta = (adj_wr - base_wr) if (adj_wr and base_wr) else None
    return {
        "valid":     True,
        "passes":    wr_delta is not None and wr_delta >= 0,
        "high_impact": False,
        "before":    {"count": top_n, "win_rate": round(base_wr,1) if base_wr else None},
        "after":     {"count": top_n, "win_rate": round(adj_wr,1) if adj_wr else None},
        "wr_delta":  round(wr_delta, 1) if wr_delta else None,
        "summary":   f"Top-{top_n}: {base_wr:.0f}% → {adj_wr:.0f}% WR ({wr_delta:+.1f}pp)"
                     if wr_delta else "Insufficient data.",
    }


def run_backtests(signals: pd.DataFrame, quant_findings: list) -> list:
    validated = []
    for f in quant_findings:
        ftype = f.get("type","")
        try:
            if ftype == "THRESHOLD_CHANGE":
                bt = backtest_threshold(signals, f)
            elif ftype in ("ENGINE_PERFORMANCE","ENGINE_WEIGHT"):
                bt = backtest_engine_weight(signals, f)
            else:
                bt = {"valid":True,"passes":True,"high_impact":False,
                      "summary":"No mechanical backtest (insight/code)."}

            f = dict(f)
            f["backtest_result"] = bt
            if bt.get("valid") and bt.get("passes"):
                validated.append(f)
            else:
                logger.debug(f"  Filtered: {f.get('key',f.get('engine','?'))} — {bt.get('reason','failed')}")
        except Exception as e:
            logger.warning(f"  Backtest error: {e}")

    logger.info(f"  Backtests: {len(validated)}/{len(quant_findings)} passed")
    return validated


# ═══════════════════════════════════════════════════════════════════════
# CHANGE MANAGER
# ═══════════════════════════════════════════════════════════════════════

AUTO_APPLICABLE = {"THRESHOLD_CHANGE","ENGINE_WEIGHT","REGIME_WEIGHT"}
REVIEW_ONLY     = {"CODE_SUGGESTION","INSIGHT","SCRIPT_PATCH"}


def save_proposals(proposals: list, run_id: str) -> list:
    sb  = get_supabase()
    ids = []
    for p in proposals:
        row = {
            "analysis_run_id": run_id,
            "proposal_type":   p.get("type","INSIGHT"),
            "target_key":      p.get("target_key"),
            "current_value":   str(p.get("current_value","")) or None,
            "proposed_value":  str(p.get("proposed_value","")) or None,
            "rationale":       str(p.get("rationale",""))[:1000],
            "evidence":        p.get("evidence") or p.get("backtest_result"),
            "backtest_result": p.get("backtest_result"),
            "confidence":      float(p.get("confidence") or 0),
            "status":          "PENDING",
            "source":          p.get("source","hybrid"),
            "priority":        int(p.get("priority",5)),
            "high_impact":     (p.get("backtest_result") or {}).get("high_impact",False),
            "script_diff":         p.get("script_diff") or None,
            "hardcoded_values": json.dumps(p.get("hardcoded_values") or []),
        }
        try:
            r = sb.table("brain_proposals").insert(row).execute()
            if r.data:
                ids.append(r.data[0]["id"])
        except Exception as e:
            logger.error(f"  Save proposal failed: {e}")
    logger.info(f"  Saved {len(ids)}/{len(proposals)} proposals")
    return ids


def get_pending_proposals(sb=None) -> list:
    sb = sb or get_supabase()
    rows = (sb.table("brain_proposals")
              .select("*")
              .eq("status","PENDING")
              .order("priority")
              .order("confidence", desc=True)
              .execute().data)
    return rows or []


def _log_config_change(sb, key, old_val, new_val, proposal_id, reason, changed_by):
    sb.table("config_change_log").insert({
        "key":         key,
        "old_value":   old_val,
        "new_value":   new_val,
        "changed_by":  changed_by,
        "proposal_id": proposal_id,
        "reason":      str(reason)[:500],
    }).execute()


def evaluate_auto_apply(proposal: dict) -> tuple[bool, str]:
    ptype = proposal.get("proposal_type","")
    if ptype in REVIEW_ONLY:
        return False, "review-only type"

    conf_thresh = cfg_float("brain_auto_apply_confidence", 0.90)
    wr_thresh   = cfg_float("brain_auto_apply_backtest_min", 5.0)
    conf        = float(proposal.get("confidence") or 0)

    if conf < conf_thresh:
        return False, f"confidence {conf:.2f} < {conf_thresh}"

    bt = proposal.get("backtest_result") or {}
    if isinstance(bt, str):
        try: bt = json.loads(bt)
        except: bt = {}

    if bt.get("high_impact"):
        return False, "HIGH_IMPACT — manual approval required"

    wr_delta = bt.get("wr_delta")
    if wr_delta is None:
        return False, "no backtest wr_delta"
    if float(wr_delta) < wr_thresh:
        return False, f"wr_delta {wr_delta:.1f}pp < {wr_thresh:.1f}pp required"

    return True, f"auto-apply: conf={conf:.2f}, wr_delta={wr_delta:.1f}pp"


def _apply_script_patch(proposal_id: int, p: dict, reviewer: str, sb) -> bool:
    """
    Apply a unified diff from a SCRIPT_PATCH proposal to the actual file.
    Called only when brain_script_patching_enabled=true AND manually approved.
    """
    import subprocess, sys
    from pathlib import Path

    script_path = p.get("target_key", "")   # e.g. "backend/ai/post_trade_analysis.py"
    diff_text   = p.get("script_diff", "")

    if not script_path or not diff_text:
        logger.error(f"Proposal {proposal_id}: missing script_path or diff")
        # Mark as FAILED rather than leaving PENDING
        sb.table("brain_proposals").update({
            "status": "FAILED",
            "rationale": f"{p.get('rationale','')} [AUTO: Missing diff — regenerated manually]",
        }).eq("id", proposal_id).execute()
        return False

    # Resolve to absolute path from backend/ root
    backend_root = Path(__file__).parent.parent
    abs_path     = backend_root / Path(script_path).relative_to("backend") \
                   if script_path.startswith("backend/") else backend_root / script_path

    if not abs_path.exists():
        logger.error(f"Proposal {proposal_id}: file not found — {abs_path}")
        return False

    # Backup original content before touching anything
    original = abs_path.read_text(encoding="utf-8")

    # Write diff to a temp file and apply with patch
    import patch as patch_lib, io
    try:
        pset = patch_lib.PatchSet()
        pset.parse(io.BytesIO(diff_text.encode("utf-8")))
        ok = pset.apply(root=str(abs_path.parent))

        if not ok:
            logger.error(f"Patch failed for {script_path} — restoring original")
            abs_path.write_text(original, encoding="utf-8")
            return False

        if not ok:
            logger.error(f"Patch failed for {script_path} — restoring original")
            abs_path.write_text(original, encoding="utf-8")
            return False

        # ── Inject missing cfg imports if needed ──────────────────────────
        patched_content = abs_path.read_text(encoding="utf-8")
        cfg_funcs_used  = {"cfg", "cfg_float", "cfg_int", "cfg_bool"}
        cfg_funcs_needed = {
            fn for fn in cfg_funcs_used
            if f"{fn}(" in patched_content
        }
        if cfg_funcs_needed:
            # Check what's already imported
            already_imported = set()
            for line in patched_content.splitlines():
                if "from config import" in line:
                    for fn in cfg_funcs_needed:
                        if fn in line:
                            already_imported.add(fn)
            missing = cfg_funcs_needed - already_imported
            if missing:
                import_line = f"from config import {', '.join(sorted(missing))}"
                # Find existing config import to extend, or add new line
                lines = patched_content.splitlines()
                new_lines = []
                injected = False
                for line in lines:
                    if "from config import" in line and not injected:
                        # Extend the existing import
                        existing = line.rstrip()
                        for fn in sorted(missing):
                            if fn not in existing:
                                existing = existing.rstrip() + f", {fn}"
                        new_lines.append(existing)
                        injected = True
                    else:
                        new_lines.append(line)
                if not injected:
                    # No existing config import — insert after last import line
                    last_import_idx = 0
                    for i, line in enumerate(new_lines):
                        if line.startswith("import ") or line.startswith("from "):
                            last_import_idx = i
                    new_lines.insert(last_import_idx + 1, import_line)
                abs_path.write_text("\n".join(new_lines), encoding="utf-8")
                logger.info(f"  Injected imports: {import_line}")
                
        # Log to script_change_log
        sb.table("script_change_log").insert({
            "proposal_id":    proposal_id,
            "script_path":    script_path,
            "change_type":    "HARDCODE_TO_CONFIG",
            "diff_text":      diff_text,
            "backup_content": original,
            "applied_by":     reviewer,
        }).execute()
        
        # Seed default values into system_config for all tunable values in this script
        hv = p.get("hardcoded_values") or []
        if isinstance(hv, str):
            try: hv = json.loads(hv)
            except: hv = []
        for h in (hv if isinstance(hv, list) else []):
            if not h.get("tunable") or not h.get("proposed_key"):
                continue
            existing = sb.table("system_config").select("key") \
                         .eq("key", h["proposed_key"]).execute()
            if not existing.data:
                sb.table("system_config").insert({
                    "key":        h["proposed_key"],
                    "value":      str(h["value"]),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
                logger.info(f"  Seeded system_config: {h['proposed_key']} = {h['value']}")

        # Mark proposal APPLIED
        sb.table("brain_proposals").update({
            "status":      "APPLIED",
            "applied_at":  datetime.now(timezone.utc).isoformat(),
            "reviewed_by": reviewer,
        }).eq("id", proposal_id).execute()

        logger.success(f"  Patch applied: {script_path}")
        return True

    except FileNotFoundError:
        logger.error("'patch' command not found. Install it: https://gnuwin32.sourceforge.net/packages/patch.htm")
        return False
    except Exception as e:
        logger.error(f"Patch apply error: {e}")
        return False

def apply_proposal(proposal_id: int, reviewer: str = "brain_engine") -> bool:
    sb       = get_supabase()
    rows     = sb.table("brain_proposals").select("*").eq("id", proposal_id).execute().data
    if not rows:
        logger.error(f"Proposal {proposal_id} not found")
        return False

    p           = rows[0]
    ptype       = p.get("proposal_type","")
    target_key  = p.get("target_key")
    new_value   = p.get("proposed_value")

    if ptype in REVIEW_ONLY:
        if ptype == "SCRIPT_PATCH" and cfg("brain_script_patching_enabled", "false").strip().lower() == "true":
            return _apply_script_patch(proposal_id, p, reviewer, sb)
        logger.warning(f"Proposal {proposal_id} is {ptype} — not auto-applicable to system_config")
        return False

    if ptype == "ENGINE_WEIGHT":
        regime  = p.get("regime") or (target_key.split(":")[0] if target_key else None)
        engine  = p.get("engine") or (target_key.split(":")[1] if target_key else None)
        direction = p.get("direction", "")
        if not regime or not engine:
            logger.error(f"Proposal {proposal_id}: ENGINE_WEIGHT missing regime/engine")
            return False
        cfg_key = "regime_engine_weights"
        cr = sb.table("system_config").select("value").eq("key", cfg_key).execute()
        old_json = cr.data[0]["value"] if cr.data else "{}"
        try:
            weights = json.loads(old_json)
        except:
            weights = {}
        weights.setdefault(regime, {})
        current_w = float(weights[regime].get(engine, 1.0))
        new_w     = round(current_w * 1.1 if direction == "BOOST" else current_w * 0.9, 3)
        weights[regime][engine] = new_w
        new_json = json.dumps(weights)
        sb.table("system_config").upsert({
            "key": cfg_key, "value": new_json,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        _log_config_change(sb, cfg_key, old_json, new_json, proposal_id, p.get("rationale",""), reviewer)
        sb.table("brain_proposals").update({
            "status": "APPLIED", "applied_at": datetime.now(timezone.utc).isoformat(),
            "reviewed_by": reviewer, "rollback_value": old_json,
        }).eq("id", proposal_id).execute()
        logger.success(f"  ENGINE_WEIGHT applied: {regime}/{engine} {current_w}→{new_w}")
        return True

    if not target_key or new_value is None:
        return False

    # Read current for lineage
    cr    = sb.table("system_config").select("value").eq("key", target_key).execute()
    old_v = cr.data[0]["value"] if cr.data else None

    try:
        sb.table("system_config").upsert({
            "key": target_key, "value": str(new_value),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

        _log_config_change(sb, target_key, old_v, str(new_value),
                           proposal_id, p.get("rationale",""), reviewer)

        sb.table("brain_proposals").update({
            "status":         "APPLIED",
            "applied_at":     datetime.now(timezone.utc).isoformat(),
            "reviewed_by":    reviewer,
            "rollback_value": old_v,
            "script_backup_path": f"script_change_log:{proposal_id}",
        }).eq("id", proposal_id).execute()

        logger.success(f"  Applied {proposal_id}: {target_key} = {new_value}")
        return True

    except Exception as e:
        logger.error(f"  Apply failed {proposal_id}: {e}")
        return False


def approve_proposal(proposal_id: int, reviewer: str = "manual") -> bool:
    sb = get_supabase()
    sb.table("brain_proposals").update({
        "status": "APPROVED",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_by": reviewer,
    }).eq("id", proposal_id).eq("status", "PENDING").execute()
    return apply_proposal(proposal_id, reviewer)


def reject_proposal(proposal_id: int, reviewer: str = "manual") -> bool:
    sb = get_supabase()
    sb.table("brain_proposals").update({
        "status": "REJECTED",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_by": reviewer,
    }).eq("id", proposal_id).execute()
    logger.info(f"  Rejected {proposal_id}")
    return True


def rollback_proposal(proposal_id: int, reviewer: str = "manual") -> bool:
    sb   = get_supabase()
    rows = sb.table("brain_proposals").select("*").eq("id", proposal_id).execute().data
    if not rows:
        return False
    p = rows[0]
    if p.get("status") != "APPLIED":
        logger.error(f"Proposal {proposal_id} not APPLIED (status={p.get('status')})")
        return False

    ptype      = p.get("proposal_type", "")
    target_key = p.get("target_key")
    rollback_v = p.get("rollback_value")

    # ── SCRIPT_PATCH: restore file from script_change_log ────────────────
    if ptype == "SCRIPT_PATCH":
        row = sb.table("script_change_log").select("backup_content") \
                .eq("proposal_id", proposal_id).execute().data
        backup = row[0]["backup_content"] if row else rollback_v
        if not backup:
            logger.error(f"Proposal {proposal_id}: no backup content to restore")
            return False
        from pathlib import Path
        backend_root = Path(__file__).parent.parent
        abs_path = backend_root / target_key
        abs_path.write_text(backup, encoding="utf-8")
        sb.table("brain_proposals").update({"status": "ROLLED_BACK"}).eq("id", proposal_id).execute()
        logger.success(f"  Rolled back script patch: {target_key}")
        return True

    # ── ENGINE_WEIGHT: restore JSON blob ─────────────────────────────────
    if ptype == "ENGINE_WEIGHT":
        if not rollback_v:
            logger.error(f"Proposal {proposal_id}: no rollback_value for ENGINE_WEIGHT")
            return False
        sb.table("system_config").upsert({
            "key": "regime_engine_weights", "value": rollback_v,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        _log_config_change(sb, "regime_engine_weights", p.get("proposed_value"),
                           rollback_v, proposal_id, f"ROLLBACK of {proposal_id}", reviewer)
        sb.table("brain_proposals").update({"status": "ROLLED_BACK"}).eq("id", proposal_id).execute()
        logger.success(f"  Rolled back ENGINE_WEIGHT proposal {proposal_id}")
        return True

    # ── THRESHOLD_CHANGE / default: plain config key restore ─────────────
    if not target_key:
        return False
    sb.table("system_config").upsert({
        "key": target_key, "value": str(rollback_v),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()
    _log_config_change(sb, target_key, p.get("proposed_value"), str(rollback_v),
                       proposal_id, f"ROLLBACK of proposal {proposal_id}", reviewer)
    sb.table("brain_proposals").update({"status": "ROLLED_BACK"}).eq("id", proposal_id).execute()
    sb.table("config_change_log").update({
        "rolled_back_at": datetime.now(timezone.utc).isoformat(),
        "rolled_back_by": reviewer,
    }).eq("proposal_id", proposal_id).execute()
    logger.success(f"  Rolled back {proposal_id}: {target_key} → {rollback_v}")
    return True

def process_auto_approvals(config: dict) -> int:
    sb      = get_supabase()
    pending = get_pending_proposals(sb)
    applied = 0
    for p in pending:
        ok, reason = evaluate_auto_apply(p)
        if ok:
            logger.info(f"  Auto-apply proposal {p['id']} ({p.get('target_key')}): {reason}")
            if apply_proposal(p["id"], reviewer="auto"):
                applied += 1
        else:
            logger.debug(f"  Hold proposal {p['id']}: {reason}")
    return applied


def send_telegram_digest(proposals: list, run_id: str, auto_applied: int = 0) -> bool:
    import os, urllib.request
    token   = os.getenv("TELEGRAM_BOT_TOKEN","")
    chat_id = os.getenv("TELEGRAM_CHAT_ID","")
    if not token or not chat_id:
        return False

    pending = [p for p in proposals if p.get("status","PENDING") == "PENDING"]
    if not pending and auto_applied == 0:
        return True

    lines = [f"🧠 *TradeOS Brain Engine*\nRun: `{run_id}`\n"]
    if auto_applied:
        lines.append(f"✅ Auto-applied: *{auto_applied}* proposals\n")
    if pending:
        lines.append(f"⏳ *{len(pending)} proposals awaiting review:*\n")
        for p in pending[:8]:
            bt  = p.get("backtest_result") or {}
            if isinstance(bt, str):
                try: bt = json.loads(bt)
                except: bt = {}
            wr_s = f"+{bt['wr_delta']:.0f}pp" if isinstance(bt.get("wr_delta"),(int,float)) else ""
            conf = float(p.get("confidence",0))
            lines.append(
                f"• `ID {p['id']}` [{p.get('proposal_type','')}] "
                f"*{str(p.get('target_key',''))[:35]}*\n"
                f"  {p.get('current_value','?')} → {p.get('proposed_value','?')}  "
                f"conf={conf:.0%}  {wr_s}\n"
                f"  _{str(p.get('rationale',''))[:90]}_\n"
            )
    lines += [
        "\n*CLI:*",
        "`python -m brain.change_manager approve <id>`",
        "`python -m brain.change_manager rollback <id>`",
        "`python -m brain.change_manager list`",
    ]

    try:
        data = json.dumps({
            "chat_id": chat_id, "text": "\n".join(lines), "parse_mode": "Markdown"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type":"application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        logger.warning(f"  Telegram digest failed: {e}")
        return False


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Brain Change Manager")
    sub    = parser.add_subparsers(dest="cmd")
    sub.add_parser("list")
    p_h = sub.add_parser("history"); p_h.add_argument("--n", type=int, default=20)
    p_a = sub.add_parser("approve");  p_a.add_argument("id", type=int)
    p_r = sub.add_parser("reject");   p_r.add_argument("id", type=int)
    p_rb= sub.add_parser("rollback"); p_rb.add_argument("id", type=int)
    args = parser.parse_args()

    sb = get_supabase()
    if args.cmd == "list":
        rows = get_pending_proposals(sb)
        print(f"\n{'ID':>5}  {'TYPE':<22} {'KEY':<40} {'CONF':>5}  STATUS")
        print("-"*90)
        for r in rows:
            print(f"{r['id']:>5}  {r.get('proposal_type',''):<22} "
                  f"{str(r.get('target_key',''))[:40]:<40} "
                  f"{float(r.get('confidence',0)):>4.0%}  {r.get('status','')}")
    elif args.cmd == "history":
        rows = (sb.table("config_change_log").select("*")
                  .order("changed_at", desc=True).limit(args.n).execute().data)
        print(f"\n{'ID':>5}  {'KEY':<40} {'OLD':>10} → {'NEW':<10}  BY  WHEN")
        print("-"*95)
        for r in rows:
            print(f"{r['id']:>5}  {str(r.get('key',''))[:40]:<40} "
                  f"{str(r.get('old_value',''))[:10]:>10} → {str(r.get('new_value','')):<10}  "
                  f"{str(r.get('changed_by',''))[:12]}  {str(r.get('changed_at',''))[:19]}")
    elif args.cmd == "approve":
        print("Applied ✓" if approve_proposal(args.id) else "FAILED ✗")
    elif args.cmd == "reject":
        print("Rejected" if reject_proposal(args.id) else "FAILED ✗")
    elif args.cmd == "rollback":
        print("Rolled back ✓" if rollback_proposal(args.id) else "FAILED ✗")
    else:
        parser.print_help()
