"""
analysis/entry_readiness.py

PURPOSE (narrowed, 2026-06 redesign):
  Answer exactly one question that step 19 (ai_decision_engine) CANNOT answer
  at pipeline run time: "where is the live price RIGHT NOW relative to the
  entry zone, and is there real volume showing up today?"

  This module does NOT re-score, re-rank, or re-weight conviction/tier.
  Those were already decided by ai_decision_engine.py using dist_entry_pct,
  vol_ratio, delivery_pct, and regime as direct prompt inputs — re-blending
  the same numbers here would just double-count what the AI already
  reasoned through with full market context. See backend audit notes
  (2026-06-23) for the evidence trail.

  What this module DOES provide, which is genuinely new at morning/afternoon
  checkpoints and did not exist at evening pipeline-run time:
    - live price vs entry_zone_low/entry_zone_high  → zone_status
    - live volume-so-far, elapsed-time-adjusted     → live_vol_note (display only)
    - a plain-English timing_note for what to do right now
    - deterministic GTT order levels (entry/SL/T1/RR) — pure arithmetic,
      not a judgment call, so no scoring concern applies here

AI-EXTENDED ZONE (2026-07 addition):
  compute_msl's entry_zone_low/high is a MECHANICAL, context-free zone —
  it has no idea whether today's conviction, momentum, or FII/sector tailwind
  justify a stock trading a few % above it. Before this addition, ANY live
  price above zone_high*1.015 was flatly "MISSED", even for TIER_1/HIGH-
  conviction picks the AI would have gladly chased with fuller context —
  which is exactly the afternoon false-MISSED problem this fixes.
  ai_max_chase_pct / ai_zone_high_extended are NOT a new score computed here.
  They are ai_decision_engine's OWN already-decided judgment (see its CHASE
  GUIDANCE prompt section), written once at step 19 and simply read here —
  the same "already decided, just display it" pattern this module already
  uses for conviction/tier/thesis. Using that stored judgment to widen the
  live zone-status ceiling is applying the AI's decision, not re-deriving one.

Called from send_alerts.py:
  Morning   → use_live=True    (yfinance live price, pre-market / early session)
  Afternoon → use_live=True    (yfinance live price, mid-session)
  Evening   → use_live=False   (called for the deterministic GTT entry/SL/T1
              levels in the "TOMORROW'S GTT ORDERS" section — no live price
              exists at 6 PM pipeline-run time, so zone_status/timing_note/
              live_vol_note all stay empty here; that part of this module is
              a no-op for evening, only the arithmetic runs)

Adds to each candidate dict:
  zone_status     str   "✅ IN ZONE (₹X)" / "⬇️ APPROACHING" / "⚠️ ABOVE ZONE" /
                        "🟡 AI-EXTENDED (₹X)" / "❌ MISSED"
  timing_note     str   plain-English action guidance for this checkpoint
  live_vol_note   str|None   "1.6× pace vs 20d avg (live)" — informational only, never scored
  gtt_entry       float      zone_low (exact GTT limit price — the original disciplined plan)
  gtt_sl          float      zone_low × (1 − stop_buffer)
  gtt_t1          float      zone_low × (1 + stop_buffer × expected_r)
  rr_ratio        float      (T1 − entry) / (entry − SL)
  ai_max_chase_pct   float|None   AI-approved % above zone_high, from master_shortlist (0-8)
  gtt_entry_chase    float|None  live price — only set while inside the AI-EXTENDED band
  gtt_sl_chase       float|None  gtt_entry_chase × (1 − stop_buffer)
  gtt_t1_chase       float|None  gtt_entry_chase × (1 + stop_buffer × expected_r)
  rr_ratio_chase     float|None  same R:R math, re-anchored to the achievable chase entry
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── yfinance (optional dep) ──────────────────────────────────────────────────
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    logger.warning("entry_readiness: yfinance not installed — live data disabled")

# ── stop buffer (read from system_config if available) ───────────────────────
try:
    from config import cfg
    STOP_BUFFER: float = float(cfg("stop_buffer_pct", "0.03"))
except Exception:
    STOP_BUFFER: float = 0.03

NSE_SESSION_START_MINS: int = 9 * 60 + 15   # 9:15 AM IST
NSE_SESSION_TOTAL_MINS: int = 375            # 9:15 → 15:30


# ── yfinance live fetch ──────────────────────────────────────────────────────

def fetch_live_data(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch live NSE price + today's volume via yfinance (15-min delayed).
    Returns: symbol → {price, volume_today, high, low, open, elapsed_pct}
    Falls back to empty dict per symbol on any error.
    """
    if not _YF_AVAILABLE or not symbols:
        return {}

    now       = datetime.now()
    cur_m     = now.hour * 60 + now.minute
    elapsed   = min(1.0, max(0.01,
                   (cur_m - NSE_SESSION_START_MINS) / NSE_SESSION_TOTAL_MINS))
    result: dict[str, dict] = {}

    try:
        ns_syms  = [f"{s}.NS" for s in symbols]
        tickers  = yf.Tickers(" ".join(ns_syms))
        for sym in symbols:
            try:
                t  = tickers.tickers.get(f"{sym}.NS")
                fi = t.fast_info if t else None
                if fi is None:
                    result[sym] = {"elapsed_pct": elapsed}
                    continue

                vol = 0
                try:
                    hist = t.history(period="1d", interval="1d")
                    vol  = int(hist["Volume"].iloc[-1]) if not hist.empty else 0
                except Exception:
                    pass

                result[sym] = {
                    "price":        float(fi.last_price or 0),
                    "volume_today": vol,
                    "high":         float(fi.day_high or 0),
                    "low":          float(fi.day_low  or 0),
                    "open":         float(fi.open     or 0),
                    "elapsed_pct":  elapsed,
                }
            except Exception as e:
                logger.debug(f"yfinance {sym}: {e}")
                result[sym] = {"elapsed_pct": elapsed}
    except Exception as e:
        logger.warning(f"entry_readiness yfinance batch failed: {e}")

    return result


# ── Stock data daily fetch (avg_vol_20d only — needed to normalise live vol) ─

def _load_avg_vol_map(sb, symbols: list[str]) -> dict[str, int]:
    """Load latest avg_vol_20d per symbol — the only field this module still
    needs from stock_data_daily, purely to elapsed-adjust today's live volume.
    vol_ratio/delivery_pct are intentionally NOT fetched here — they're
    already inputs to ai_decision_engine's conviction; re-displaying yesterday's
    EOD values as if they were live context at 1 PM would be misleading."""
    out: dict[str, int] = {}
    if not sb or not symbols:
        return out
    try:
        rows = (
            sb.table("stock_data_daily")
              .select("symbol,avg_vol_20d")
              .in_("symbol", symbols)
              .order("date", desc=True)
              .limit(len(symbols) * 3)
              .execute().data
        )
        seen: set[str] = set()
        for r in rows:
            s = r.get("symbol", "")
            if s and s not in seen:
                out[s] = int(r.get("avg_vol_20d") or 0)
                seen.add(s)
    except Exception as e:
        logger.warning(f"entry_readiness avg_vol fetch failed: {e}")
    return out


# ── Main public function ─────────────────────────────────────────────────────

def compute_entry_readiness(
    candidates: list[dict],
    msl_map: dict,
    sb=None,
    use_live: bool = True,
) -> list[dict]:
    """
    Enriches each candidate dict in-place with live zone-status + GTT fields.
    Does NOT touch conviction, tier, confidence, thesis, entry_note, risks,
    lessons_applied, or any other field ai_decision_engine already decided —
    those are frozen and displayed as-is by send_alerts.py.

    Args:
        candidates : TIER_1 ranked_candidate dicts
        msl_map    : symbol → master_shortlist row (already loaded by send_alerts.py)
        sb         : Supabase client (used only for avg_vol_20d lookup)
        use_live   : if False, this function is a no-op pass-through
                     (evening should not call this at all — see module docstring)

    Returns the same list, each dict enriched with zone_status/timing_note/
    live_vol_note/gtt_* keys.
    """
    if not use_live:
        return candidates

    syms        = [c.get("symbol", "") for c in candidates]
    avg_vol_map = _load_avg_vol_map(sb, syms)
    live_data   = fetch_live_data(syms)
    _now        = datetime.now()
    _now_min    = _now.hour * 60 + _now.minute
    _pre_market = _now_min < NSE_SESSION_START_MINS  # True before 9:15 AM IST

    for c in candidates:
        sym  = c.get("symbol", "")
        msl  = msl_map.get(sym, {})
        live = live_data.get(sym, {})

        zl = float(msl.get("entry_zone_low")  or 0)
        zh = float(msl.get("entry_zone_high") or (zl * 1.02 if zl else 0))

        # ── AI-approved chase ceiling — already decided by ai_decision_engine
        # (step 19) and written to master_shortlist; read-only here, never
        # recomputed. 0/None → AI did not approve chasing this symbol above
        # zh today (SKIP/WAIT_FOR_TRIGGER action, or compute_msl's own
        # entry_timing_type was already EXTENDED/WAIT — see CHASE GUIDANCE
        # in ai_decision_engine.build_prompt for the exact decision rule).
        ai_max_chase_pct = float(msl.get("ai_max_chase_pct") or 0)
        ai_chase_rationale = msl.get("ai_chase_rationale") or ""
        ai_zone_high_ext = msl.get("ai_zone_high_extended")
        if ai_zone_high_ext is not None:
            ai_zone_high_ext = float(ai_zone_high_ext)
        elif zh and ai_max_chase_pct > 0:
            # Fallback if ai_zone_high_extended wasn't populated for some reason
            # (e.g. older row written before this field existed) but the
            # percentage was — recompute the same deterministic arithmetic
            # ai_decision_engine already does, so behaviour still matches.
            ai_zone_high_ext = round(zh * (1 + ai_max_chase_pct / 100), 2)
        else:
            ai_zone_high_ext = zh

        # ── Live volume note — informational only, never scored ─────────────
        live_vol_note: Optional[str] = None
        live_vol  = live.get("volume_today")
        elapsed   = live.get("elapsed_pct", 1.0)
        avg_vol   = avg_vol_map.get(sym, 0)
        if live_vol and avg_vol and elapsed > 0.05:
            expected_by_now = avg_vol * elapsed
            if expected_by_now > 0:
                live_ratio = live_vol / expected_by_now
                live_vol_note = f"{live_ratio:.1f}× pace vs 20d avg (live)"

        # ── Zone status (live price vs zone bounds) — the one genuinely
        #    new fact at this checkpoint ────────────────────────────────────
        zone_status = ""
        timing_note = ""
        live_price  = live.get("price")
        in_chase_band = False

        if live_price and zl:
            if live_price < zl * 0.995:
                pct = (zl - live_price) / zl * 100
                zone_status = f"⬇️ APPROACHING (₹{live_price:,.0f}, {pct:.1f}% below zone)"
                timing_note = (
                    "Wait · price below zone · watch for gap-up at open"
                    if _pre_market else "Wait · price below zone"
                )
            elif live_price <= zh:
                zone_status = f"✅ IN ZONE (₹{live_price:,.0f})"
                timing_note = (
                    "✅ GTT ready · place limit at zone_low before 9:15 AM"
                    if _pre_market else "1:30–2:30 PM · limit at zone_low"
                )
            elif live_price <= zh * 1.015:
                pct = (live_price - zh) / zh * 100
                zone_status = f"⚠️ ABOVE ZONE (₹{live_price:,.0f}, +{pct:.1f}%)"
                timing_note = (
                    "Closed above zone · confirm at open — pullback to zone needed"
                    if _pre_market else "Watch for afternoon pullback to zone"
                )
            elif ai_zone_high_ext > zh and live_price <= ai_zone_high_ext:
                pct = (live_price - zh) / zh * 100
                zone_status = (
                    f"🟡 AI-EXTENDED (₹{live_price:,.0f}, +{pct:.1f}% — "
                    f"chase OK to +{ai_max_chase_pct:.1f}%)"
                )
                timing_note = (
                    f"AI conviction supports chasing · {ai_chase_rationale[:80]}"
                    if ai_chase_rationale else
                    "AI conviction supports chasing above mechanical zone · reduce size"
                )
                in_chase_band = True
            else:
                pct = (live_price - zh) / zh * 100
                zone_status = f"❌ MISSED (₹{live_price:,.0f}, +{pct:.1f}% above zone)"
                timing_note = "Skip today · re-evaluate tomorrow"

        # ── GTT values — deterministic arithmetic, no judgment, no scoring ──
        er        = float(msl.get("expected_r") or 2.0)
        gtt_entry = zl if zl else None
        gtt_sl    = round(zl * (1.0 - STOP_BUFFER), 2) if zl else None
        gtt_t1    = round(zl * (1.0 + STOP_BUFFER * er), 2) if zl else None
        rr_ratio  = (
            round((gtt_t1 - gtt_entry) / (gtt_entry - gtt_sl), 1)
            if gtt_t1 and gtt_sl and gtt_entry and gtt_sl < gtt_entry
            else None
        )

        # ── Chase-anchored GTT variant — only while inside the AI-EXTENDED
        # band. zone_low is already blown through by this point, so re-anchor
        # entry/SL/T1 to the actual achievable live price instead. Same
        # STOP_BUFFER/expected_r discipline as the original plan above —
        # just re-anchored, not re-derived. NOTE: STOP_BUFFER was presumably
        # calibrated for zone_low entries; whether a chase entry should use a
        # tighter buffer is a judgment call this module deliberately leaves
        # alone (see docstring) — worth a look if chase R:R looks off.
        gtt_entry_chase = gtt_sl_chase = gtt_t1_chase = rr_ratio_chase = None
        if in_chase_band and live_price:
            gtt_entry_chase = round(live_price, 2)
            gtt_sl_chase    = round(gtt_entry_chase * (1.0 - STOP_BUFFER), 2)
            gtt_t1_chase    = round(gtt_entry_chase * (1.0 + STOP_BUFFER * er), 2)
            rr_ratio_chase  = (
                round((gtt_t1_chase - gtt_entry_chase) / (gtt_entry_chase - gtt_sl_chase), 1)
                if gtt_sl_chase < gtt_entry_chase else None
            )

        c.update({
            "zone_status":      zone_status,
            "timing_note":      timing_note,
            "live_vol_note":    live_vol_note,
            "gtt_entry":        gtt_entry,
            "gtt_sl":           gtt_sl,
            "gtt_t1":           gtt_t1,
            "rr_ratio":         rr_ratio,
            "ai_max_chase_pct": ai_max_chase_pct if ai_max_chase_pct else None,
            "gtt_entry_chase":  gtt_entry_chase,
            "gtt_sl_chase":     gtt_sl_chase,
            "gtt_t1_chase":     gtt_t1_chase,
            "rr_ratio_chase":   rr_ratio_chase,
        })

    return candidates


# ════════════════════════════════════════════════════════════════════════════
# STANDALONE SELF-TEST
# ════════════════════════════════════════════════════════════════════════════
# This module has no main pipeline role of its own — it's a library imported
# by send_alerts.py. Running `python entry_readiness.py` with no entry point
# does nothing by design; that's not a connection failure.
#
# This block gives you a real, isolated check instead. It does NOT touch
# Supabase — zone bounds below are hard-coded mock values, not from
# master_shortlist. What IS real: the yfinance live-price fetch for RELIANCE
# (a highly liquid, always-available NSE ticker) and the GTT arithmetic.
# If you see a live price below, your yfinance + network path genuinely works.
#
# Correct usage (so config.py + STOP_BUFFER resolve correctly):
#   cd backend
#   python -m analysis.entry_readiness
#
# Running it directly from inside analysis/ also works for this self-test
# specifically (it doesn't require config.py), but won't reflect your real
# stop_buffer_pct from system_config — it'll show the 0.03 default instead.
#
# To verify the FULL integration (this module + send_alerts.py + Supabase +
# whichever alert channel is active), see the printed instructions at the
# end of this self-test — that is the actual end-to-end proof, not this file.
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 64)
    print("entry_readiness.py — standalone self-test")
    print("=" * 64)
    print(f"yfinance available : {_YF_AVAILABLE}")
    print(f"STOP_BUFFER         : {STOP_BUFFER}  "
          f"({'from config.py' if STOP_BUFFER != 0.03 else 'default — config.py not reachable from this working directory, see note above'})")
    print()

    # Mock candidates + mock zone bounds (NOT from Supabase — this test never
    # touches your database). Three different, reliably-liquid NSE blue chips
    # are used (not three mock rows for one symbol) because msl_map is keyed
    # by symbol — yfinance needs a real, distinct ticker per row to fetch a
    # live price against.
    #   RELIANCE : realistic zone around its actual price — exercises the
    #              original IN ZONE / APPROACHING / ABOVE ZONE / MISSED logic,
    #              unchanged from before this addition (no AI chase fields).
    #   TCS      : zone deliberately far below its real price with NO AI chase
    #              approval → should land on MISSED, proving the AI-EXTENDED
    #              branch does NOT fire without an explicit AI-approved ceiling.
    #   INFY     : same deliberately-low zone but WITH a generous AI-approved
    #              ceiling → should land on AI-EXTENDED, proving the new
    #              branch fires correctly when ai_decision_engine has approved it.
    _mock_candidates = [
        {
            "symbol": "RELIANCE", "conviction": "HIGH", "confidence": 0.87,
            "action": "BUY", "suggested_allocation_pct": 7,
            "correlation_group": "ENERGY",
            "entry_note": "(mock — real entry_note comes from ai_decision_engine)",
            "invalidation": "(mock — real invalidation comes from ai_decision_engine)",
        },
        {
            "symbol": "TCS", "conviction": "MEDIUM", "confidence": 0.6,
            "action": "SKIP", "suggested_allocation_pct": 0,
            "correlation_group": "IT",
            "entry_note": "(mock)", "invalidation": "(mock)",
        },
        {
            "symbol": "INFY", "conviction": "HIGH", "confidence": 0.83,
            "action": "ENTER_NOW", "suggested_allocation_pct": 6,
            "correlation_group": "IT",
            "entry_note": "(mock)", "invalidation": "(mock)",
        },
    ]
    _mock_msl_map = {
        "RELIANCE": {"entry_zone_low": 2900, "entry_zone_high": 2950,
                     "expected_r": 2.0, "dist_entry_pct": -1.0},
        "TCS": {"entry_zone_low": 100, "entry_zone_high": 110,
                "expected_r": 2.0,
                "ai_max_chase_pct": 0, "ai_chase_rationale": "SKIP — no chase approved"},
        "INFY": {"entry_zone_low": 100, "entry_zone_high": 110,
                 "expected_r": 2.0, "ai_max_chase_pct": 8.0,
                 "ai_zone_high_extended": 99999,
                 "ai_chase_rationale": "mock: HIGH conviction, ACCELERATING, struct_edge YES"},
    }

    print("Calling compute_entry_readiness() with 3 mock candidates, use_live=True...")
    print("(fetching live prices via yfinance — needs internet)")
    print()

    out = compute_entry_readiness(_mock_candidates, _mock_msl_map, sb=None, use_live=True)

    for c in out:
        print(f"── {c.get('symbol')} " + "─" * (58 - len(c.get('symbol', ''))))
        print(f"  zone_status        : {c.get('zone_status') or '(empty)'}")
        print(f"  timing_note        : {c.get('timing_note') or '(empty)'}")
        print(f"  ai_max_chase_pct   : {c.get('ai_max_chase_pct')}")
        print(f"  GTT entry/SL/T1/RR : {c.get('gtt_entry')} / {c.get('gtt_sl')} / "
              f"{c.get('gtt_t1')} / {c.get('rr_ratio')}")
        if c.get("gtt_entry_chase"):
            print(f"  CHASE entry/SL/T1/RR : {c.get('gtt_entry_chase')} / {c.get('gtt_sl_chase')} / "
                  f"{c.get('gtt_t1_chase')} / {c.get('rr_ratio_chase')}")
        print()

    c = out[0]  # RELIANCE — used for the original connectivity checks below

    gtt_ok = c.get("gtt_entry") == 2900 and c.get("gtt_sl") and c.get("gtt_t1")
    live_ok = bool(c.get("zone_status"))

    print("─" * 64)
    print(f"  GTT math          : {'✅ correct' if gtt_ok else '❌ check STOP_BUFFER / msl_map'}")
    print(f"  Live price fetch  : {'✅ got a live price' if live_ok else '⚠️  empty — check internet, or run during/after NSE hours'}")
    print("─" * 64)
    print()
    print("This only proves the MODULE works in isolation.")
    print("To verify it's actually wired into your real pipeline, run the")
    print("real entry point instead:")
    print()
    print("    cd backend")
    print("    python alerts/send_alerts.py --morning")
    print()
    print("Look for this exact log line in the output:")
    print("    Data loaded: step19=... | readiness=✅ ... | channel=...")
    print()
    print("  readiness=✅  → entry_readiness imported successfully into send_alerts.py")
    print("  readiness=❌  → import failed; check for an ImportError above this line")
    print("  channel=...   → confirms system_config / multi-channel router loaded")
    print()
    print("If you only see ONE log line and the script exits immediately,")
    print("check system_config / env for telegram_alerts_enabled=true — main()")
    print("returns early before reaching readiness/data loading if that's false.")