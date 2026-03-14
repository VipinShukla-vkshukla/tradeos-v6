"""
TradeOS v6 — Patched functions for generate_signals.py

PATCH APPLIED (Strategic Fix #4 + #8).

This file contains REPLACEMENT functions only — not the full script.
Apply the two function replacements below into the existing
backend/signals/generate_signals.py file.

────────────────────────────────────────────────────────────────
CHANGE 1 — get_eap_action() (Strategic Fix #8)
  Problem: All event types (results, AGM, dividend, board meeting)
  were treated identically. Event_type and purpose fields are
  ignored — even a dividend record triggered AVOID_ENTRY.
  Fix: Weight by event_type. RESULTS/EARNINGS = high impact,
  BOARD/AGM = medium, DIVIDEND = low (only triggers PRIORITISE,
  never AVOID_ENTRY).

CHANGE 2 — generate() regime block (Strategic Fix #4)
  Problem: ml_regime_classifier outputs 4 labels:
    TRENDING / NEUTRAL / CAUTION / RISK OFF
  But generate_signals only handled RISK OFF. CAUTION was silently
  ignored — no score penalty, no regime_warning flag.
  Fix 1: CAUTION now reduces score by 15% on BUY_CANDIDATE signals
  and sets regime_warning=True.
  Fix 2: When Phase 2 is active (ml_regime_classifier running),
  prefer predicted_regime over manual regime field if available
  and fresher than 24h.

────────────────────────────────────────────────────────────────
HOW TO APPLY:
  1. Open backend/signals/generate_signals.py
  2. Replace the existing get_eap_action() function (lines ~110–132)
     with REPLACEMENT 1 below.
  3. In the generate() function, find the regime handling block
     (lines ~160–165 approx):
        regime_name = regime.get("regime", "NEUTRAL")
        block_buys  = cfg_bool(...) and regime_name == "RISK OFF"
     and replace with REPLACEMENT 2 below.
  4. In the generate() loop, find:
        if position_state == "BUY_CANDIDATE" and regime_name == "RISK OFF":
            regime_warning = True
            if block_buys:
                signal_type = "BUY_BLOCKED_REGIME"
     and replace with REPLACEMENT 3 below.
"""

# ════════════════════════════════════════════════════════════════
# REPLACEMENT 1 — get_eap_action() with event-type weighting
# Replace the existing get_eap_action() function with this.
# ════════════════════════════════════════════════════════════════

def get_eap_action(symbol: str, sector: str, events: list, cfg_eap: dict) -> str:
    """
    EAP overlay — checks event calendar for timing signals.

    PATCHED: Event-type weighting (Strategic Fix #8).
    Event weights:
      HIGH   — RESULTS, EARNINGS, QUARTERLY_RESULTS, BOARD_MEETING
               triggers AVOID_ENTRY (pre) and PRIORITISE (post)
      MEDIUM — AGM, ANALYST_MEET, CONCALL
               triggers AVOID_ENTRY only (no post-event PRIORITISE)
      LOW    — DIVIDEND, BONUS, SPLIT, BUYBACK
               triggers PRIORITISE only (never AVOID_ENTRY — not risky)

    Also added: per-symbol event check (nifty_upcoming_events has
    symbol field). Sector-level events still checked as before.
    """
    from datetime import date as _date

    pre_days  = int(cfg_eap.get("pre_event_days", 2))

    HIGH_IMPACT   = {"RESULTS", "EARNINGS", "QUARTERLY_RESULTS", "BOARD_MEETING",
                     "BOARD MEETING", "FINANCIAL RESULTS"}
    MEDIUM_IMPACT = {"AGM", "ANALYST_MEET", "CONCALL", "INVESTOR_MEET"}
    LOW_IMPACT    = {"DIVIDEND", "BONUS", "SPLIT", "BUYBACK", "RIGHTS"}

    def _classify(ev: dict) -> str:
        """Classify event impact level from event_type or purpose fields."""
        raw = (str(ev.get("event_type") or "") + " " +
               str(ev.get("purpose") or "")).upper()
        if any(k in raw for k in HIGH_IMPACT):
            return "HIGH"
        if any(k in raw for k in MEDIUM_IMPACT):
            return "MEDIUM"
        if any(k in raw for k in LOW_IMPACT):
            return "LOW"
        return "MEDIUM"   # default unknown to medium

    best_action = "NO_CHANGE"

    for ev in events:
        if not ev.get("is_active"):
            continue

        # Check if this event applies to this symbol or sector
        ev_symbol   = ev.get("symbol", "")
        affected    = str(ev.get("affected_sectors", "")).lower()
        is_relevant = (
            (ev_symbol and ev_symbol == symbol) or
            (sector and sector.lower() in affected) or
            ev.get("event_category") == "GLOBAL"
        )
        if not is_relevant:
            continue

        start_d = ev.get("start_date") or ev.get("event_date")
        if not start_d:
            continue

        try:
            sd   = _date.fromisoformat(str(start_d)[:10])
            days = (sd - today_ist()).days   # positive = upcoming, negative = past
        except Exception:
            continue

        impact = _classify(ev)

        # ── HIGH impact events: AVOID_ENTRY pre, PRIORITISE post ────────────
        if impact == "HIGH":
            if 0 <= days <= pre_days:
                return "AVOID_ENTRY"          # immediate — stop checking
            if -pre_days <= days < 0:
                best_action = "PRIORITISE"

        # ── MEDIUM impact events: AVOID_ENTRY pre only ───────────────────────
        elif impact == "MEDIUM":
            if 0 <= days <= pre_days:
                if best_action != "AVOID_ENTRY":  # HIGH takes precedence
                    best_action = "AVOID_ENTRY"

        # ── LOW impact events: PRIORITISE post only ──────────────────────────
        elif impact == "LOW":
            if -pre_days <= days < 0:
                if best_action == "NO_CHANGE":
                    best_action = "PRIORITISE"

    return best_action


# ════════════════════════════════════════════════════════════════
# REPLACEMENT 2 — Regime resolution with ML predicted_regime
# Replace the 2 lines at the top of generate() that set regime_name
# and block_buys with the block below.
# ════════════════════════════════════════════════════════════════

def _resolve_regime(regime: dict) -> str:
    """
    PATCHED: Prefer ml_regime_classifier predicted_regime when:
      - predicted_regime field exists and is non-null
      - regime_predicted_at is within the last 24 hours

    Falls back to manual regime field if predicted is stale or absent.
    This allows Phase 2 ML predictions to drive generate_signals without
    requiring a schema change to existing 'regime' column.
    """
    from datetime import datetime, timedelta, timezone

    manual    = regime.get("regime", "NEUTRAL")
    predicted = regime.get("predicted_regime")
    pred_at   = regime.get("regime_predicted_at")

    if not predicted or not pred_at:
        return manual

    try:
        # Parse ISO timestamp, handle both with/without timezone
        if isinstance(pred_at, str):
            pred_dt = datetime.fromisoformat(pred_at.replace("Z", "+00:00"))
        else:
            pred_dt = pred_at
        if pred_dt.tzinfo is None:
            pred_dt = pred_dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - pred_dt).total_seconds() / 3600
        if age_hours <= 24:
            return predicted
    except Exception:
        pass

    return manual


# ════════════════════════════════════════════════════════════════
# REPLACEMENT 3 — Regime gate in per-symbol signal loop
# Replace the existing regime gate block in generate() with this.
# ════════════════════════════════════════════════════════════════

# Paste this block INSIDE the generate() for-loop, replacing:
#   if position_state == "BUY_CANDIDATE" and regime_name == "RISK OFF":
#       regime_warning = True
#       if block_buys: signal_type = "BUY_BLOCKED_REGIME"

# ── PATCHED regime gate (CAUTION + RISK OFF) ────────────────────────────────
#   regime_warning = False
#   caution_score_penalty = 0.0
#
#   if position_state == "BUY_CANDIDATE":
#       if regime_name == "RISK OFF":
#           regime_warning = True
#           if block_buys:
#               signal_type = "BUY_BLOCKED_REGIME"
#
#       elif regime_name == "CAUTION":
#           # CAUTION: don't block, but mark warning + reduce score 15%
#           regime_warning      = True
#           caution_score_penalty = 0.15
#           score = round(score * (1.0 - caution_score_penalty), 1)
#           logger.debug(f"{sym}: CAUTION regime — score penalised "
#                        f"{caution_score_penalty*100:.0f}% to {score}")
# ── END PATCHED block ────────────────────────────────────────────────────────


# ════════════════════════════════════════════════════════════════
# INSTRUCTIONS: where to use _resolve_regime()
# In generate(), change the two lines:
#   regime_name = regime.get("regime", "NEUTRAL")
#   block_buys  = cfg_bool("block_buys_risk_off", False) and regime_name == "RISK OFF"
# to:
#   regime_name = _resolve_regime(regime)
#   block_buys  = cfg_bool("block_buys_risk_off", False) and regime_name == "RISK OFF"
# ════════════════════════════════════════════════════════════════


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Quick smoke-test for get_eap_action
    from datetime import date, timedelta

    mock_events = [
        {
            "is_active": True,
            "symbol": "RELIANCE",
            "event_type": "QUARTERLY_RESULTS",
            "purpose": "Financial Results",
            "start_date": (date.today() + timedelta(days=1)).isoformat(),
            "affected_sectors": "Energy",
            "event_category": "STOCK",
        },
        {
            "is_active": True,
            "symbol": "",
            "event_type": "DIVIDEND",
            "purpose": "Interim Dividend",
            "start_date": (date.today() - timedelta(days=1)).isoformat(),
            "affected_sectors": "Banking",
            "event_category": "STOCK",
        },
    ]

    action1 = get_eap_action("RELIANCE", "Energy", mock_events, {"pre_event_days": 2})
    action2 = get_eap_action("HDFCBANK", "Banking", mock_events, {"pre_event_days": 2})
    action3 = get_eap_action("TCS", "IT", mock_events, {"pre_event_days": 2})

    print(f"RELIANCE (results tomorrow): {action1}")   # expect AVOID_ENTRY
    print(f"HDFCBANK (dividend yesterday): {action2}") # expect PRIORITISE
    print(f"TCS (no event): {action3}")                # expect NO_CHANGE

    regime_with_ml = {
        "regime": "NEUTRAL",
        "predicted_regime": "CAUTION",
        "regime_predicted_at": "2099-01-01T10:00:00+00:00"  # future = fresh
    }
    regime_stale_ml = {
        "regime": "TRENDING",
        "predicted_regime": "CAUTION",
        "regime_predicted_at": "2020-01-01T10:00:00+00:00"  # stale
    }
    print(f"ML fresh → use predicted: {_resolve_regime(regime_with_ml)}")   # CAUTION
    print(f"ML stale → use manual:    {_resolve_regime(regime_stale_ml)}")  # TRENDING
