"""
TradeOS v7 — Position Lifecycle Manager
========================================
The missing link between signals, the broker, and outcomes.

WHAT IT REPLACES
  brain/position_manager.py  — deleted. It wrote columns that did not exist
                               (id, entry_price_actual, stop_loss,
                               regime_at_entry, telegram_msg_id, quantity,
                               open_position_id), used status='OPEN'/'CLOSED'
                               while every reader filters status='ACTIVE', and
                               read signal_log.current_price which was not a
                               column. Its Telegram keyboards had no receiver.

RESPONSIBILITIES
  1. RECONCILE   Kite holdings -> open_positions. Opens rows for new holdings,
                 closes rows for vanished ones, adjusts quantity on partial
                 fills. Kite is authoritative; every divergence is logged to
                 position_reconcile_log rather than silently absorbed.
  2. ATTRIBUTE   Links each position to the signal that produced it
                 (signal_id, signal_date, entry_signal_type) and freezes the
                 trade plan at entry. closed_positions.signal_date was NULL on
                 all 70 historical rows, which is the sole reason
                 signal_outcomes holds 2 records and the Brain has learned
                 nothing.
  3. MANAGE      Applies the exit policy: partial booking at N×R, stop to
                 breakeven, delayed trailing, hard target, time stop.
  4. CLOSE       Writes closed_positions with a machine-generated exit_reason,
                 realised R-multiple, hold days and excursion stats.

EXIT POLICY, AND WHY IT IS SHAPED THIS WAY
------------------------------------------
Measured over the 70 realised trades:
    in-trade MFE +3.40%  vs  in-trade MAE -3.43%     (roughly symmetric)
    capture ratio (realised / in-trade MFE): median 3%
    46% of trades never achieved even +2% at any point
    exit_reason 'TRAIL SL HIT' = 37/70, average P&L -0.40%

Two conclusions drove the design:

  a) Trailing from entry is what compressed the average winner to +3.89%.
     A stop trailing 6% below the high-water mark from day one gets hit during
     ordinary retracement long before a 1-3 week thesis plays out. Trailing is
     now DELAYED until the position is up exit_trail_after_r (2R).

  b) Exits are NOT the main problem and this policy is not asked to rescue bad
     entries. Simulation showed every exit variant is negative over all 70
     trades, but on the subset whose entries worked (n=44) the EXISTING policy
     already returns +1.26%. So the policy targets a modest, evidence-backed
     improvement (+1.50% on that subset, +2.81% on trades reaching +5%) and
     leaves the heavy lifting to the entry gate fix in generate_signals.

Everything is expressed in R (multiples of initial risk), not fixed percent, so
a 2.5% ATR stock and a 6% ATR stock are managed on the same logic.

SAFETY
  Never places or cancels an order. Order placement stays in
  control/execution_engine.py behind its own autonomy-phase gate. This module
  only records state and raises alerts.
"""

from __future__ import annotations

import re
import sys
import json
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import (
    get_supabase, today_ist, IST, DRY_RUN,
    is_kill_switch_active, cfg, cfg_bool, cfg_float, cfg_int,
    TOTAL_CAPITAL,
)


# ─────────────────────────────────────────────────────────────────────────────
# EXIT POLICY
# ─────────────────────────────────────────────────────────────────────────────

def _patch_position(sb, pos: dict, patch: dict) -> None:
    """
    Update ONE position row, keyed on (symbol, product).

    open_positions is unique on the pair since migration 028, so matching on
    symbol alone writes this tranche's values onto the other one whenever swing
    holds a name CNC and intraday holds it MIS. Four reconcile/backfill paths
    still did that after the composite key landed — plan levels, identity,
    reconcile status and, worst of the four, the QUANTITY sync, which decides
    how many shares an exit order sells.

    Falls back to symbol alone when product is unset so pre-028 rows still
    update, which is the same compromise the exit path already makes.
    """
    q = sb.table("open_positions").update(patch).eq("symbol", pos["symbol"])
    if pos.get("product"):
        q = q.eq("product", pos["product"])
    q.execute()


def _update_stripping_unknown(write, payload: dict, what: str, _depth: int = 0):
    """
    Write a payload, dropping columns the schema does not have, and say so.

    POSTGREST FAILS THE WHOLE STATEMENT ON ONE UNKNOWN COLUMN.

    Not the column — the statement. A payload carrying `current_price`,
    `high_water_mark`, `max_favorable_excursion`, `active_sl`, `exit_signal`
    and one column that does not exist writes NONE of them, and the caller here
    logged a warning and moved on. Every position in the book would silently
    stop updating its price, its excursion and its stop, and the only symptom
    would be one warning line per cycle among hundreds.

    That is not hypothetical: it is the failure mode that arrives the moment
    code is deployed ahead of its migration, which is the normal order of
    events. Migration 031 adds the runner columns this payload now carries, and
    between deploying and applying it, this function is the difference between
    "the runner fields are not recorded yet" and "the book stopped updating".

    So: strip the column the error names, retry, and log at WARNING which field
    was dropped. Bounded by the payload size so a genuinely broken write cannot
    loop.
    """
    try:
        return write(payload)
    except Exception as e:
        msg = str(e)
        # PGRST204: "Could not find the 'x' column of 'y' in the schema cache"
        # 42703:    "column y.x does not exist"
        m = (re.search(r"Could not find the '([^']+)' column", msg)
             or re.search(r"column \"?(?:[\w.]+\.)?([\w]+)\"? does not exist", msg))
        col = m.group(1) if m else None
        if col and col in payload and _depth < len(payload):
            trimmed = {k: v for k, v in payload.items() if k != col}
            logger.warning(f"  {what}: column '{col}' does not exist — dropped it and "
                           f"retried so the other {len(trimmed)} field(s) still land. "
                           f"A migration is probably unapplied.")
            return _update_stripping_unknown(write, trimmed, what, _depth + 1)
        logger.warning(f"  {what}: update failed: {e}")
        return None


def _upsert_position(sb, row: dict, _depth: int = 0):
    """
    Upsert on (symbol, product), falling back to symbol alone — and, for any
    column the schema does not have yet, dropping it and retrying rather
    than failing the whole write.

    TWO INDEPENDENT SCHEMA-AHEAD-OF-DEPLOY GAPS, TWO PROTECTIONS.
    The composite key (migration 028) already had one. A single new column
    can break the same way: PostgREST fails the WHOLE statement on one
    unknown column, and entry_order_id (migration 045) is exactly such a
    column landing in code before the user has necessarily run the migration
    that adds it — the normal order of events in this project. A position
    that cannot be recorded is precisely the failure that causes a double
    buy, which is the same reason the composite-key fallback exists.

    RAISES rather than swallowing a write that still fails after stripping
    what it can. _maybe_enter_swing() relies on an exception meaning "this
    position is NOT recorded" to decide whether it is safe to retry —
    silently returning as if the write succeeded when it did not is worse
    than the column-stripping problem this is working around.
    """
    try:
        try:
            return sb.table("open_positions").upsert(
                row, on_conflict="symbol,product").execute()
        except Exception as e:
            if "42P10" not in str(e) and "ON CONFLICT" not in str(e):
                raise
            global _WARNED_NO_COMPOSITE_KEY
            if not _WARNED_NO_COMPOSITE_KEY:
                logger.warning("  open_positions has no (symbol, product) key — apply "
                               "migration 028. Falling back to symbol; one book per "
                               "symbol until then.")
                _WARNED_NO_COMPOSITE_KEY = True
            return sb.table("open_positions").upsert(row, on_conflict="symbol").execute()
    except Exception as e:
        msg = str(e)
        # PGRST204: "Could not find the 'x' column of 'y' in the schema cache"
        # 42703:    "column y.x does not exist"
        m = (re.search(r"Could not find the '([^']+)' column", msg)
             or re.search(r"column \"?(?:[\w.]+\.)?([\w]+)\"? does not exist", msg))
        col = m.group(1) if m else None
        if col and col in row and _depth < len(row):
            logger.warning(f"  open_positions upsert: column '{col}' does not exist — "
                           f"dropped it and retried so the other {len(row) - 1} field(s) "
                           f"still land. A migration is probably unapplied.")
            return _upsert_position(
                sb, {k: v for k, v in row.items() if k != col}, _depth + 1)
        raise


_WARNED_NO_COMPOSITE_KEY = False


def load_exit_policy() -> dict:
    """
    NOTE ON trail_r (was trail_pct).

    The trail was originally a PERCENTAGE below the high-water mark. That does
    not compose with R-based targets and rendered the trailing branch
    unreachable. Worked through: entry 100, planned stop 95 (risk 5%), so
    1R=+5% and the 3R target sits at 115. A 10% trail gives:
        HWM 110 (2.0R) -> trail 99.00   below the breakeven stop of 100 — ignored
        HWM 114 (2.8R) -> trail 102.60  only 0.5R locked
        HWM 115 (3.0R) -> hard target fires first
    The trail never bound, at any price, for any stock whose risk was under
    ~10%. Expressing it in R fixes that and makes it volatility-adaptive:
        HWM 110 (2.0R) -> trail 102.50  0.5R locked
        HWM 113 (2.6R) -> trail 105.50  1.1R locked
    A percentage trail also silently means "tighter for volatile stocks, looser
    for calm ones", which is backwards.
    """
    return {
        # REPRICED 20-Aug-2026 (F-43). The ladder above (1.5R partial, 2.0R
        # trail, 1.0R breakeven) was calibrated for a distribution this book
        # does not produce: median peak run across 13 closed swing trades was
        # 0.67R, only 2 of 13 ever cleared 1.0R, and 1.5R was reached once.
        # With 1R sitting at 5-9% of price here, a rung above where the book's
        # own trades actually peak is not conservative, it is unreachable —
        # which is why the give-back guard below (peaked, gave most of it
        # back) ended up doing 100% of the profit-taking, at its own 50%
        # setting, on 7 of 9 winners. Moved down to where the trades are.
        "partial_book_r":    cfg_float("exit_partial_book_r",    1.0),
        "partial_book_pct":  cfg_float("exit_partial_book_pct",  50.0),
        "move_to_breakeven": cfg_bool("exit_move_to_breakeven",  True),
        "trail_r":           cfg_float("exit_trail_r",           1.0),
        "trail_after_r":     cfg_float("exit_trail_after_r",     1.5),
        "target_r":          cfg_float("exit_target_r",          3.0),
        "time_stop_days":    cfg_int("exit_time_stop_days",      15),
        "time_stop_min_r":   cfg_float("exit_time_stop_min_r",   0.5),
        # ── the four keys below exist because the ladder above was priced for
        #    a distribution this book does not produce. See evaluate_exit. ────
        "use_setup_target":   cfg_bool("swing_use_setup_target",   True),
        "setup_target_min_r": cfg_float("swing_setup_target_min_r", 0.6),
        "breakeven_at_r":     cfg_float("exit_breakeven_at_r",     0.5),
        # Deliberately the SAME number as stall_peak_r, so the two profit-side
        # rules partition the space with no gap: a position whose peak never
        # reached 0.5R is handled by the stall exit, and one whose peak cleared
        # 0.5R is handled by the give-back guard.
        "giveback_min_r":     cfg_float("exit_giveback_min_r",     0.5),
        "giveback_pct":       cfg_float("exit_giveback_pct",       50.0),
        # TIGHTENS ONCE A PARTIAL SHOULD ALREADY BE BANKED — F-43. Below
        # giveback_runner_min_r this is the only thing standing between a
        # winner and a loser, so it stays loose (50%): a trade that has not
        # yet earned a partial should not be chopped on ordinary wobble.
        # Past that line — matched to partial_book_r, so the two rules hand
        # off with no gap — a partial should already be banked and the stop
        # at breakeven, so the remaining size is risking PROFIT, not capital,
        # and can afford to be defended harder. See evaluate_exit().
        "giveback_pct_runner":     cfg_float("exit_giveback_pct_runner",     30.0),
        "giveback_runner_min_r":   cfg_float("exit_giveback_runner_min_r",   1.0),
        "cost_buffer_pct":    cfg_float("exit_breakeven_cost_pct", 0.20),
        "stall_days":         cfg_int("exit_stall_days",           10),
        "stall_peak_r":       cfg_float("exit_stall_peak_r",       0.5),
        # The fast arm of the same idea — see the EXIT_FASTFAIL block in
        # evaluate_exit(). Enabled by its own switch because it is the only
        # rule here that closes a position while the ordinary stop is still
        # a long way off.
        "fastfail_enabled":   cfg_bool("exit_fastfail_enabled",    False),
        "fastfail_days":      cfg_int("exit_fastfail_days",        4),
        "fastfail_peak_r":    cfg_float("exit_fastfail_peak_r",    0.25),
        "fastfail_gain_r":    cfg_float("exit_fastfail_gain_r",   -0.5),
    }


def _sell_price_today(symbol: str) -> float | None:
    """
    Average price of today's SELL trades for a symbol, from the broker.

    The realised price of a partial is a fact the broker knows and we should
    never guess: using the current LTP instead would record a profit that was
    never earned, on the exact trades the exit policy is judged by. LTP is only
    a last resort, and the caller logs when it falls back.

    Returns None when the broker is unreachable or has no matching fill.
    """
    try:
        from kite import kite_client
        trades = kite_client.fetch_trades() or []
    except Exception:
        return None

    today = datetime.now(IST).date().isoformat()
    qty = value = 0.0
    for t in trades:
        if (t.get("symbol") or t.get("tradingsymbol")) != symbol:
            continue
        if (t.get("transaction_type") or "").upper() != "SELL":
            continue
        ts = str(t.get("timestamp") or t.get("fill_timestamp") or "")
        if ts and not ts.startswith(today):
            continue
        q = float(t.get("quantity") or 0)
        p = float(t.get("average_price") or t.get("price") or 0)
        if q and p:
            qty += q
            value += q * p
    return round(value / qty, 2) if qty else None


def evaluate_exit(pos: dict, ltp: float, sessions_held: int, policy: dict) -> dict:
    """
    Decide what should happen to one open position at the current price.

    Returns {action, reason, detail, new_sl, book_qty}. Pure — no I/O, no
    mutation — so it is directly testable and the caller owns all writes.

    action is one of:
        HOLD           nothing to do
        BOOK_PARTIAL   first target reached, take exit_partial_book_pct off
        TRAIL_SL       ratchet the stop up (also used for the breakeven move)
        EXIT_TARGET    hard target reached
        EXIT_STOP      stop breached
        EXIT_TIME      time stop, position going nowhere
        EXIT_GIVEBACK  was a winner, handed back too much of the move
        EXIT_FASTFAIL  never worked and is well underwater, four sessions in
        EXIT_STALL     never went anywhere, the slot is worth more elsewhere
        RUN            at target with the trend intact — hold as a runner
        EXIT_DETERIORATION  profitable but the thesis broke
    """
    entry = float(pos.get("entry_price") or 0)
    stop0 = float(pos.get("planned_stop") or 0)
    sl    = float(pos.get("active_sl") or stop0 or 0)

    if not entry or not ltp:
        return {"action": "HOLD", "reason": "missing_price", "detail": "",
                "new_sl": None, "book_qty": 0}

    # Initial risk per share. Everything below is measured against it.
    risk = entry - stop0 if stop0 and stop0 < entry else None
    if not risk or risk <= 0:
        # No usable planned_stop (a pre-migration or manually created row).
        # Fall back to percent-based management so the position is not
        # unmanaged, but say so in the detail string.
        risk = entry * 0.05
        fallback = True
    else:
        fallback = False

    gain_r   = (ltp - entry) / risk
    gain_pct = (ltp - entry) / entry * 100.0

    # ── REGIME-AWARE MULTIPLIER — Track E, Stage E3 ──────────────────────────
    # evaluate_exit() has never read the market's CURRENT state, only
    # regime_at_entry (frozen the day a position opened) — a position held
    # 1-3 weeks can easily outlive the regime it was entered in. This
    # computes what a regime-aware adjustment to the giveback/stall
    # thresholds WOULD be, and only actually applies it behind
    # swing_regime_aware_exits_enabled (ships OFF). E2's own quantify pass
    # (F-68) found every resolved swing outcome on record reads
    # regime='NEUTRAL' — no historical diversity exists yet to validate
    # this against, which is exactly why this ships shadow-logged rather
    # than armed, unlike F-43/F-46's calibration work, which had real
    # resolved-outcome evidence behind it before it went live.
    #
    # ONE multiplier, read by both consumers below (giveback_pct, a percent
    # allowance, and stall_days, a day count) — the same direction works
    # for both without inverted logic: a mult < 1.0 makes a smaller
    # giveback allowance (tighter) AND a shorter stall clock (also
    # tighter); a mult > 1.0 loosens both. RISK OFF tightens; a strong
    # RISK ON / TRENDING tape loosens slightly, on the reasoning that more
    # patience is a legitimate professional response to a market that is
    # actually running, not just permissiveness for its own sake — capped
    # both directions in config so one regime read can never swing the
    # ladder to an extreme.
    current_regime = str(policy.get("_current_regime") or "NEUTRAL").upper()
    if current_regime in ("RISK OFF", "RISK_OFF"):
        regime_mult = cfg_float("swing_regime_mult_risk_off", 0.7)
    elif current_regime in ("RISK ON", "RISK_ON", "TRENDING"):
        regime_mult = cfg_float("swing_regime_mult_risk_on", 1.2)
    else:
        regime_mult = 1.0
    regime_aware_live = cfg_bool("swing_regime_aware_exits_enabled", False)
    if regime_mult != 1.0 and not regime_aware_live:
        logger.debug(
            f"  {pos.get('symbol')}: regime-aware shadow — current regime "
            f"{current_regime}, would apply x{regime_mult:.2f} to the "
            f"giveback/stall thresholds — swing_regime_aware_exits_enabled "
            f"is off")
    applied_regime_mult = regime_mult if regime_aware_live else 1.0

    # ── THE TARGET THIS POSITION IS ACTUALLY MANAGED TO ──────────────────────
    #
    # exit_target_r is 3.0. On this book 1R (entry minus planned_stop) is 5-9%
    # of price, so 3R means waiting for a 15-28% move. Measured over the 55
    # closed swing positions that carry a favourable-excursion figure:
    #
    #     MFE >= 7.5% (1.5R, the partial)   11/55 = 20%
    #     MFE >= 10%  (2.0R, the trail on)   6/55 = 11%
    #     MFE >= 15%  (3.0R, the target)     3/55 =  5%
    #
    # The whole profit-taking ladder sat ABOVE the distribution. The partial
    # fired on one trade in five, breakeven is attached to the partial so it
    # almost never moved, and the trail effectively never engaged. The measured
    # consequence: median capture ratio 0% of favourable excursion, and 24 of
    # the 28 losing trades had been more than 0.5% in PROFIT before they lost.
    #
    # Meanwhile the pipeline computes planned_target for every plan and writes
    # it to the row — and on the four live positions it sits at 1.04R, 1.24R,
    # 1.29R and 1.91R. It is displayed on the dashboard, it is what the R:R at
    # entry was justified with, and evaluate_exit never read it. The intraday
    # book fixed exactly this with intraday_use_setup_target; swing had no
    # equivalent, so the plan's own target was decoration.
    #
    # So the plan's target becomes T1 — book half and move to breakeven — and
    # the hard multiple stays T2, where the bank-or-run decision is made.
    #
    # IT IS T1 AND NOT T2 FOR A SAFETY REASON, not a stylistic one. exit_rules
    # is explicit that running a position is safe only because "a runner has
    # already booked half at 1.5R and its stop is at or above breakeven, so the
    # decision to run risks only open profit, never capital". Moving the
    # bank-or-run decision down to 1.2R would let RUN fire on a full-size
    # position whose stop is still below entry — a runner risking capital,
    # which is the one thing that asymmetry forbids. Attaching the plan's
    # target to the partial instead makes the rung reachable AND keeps the
    # invariant that makes the runner logic safe.
    #
    # Floored at setup_target_min_r because a target computed too close — the
    # mirror of the KIMS 19R stop fault that entry_ranking already caps — must
    # not book half the position on the first tick.
    target_r = policy["target_r"]
    partial_r = policy["partial_book_r"]
    partial_src = f"{partial_r:.1f}R"
    planned_target = float(pos.get("planned_target") or pos.get("target_price") or 0)
    if policy.get("use_setup_target") and planned_target > entry:
        setup_r = (planned_target - entry) / risk
        if policy["setup_target_min_r"] <= setup_r < partial_r:
            partial_r = setup_r
            partial_src = (f"the plan's own target {planned_target:.2f} "
                           f"({setup_r:.2f}R)")
    target_src = f"{target_r:.1f}R"

    # ── 1. Stop breach — always checked first ────────────────────────────────
    if sl and ltp <= sl:
        return {
            "action": "EXIT_STOP",
            "reason": "STOP_LOSS_HIT" if not pos.get("trail_activated") else "TRAIL_SL_HIT",
            "detail": f"ltp {ltp:.2f} <= sl {sl:.2f} ({gain_pct:+.2f}%, {gain_r:+.2f}R)",
            "new_sl": None, "book_qty": 0,
        }

    # ── 2. Hard target — EXIT, or run it ─────────────────────────────────────
    # The old rule exited unconditionally at 3R, which treats every trade the
    # same at exactly the moment they stop being the same. Over the 70 realised
    # trades the median capture ratio was 3% — nearly all of the favourable
    # excursion given back — and cutting winners at a fixed multiple guarantees
    # that never improves.
    #
    # A position here has already booked half at 1.5R with its stop at or above
    # breakeven, so running risks open PROFIT, never capital. That asymmetry is
    # what makes the discretion safe; the loss-side rules below stay strict and
    # unconditional.
    if gain_r >= target_r:
        sig = (policy.get("_trend_ctx") or {}).get(pos.get("symbol")) or {}
        try:
            from control.exit_rules import assess_trend, target_decision
            tq = assess_trend(sig, pos)
            d = target_decision(pos, gain_r, tq, policy)
            d["detail"] = f"{d['detail']} [target: {target_src}]"
            return d
        except Exception as e:
            # A failure to assess must not become a failure to bank the gain.
            logger.warning(f"  trend assessment failed for {pos.get('symbol')}: {e}")
            return {
                "action": "EXIT_TARGET", "reason": "TARGET_HIT",
                "detail": (f"{gain_r:.2f}R >= {target_src} ({gain_pct:+.2f}%) — "
                           f"could not assess the trend, so banking rather than running"),
                "new_sl": None, "book_qty": 0,
            }

    # ── 2b. Thesis broken while in profit ────────────────────────────────────
    # The trail is a price mechanism and only reacts after the damage. This
    # reacts to the reason while there is still profit to protect.
    if gain_r >= 1.0:
        sig = (policy.get("_trend_ctx") or {}).get(pos.get("symbol")) or {}
        if sig:
            try:
                from control.exit_rules import assess_trend, deterioration_check
                d = deterioration_check(pos, gain_r, assess_trend(sig, pos))
                if d:
                    return d
            except Exception as e:
                logger.debug(f"  deterioration check skipped: {e}")

    # ── 2c. AI-flagged risk — tighten toward price, never invents an exit ────
    # Track E, Stage E3 (docs/TRADEOS_ROADMAP.md). `ai_recommended_action`
    # is written by `ai/ai_decision_engine.py` and, before this, read by
    # exactly one place — `alerts/send_alerts.py`, to DISPLAY it.
    # HINDCOPPER's own TIGHTEN_SL recommendation over a live geopolitical
    # risk ("protect gains... tighter stop") sat as a notification; nothing
    # executed it. This does — behind a switch that ships OFF
    # (`swing_ai_tighten_enabled`), Stage E3's own default posture for
    # anything that changes live exit behaviour.
    #
    # ONE-DIRECTIONAL ONLY, the same asymmetry every rule in this ladder
    # already respects: moves the stop a FRACTION of the way from where it
    # sits toward the live price — never loosens it (`candidate_sl > sl`
    # guards that), never sells anything by itself. Checked regardless of
    # profit level, unlike 2b above — an escalating risk does not wait for
    # a position to be profitable before it matters.
    #
    # HOLD/TRIM/EXIT/NO_ACTION are deliberately NOT executed here. TIGHTEN_
    # SL is the one recommendation that is safe to automate under the same
    # test every other rung in this ladder already passes — it can only
    # ever protect capital, never spend it. TRIM/EXIT would mean acting
    # directly on ai_tier-adjacent judgement, which entry_ranking.py's own
    # 04-Aug history already found unpredictive enough to demote to zero
    # ranking weight; automating a sale on the same basis would undo that.
    if str(pos.get("ai_recommended_action") or "").upper() == "TIGHTEN_SL":
        frac = cfg_float("swing_ai_tighten_fraction", 0.5)
        candidate_sl = round(sl + frac * (ltp - sl), 2)
        if candidate_sl > sl:
            reason_text = str(pos.get("ai_action_reason") or "AI flagged a risk")
            if cfg_bool("swing_ai_tighten_enabled", False):
                return {
                    "action": "TRAIL_SL", "reason": "AI_TIGHTEN_SL",
                    "detail": (f"AI recommended TIGHTEN_SL — {reason_text} — "
                               f"sl {sl:.2f} -> {candidate_sl:.2f}"),
                    "new_sl": candidate_sl, "book_qty": 0,
                }
            logger.info(
                f"  {pos.get('symbol')}: AI TIGHTEN_SL shadow — would move "
                f"sl {sl:.2f} -> {candidate_sl:.2f} ({reason_text}) — "
                f"swing_ai_tighten_enabled is off")

    # ── 3. Partial booking at the first meaningful multiple ──────────────────
    already_booked = int(pos.get("partial_booked_qty") or 0) > 0
    current_qty    = int(pos.get("current_qty") or pos.get("actual_qty") or 0)
    if not already_booked and gain_r >= partial_r and current_qty > 1:
        book_qty = max(1, int(current_qty * policy["partial_book_pct"] / 100.0))
        return {
            "action": "BOOK_PARTIAL",
            "reason": "PARTIAL_TARGET",
            "detail": (f"{gain_r:.2f}R >= {partial_src} — book "
                       f"{book_qty}/{current_qty} @ {ltp:.2f} ({gain_pct:+.2f}%)"),
            # Entry plus the round trip, not entry. A stop at exactly the entry
            # price still loses the charges on both legs.
            "new_sl": (round(entry * (1 + policy["cost_buffer_pct"] / 100.0), 2)
                       if policy["move_to_breakeven"] else None),
            "book_qty": book_qty,
        }

    # ── 3b. GIVE-BACK GUARD — do not let a winner become a loser ─────────────
    #
    # THE MEASUREMENT THAT PUT THIS HERE. Of the 28 losing swing trades that
    # carry a favourable-excursion figure, 24 had been more than 0.5% in profit
    # before they lost. Median given back from the high: 3.14%; across the book,
    # 400 percentage points of favourable excursion were handed back.
    #
    # None of the existing rules could have stopped that. The trail needs 2R
    # (10-18% on this book) before it engages, the partial needs 1.5R, and
    # breakeven is attached to the partial — so a position that ran to +4% and
    # returned to -3% passed through the entire ladder without touching a rung.
    #
    # This works in EXCURSION space rather than R space, which is the point: it
    # engages on moves the R-based rungs never see. Once a position has been up
    # by giveback_min_r, handing back more than half of that peak is the move
    # failing, not noise.
    #
    # DELIBERATELY OFF ABOVE THE TARGET. Past target_r the position is either
    # banked or a runner, and a runner exists precisely to give back some open
    # profit in exchange for the tail. Applying this there would re-create the
    # fixed-multiple exit that the runner logic was written to remove — the 5%
    # of trades that reach 3R are where the winners live, and they must not be
    # strangled by a rule built from the 63% that never clear 2%.
    #
    # TIERED BY PEAK — F-43, 20-Aug-2026. This guard was measured doing 100%
    # of the book's profit-taking, at its flat 50% setting, on 7 of the last 9
    # winning swing exits — because the ladder above never got reached (see
    # the reprice note in load_exit_policy). That is correct BEFORE a partial
    # has fired: below giveback_runner_min_r it is the only thing standing
    # between a winner and a loser, so 50% stays deliberately loose. Once peak
    # clears that line a partial should already be banked and the stop moved
    # to breakeven, so what remains is risking PROFIT rather than capital and
    # is worth defending harder than an ordinary retracement allowance.
    hwm_px = float(pos.get("high_water_mark") or 0)
    if (policy["giveback_pct"] > 0 and hwm_px > entry and gain_r < target_r):
        peak_r = (hwm_px - entry) / risk
        if peak_r >= policy["giveback_min_r"]:
            gb_pct = (policy["giveback_pct_runner"]
                      if peak_r >= policy["giveback_runner_min_r"]
                      else policy["giveback_pct"]) * applied_regime_mult
            kept = gain_r / peak_r if peak_r > 0 else 1.0
            if kept < (1.0 - gb_pct / 100.0):
                return {
                    "action": "EXIT_GIVEBACK",
                    "reason": "GAVE_BACK_THE_MOVE",
                    "detail": (
                        f"peaked at {peak_r:.2f}R ({(hwm_px - entry) / entry * 100:+.2f}%) "
                        f"and is back to {gain_r:+.2f}R ({gain_pct:+.2f}%) — "
                        f"{1 - kept:.0%} of the move handed back, over the "
                        f"{gb_pct:.0f}% limit. Taking what is left "
                        f"rather than watching a winner finish as a loser"),
                    "new_sl": None, "book_qty": 0,
                }

    # ── 3c. BREAKEVEN RATCHET — independent of the partial ───────────────────
    #
    # exit_move_to_breakeven has been true in system_config throughout, and it
    # only ever took effect inside BOOK_PARTIAL — which needs 1.5R, which needs
    # a 7.5-14% move, which 20% of this book's trades achieved. So the stop
    # essentially never moved to entry, and a trade that paid 1R could still
    # finish at a full -1R loss.
    #
    # Breakeven is set slightly ABOVE entry, by the round-trip cost. A stop at
    # exactly the entry price is not breakeven — it is a small loss, because
    # brokerage, STT and stamp duty are still owed on both legs. Getting this
    # wrong is a rounding error per trade and a real number over seventy.
    if policy["move_to_breakeven"] and gain_r >= policy["breakeven_at_r"]:
        be = round(entry * (1 + policy["cost_buffer_pct"] / 100.0), 2)
        if be > sl:
            return {
                "action": "TRAIL_SL", "reason": "BREAKEVEN",
                "detail": (f"{gain_r:.2f}R >= {policy['breakeven_at_r']}R — sl "
                           f"{sl:.2f} -> {be:.2f}, entry plus the "
                           f"{policy['cost_buffer_pct']:.2f}% round trip so a stop "
                           f"here is genuinely flat rather than a small loss. "
                           f"This trade can no longer cost money"),
                "new_sl": be, "book_qty": 0,
            }

    # ── 4. Trailing — DELAYED until trail_after_r ────────────────────────────
    # Trailing from entry is what produced the -0.40% average on 37 TRAIL SL HIT
    # exits: an ordinary retracement takes out the stop before the 1-3 week
    # thesis resolves.
    if gain_r >= policy["trail_after_r"]:
        hwm      = max(float(pos.get("high_water_mark") or entry), ltp)
        # Trail a fixed number of R below the high-water mark, NOT a percentage
        # (see load_exit_policy for why percentage trailing never bound).
        trail_sl = hwm - policy["trail_r"] * risk
        # Never let the trail loosen an existing stop.
        if trail_sl > sl:
            locked_r = (trail_sl - entry) / risk
            return {
                "action": "TRAIL_SL",
                "reason": "TRAIL_UPDATE",
                "detail": (f"{gain_r:.2f}R >= {policy['trail_after_r']}R — "
                           f"sl {sl:.2f} -> {trail_sl:.2f} "
                           f"(hwm {hwm:.2f}, locks {locked_r:+.2f}R)"),
                "new_sl": round(trail_sl, 2), "book_qty": 0,
            }

    # NOTE — a "maximum stop lag" rule was added here on 31 Jul and REMOVED
    # the same day. It tightened the stop whenever it sat more than 8% below
    # price, on the reasoning that a 12% gap on a position that is up looks
    # wrong.
    #
    # It was wrong. Risk is entry minus stop, so a plan with a 10% ATR stop has
    # 1R = 10%: a position at +2% is 0.2R, which is an ordinary healthy swing
    # trade, and tolerating a 12% adverse move is precisely what that stop was
    # sized to do. Overriding an ATR-derived level with a flat percentage
    # discards the volatility model that produced it and cuts winners early —
    # the exact failure already measured on this book, where 37 of 70 exits were
    # trailing stops averaging -0.40% and the median capture ratio was 3%.
    #
    # The ladder below already answers "not too late": deterioration exits above
    # 1R when the trend breaks, and the time stop closes anything going nowhere.
    # Neither needs a percentage invented on the day.

    # Peak excursion in R, shared by both arms below. `hwm_px <= entry` reads
    # as a peak of exactly zero rather than a negative number: a trade that
    # never traded above entry has no favourable excursion, it does not have a
    # bad one.
    hwm_px = float(pos.get("high_water_mark") or 0)
    peak_r = (hwm_px - entry) / risk if hwm_px > entry else 0.0

    # ── 4a. FAST FAIL — the same question, asked four sessions sooner ────────
    #
    # 18-Aug-2026. The stall exit below is correct and its evidence is sound,
    # but its clock is ten sessions, and nothing between entry and day ten asks
    # whether a position has ever worked. GABRIEL sat in that gap for its whole
    # life: entered 06-Aug at 1554.80, its high-water mark was 1555.20 — forty
    # paise, +0.003R — and it was never once profitable. Every profit-side rule
    # above (target, partial, breakeven trail, give-back) is unreachable by
    # construction on such a trade, and EXIT_DETERIORATION is gated at +1.0R.
    # The stop was 9.7% away, so the only rule that could see it was the stall,
    # on day ten. It closed at -7.86%.
    #
    # On 10-Aug — four sessions in — GABRIEL was at -5.5% and -0.56R with a peak
    # of 0.00R. Every term this rule reads was already true and had been for
    # days. The same is true of SCI right now: entered 10-Aug at 300.60, peak
    # 301.60 (+0.05R), currently -0.61R.
    #
    # WHY THREE CONDITIONS AND NOT A PERCENTAGE. `peak_r` is what separates
    # "never worked" from "worked and gave it back" — the give-back guard owns
    # the second case and would be wrong to fire here. `gain_r` requires the
    # position to be meaningfully underwater NOW, so a flat trade going nowhere
    # is left to the stall rule rather than being cut on noise. `sessions_held`
    # is the patience: fewer than four sessions is inside the normal wobble of
    # an entry and cutting there would cost more in good trades than it saves.
    #
    # OFF BY DEFAULT (`exit_fastfail_enabled`). This is the one rule in the
    # ladder that sells while the ordinary stop is still far away, which means
    # a miscalibration here shows up as real money rather than as a missed
    # opportunity. It should be switched on deliberately, with the book watched
    # for a few sessions afterwards.
    if (policy.get("fastfail_enabled")
            and policy["fastfail_days"] > 0
            and sessions_held >= policy["fastfail_days"]
            and peak_r < policy["fastfail_peak_r"]
            and gain_r <= policy["fastfail_gain_r"]):
        return {
            "action": "EXIT_FASTFAIL",
            "reason": "NEVER_WORKED_FAST",
            "detail": (f"{sessions_held} sessions, never better than "
                       f"{peak_r:+.2f}R ({(hwm_px - entry) / entry * 100 if hwm_px else 0:+.2f}%), "
                       f"now {gain_r:+.2f}R. A swing trade that has not worked in "
                       f"{policy['fastfail_days']} sessions and is {abs(policy['fastfail_gain_r'])}R "
                       f"down is the book's worst bucket — cutting here rather than "
                       f"waiting {policy['stall_days'] - sessions_held} more sessions "
                       f"for the stall rule"),
            "new_sl": None, "book_qty": 0,
        }

    # ── 4b. STALL EXIT — the trade that never worked at all ──────────────────
    #
    # The give-back guard above needs a position to have made a full R before it
    # protects anything, so it cannot touch the largest loss bucket in this
    # book. Sorting the 55 closed swing trades that carry a favourable-excursion
    # figure by how far they ever got:
    #
    #     peak MFE      n   median result   net Rs   win rate
    #     0-1%          9        -2.69%      -3,006      0%
    #     1-2%         11        -2.44%      -5,203     27%
    #     2-3%          9        -0.03%        -316     33%
    #     3-5%          8        +1.03%         -20     62%
    #     5-8%          8        +3.93%      +3,442     88%
    #     8%+           9        +7.74%      +8,026     89%
    #
    # It is monotonic and it is brutal. A swing trade that never gets more than
    # 2% in its favour is a 15%-win-rate proposition worth -Rs 8,209 over twenty
    # trades; one that clears 5% wins 88% of the time. Trades that work, work
    # reasonably soon — and the existing time stop cannot express that, because
    # it asks where the position is NOW (gain_r) after fifteen sessions, by
    # which point a stock that ran 4% and came back looks identical to one that
    # never moved. Those deserve opposite treatment.
    #
    # So this asks about the PEAK, not the present: in `stall_days` sessions,
    # did this position ever go anywhere? A position that ran and pulled back
    # is not stalled — the give-back guard owns that case — and is excluded by
    # construction, because its high-water mark cleared the bar.
    #
    # CALIBRATION CAVEAT, stated rather than buried: the distribution above
    # comes from 55 trades, most of them from a legacy manually-sized book
    # (Jan-Mar 2026, positions of Rs 16,000-20,000 against Rs 20,000 of
    # capital). The SHAPE of the relationship is a well-established property of
    # swing trading and is not in doubt; the exact 10-session and 0.5R numbers
    # are a judgement and should be re-derived once the current book has 20+
    # closes of its own. Both are config keys for exactly that reason.
    #
    # THE CLOCK IS NOW PER ENGINE FAMILY, NOT ONE NUMBER FOR THE WHOLE BOOK —
    # F-46, 21-Aug-2026. `stall_days` above was a single judgement call; a
    # CTL breakout and a MOM continuation trade do not resolve on the same
    # clock, and swing/signals/outcomes.py already resolves enough of both to
    # say by how much (see swing/signals/pace_calibration.py): CONTINUATION's
    # own winners clear target in 6 sessions three times out of four (n=296),
    # MOM's in 7 (n=86) — against this rule's flat 10. `policy[
    # "stall_days_by_family"]` is built once per daemon start from that live
    # data and can only ever TIGHTEN the clock below the configured
    # `stall_days` (see that module's docstring for why loosening is not on
    # the table); a family with too thin a sample, or no calibration
    # available at all, simply falls back to `stall_days` unchanged — the
    # exact behaviour this rule has always had.
    from allocation.scoring import swing_family
    family = swing_family(pos.get("strategy"))
    calibrated = family in (policy.get("stall_days_by_family") or {})
    stall_days = (policy.get("stall_days_by_family") or {}).get(
        family, policy["stall_days"])
    # REGIME ADJUSTMENT — separate axis from the per-family calibration
    # directly above, and NOT the same promise F-46 makes for that
    # calibration (which is capped at the book-wide default and can only
    # tighten). This adjusts the family-calibrated (or default) number by
    # current market condition, and — only once armed — can loosen it in a
    # genuinely strong tape as well as tighten it in a weak one. A no-op
    # today: applied_regime_mult is 1.0 unless swing_regime_aware_exits_
    # enabled is on, which it is not by default.
    if applied_regime_mult != 1.0:
        stall_days = max(2, round(stall_days * applied_regime_mult))
    if (stall_days > 0
            and sessions_held >= stall_days
            and peak_r < policy["stall_peak_r"]
            and gain_r < policy["stall_peak_r"]):
        return {
            "action": "EXIT_STALL",
            "reason": "NEVER_WORKED",
            "detail": (f"{sessions_held} sessions"
                       + (f" ({family}'s own calibrated clock is "
                          f"{stall_days})" if calibrated else
                          f" (>= {stall_days}, the book-wide default)")
                       + f" and the best this ever showed was "
                       f"{peak_r:+.2f}R ({(hwm_px - entry) / entry * 100 if hwm_px else 0:+.2f}%), "
                       f"now {gain_r:+.2f}R. On this book a swing trade that never "
                       f"clears {policy['stall_peak_r']}R wins about one time in six — "
                       f"the capital and the slot are worth more elsewhere"),
            "new_sl": None, "book_qty": 0,
        }

    # ── 5. Time stop — only on positions that are NOT working ────────────────
    if (sessions_held >= policy["time_stop_days"]
            and gain_r < policy["time_stop_min_r"]):
        return {
            "action": "EXIT_TIME",
            "reason": "TIME_EXIT",
            "detail": (f"{sessions_held} sessions held, {gain_r:+.2f}R "
                       f"< {policy['time_stop_min_r']}R — capital better deployed"),
            "new_sl": None, "book_qty": 0,
        }

    return {
        "action": "HOLD", "reason": "holding",
        "detail": (f"{gain_r:+.2f}R ({gain_pct:+.2f}%)"
                   + (" [no planned_stop — 5% fallback risk]" if fallback else "")),
        "new_sl": None, "book_qty": 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL ATTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────

def find_originating_signal(sb, symbol: str, entry_date: str,
                            lookback_days: int = 10) -> dict:
    """
    Locate the signal that produced this entry.

    Searches backwards from entry_date for the most recent actionable signal on
    the symbol. Falls back to any signal (including WATCH) so that a
    discretionary entry still records what the system was saying at the time —
    which is itself valuable: it tells you whether your manual overrides beat
    the system.

    Populating this is the entire reason signal_outcomes exists. All 70
    historical closed_positions rows have signal_date NULL, so no outcome has
    ever been attributable to a signal.
    """
    try:
        start = str(date.fromisoformat(str(entry_date)[:10]) - timedelta(days=lookback_days))
    except Exception:
        return {}

    base = ("id,date,symbol,signal_type,signal_subtype,strategy,sector,"
            "company_name,score_adjusted,regime,sector_rank_at_entry,"
            "entry_timing_type,lifecycle,expected_r_msl,"
            "planned_stop,planned_target,planned_risk_pct,ai_conviction")

    for types in (["PRIME_SETUP", "BREAKOUT_SETUP", "REENTRY_SETUP",
                   "BUY_CANDIDATE", "STAGED_ENTRY", "MOMENTUM_CONTINUATION"], None):
        try:
            q = (sb.table("signal_log").select(base)
                   .eq("symbol", symbol)
                   .gte("date", start).lte("date", str(entry_date)[:10]))
            if types:
                q = q.in_("signal_type", types)
            rows = q.order("date", desc=True).limit(1).execute().data
            if rows:
                return rows[0]
        except Exception as e:
            logger.debug(f"  signal lookup failed for {symbol}: {e}")
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# RECONCILIATION
# ─────────────────────────────────────────────────────────────────────────────

def _log_reconcile(sb, trade_date, symbol, action, **kw):
    if DRY_RUN:
        return
    try:
        sb.table("position_reconcile_log").insert({
            "trade_date": trade_date, "symbol": symbol, "action": action,
            "db_qty":  kw.get("db_qty"),  "broker_qty":   kw.get("broker_qty"),
            "db_price": kw.get("db_price"), "broker_price": kw.get("broker_price"),
            "detail":  kw.get("detail", ""), "resolved": kw.get("resolved", True),
        }).execute()
    except Exception as e:
        logger.debug(f"  reconcile log write failed: {e}")


def open_position_from_holding(sb, holding: dict, trade_date: str) -> bool:
    """Create an open_positions row for a Kite holding the DB does not know about."""
    sym   = holding["symbol"]
    qty   = holding["total_quantity"]
    entry = holding["average_price"]

    sig = find_originating_signal(sb, sym, trade_date)

    # Rank the originating plan, so a position discovered from a broker holding
    # carries the same attribution an auto-entry does. Without it, every
    # manually-entered or reconciled position is invisible to review_ranking() —
    # and those are exactly the discretionary entries most worth comparing the
    # ranking against.
    #
    # RANKED FROM signal_output_daily, NOT from `sig`. find_originating_signal
    # reads signal_log, which carries the attribution keys but NONE of the eight
    # columns score_plan() actually weighs — no final_score, no ai_tier, no
    # implied_rr, no entry_timing_type. Ranking that row returns the function's
    # defaults, which is worse than recording nothing: it looks like a measured
    # rank and every position gets roughly 50, so review_ranking() would compare
    # noise against noise and conclude the weights do not separate winners.
    # signal_output_daily is the immutable plan table and has all of them.
    entry_rationale = None
    plan_row: dict = {}
    try:
        from analysis.entry_ranking import score_plan
        plan = (sb.table("signal_output_daily").select("*")
                  .eq("symbol", sym).lte("date", str(trade_date)[:10])
                  .order("date", desc=True).limit(1).execute().data or [])
        if plan:
            plan_row = plan[0]
            rk = score_plan(plan_row)
            entry_rationale = f"rank {rk.total:.0f} — {rk.why()}"
        else:
            logger.debug(f"  {sym}: no plan row to rank against")
    except Exception as e:
        logger.debug(f"  {sym}: could not rank the originating plan: {e}")

    # Trade plan: prefer the levels the signal was validated against. Only if
    # the signal has none (pre-migration row, or a discretionary entry with no
    # signal at all) do we derive a stop from live ATR.
    planned_stop   = sig.get("planned_stop")
    planned_target = sig.get("planned_target")

    if not planned_stop:
        try:
            from analysis.risk_model import compute_trade_levels
            sd = (sb.table("stock_data_daily")
                    .select("atr_14,supertrend").eq("symbol", sym)
                    .order("date", desc=True).limit(1).execute().data)
            atr = float(sd[0].get("atr_14") or 0) if sd else 0
            if atr > 0:
                lv = compute_trade_levels(
                    entry_price=entry, atr_abs=atr, anchor_price=entry,
                    structure_stop=float(sd[0].get("supertrend") or 0) or None,
                    regime=sig.get("regime") or "NEUTRAL")
                if lv.valid:
                    planned_stop, planned_target = lv.stop, lv.target
        except Exception as e:
            logger.warning(f"  {sym}: could not derive trade plan: {e}")

    row = {
        "symbol":            sym,
        "company_name":      sig.get("company_name"),
        "sector":            sig.get("sector"),
        "strategy":          sig.get("strategy") or sig.get("signal_type"),
        "entry_date":        trade_date,
        "entry_price":       entry,
        "kite_avg_price":    entry,
        "actual_qty":        qty,
        "original_qty":      qty,
        "current_qty":       qty,
        "kite_qty":          qty,
        "invested_value":    round(qty * entry, 2),
        "current_price":     holding.get("last_price") or entry,
        "high_water_mark":   max(entry, holding.get("last_price") or entry),
        "status":            "ACTIVE",
        "source":            "kite",
        # Frozen trade plan
        "planned_stop":      planned_stop,
        "planned_target":    planned_target,
        "active_sl":         planned_stop,
        "target_price":      planned_target,
        "sl_type":           "ATR_PLANNED" if planned_stop else None,
        "expected_r_at_entry": sig.get("expected_r_msl"),
        # Attribution — the whole point
        "signal_id":         sig.get("id"),
        "signal_date":       sig.get("date"),
        "signal_subtype":    sig.get("signal_subtype"),
        "entry_signal_type": sig.get("signal_type"),
        # SOURCED FROM plan_row (signal_output_daily), NOT sig (signal_log) —
        # 07-Aug-2026. `sig` carries the attribution keys but not reliably the
        # eight columns score_plan() weighs (see the comment above this
        # function on why entry_rationale itself had to be re-fetched from
        # signal_output_daily rather than trusted from sig). Confirmed live:
        # PPLPHARMA's reconciled row had entry_timing_type populated from
        # sig, CIPLA's and ETERNAL's did not, on the same code path — an
        # inconsistency that reading from the immutable plan table the way
        # entry_rationale already does removes rather than papers over.
        "entry_timing_type": plan_row.get("entry_timing_type") or sig.get("entry_timing_type"),
        "lifecycle_at_entry": sig.get("lifecycle"),
        "regime_at_entry":   sig.get("regime"),
        "sector_rank_at_entry": plan_row.get("sector_rank_at_entry") or sig.get("sector_rank_at_entry"),
        "entry_rationale":   entry_rationale,
        "max_favorable_excursion": 0.0,
        "max_adverse_excursion":   0.0,
        "reconcile_status":  "SYNCED",
        "last_reconciled_at": datetime.now(IST).isoformat(),
        "synced_at":         datetime.now(IST).isoformat(),
    }

    if DRY_RUN:
        logger.info(f"[DRY RUN] would OPEN {sym} qty={qty} @ {entry:.2f} "
                    f"sl={planned_stop} tgt={planned_target} signal={sig.get('id')}")
        return True

    try:
        _upsert_position(sb, row)
        logger.success(
            f"  OPENED {sym:<12} qty={qty:<5} @ Rs.{entry:.2f}  "
            f"sl={planned_stop or '-'}  tgt={planned_target or '-'}  "
            f"signal={'#' + str(sig.get('id')) if sig.get('id') else 'NONE (discretionary)'}"
        )
        _log_reconcile(sb, trade_date, sym, "OPENED", broker_qty=qty,
                       broker_price=entry,
                       detail=f"signal_id={sig.get('id')} type={sig.get('signal_type')}")
        return True
    except Exception as e:
        logger.error(f"  {sym}: open_positions insert failed: {e}")
        return False


def close_position(sb, pos: dict, exit_price: float, exit_reason: str,
                   detail: str, trade_date: str, source: str = "kite") -> bool:
    """Move an open position into closed_positions with full outcome metrics."""
    # DIRECTION-AWARE, and this is the one place in the whole spine that was
    # not. (exit_price - entry) * qty is a LONG's P&L; for a SHORT it is that
    # P&L's exact negative, so every short closed through this function had
    # its sign flipped — a real loss recorded as a gain. It went unnoticed
    # because it could not fire: the intraday runway gate refused every short
    # before this morning's fix, so SAPPHIRE (2026-08-06) is the first short
    # this system has ever closed. A scan of all 102 rows in closed_positions
    # for the short-shaped stop (planned_stop_at_entry > entry_price) found
    # exactly that one match — the blast radius is one row.
    from intraday import direction as D
    sym   = pos["symbol"]
    d     = D.normalise(pos.get("direction"))
    entry = float(pos.get("entry_price") or 0)
    qty   = int(pos.get("current_qty") or pos.get("actual_qty") or 0)
    if not entry or not exit_price:
        logger.warning(f"  {sym}: cannot close — entry={entry} exit={exit_price}")
        return False

    # Partial bookings already realised are folded into total P&L so the
    # recorded outcome reflects the whole trade, not just the final tranche.
    booked_qty   = int(pos.get("partial_booked_qty") or 0)
    booked_price = float(pos.get("partial_booked_price") or 0)
    booked_pnl   = (D.sign(d) * (booked_price - entry) * booked_qty
                    if booked_qty and booked_price else 0.0)

    final_pnl    = D.sign(d) * (exit_price - entry) * qty
    # GROSS, deliberately. 70 trades closed before charges were recorded and
    # their costs cannot be reconstructed; redefining this column would make old
    # and new rows incomparable while looking identical. Charges are recorded
    # separately and netted where they are known.
    realized_pnl = round(final_pnl + booked_pnl, 2)

    # Round trip = the entry leg already carried on the position, plus the exit
    # leg priced now. Slippage is excluded on both: for paper it is inside the
    # fill price, for live it is inside the actual traded price.
    entry_charges = float(pos.get("charges") or 0.0)
    exit_charges = 0.0
    try:
        # PRODUCT-AWARE, and the sell leg priced as a sell leg.
        #
        # This used to compute the full round trip on MIS rates and halve the
        # statutory total. Two errors compounded: a CNC position was priced as
        # intraday (no DP, 0.025% STT instead of 0.1% on both legs), and halving
        # charged the exit half an STT it owes in full plus half a stamp duty it
        # does not owe at all. On a Rs 2,900 CNC exit that recorded about Rs 1.60
        # against a true Rs 18.09 — so every closed swing row understated its
        # charges by roughly a tenth of the per-trade risk budget.
        from intraday.cost_model import exit_leg
        exit_charges = exit_leg(float(exit_price), int(qty),
                                product=(pos.get("product") or "CNC"))
    except Exception:
        exit_charges = 0.0
    total_charges = round(entry_charges + exit_charges, 2) if (entry_charges or exit_charges) else None
    total_qty    = qty + booked_qty
    invested     = entry * total_qty
    pnl_pct      = round(realized_pnl / invested * 100, 3) if invested else 0.0

    # risk_per_share, not the long-only `entry - stop0 if stop0 < entry`: that
    # guard reads a short's correctly-placed stop (above entry, so
    # entry - stop0 is negative) as invalid and discards it, rather than
    # recognising the other side. risk_per_share returns 0.0 for a stop that
    # is genuinely on the wrong side regardless of direction, which is the
    # one case this must still refuse.
    stop0 = float(pos.get("planned_stop") or 0)
    risk  = D.risk_per_share(entry, stop0, d)
    r_mult = round((realized_pnl / total_qty) / risk, 3) if (risk > 0 and total_qty) else None

    hold_days = None
    try:
        hold_days = (date.fromisoformat(trade_date)
                     - date.fromisoformat(str(pos["entry_date"])[:10])).days
    except Exception:
        pass

    closed = {
        "symbol":           sym,
        "company_name":     pos.get("company_name"),
        "sector":           pos.get("sector"),
        "pick_label":       pos.get("pick_label"),
        "strategy":         pos.get("strategy"),
        # Carry mode and framework through. Both columns DEFAULT to LIVE/SWING,
        # so omitting them silently recorded every paper intraday trade as a
        # live swing one — contaminating the real performance record with
        # simulated results, which is the one thing paper mode must never do.
        "mode":             pos.get("mode") or "LIVE",
        "framework":        pos.get("framework") or "SWING",
        "product":          pos.get("product") or "CNC",
        # Migration 051. Without this a closed row cannot be told apart from a
        # long after the fact — which is exactly why correcting SAPPHIRE's
        # existing row needed the alert log instead of just reading the row.
        "direction":        d,
        "intraday_strategy": pos.get("intraday_strategy"),
        # THE CONDITION, NOT JUST THE FAMILY — 22-Aug-2026, mirrors why
        # `pick_label`/`entry_rationale` are carried through above rather
        # than left to be reconstructed later: closed_positions is the one
        # table the dashboard's StrategyBreakdown panel actually reads, and
        # it has never been able to tell SDN's 83%-win VREJ condition apart
        # from its 8%-win BRKD one, because open_positions never carried
        # this field to lose in the first place. None on rows opened before
        # this column existed and on every SWING row (sub_engine is an
        # intraday-only vocabulary) — both read back as `strategy` alone.
        "sub_engine":       pos.get("sub_engine"),
        "entry_date":       pos.get("entry_date"),
        "entry_price":      entry,
        "actual_qty":       total_qty,
        "proposed_qty":     pos.get("proposed_qty"),
        "invested_value":   round(invested, 2),
        "exit_date":        trade_date,
        "exit_price":       exit_price,
        "exit_value":       round(exit_price * qty + booked_price * booked_qty, 2),
        "realized_pnl":     realized_pnl,
        # Round-trip cost. NULL rather than 0.0 when unknown, because "no
        # charges" and "charges not recorded" must not read the same — a zero
        # here would silently claim a free trade.
        "charges":          total_charges,
        "pnl_pct":          pnl_pct,
        "exit_reason":      exit_reason,
        "exit_reason_detail": detail,
        "hold_days":        hold_days,
        "r_multiple":       r_mult,
        "high_water_mark":  pos.get("high_water_mark"),
        "max_favorable_excursion": pos.get("max_favorable_excursion"),
        "max_adverse_excursion":   pos.get("max_adverse_excursion"),
        # Attribution — carried through so post_trade_analysis and
        # performance_tracker can finally join outcomes back to signals.
        "signal_id":        pos.get("signal_id"),
        "signal_date":      pos.get("signal_date"),
        "entry_signal_type": pos.get("entry_signal_type"),
        "entry_timing_type": pos.get("entry_timing_type"),
        "lifecycle_at_entry": pos.get("lifecycle_at_entry"),
        "regime_at_entry":  pos.get("regime_at_entry"),
        "sector_rank_at_entry": pos.get("sector_rank_at_entry"),
        "reentry_mode":     pos.get("reentry_mode"),
        "expected_r_at_entry":     pos.get("expected_r_at_entry"),
        "planned_stop_at_entry":   pos.get("planned_stop"),
        "planned_target_at_entry": pos.get("planned_target"),
        "partial_bookings": pos.get("partial_bookings"),
        # THE RANK THIS POSITION WAS CHOSEN ON.
        #
        # Written at entry by both the live and paper swing paths, and dropped
        # here — so it never reached closed_positions, which is the only table
        # weekly_review.review_ranking() reads. That function exists to answer
        # "do higher-ranked picks actually do better", and it opens with
        #     if "rank" not in raw or r.get("r_multiple") is None: continue
        # against a column that was structurally always NULL. It could not
        # produce a verdict for any book, in any window, ever — it reported
        # "0 ranked trades closed, needs ~6" indefinitely, which reads like
        # waiting for data rather than a broken join.
        #
        # A check that cannot fail is not a check. entry_ranking has eight
        # weighted components and a configurable weight on each; without this
        # line there was no way to discover that any of them were decoration.
        "entry_rationale":  pos.get("entry_rationale"),
        "source":           source,
    }

    if DRY_RUN:
        logger.info(f"[DRY RUN] would CLOSE {sym} @ {exit_price:.2f} "
                    f"{pnl_pct:+.2f}% ({r_mult:+.2f}R) — {exit_reason}")
        return True

    try:
        try:
            sb.table("closed_positions").insert(closed).execute()
        except Exception as e:
            # Migration 025 adds closed_positions.charges. If the code is ahead
            # of the schema, close the trade WITHOUT the cost figure rather than
            # not at all: a position that cannot be recorded as closed stays
            # open forever in the book, and losing one optional column is
            # trivially recoverable next to that.
            if "23505" in str(e):
                # closed_positions is unique on (symbol, entry_date, exit_date),
                # so a symbol entered AND closed twice in one day cannot record
                # its second round trip. The insert failed, the delete below
                # never ran, and the position stayed open — so the exit fired
                # again 15s later, and again, forever. ZEEL did this 23 times on
                # 31 Jul and was still open at the close.
                #
                # Fold the second leg into the existing row rather than dropping
                # it. Same symbol, same day, same session: the aggregate is the
                # honest outcome, and an aggregated trade is far better than a
                # phantom position the square-off can never clear.
                prior = (sb.table("closed_positions").select("*")
                         .eq("symbol", sym).eq("entry_date", closed.get("entry_date"))
                         .eq("exit_date", closed.get("exit_date")).execute().data or [])
                if not prior:
                    raise
                p0 = prior[0]
                agg_pnl = float(p0.get("realized_pnl") or 0) + float(closed.get("realized_pnl") or 0)
                # invested_value and pnl_pct were frozen at leg 1's own numbers
                # here — this merge touched realized_pnl but not the two fields
                # it is a percentage OF, so a two-leg SAPPHIRE showed a pnl_pct
                # computed from HALF its true invested value. Aggregate both,
                # the same way realized_pnl already was.
                agg_inv = float(p0.get("invested_value") or 0) + float(closed.get("invested_value") or 0)
                merged = {
                    "realized_pnl":   agg_pnl,
                    "charges":        float(p0.get("charges") or 0) + float(closed.get("charges") or 0),
                    "actual_qty":     int(p0.get("actual_qty") or 0) + int(closed.get("actual_qty") or 0),
                    "invested_value": round(agg_inv, 2),
                    "pnl_pct":        round(agg_pnl / agg_inv * 100, 3) if agg_inv else None,
                    "exit_reason":  "MULTI_LEG",
                    "exit_reason_detail": (f"{p0.get('exit_reason')} then "
                                           f"{closed.get('exit_reason')} — two round trips "
                                           f"in {sym} on {closed.get('exit_date')}, aggregated"),
                }
                # r_multiple has no single denominator across two legs with
                # different entries/stops, so there is no exact aggregate R —
                # a qty-weighted blend of each leg's own R is the closest
                # honest answer, and strictly better than the None a merge
                # left behind before (leg 1's own R, silently dropped here).
                r0, q0 = p0.get("r_multiple"), int(p0.get("actual_qty") or 0)
                r1, q1 = closed.get("r_multiple"), int(closed.get("actual_qty") or 0)
                if r0 is not None and r1 is not None and (q0 + q1):
                    merged["r_multiple"] = round((float(r0) * q0 + float(r1) * q1) / (q0 + q1), 3)
                sb.table("closed_positions").update(merged).eq("id", p0["id"]).execute()
                logger.warning(f"  {sym}: second round trip today folded into the "
                               f"existing outcome row (total P&L "
                               f"Rs.{merged['realized_pnl']:+.2f}). Re-entry into a "
                               f"name that already traded today is blocked by "
                               f"intraday_block_reentry_after_loss.")
            elif "charges" in str(e):
                logger.warning("  closed_positions.charges is missing — apply "
                               "migration 025. Closing without the cost figure.")
                sb.table("closed_positions").insert(
                    {k: v for k, v in closed.items() if k != "charges"}).execute()
            elif "direction" in str(e):
                # Ships ahead of its own migration on purpose: the P&L and R
                # fixes above must not wait on a column, only the AUDIT TRAIL
                # of which side each row was does. Degrades to recording the
                # right numbers under the wrong-shaped schema, once, until
                # migration 051 lands — never to the old sign-flipped math.
                logger.warning("  closed_positions.direction is missing — apply "
                               "migration 051. Closing without recording direction "
                               "(the P&L and R above are still correct).")
                sb.table("closed_positions").insert(
                    {k: v for k, v in closed.items() if k != "direction"}).execute()
            else:
                raise
        # KEYED ON (symbol, product), like every other write since migration 028.
        # This deleted on symbol alone, so closing the intraday MIS tranche of a
        # name would also delete the swing CNC position in it — the book would
        # lose a live holding with a real GTT behind it, and reconcile would then
        # "discover" it in Kite and re-open it with a guessed stop.
        (sb.table("open_positions").delete()
           .eq("symbol", sym).eq("product", pos.get("product") or "CNC").execute())
        icon = "🟢" if realized_pnl > 0 else "🔴"
        logger.success(
            f"  {icon} CLOSED {sym:<12} @ Rs.{exit_price:.2f}  "
            f"{pnl_pct:+.2f}%  ({r_mult if r_mult is not None else '?'}R)  "
            f"{hold_days}d  — {exit_reason}"
        )
        _log_reconcile(sb, trade_date, sym, "CLOSED", db_qty=qty,
                       broker_qty=0, broker_price=exit_price, detail=detail)
        return True
    except Exception as e:
        logger.error(f"  {sym}: close failed: {e}")
        return False


def _mirror_qty_drift(pos: dict, br_qty: int) -> dict:
    """
    PURE. Which of actual_qty/kite_qty disagree with the broker's real
    quantity, extracted out of reconcile_with_broker()'s "current_qty
    already matches" fast path so it is testable without that function's
    kite_client/holdings/multi-table I/O (11-Aug-2026).

    Only called from the branch where current_qty ALREADY equals br_qty —
    this is deliberately not a general "does everything match" check, it
    is specifically the second half of that question: current_qty being
    right does not mean actual_qty and kite_qty are too, and a write path
    that shrinks current_qty alone (engine.py's BOOK_PARTIAL, before this
    session's fix) can leave them stuck at a stale value indefinitely,
    because the ONLY branch that ever re-reads the broker for this
    position is the one this function guards — and it used to stop
    looking the moment current_qty agreed.

    Returns a patch dict containing only the fields that actually need to
    change — empty when both already agree, so a caller can use `if drift`
    to decide whether anything happened.
    """
    drift = {}
    if int(pos.get("actual_qty") or 0) != br_qty:
        drift["actual_qty"] = br_qty
    if int(pos.get("kite_qty") or 0) != br_qty:
        drift["kite_qty"] = br_qty
    return drift


def reconcile_with_broker(sb, trade_date: str) -> dict:
    """
    Kite holdings are the truth. Bring open_positions into agreement.

    Four divergence classes:
      in Kite, not in DB      -> OPENED        (entry made outside the framework,
                                                or the framework's own fill)
      in DB, not in Kite      -> CLOSED        (position was sold)
      qty(Kite) < qty(DB)     -> QTY_REDUCED   (partial exit)
      qty(Kite) > qty(DB)     -> QTY_INCREASED (added to the position)
    """
    from kite.kite_client import fetch_holdings, is_available

    if not is_available():
        logger.info("  Kite unavailable — skipping broker reconciliation "
                    "(Telegram-reported positions remain authoritative)")
        return {"status": "no_broker", "opened": 0, "closed": 0, "adjusted": 0}

    holdings = fetch_holdings()
    # KEYED ON (symbol, product), matching open_positions since migration 028.
    #
    # holdings() is delivery only, so everything it returns is CNC. Day
    # positions below carry their own product and are keyed on it, which is what
    # lets a swing CNC core and an intraday MIS tranche in the same name be
    # reconciled independently — and is the entire reason the schema change did
    # not need a fill-attribution layer. Kite separates them; this follows.
    hold_map = {(h["symbol"], "CNC"): h for h in holdings}

    # PAPER positions are excluded, and this is not a refinement.
    #
    # Broker holdings are the truth for positions the broker HAS. A simulated
    # position is deliberately absent from Kite, so the "in DB, not in Kite ->
    # CLOSED" rule reads every paper trade as sold and closes it at its entry
    # price for a flat 0.00% — silently destroying the open paper book on the
    # first reconcile after it was created. Three intraday paper positions were
    # wiped exactly this way before the filter existed.
    #
    # Paper positions are closed by the exit engine and by square_off_paper(),
    # both of which know they are simulated.
    #
    # ACTIVE and CLOSING, not ACTIVE alone. intraday/engine.py writes
    # status='CLOSING' the moment a live exit order is PLACED — optimistic,
    # before the fill is confirmed, so the same exit is not re-derived and
    # re-sent next cycle (see the comment where it is written). Nothing ever
    # read CLOSING back out again: this function is the one place a sold
    # position gets its Kite fill looked up and moved into closed_positions,
    # and it only ever asked for ACTIVE. Three real positions — TRAVELFOOD,
    # KIMS (2026-08-06) and BHEL (2026-08-03) — were sold, confirmed gone
    # from holdings, and then permanently invisible to this function: not
    # ACTIVE, so never reconciled; not in closed_positions, so no realized
    # P&L was ever recorded for a real sale. Not a race that self-corrects —
    # every reconcile since has skipped them the same way.
    # CNC ONLY. holdings() is delivery-only (see hold_map above) — an MIS
    # position never appears there, live or dead, so "not in holdings = sold"
    # is not wrong-but-approximate for MIS, it is inapplicable. mode!=PAPER
    # already excludes every intraday row today because intraday is 100%
    # PAPER — but that is a coincidence of intraday not being live yet, not a
    # guarantee. The day intraday goes LIVE its rows would carry mode='LIVE'
    # too, and without this filter this function would read every open MIS
    # position as "vanished from holdings" and close it mid-session. Filtered
    # explicitly rather than relying on the paper/live axis to keep doing it
    # by accident.
    db_rows = [r for r in ((sb.table("open_positions").select("*")
                              .in_("status", ["ACTIVE", "CLOSING"]).execute().data) or [])
               if (r.get("mode") or "LIVE").upper() != "PAPER"
               and (r.get("product") or "CNC").upper() == "CNC"]

    # A CNC BUY FILLED TODAY IS NOT IN holdings() YET.
    #
    # Kite moves a delivery purchase into holdings on T+1. Until then it lives
    # only in positions(), so "in DB, not in Kite" is true of every position the
    # system opened this morning — and this function would close all of them as
    # BROKER_EXIT at their entry price, minutes after buying them.
    #
    # CIPLA, ETERNAL and GABRIEL were bought at 14:28 today and appear in
    # positions() with product=CNC while holdings() shows only BHEL. Reconciling
    # without this would have wiped three real positions and left the account
    # holding stock the book had no record of.
    try:
        from kite.kite_client import fetch_positions
        day_qty = {}
        for p in (fetch_positions().get("net") or []):
            q = int(p.get("quantity") or 0)
            if q > 0:
                # Carry the REAL average price. It was dropped, so a position
                # adopted from day positions opened at entry_price 0.00 — which
                # makes the R multiple, the P&L and every rung of the exit ladder
                # meaningless, on a position the system just bought.
                day_qty[(p.get("tradingsymbol"),
                         (p.get("product") or "CNC").upper())] = (
                    q, float(p.get("average_price") or p.get("last_price") or 0))
    except Exception as e:
        # Cannot see today's fills -> cannot safely decide anything is gone.
        logger.warning(f"  reconcile: day positions unavailable ({e}) — "
                       f"skipping to avoid closing settled-tomorrow buys")
        return {"status": "no_day_positions", "opened": 0, "closed": 0, "adjusted": 0}

    for key, (q, avg) in day_qty.items():
        if key not in hold_map:
            # Same shape fetch_holdings() returns, total_quantity included —
            # callers below read that key and a partial dict would raise here
            # rather than at the boundary where it was built.
            hold_map[key] = {"symbol": key[0], "quantity": q, "t1_quantity": 0,
                             "total_quantity": q, "average_price": avg,
                             "last_price": avg, "product": key[1],
                             "pnl": 0.0, "close_price": 0.0, "day_change_pct": 0.0}
    db_map  = {(r["symbol"], (r.get("product") or "CNC").upper()): r
               for r in db_rows}

    logger.info(f"  Reconcile: broker={len(hold_map)} holdings | db={len(db_map)} open")

    opened = closed = adjusted = 0

    # ── In Kite, not in DB ───────────────────────────────────────────────────
    for (sym, _prod), h in hold_map.items():
        if (sym, _prod) not in db_map:
            if open_position_from_holding(sb, h, trade_date):
                opened += 1

    # ── In DB, not in Kite -> sold ───────────────────────────────────────────
    if cfg_bool("position_auto_close", True):
        from kite.kite_client import fetch_trades
        trades = fetch_trades()
        sell_map = {t["symbol"]: t for t in trades if t["transaction_type"] == "SELL"}

        for (sym, _prod), pos in db_map.items():
            if (sym, _prod) in hold_map:
                continue
            # Exact fill price when the sale happened today; otherwise fall back
            # to the last known price and say so, rather than inventing one.
            sale = sell_map.get(sym)
            if sale:
                exit_price, detail = sale["price"], f"Kite SELL fill at {sale['timestamp']}"
            else:
                exit_price = float(pos.get("current_price") or pos.get("entry_price") or 0)
                detail = ("no same-day SELL trade found — exit price approximated "
                          "from last known price; Kite exposes trades for the "
                          "current day only")
            if exit_price:
                if close_position(sb, pos, exit_price,
                                  pos.get("exit_signal") or "BROKER_EXIT",
                                  detail, trade_date):
                    closed += 1

    # ── Backfill the trade plan on pre-existing positions ────────────────────
    # A position that already existed in open_positions (entered manually, or
    # created before migration 003) never passes through
    # open_position_from_holding, so planned_stop / planned_target stay NULL.
    # evaluate_exit then falls back to a generic 5%-of-entry risk assumption:
    # the position is nominally managed but every R multiple, the partial-book
    # trigger and the trail are computed off a number nobody chose.
    # Observed live: both open positions had sl/tgt/R/signal_id all NULL after
    # a successful reconcile.
    for (sym, _prod), pos in db_map.items():
        if pos.get("planned_stop"):
            continue
        h = hold_map.get((sym, _prod))
        entry = float(pos.get("entry_price") or (h or {}).get("average_price") or 0)
        if not entry:
            continue
        sig = find_originating_signal(sb, sym, str(pos.get("entry_date") or trade_date))
        stop, target = sig.get("planned_stop"), sig.get("planned_target")
        if not stop:
            try:
                from analysis.risk_model import compute_trade_levels
                sd = (sb.table("stock_data_daily")
                        .select("atr_14,supertrend").eq("symbol", sym)
                        .order("date", desc=True).limit(1).execute().data)
                atr = float(sd[0].get("atr_14") or 0) if sd else 0
                if atr > 0:
                    lv = compute_trade_levels(
                        entry_price=entry, atr_abs=atr, anchor_price=entry,
                        structure_stop=float(sd[0].get("supertrend") or 0) or None,
                        regime=sig.get("regime") or "NEUTRAL")
                    if lv.valid:
                        stop, target = lv.stop, lv.target
            except Exception as e:
                logger.warning(f"  {sym}: trade-plan backfill failed: {e}")
        if not stop:
            continue

        patch = {
            "planned_stop":   stop,
            "planned_target": target,
            "target_price":   pos.get("target_price") or target,
            "sl_type":        pos.get("sl_type") or "ATR_BACKFILLED",
            # Only seed active_sl if nothing is protecting the position yet —
            # never loosen a stop the operator has already tightened.
            "active_sl":      pos.get("active_sl") or stop,
            "signal_id":      pos.get("signal_id")   or sig.get("id"),
            "signal_date":    pos.get("signal_date") or sig.get("date"),
            "entry_signal_type":  pos.get("entry_signal_type")  or sig.get("signal_type"),
            "regime_at_entry":    pos.get("regime_at_entry")    or sig.get("regime"),
            "high_water_mark":    pos.get("high_water_mark")    or entry,
        }
        if DRY_RUN:
            logger.info(f"[DRY RUN] would backfill plan for {sym}: sl={stop} tgt={target}")
        else:
            try:
                _patch_position(sb, pos, patch)
                logger.success(
                    f"  PLAN BACKFILLED {sym:<12} entry={entry:.2f} "
                    f"sl={stop} tgt={target} "
                    f"signal={'#' + str(sig.get('id')) if sig.get('id') else 'none'}"
                )
                _log_reconcile(sb, trade_date, sym, "PLAN_BACKFILL",
                               db_price=entry,
                               detail=f"stop={stop} target={target}")
            except Exception as e:
                logger.warning(f"  {sym}: plan backfill write failed: {e}")

    # ── Backfill identity fields from the originating signal ─────────────────
    # Deliberately a SEPARATE pass, not part of the block above. That one exits
    # early on `if pos.get("planned_stop"): continue`, so the moment a position
    # has a stop it can never gain anything else — PPLPHARMA carried
    # signal_id=5038 and strategy=None indefinitely, and the digest rendered it
    # as "PPLPHARMA [None] [None]" beside "BHEL [CTL+MOM+SEC]".
    #
    # Cosmetic in the alert, but not only cosmetic: strategy is what attributes
    # a realised outcome back to the engine that produced it. A position that
    # closes with strategy NULL cannot be credited to CTL, SBS or anything else,
    # so the engine scoring never learns from it.
    # Candidate fields, intersected with what open_positions ACTUALLY has.
    # db_map rows come from select("*"), so their keys are the live schema —
    # deriving the list from them beats hardcoding it, which is how `industry`
    # (a signal_log column that open_positions does not have) got in here and
    # made PostgREST reject the whole update.
    _IDENTITY_CANDIDATES = ("strategy", "signal_subtype", "sector", "company_name")
    _sample = next(iter(db_map.values()), {})
    _IDENTITY = tuple(f for f in _IDENTITY_CANDIDATES if f in _sample)
    for (sym, _prod), pos in db_map.items():
        missing = [f for f in _IDENTITY if not pos.get(f)]
        if not missing or not pos.get("signal_id"):
            continue
        try:
            sig_rows = (sb.table("signal_log").select(",".join(("id",) + _IDENTITY))
                          .eq("id", pos["signal_id"]).limit(1).execute().data) or []
            if not sig_rows:
                continue
            patch = {f: sig_rows[0].get(f) for f in missing if sig_rows[0].get(f)}
            if not patch:
                continue
            if DRY_RUN:
                logger.info(f"[DRY RUN] would backfill identity for {sym}: {patch}")
                continue
            _patch_position(sb, pos, patch)
            logger.success(f"  IDENTITY BACKFILLED {sym:<12} "
                           + ", ".join(f"{k}={v}" for k, v in patch.items())
                           + f" (from signal #{pos['signal_id']})")
        except Exception as e:
            logger.warning(f"  {sym}: identity backfill failed: {e}")

    # ── Quantity drift ───────────────────────────────────────────────────────
    for (sym, _prod), pos in db_map.items():
        h = hold_map.get((sym, _prod))
        if not h:
            continue
        db_qty, br_qty = int(pos.get("current_qty") or 0), h["total_quantity"]
        if db_qty == br_qty:
            # Agreement is a RESULT and has to be recorded as one. Skipping the
            # write left whatever drift status was set on the last mismatch
            # sitting on the row forever, so a position that reconciled cleanly
            # weeks ago still reported "QTY_INCREASED — Kite shows 15, book
            # shows 15" on the dashboard: a broker alarm contradicted by the
            # very numbers printed next to it.
            #
            # ALSO RE-SYNCS actual_qty/kite_qty HERE, NOT JUST current_qty —
            # 11-Aug-2026. This branch used to check ONLY current_qty against
            # the broker and, on agreement, write nothing but the status —
            # correct as far as it went, but current_qty is not the only
            # column that claims to hold the broker's quantity. A BOOK_PARTIAL
            # fill (engine.py) shrinks current_qty directly and is the reason
            # this branch is even reachable with db_qty==br_qty in the first
            # place — PPLPHARMA booked 5 of 11 on 10-Aug, current_qty went to
            # 6, the broker's real holding was also 6, so this branch declared
            # MATCHED... while actual_qty and kite_qty, never touched by that
            # write, sat at the pre-partial 11 PERMANENTLY, because the one
            # path that could have corrected them concluded there was nothing
            # to correct. The dashboard's own mismatch banner is gated on
            # reconcile_status != 'MATCHED', so a false MATCHED here is not a
            # cosmetic miss — it is the thing that made kite_qty=11 sitting
            # next to current_qty=6 invisible. engine.py's BOOK_PARTIAL write
            # is fixed to keep all three fields together from now on; this is
            # the second line of defence for any other path that repeats that
            # mistake, and for rows already-drifted before that fix landed.
            drift = _mirror_qty_drift(pos, br_qty)
            if pos.get("reconcile_status") != "MATCHED" or drift:
                if not DRY_RUN:
                    try:
                        _patch_position(sb, pos, {
                            **drift,
                            "reconcile_status":   "MATCHED",
                            "last_reconciled_at": datetime.now(IST).isoformat(),
                        })
                        if drift:
                            logger.info(f"  {sym}: actual_qty/kite_qty resynced "
                                       f"to {br_qty} (current_qty already agreed "
                                       f"with the broker; the mirror fields had "
                                       f"not) — {drift}")
                    except Exception as e:
                        logger.warning(f"  {sym}: reconcile clear failed: {e}")
            continue
        action = "QTY_REDUCED" if br_qty < db_qty else "QTY_INCREASED"

        patch = {
            "current_qty":    br_qty,
            "kite_qty":       br_qty,
            "actual_qty":     br_qty,
            "kite_avg_price": h["average_price"],
            "invested_value": round(br_qty * h["average_price"], 2),
            "reconcile_status": action,
            "last_reconciled_at": datetime.now(IST).isoformat(),
        }

        # A reduction IS the partial the exit engine asked for, arriving after
        # you executed it. Nothing recorded it: BOOK_PARTIAL wrote only the
        # instruction, and this branch only shrank the quantity. But
        # close_position READS partial_booked_qty/price to fold realised partial
        # profit into the final outcome — so booking 7 of 15 into a winner and
        # closing the rest recorded the P&L of 8 shares and silently discarded
        # the profit on the 7. The learning loop then studied a smaller win than
        # actually happened, on the trades the policy is designed to produce.
        if action == "QTY_REDUCED":
            sold = db_qty - br_qty
            prior_qty = int(pos.get("partial_booked_qty") or 0)
            # Broker fill first; the holding's last_price only as a fallback,
            # which the else-branch below reports rather than hides.
            price = _sell_price_today(sym) or float(h.get("last_price") or 0) or None
            if price:
                prior_price = float(pos.get("partial_booked_price") or 0)
                # Weighted average across multiple partials, so a second
                # reduction does not overwrite the first one's price.
                total_qty = prior_qty + sold
                patch["partial_booked_qty"] = total_qty
                patch["partial_booked_price"] = round(
                    ((prior_price * prior_qty) + (price * sold)) / total_qty, 2)
                patch["partial_booked_at"] = datetime.now(IST).isoformat()
                logger.info(f"  PARTIAL RECORDED {sym}: {sold} sh @ ~{price:.2f} "
                            f"(cumulative {total_qty})")
            else:
                logger.warning(
                    f"  {sym}: {sold} sh left the account but no sell price could be "
                    f"determined — partial profit will be missing from the closed "
                    f"outcome. Check Kite trades for today."
                )

        if not DRY_RUN:
            try:
                _patch_position(sb, pos, patch)
            except Exception as e:
                logger.warning(f"  {sym}: qty sync failed: {e}")
                continue
        logger.info(f"  {action}: {sym} db={db_qty} -> broker={br_qty}")
        _log_reconcile(sb, trade_date, sym, action, db_qty=db_qty,
                       broker_qty=br_qty, broker_price=h["average_price"],
                       detail=f"quantity drift resolved in favour of broker")
        adjusted += 1

    return {"status": "ok", "opened": opened, "closed": closed, "adjusted": adjusted}


# ─────────────────────────────────────────────────────────────────────────────
# LIVE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def session_dates(sb, since: str) -> list[str]:
    """
    Every trading session on or after `since`, as ISO date strings.

    Split out from _sessions_held so the LIVE DAEMON can hold one cached copy
    for the day instead of issuing a database read per position per 15-second
    cycle. `market_regime` has exactly one row per session, which is what makes
    it the calendar of record here.
    """
    try:
        rows = (sb.table("market_regime").select("date")
                  .gte("date", str(since)[:10])
                  .order("date").execute().data) or []
        return [str(r["date"])[:10] for r in rows]
    except Exception:
        return []


def sessions_between(dates: list[str], entry_date: str, trade_date: str) -> int:
    """
    Sessions held, counted from a calendar. Pure — no I/O — so both callers of
    evaluate_exit() can reach the same answer without both hitting the database.

    ONE QUANTITY, ONE DEFINITION — 18-Aug-2026. This function exists because
    there were two. The pipeline counted trading sessions; intraday/engine.py's
    live loop counted CALENDAR days off datetime.now(). GABRIEL, entered 06-Aug
    and exited 17-Aug, was reported to the operator as "11 sessions" when it had
    been held for 8 — so `exit_stall_days = 10`, calibrated in sessions, was
    really firing at 7 sessions in the only process that ever fires it.

    A gate and the thing it gates must be the same quantity; a gate reached
    through two callers must be the same quantity in both.
    """
    e, t = str(entry_date)[:10], str(trade_date)[:10]
    if not dates:
        # A missing calendar must not silently become a DIFFERENT clock. Fall
        # back to calendar days, which is the only thing left, but understate
        # rather than overstate: five calendar days are at most five sessions
        # and usually fewer, and an exit that fires late is recoverable where
        # one that fires early on bad arithmetic is not.
        try:
            gap = (date.fromisoformat(t) - date.fromisoformat(e)).days
            return max(0, int(gap * 5 / 7))
        except Exception:
            return 0
    return sum(1 for d in dates if e < d <= t)


def _sessions_held(sb, entry_date: str, trade_date: str) -> int:
    """Trading sessions between two dates — calendar days badly overstate a
    time stop across weekends and NSE holidays."""
    return sessions_between(
        session_dates(sb, str(entry_date)[:10]), entry_date, trade_date)


def manage_open_positions(sb, trade_date: str, require_live: bool = False) -> dict:
    """
    Apply the exit policy to every open position at the current price.

    require_live=True (intraday runs) refuses to proceed on stale prices.

    WHY THAT MATTERS. The EOD fallback below reads stock_data_daily for
    trade_date. During market hours on a trading day, bhavcopy has not been
    written yet, so get_trade_date resolves to the PREVIOUS session and those
    "closes" are yesterday's. Evaluating stops against yesterday's close at
    11:00 is not a degraded check, it is a wrong one: it would miss a real
    intraday breach and re-fire yesterday's alerts every 30 minutes.
    The fallback is correct for the evening pipeline, where trade_date IS the
    session that just closed. It is never correct intraday.
    """
    # Kite market-data endpoints (ltp/quote/ohlc) require a PAID Kite Connect
    # subscription that this account does not have — verified directly:
    # profile/holdings/positions/margins all succeed, ltp/quote/ohlc return
    # "Insufficient permission for that call."
    #
    # Using fetch_ltp alone therefore returned {} on every cycle, and with
    # require_live=True the monitor skipped every single run — stop monitoring
    # would have stayed silently dead. The shared helper falls back to
    # yfinance (~15 min delayed) and reports which source it used, so a
    # delayed price is never presented as real-time.
    from analysis.trade_decision import fetch_live_prices

    policy = load_exit_policy()
    rows = (sb.table("open_positions").select("*")
              .eq("status", "ACTIVE").execute().data) or []

    # Do not apply the SWING exit policy to INTRADAY positions.
    #
    # This engine books a partial at 1.5R, trails after 2R, targets 3R and time
    # stops after fifteen SESSIONS. An intraday position must be flat the same
    # day — often within the hour — so inheriting those rules would quietly
    # carry a four-hour thesis for three weeks. Nothing would error; the
    # position would simply be managed by rules never meant for it.
    if not cfg_bool("swing_manages_intraday_rows", False):
        intraday = [r for r in rows if (r.get("framework") or "SWING").upper() == "INTRADAY"]
        if intraday:
            logger.info(f"  skipping {len(intraday)} INTRADAY position(s) — "
                        f"managed by intraday/engine.py, not the swing exit policy: "
                        f"{', '.join(r['symbol'] for r in intraday)}")
        rows = [r for r in rows if (r.get("framework") or "SWING").upper() != "INTRADAY"]

    if not rows:
        return {"status": "ok", "managed": 0, "actions": []}

    # Trend evidence for every managed symbol, fetched ONCE. Passed through the
    # policy dict so evaluate_exit stays pure — it does no I/O and can still be
    # tested against a recorded row.
    #
    # THIS BLOCK USED TO SIT ABOVE THE `rows` QUERY.
    #
    # It read `rows` four lines before `rows` was assigned, which is an
    # UnboundLocalError, which the `except Exception` below swallowed on every
    # single run. `_trend_ctx` was therefore ALWAYS `{}`, and two of the most
    # considered rules in this system were inert with nothing to show for it:
    #
    #   · target_decision() got an empty signal, so assess_trend() counted zero
    #     checks and returned INTACT/0.50 — whose should_run is True. Every
    #     position reaching 3R was converted to a RUNNER on no evidence at all,
    #     which is the precise opposite of what exit_rules.py was written to do.
    #   · deterioration_check() is gated on `if sig:` in evaluate_exit, and an
    #     empty dict is falsy, so the "thesis broken while in profit" exit could
    #     never fire. exit_deterioration_enabled has been true in system_config
    #     the whole time and did nothing.
    #
    # The failure is now logged at WARNING, not DEBUG. A missing trend context
    # changes which exits fire; that is not a debug detail.
    try:
        from control.exit_rules import load_signal_context
        policy["_trend_ctx"] = load_signal_context(
            sb, [r.get("symbol") for r in rows if r.get("symbol")])
        if not policy["_trend_ctx"] and rows:
            logger.warning(f"  no trend context for any of {len(rows)} position(s) — "
                           f"runners and deterioration exits will abstain")
    except Exception as e:
        logger.warning(f"  trend context unavailable ({e}) — runners and "
                       f"deterioration exits will abstain this cycle")
        policy["_trend_ctx"] = {}

    symbols = [r["symbol"] for r in rows if r.get("symbol")]
    prices, price_source = fetch_live_prices(symbols)
    if prices:
        logger.info(f"  Live prices for {len(prices)}/{len(symbols)} via {price_source}"
                    + ("  ⚠️ ~15 min delayed" if price_source == "yfinance" else ""))

    if not prices and require_live:
        logger.warning(
            "  No live broker prices and require_live=True — skipping. "
            "Falling back to stock_data_daily intraday would evaluate stops "
            "against the PREVIOUS session's close (bhavcopy is not written "
            "until ~18:00 IST), producing missed breaches and repeated alerts. "
            "Refresh the Kite session: python -m kite.token_manager --login-url"
        )
        return {"status": "no_live_prices", "managed": 0, "actions": []}

    if not prices:
        # Fall back to the last daily close so the policy still runs (and the
        # evening pipeline still manages positions) when the broker is down.
        # Safe there: trade_date IS the session that just closed.
        try:
            latest = (sb.table("stock_data_daily").select("symbol,close")
                        .eq("date", trade_date).in_("symbol", symbols).execute().data)
            prices = {r["symbol"]: float(r["close"]) for r in latest if r.get("close")}
            logger.info(f"  Using EOD closes for {len(prices)} symbols (no live broker feed)")
        except Exception as e:
            logger.warning(f"  Could not load fallback prices: {e}")
            return {"status": "no_prices", "managed": 0, "actions": []}

    actions = []
    for pos in rows:
        sym = pos["symbol"]
        ltp = prices.get(sym)
        if not ltp:
            continue

        entry    = float(pos.get("entry_price") or 0)
        held     = _sessions_held(sb, pos.get("entry_date"), trade_date)
        decision = evaluate_exit(pos, ltp, held, policy)

        # Live metrics, refreshed every run
        hwm      = max(float(pos.get("high_water_mark") or entry), ltp)
        mfe      = max(float(pos.get("max_favorable_excursion") or 0), (ltp - entry) / entry * 100)
        mae      = min(float(pos.get("max_adverse_excursion")  or 0), (ltp - entry) / entry * 100)
        stop0    = float(pos.get("planned_stop") or 0)
        risk     = entry - stop0 if stop0 and stop0 < entry else None
        r_now    = round((ltp - entry) / risk, 3) if risk else None
        qty      = int(pos.get("current_qty") or pos.get("actual_qty") or 0)

        update = {
            "current_price":     ltp,
            "current_value":     round(ltp * qty, 2),
            "unrealized_pnl":    round((ltp - entry) * qty, 2),
            "pnl_pct":           round((ltp - entry) / entry * 100, 3) if entry else None,
            "high_water_mark":   round(hwm, 2),
            "max_favorable_excursion": round(mfe, 3),
            "max_adverse_excursion":   round(mae, 3),
            "r_multiple_current":      r_now,
            "last_reconciled_at": datetime.now(IST).isoformat(),
        }

        act = decision["action"]

        # RUNNER INSTRUMENTATION — recorded on every target touch, not just the
        # ones that run. "The rule declined correctly" and "the rule could not
        # see anything to decide on" both produce zero runners, and only the
        # evidence count separates them. Written for the declining branches too,
        # which are the ones carrying the information.
        for k in ("runner_evidence", "runner_verdict", "runner_since_r"):
            if decision.get(k) is not None:
                update[k] = decision[k]

        if act in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME", "EXIT_GIVEBACK",
                   "EXIT_STALL", "EXIT_FASTFAIL"):
            # RECORD ONLY. This module never places orders — it raises the
            # alert and waits for the sale to appear in Kite, at which point
            # reconcile_with_broker writes the closed_positions row.
            update["exit_signal"]     = decision["reason"]
            update["action_required"] = f"EXIT — {decision['detail']}"
        elif act == "TRAIL_SL":
            update["active_sl"] = decision["new_sl"]
            # BREAKEVEN is not a trail and must not be recorded as one.
            # trail_activated flips the stop-breach reason to TRAIL_SL_HIT, and
            # labelling a breakeven stop-out as a trailing stop is what made the
            # "37 TRAIL SL HIT exits averaging -0.40%" bucket unreadable — it
            # mixed three different rules under one name.
            if decision["reason"] == "BREAKEVEN":
                update["breakeven_moved"] = True
            else:
                update["trail_activated"] = True
        elif act == "RUN":
            # Converted to a runner: keep the position, ratchet the stop, and
            # record that the hard target has been consciously passed so the
            # next cycle does not re-ask the same question from scratch.
            if decision.get("new_sl"):
                update["active_sl"] = decision["new_sl"]
                update["trail_activated"] = True
            update["action_required"] = f"RUNNER — {decision['detail']}"
        elif act == "EXIT_DETERIORATION":
            update["exit_signal"] = decision["reason"]
            update["action_required"] = f"EXIT — {decision['detail']}"
        elif act == "BOOK_PARTIAL":
            update["action_required"] = f"BOOK {decision['book_qty']} — {decision['detail']}"
            if decision["new_sl"]:
                update["active_sl"]       = decision["new_sl"]
                update["breakeven_moved"] = True

        if not DRY_RUN:
            # Keyed on (symbol, product), not symbol. open_positions is unique
            # on the pair since migration 028, so a bare symbol match would
            # write this position's price, stop and exit signal onto BOTH
            # tranches when the same name is held CNC by swing and MIS by
            # intraday — the exact corruption the composite key exists to
            # prevent. Falls back to symbol alone when product is unset, so
            # pre-028 rows still update.
            def _write(payload: dict):
                q = sb.table("open_positions").update(payload).eq("symbol", sym)
                if pos.get("product"):
                    q = q.eq("product", pos["product"])
                return q.eq("status", "ACTIVE").execute()

            _update_stripping_unknown(_write, update, f"open_positions/{sym}")

        # Phase 3: place the order the alert describes, for SWING positions.
        # Gated per framework via execution/gates — being comfortable
        # automating intraday exits says nothing about swing ones, so one
        # switch would force a decision there is no reason to make.
        if act in ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME",
                   "EXIT_DETERIORATION", "EXIT_GIVEBACK", "EXIT_STALL",
                   "EXIT_FASTFAIL", "BOOK_PARTIAL"):
            try:
                from execution.gates import auto_exit_enabled
                fw = (pos.get("framework") or "SWING").upper()
                if auto_exit_enabled(fw):
                    from execution.order_manager import OrderRequest, place
                    qty = int(decision.get("book_qty") or 0) or int(
                        pos.get("current_qty") or pos.get("actual_qty") or 0)
                    if qty > 0 and ltp:
                        # BUFFER SCALED TO THE NAME, NOT A FLAT 30bps — see
                        # execution/exit_orders.py::exit_limit_price. A fixed
                        # 0.30% under the last trade is inside the noise of a
                        # 4.7%-ATR stock, so the limit is behind the market the
                        # moment it arrives; GABRIEL's 17-Aug exit rested
                        # unfilled for 2h33m and was eventually filled by hand.
                        from execution.exit_orders import exit_limit_price, atr_pct_for
                        px = exit_limit_price(float(ltp), atr_pct_for(sb, sym))
                        place(OrderRequest(
                            sym, "SELL", qty, "LIMIT", px,
                            reason=f"{act}: {decision['detail']}"), sb)
            except Exception as e:
                logger.warning(f"  {sym}: auto-exit failed, alert still sent — {e}")

        if act != "HOLD":
            actions.append({"symbol": sym, "action": act,
                            "reason": decision["reason"], "detail": decision["detail"],
                            "ltp": ltp, "r": r_now})
            logger.info(f"  {act:<13} {sym:<12} {decision['detail']}")
        else:
            logger.debug(f"  HOLD          {sym:<12} {decision['detail']}")

    return {"status": "ok", "managed": len(rows), "actions": actions}


def send_action_alerts(actions: list[dict]):
    """Telegram alert for anything needing a decision. Silent when nothing does."""
    if not actions:
        return
    # EVERY ACTION THAT CAN SELL MUST BE ABLE TO ALERT.
    #
    # EXIT_GIVEBACK and EXIT_STALL were added to the order-placing branch above
    # but not to this list, so auto-exit would have sold the position and sent
    # nothing — the operator's first notice would have been the tradebook. That
    # asymmetry is worse than either extreme: an exit you did not authorise and
    # were not told about. This list and the order-placing list are the same set
    # for that reason, and adding a rule to one without the other is the bug.
    SELLABLE = ("EXIT_STOP", "EXIT_TARGET", "EXIT_TIME", "EXIT_DETERIORATION",
                "EXIT_GIVEBACK", "EXIT_STALL", "EXIT_FASTFAIL", "BOOK_PARTIAL")
    urgent = [a for a in actions if a["action"] in SELLABLE]
    if not urgent:
        return
    icons = {"EXIT_STOP": "🔴", "EXIT_TARGET": "🎯", "EXIT_TIME": "⏰",
             "EXIT_DETERIORATION": "⚠️", "EXIT_GIVEBACK": "🩸",
             "EXIT_STALL": "🐌", "EXIT_FASTFAIL": "✂️",
             "BOOK_PARTIAL": "💰", "RUN": "🏃"}
    lines = ["<b>⚡ Position Actions Required</b>", ""]
    for a in urgent:
        lines.append(f"{icons.get(a['action'], '•')} <b>{a['symbol']}</b> — {a['action']}")
        lines.append(f"    LTP Rs.{a['ltp']:.2f}"
                     + (f" | {a['r']:+.2f}R" if a.get("r") is not None else ""))
        lines.append(f"    <i>{a['detail']}</i>")
    lines.append("\n<i>Execute in Kite. Positions reconcile automatically on the next run.</i>")
    try:
        from alerts.send_alerts import send_message
        send_message("\n".join(lines), subject_suffix="Position Actions")
    except Exception as e:
        logger.warning(f"  Position alert send failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main(reconcile: bool = True, manage: bool = True,
         require_live: bool = False, alert: bool = True) -> dict:
    """
    alert=False suppresses the standalone "Position Actions Required" message.

    Set by run_pipeline for the evening run, where send_alerts renders the very
    same instruction inside the digest's OPEN POSITIONS block a few steps later
    — so one BOOK_PARTIAL on one position produced two notifications describing
    it in different words. Duplicate alerts for a single fact are worse than one
    alert: they make you check whether something happened twice.

    It stays True for the intraday and ad-hoc paths, which have no digest
    following them and would otherwise report nothing at all.
    """
    if is_kill_switch_active():
        logger.warning("Kill switch active — position_lifecycle skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    sb = get_supabase()
    from config import get_trade_date
    trade_date = get_trade_date(sb, mode="auto", caller="position_lifecycle")

    logger.info("=" * 66)
    logger.info(f"Position Lifecycle | {trade_date}" + (" [DRY RUN]" if DRY_RUN else ""))
    logger.info("=" * 66)

    result = {"trade_date": trade_date}

    if reconcile and cfg_bool("position_reconcile_enabled", True):
        result["reconcile"] = reconcile_with_broker(sb, trade_date)

    # Capital is checked here because this is the one component that runs
    # several times a day AND already holds a broker session. Sizing errors are
    # silent until an order is rejected mid-session, so the comparison wants to
    # be in front of you before the next entry, not after.
    try:
        from control.capital_check import log_capital_status
        result["capital"] = log_capital_status(sb)
    except Exception as e:
        logger.debug(f"  capital check skipped: {e}")

    if manage:
        managed = manage_open_positions(sb, trade_date, require_live=require_live)
        result["manage"] = managed
        if alert:
            send_action_alerts(managed.get("actions", []))
        elif managed.get("actions"):
            logger.info(f"  {len(managed['actions'])} action(s) — alerting suppressed; "
                        f"the evening digest carries them")

    logger.success(f"Position lifecycle complete: {result}")
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="TradeOS v7 — Position Lifecycle Manager")
    ap.add_argument("--dry-run",           action="store_true")
    ap.add_argument("--reconcile-only",    action="store_true")
    ap.add_argument("--manage-only",       action="store_true")
    ap.add_argument("--require-live",      action="store_true",
                    help="Intraday mode: skip rather than fall back to the "
                         "previous session's closes when the broker feed is down.")
    args = ap.parse_args()
    if args.dry_run:
        import os; os.environ["DRY_RUN"] = "True"
    print(main(reconcile=not args.manage_only,
               manage=not args.reconcile_only,
               require_live=args.require_live))
