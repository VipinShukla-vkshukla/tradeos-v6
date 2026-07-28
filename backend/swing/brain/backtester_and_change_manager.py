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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config import get_supabase, cfg

MIN_BACKTEST = 10
MAX_CHANGE   = 0.30   # max 30% change per cycle
HIGH_IMPACT  = 0.50   # >50% signal reduction = HIGH_IMPACT

ENGINE_WEIGHT_MIN  = 0.3   # clamp floor — README has documented this since the start,
ENGINE_WEIGHT_MAX  = 2.0   # the clamp itself was just never implemented until now
ENGINE_WEIGHT_STEP = 0.10  # ±10% per cycle — must match backtest_engine_weight's
                           # simulated delta exactly, or the backtested wr_delta
                           # doesn't correspond to the change actually deployed


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


def backtest_score_component(signals: pd.DataFrame, finding: dict) -> dict:
    """
    Validates a SCORE_WEIGHT_CHANGE finding. Unlike backtest_threshold, there's
    no independent "what if we'd filtered at the proposed value" simulation
    possible here — a score magnitude change doesn't filter signals, it
    reshuffles downstream ranking, and simulating that would mean re-running
    the screener's selection logic counterfactually (out of scope). This re-
    applies the same safety caps (max change per cycle, minimum sample floor)
    against the win-rate-gap evidence analyze_score_component_sensitivity
    already computed, rather than re-deriving a different number from a
    different angle. Said plainly in the summary, not just in this docstring.
    """
    current  = finding.get("current")
    proposed = finding.get("proposed")
    wr_with, wr_without = finding.get("wr_with"), finding.get("wr_without")
    n        = finding.get("sample_size", 0)

    if current is None or proposed is None or wr_with is None or wr_without is None:
        return {"valid": False, "reason": "missing finding fields"}

    if current != 0 and abs(proposed - current) / abs(current) > MAX_CHANGE:
        return {"valid": False, "reason": f"change exceeds {MAX_CHANGE:.0%} safety cap"}

    if n < MIN_BACKTEST * 2:  # both cohorts need to individually clear MIN_BACKTEST
        return {"valid": False, "reason": f"insufficient samples: n={n}"}

    wr_delta    = round(wr_with - wr_without, 1)
    high_impact = bool(finding.get("high_impact", False))

    return {
        "valid":       True,
        "passes":      abs(wr_delta) >= 3.0,  # gap has to be more than noise to act on
        "high_impact": high_impact,
        "wr_delta":    wr_delta,
        "with":        {"count": int(n * finding.get("condition_share_pct", 50) / 100), "win_rate": wr_with},
        "without":     {"win_rate": wr_without},
        "summary": (
            f"Condition-true: {wr_with:.0f}% WR. Condition-false: {wr_without:.0f}% WR. "
            f"Δ={wr_delta:+.1f}pp on n={n}. Validated against the same evidence used to "
            f"generate this finding — no independent re-simulation of downstream ranking effects."
            + (" ⚠️ HIGH_IMPACT — condition covers <5% or >95% of the signal pool." if high_impact else "")
        ),
    }



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

    delta = 1 + ENGINE_WEIGHT_STEP if direction == "OUTPERFORMING" else 1 - ENGINE_WEIGHT_STEP
    ev["_adj"] = ev.apply(
        lambda r: r["score_adjusted"] * delta if r["_has"] else r["score_adjusted"], axis=1
    )
    adj_topn = ev.nlargest(top_n, "_adj")
    adj_wr   = float(adj_topn["outcome_win"].mean())*100 \
               if adj_topn["outcome_win"].notna().any() else None

    wr_delta = (adj_wr - base_wr) if (adj_wr and base_wr) else None

    base_set   = set(base_topn.index)
    adj_set    = set(adj_topn.index)
    overlap_pct = (len(base_set & adj_set) / len(base_set) * 100) if base_set else 100
    high_impact = overlap_pct < 50  # more than half the top-N changed

    return {
        "valid":     True,
        "passes":    wr_delta is not None and wr_delta >= 0,
        "high_impact": high_impact,
        "before":    {"count": top_n, "win_rate": round(base_wr,1) if base_wr else None},
        "after":     {"count": top_n, "win_rate": round(adj_wr,1) if adj_wr else None},
        "wr_delta":  round(wr_delta, 1) if wr_delta else None,
        "summary":   (f"Top-{top_n}: {base_wr:.0f}% → {adj_wr:.0f}% WR ({wr_delta:+.1f}pp). "
                      f"Candidate pool overlap: {overlap_pct:.0f}%."
                      + (" ⚠️ HIGH_IMPACT" if high_impact else ""))
                     if wr_delta else "Insufficient data.",
    }


def run_backtests(signals: pd.DataFrame, quant_findings: list) -> list:
    validated = []
    for f in quant_findings:
        ftype = f.get("type","")
        try:
            if ftype == "THRESHOLD_CHANGE":
                bt = backtest_threshold(signals, f)
            elif ftype == "SCORE_WEIGHT_CHANGE":
                bt = backtest_score_component(signals, f)
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

AUTO_APPLICABLE = {"THRESHOLD_CHANGE","ENGINE_WEIGHT","REGIME_WEIGHT","SCORE_WEIGHT_CHANGE"}
REVIEW_ONLY     = {"CODE_SUGGESTION","INSIGHT","SCRIPT_PATCH",
                    "ENGINE_PERFORMANCE","CONSISTENCY_CONFLICT"}


def save_proposals(proposals: list, run_id: str) -> list:
    sb  = get_supabase()
    ids = []

    existing_pending = {
        (r["target_key"], r["proposal_type"])
        for r in (sb.table("brain_proposals")
                    .select("target_key,proposal_type")
                    .eq("status", "PENDING")
                    .execute().data or [])
    }

    skipped = 0
    for p in proposals:
        ptype = p.get("type", "INSIGHT")
        tkey  = p.get("target_key")
        if (tkey, ptype) in existing_pending:
            skipped += 1
            continue

        row = {
            "analysis_run_id": run_id,
            "proposal_type":   ptype,
            "target_key":      tkey,
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
                existing_pending.add((tkey, ptype))  # guard within this same batch too
        except Exception as e:
            logger.error(f"  Save proposal failed: {e}")
    if skipped:
        logger.info(f"  Skipped {skipped} duplicate(s) of already-PENDING proposals")
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


_DEFAULT_AUTO_APPLY_POLICY = {
    "THRESHOLD_CHANGE":     {"mode": "auto",   "min_confidence": 0.75, "min_wr_delta_pp": 5.0},
    "ENGINE_WEIGHT":        {"mode": "review"},
    "SCORE_WEIGHT_CHANGE":  {"mode": "review"},
    "SCRIPT_PATCH":         {"mode": "review"},
    "CODE_SUGGESTION":      {"mode": "review"},
    "CONSISTENCY_CONFLICT": {"mode": "review"},
    "ENGINE_PERFORMANCE":   {"mode": "review"},
    "INSIGHT":              {"mode": "review"},
}


def _get_auto_apply_policy() -> dict:
    """
    Per-type auto-apply policy, editable at system_config.brain_auto_apply_policy
    without a code change. Falls back to the conservative default above if the
    key is missing or malformed — never crashes the run over a bad JSON edit.
    """
    raw = cfg("brain_auto_apply_policy", "")
    if not raw:
        return _DEFAULT_AUTO_APPLY_POLICY
    try:
        policy = json.loads(raw)
        if not isinstance(policy, dict):
            return _DEFAULT_AUTO_APPLY_POLICY
        return policy
    except Exception as e:
        logger.warning(f"  brain_auto_apply_policy malformed ({e}) — using defaults")
        return _DEFAULT_AUTO_APPLY_POLICY


def evaluate_auto_apply(proposal: dict) -> tuple[bool, str]:
    ptype = proposal.get("proposal_type","")
    if ptype in REVIEW_ONLY:
        return False, "review-only type"

    policy = _get_auto_apply_policy().get(ptype, {"mode": "review"})
    if policy.get("mode") != "auto":
        return False, f"{ptype} set to review in brain_auto_apply_policy"

    conf_thresh = float(policy.get("min_confidence", 0.90))
    wr_thresh   = float(policy.get("min_wr_delta_pp", 5.0))
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
        logger.info(f"Proposal {proposal_id} is {ptype} — acknowledged, no automated "
                    f"action for this type. Diff/suggestion is in the proposal record "
                    f"for you to apply manually.")
        return True

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
        step      = 1 + ENGINE_WEIGHT_STEP if direction == "BOOST" else 1 - ENGINE_WEIGHT_STEP
        new_w     = round(current_w * step, 3)
        new_w     = max(ENGINE_WEIGHT_MIN, min(ENGINE_WEIGHT_MAX, new_w))
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

    def esc(s):
        """Escape HTML special chars for Telegram HTML parse mode."""
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    pending = [p for p in proposals if p.get("status", "PENDING") == "PENDING"]
    if not pending and auto_applied == 0:
        return True

    lines = [f"🧠 <b>TradeOS Brain Engine</b>\nRun: <code>{esc(run_id)}</code>\n"]
    if auto_applied:
        lines.append(f"✅ Auto-applied: <b>{auto_applied}</b> proposals\n")
    if pending:
        lines.append(f"⏳ <b>{len(pending)} proposals awaiting review:</b>\n")
        budget = 3400  # leave generous margin under Telegram's 4096 limit for
                       # the header/footer already in `lines` plus the footer below
        used   = sum(len(l) for l in lines)
        shown  = 0
        for p in pending[:8]:
            bt = p.get("backtest_result") or {}
            if isinstance(bt, str):
                try: bt = json.loads(bt)
                except: bt = {}
            wr_s = f"+{bt['wr_delta']:.0f}pp" if isinstance(bt.get("wr_delta"), (int, float)) else ""
            conf = float(p.get("confidence", 0))
            block = (
                f"• <code>ID {p['id']}</code> [{esc(p.get('proposal_type', ''))}] "
                f"<b>{esc(str(p.get('target_key', ''))[:35])}</b>\n"
                f"  {esc(str(p.get('current_value', '?'))[:60])} → {esc(str(p.get('proposed_value', '?'))[:60])}  "
                f"conf={conf:.0%}  {esc(wr_s)}\n"
                f"  <i>{esc(str(p.get('rationale', ''))[:90])}</i>\n"
            )
            if used + len(block) > budget:
                break  # drop whole blocks from here, never split one mid-tag
            lines.append(block)
            used  += len(block)
            shown += 1
        if shown < len(pending[:8]):
            lines.append(f"… +{len(pending) - shown} more, see CLI `list`\n")
    lines += [
        "\n<b>CLI:</b>",
        "<code>python -m swing.brain.backtester_and_change_manager approve &lt;id&gt;</code>",
        "<code>python -m swing.brain.backtester_and_change_manager rollback &lt;id&gt;</code>",
        "<code>python -m swing.brain.backtester_and_change_manager list</code>",
    ]

    try:
        data = json.dumps({
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
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
