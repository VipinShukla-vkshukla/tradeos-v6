"""
Cutting intraday losses — the rules the swing policy cannot express.

WHAT WAS WRONG
--------------
Intraday positions were being managed by load_exit_policy(), the SWING policy.
Three things about that are incorrect, and the third is the dangerous one:

  target_r = 3.0        Every intraday engine sets its OWN target — ORB at 2R,
                        PBK and VCE at 2.5R, RNG at the opposite edge of a
                        proven range. Overriding those with a flat 3R holds
                        past the level the setup was actually built around.

  time_stop_days = 15   Fifteen SESSIONS, on a trade that must be flat by 15:20.
                        A dead intraday position would ride the entire day and
                        into the square-off having never triggered a time stop.

  invalidation          Every Setup states the condition under which it stops
                        being the trade it was — "a close back below the range
                        high", "losing VWAP", "back inside the coil". NOTHING
                        CHECKED IT. The whole point of writing those conditions
                        was that an intraday thesis dies before the stop is
                        reached, and the position was instead held to a stop
                        that is two or three times further away.

THE ASYMMETRY THAT SHAPES ALL OF THIS
-------------------------------------
An intraday stop is 0.5-1.2%, and the round trip costs 0.21%. There is no room
to be wrong slowly. A swing position can spend a week being wrong and recover;
an intraday position that is wrong is wrong for the rest of the session, because
the session ends. So every rule here cuts EARLIER than its swing equivalent, and
the invalidation check exists specifically to cut before the stop.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg, cfg_bool, cfg_float, cfg_int
from intraday.session import (phase_at, SQUARE_OFF, CLOSED, minutes_to_close,
                              minutes_to_cover_deadline)
from intraday import direction as D


def load_intraday_policy() -> dict:
    """Deliberately NOT load_exit_policy(). See the module docstring."""
    return {
        "use_setup_target":   cfg_bool("intraday_use_setup_target", True),
        "partial_book_r":     cfg_float("intraday_partial_book_r", 1.2),
        "partial_book_pct":   cfg_float("intraday_partial_book_pct", 50.0),
        "move_to_breakeven":  cfg_bool("intraday_move_to_breakeven", True),
        "trail_r":            cfg_float("intraday_trail_r", 1.0),
        "trail_after_r":      cfg_float("intraday_trail_after_r", 1.5),
        "target_r":           cfg_float("intraday_target_r", 2.0),
        "time_stop_min":      cfg_int("intraday_time_stop_minutes", 75),
        "time_stop_min_r":    cfg_float("intraday_time_stop_min_r", 0.3),
        "squareoff_buffer":   cfg_int("intraday_squareoff_buffer_min", 12),
        "check_invalidation": cfg_bool("intraday_check_invalidation", True),
        "must_exit_time":     cfg("intraday_must_exit_time", "15:15"),
        # Breakeven as its OWN rung, defaulting to the partial's level so a
        # multi-share position behaves exactly as before. See evaluate_intraday_exit.
        "breakeven_at_r":     cfg_float("intraday_breakeven_at_r",
                                        cfg_float("intraday_partial_book_r", 1.2)),
        "cost_buffer_pct":    cfg_float("intraday_breakeven_cost_pct", 0.21),
        # Give-back guard. OFF by default — see the note in evaluate_intraday_exit.
        "giveback_pct":       cfg_float("intraday_giveback_pct", 0.0),
        # Runway-aware tightening for OPEN shorts. OFF by default, same
        # reasoning as giveback_pct just above: never measured against this
        # book's own outcomes yet, so it ships inert until an operator arms
        # it. See rung 6b in evaluate_intraday_exit. Reuses
        # intraday_short_min_runway_min — the SAME number can_short() gates
        # NEW entries on — rather than a second key that could drift from it.
        "short_runway_tighten_enabled": cfg_bool("intraday_short_runway_tighten_enabled", False),
        "short_runway_min":            cfg_int("intraday_short_min_runway_min", 75),
        "short_runway_tighten_floor_pct": cfg_float("intraday_short_runway_tighten_floor_pct", 40.0),
        #
        # min_r LOWERED 1.0 -> 0.5, 11-Aug-2026, from the closed book's own
        # MFE quantiles (tools.exit_ladder_replay / direct query,
        # framework=INTRADAY, n=27 with excursion data):
        #
        #   winners (n=10): MIN peak 0.499R, p25 0.849R, median 1.550R
        #   losers  (n=17): p75 peak 0.490R, median 0.145R
        #
        # Every winner in the sample peaked at >=0.499R at some point; 75%
        # of losers NEVER reached 0.490R before turning into a loss. Those
        # two numbers coincide almost exactly, which is what makes 0.5 the
        # boundary the data itself draws, not a round number chosen to fit
        # the backtest. At the old 1.0R floor, a full quarter of winners
        # (p25 MFE 0.849) never had the guard active during their most
        # fragile stretch — exactly the population exit_ladder_replay names:
        # KAYNES, SAPPHIRE, ADANIGREEN, IFCI, HINDCOPPER, SWIGGY all peaked
        # 0.5-1.1R and reversed to a loss via SETUP_INVALIDATED/TIME_EXIT,
        # never touching a stop the guard could have pre-empted. Re-run
        # `python -m tools.exit_ladder_replay --min-r <x>` as the sample
        # grows — n=27 is real but still thin; treat this as the data's
        # current best answer, not a settled one.
        "giveback_min_r":     cfg_float("intraday_giveback_min_r", 0.5),
    }


def last_completed_close(bars: list, now: datetime | None = None) -> float | None:
    """
    The close of the most recent bar that has actually FINISHED, or None.

    PURE. `bars` is any sequence of objects with `.ts` and `.close`, oldest
    first — intraday.strategies.base.Bar in production. The interval is
    inferred from the spacing of the last two bars rather than read from
    config, because a config key that disagrees with the builder's real
    interval would silently mis-classify the forming bar as complete, which is
    the exact wick-exit this function exists to prevent.

    The last bar in the list is normally still forming: its close is the last
    tick, which is `ltp` under another name. Using it would make a "close"
    rule identical to the tick rule it replaces — the silent no-op this
    project has shipped four times.
    """
    if not bars or len(bars) < 2:
        return None
    now = now or datetime.now(IST)
    try:
        interval = (bars[-1].ts - bars[-2].ts)
        if interval.total_seconds() <= 0:
            return None
        for b in reversed(bars):
            if b.ts + interval <= now:
                return float(b.close)
    except (AttributeError, TypeError):
        return None
    return None


def _invalidated(pos: dict, ltp: float,
                 last_close: float | None = None) -> tuple[bool, str]:
    """
    Has the setup's own thesis died?

    invalidation_level is the price the engine named when the setup was created
    — the range high for ORB, VWAP for a reclaim, the coil high for a squeeze.
    Losing it means the structure that justified the trade is gone, and that is
    knowable well before a stop sitting a full R below.

    Requires a small buffer so an ordinary wick through the level does not
    trigger an exit: intraday levels get tested constantly, and a rule that
    fires on every touch would exit every trade in its first ten minutes.

    DIRECTIONAL. A long is invalidated by price falling BELOW its level; a short
    by price rising ABOVE it — a breakdown that reclaims the level it broke is
    exactly as dead as a breakout that loses the one it cleared. Written in the
    long form (`ltp < level * (1 - buf)`) this fired on a short the moment the
    trade started WORKING, cutting every winner at its first tick of profit.
    """
    level = pos.get("invalidation_level")
    if not level:
        return False, ""
    try:
        level = float(level)
    except (TypeError, ValueError):
        return False, ""
    if level <= 0:
        return False, ""

    d = D.normalise(pos.get("direction"))
    buf = cfg_float("intraday_invalidation_buffer_pct", 0.12) / 100.0
    breached = level * (1 - D.sign(d) * buf)

    # ── A CLOSE, NOT A TICK — 12-Aug-2026 ───────────────────────────────────
    # Every engine's invalidation prose promises a CLOSE ("a close back below
    # 1081.80", "a close back under VWAP"); this compared the last traded
    # price. Those are different quantities, and the gap between them is where
    # the book died on 12-Aug: six of nine exits were SETUP_INVALIDATED at
    # -0.15R to -0.72R, and SBIN's was ten paise through the line on a trade
    # risking 1.2%. One seller is not a structure failing.
    #
    # A completed bar closing beyond the level is the thing the prose has
    # always described: the level failed to hold for a whole bar, which is
    # what a disciplined discretionary trader waits for before conceding the
    # idea. Off by default so the change is measured, not assumed.
    #
    # NO COMPLETED BAR MEANS NO INVALIDATION, DELIBERATELY. Early in a trade
    # there may be no finished bar yet. Falling back to `ltp` there would
    # reintroduce the tick exit at exactly the moment it does the most damage,
    # so the answer is "not yet confirmed" — and nothing is unprotected,
    # because the invalidation is an OPTIMISATION that cuts before the stop,
    # while the stop itself (checked above, and untouched) is the safety
    # control.
    ref = ltp
    if cfg_bool("intraday_invalidation_require_close", False):
        if last_close is None:
            return False, ""
        ref = last_close

    if not D.is_better_price(ref, breached, d):
        note = pos.get("invalidation_note") or f"lost {level:.2f}"
        return True, note
    return False, ""


def evaluate_intraday_exit(pos: dict, ltp: float, policy: dict,
                           now: datetime | None = None,
                           last_close: float | None = None) -> dict:
    """
    What to do with one intraday position. Pure — no I/O.

    Checked in order of how expensive it is to be wrong about each one.
    """
    now = now or datetime.now(IST)
    entry = float(pos.get("entry_price") or 0)
    stop0 = float(pos.get("planned_stop") or 0)
    sl = float(pos.get("active_sl") or stop0 or 0)
    target = float(pos.get("planned_target") or 0)

    if not entry or not ltp:
        return {"action": "HOLD", "reason": "missing_price", "detail": "",
                "new_sl": None, "book_qty": 0}

    # DIRECTION, READ FROM THE POSITION AND APPLIED TO EVERY RUNG BELOW.
    #
    # This whole ladder was written in the long form and there is no version of
    # it that is merely "slightly off" for a short. `risk = entry - stop0` is
    # NEGATIVE when the stop sits above entry, so the `stop0 < entry` guard sent
    # it to the 0.5% fallback and discarded the real stop; then
    # `gain_r = (ltp - entry) / risk` rose as the price rose. A short losing
    # money read as a winning trade — it would have booked a partial, moved the
    # stop to "breakeven" on the wrong side, and trailed away from the entry as
    # the loss grew. Every rung, in the wrong direction, confidently.
    #
    # `direction` defaults to LONG for any row without one, which is every
    # position written before this change.
    d       = D.normalise(pos.get("direction"))
    risk    = D.risk_per_share(entry, stop0, d) or entry * 0.005
    gain_r  = D.gain_r(entry, ltp, risk, d)
    gain_pct = D.gain_pct(entry, ltp, d)

    # ── 1. Square-off deadline — nothing outranks being flat ────────────────
    ph = phase_at(now)
    if ph in (SQUARE_OFF, CLOSED):
        return {
            "action": "EXIT_SQUAREOFF", "reason": "SESSION_END",
            "detail": f"session over — flat by design ({gain_pct:+.2f}%, {gain_r:+.2f}R)",
            "new_sl": None, "book_qty": 0,
        }
    # intraday_must_exit_time is a wall-clock deadline; squareoff_buffer is
    # relative to the close. Whichever comes first wins, so a deliberate 15:15
    # is honoured even when the buffer alone would allow later. This key was
    # configured and unread until now — it looked like a hard deadline and was
    # not one.
    must_exit = policy.get("must_exit_time")
    if must_exit:
        try:
            hh, mm = (int(x) for x in str(must_exit).split(":")[:2])

            # A SHORT'S DEADLINE IS THE LONG'S, BROUGHT FORWARD.
            #
            # Measured against `must_exit_time` rather than against the close,
            # and this is the whole reason it works. The first version of this
            # inflated `squareoff_buffer` instead — but SQUARE_OFF phase begins
            # at 15:20, i.e. `minutes_to_close` = 10, so any buffer at or under
            # ten minutes was unreachable: rung 1 fired first, every time. It
            # would have been a CRITICAL config key that did nothing, which is
            # this project's most-repeated defect and one I nearly shipped here.
            # Caught by testing it rather than by reading it.
            #
            # Anchoring to must_exit_time also means the lead stays correct if
            # the operator moves that deadline — a short is always covered N
            # minutes before a long would be exited, whenever that is.
            lead = cfg_int("intraday_short_cover_lead_min", 10) if D.is_short(d) else 0
            deadline_min = hh * 60 + mm - lead
            if (now.hour * 60 + now.minute) >= deadline_min:
                dl = f"{deadline_min // 60:02d}:{deadline_min % 60:02d}"
                return {
                    "action": "EXIT_SQUAREOFF", "reason": "MUST_EXIT_TIME",
                    "detail": (
                        (f"past the {dl} cover deadline ({lead} min ahead of the "
                         f"{must_exit} exit) — an UNCOVERED SHORT goes to the auction "
                         f"session and the penalty runs to ~20% of the trade, which "
                         f"dwarfs any stop here"
                         if D.is_short(d) else
                         f"past the {must_exit} deadline — flat on our schedule")
                        + f" ({gain_pct:+.2f}%, {gain_r:+.2f}R)"),
                    "new_sl": None, "book_qty": 0,
                }
        except (ValueError, TypeError):
            logger.warning(f"  intraday_must_exit_time is '{must_exit}', which is not "
                           f"HH:MM — ignoring it and using the close buffer only")

    # A SHORT COVERS EARLIER THAN A LONG EXITS, AND THIS IS NOT SYMMETRY.
    #
    # The two failures are not the same size. A long left open at the close
    # becomes delivery: you need the cash, which is inconvenient and recoverable.
    # A short left open is SHORT DELIVERY — the exchange buys it back for you in
    # the auction session and the penalty runs to roughly 20% of the trade value,
    # which dwarfs any stop this system would ever set. It is the one outcome
    # here that is an order of magnitude worse than being wrong about direction.
    #
    # So a short gets its own, earlier buffer on top of the shared one. Paying a
    # few minutes of give-back on every short is a trivially cheap insurance
    # premium against one un-covered position.
    mins_left = minutes_to_close(now) - policy["squareoff_buffer"]
    if mins_left <= 0:
        return {
            "action": "EXIT_SQUAREOFF", "reason": "SQUAREOFF_APPROACHING",
            "detail": (f"{policy['squareoff_buffer']} min to the close — exiting on our "
                       f"terms rather than the broker's ({gain_pct:+.2f}%)"),
            "new_sl": None, "book_qty": 0,
        }

    # ── 2. Stop ─────────────────────────────────────────────────────────────
    # The REASON distinguishes the three ways a stop gets hit, because they mean
    # opposite things and were all being recorded as STOP_LOSS_HIT. ATHERENERG
    # closed at +0.84% / +1.93R labelled STOP_LOSS_HIT — a trailing stop doing
    # its job, filed under the bucket for trades that were simply wrong. Three
    # of the paper book's fourteen exits are in that bucket and it nets POSITIVE,
    # which makes the exit-reason table unreadable and would teach the weekly
    # review that being stopped out is profitable.
    # `ltp <= sl` is the long form. A short is stopped when price rises THROUGH
    # the stop, so the comparison is "is the stop now a better price than where
    # we are" — one expression, both directions.
    if sl and not D.is_better_price(ltp, sl, d):
        if pos.get("trail_activated"):
            reason = "TRAIL_SL_HIT"
        elif pos.get("breakeven_moved"):
            reason = "BREAKEVEN_STOP_HIT"
        else:
            reason = "STOP_LOSS_HIT"
        return {
            "action": "EXIT_STOP", "reason": reason,
            "detail": f"ltp {ltp:.2f} <= sl {sl:.2f} ({gain_pct:+.2f}%, {gain_r:+.2f}R)",
            "new_sl": None, "book_qty": 0,
        }

    # ── 3. Thesis invalidated — the intraday-specific loss cut ──────────────
    # Deliberately ABOVE the target check: a setup whose structure has broken is
    # not a setup that deserves to be held for its target, and this is the rule
    # that cuts before the stop rather than after.
    if policy["check_invalidation"]:
        bad, why = _invalidated(pos, ltp, last_close)
        if bad:
            return {
                "action": "EXIT_INVALIDATED", "reason": "SETUP_INVALIDATED",
                "detail": (f"{why} — the structure that justified this trade is gone "
                           f"({gain_pct:+.2f}%, {gain_r:+.2f}R). Cutting here rather than "
                           f"waiting for the stop at {sl:.2f}"),
                "new_sl": None, "book_qty": 0,
            }

    # ── 4. The setup's OWN target, not a global multiple ────────────────────
    if policy["use_setup_target"] and target and not D.is_better_price(target, ltp, d):
        return {
            "action": "EXIT_TARGET", "reason": "TARGET_HIT",
            "detail": f"reached the setup's target {target:.2f} ({gain_pct:+.2f}%, {gain_r:+.2f}R)",
            "new_sl": None, "book_qty": 0,
        }
    if not target and gain_r >= policy["target_r"]:
        return {
            "action": "EXIT_TARGET", "reason": "TARGET_HIT",
            "detail": f"{gain_r:.2f}R >= {policy['target_r']}R ({gain_pct:+.2f}%)",
            "new_sl": None, "book_qty": 0,
        }

    # ── 5. Partial + breakeven ──────────────────────────────────────────────
    booked = int(pos.get("partial_booked_qty") or 0) > 0
    qty = int(pos.get("current_qty") or pos.get("actual_qty") or 0)
    if not booked and gain_r >= policy["partial_book_r"] and qty > 1:
        n = max(1, int(qty * policy["partial_book_pct"] / 100.0))
        return {
            "action": "BOOK_PARTIAL", "reason": "PARTIAL_TARGET",
            "detail": (f"{gain_r:.2f}R >= {policy['partial_book_r']}R — book {n}/{qty} "
                       f"@ {ltp:.2f} ({gain_pct:+.2f}%)"),
            "new_sl": entry if policy["move_to_breakeven"] else None,
            "book_qty": n,
        }

    # ── 5b. GIVE-BACK GUARD — off by default, and deliberately ──────────────
    #
    # The swing book has this rule because it could be measured there: 24 of 28
    # losing swing trades had been in profit first, giving back a median 3.14%.
    # The same rule almost certainly belongs here — an intraday position has
    # LESS time to recover, not more — but it cannot be calibrated yet, because
    # max_favorable_excursion is NULL on all fourteen closed intraday positions.
    # The engine never maintained it (fixed in engine.py alongside this), so
    # there is no record of how far any intraday trade ran before it faded.
    #
    # Shipping an uncalibrated exit into the book it cannot be measured against
    # is how a system gets worse while looking busier. So the rule is built and
    # switched OFF: set intraday_giveback_pct once ~20 intraday positions have
    # closed carrying excursion data, and calibrate it on those rather than on
    # the swing number, which comes from a different horizon.
    # high_water_mark holds the MOST FAVOURABLE price seen, which for a short is
    # the session low. The column keeps its name; its meaning is direction-
    # dependent and is defined in intraday/direction.py rather than here.
    hwm_px = float(pos.get("high_water_mark") or 0)
    if policy["giveback_pct"] > 0 and hwm_px and D.is_better_price(hwm_px, entry, d):
        peak_r = D.gain_r(entry, hwm_px, risk, d)
        if peak_r >= policy["giveback_min_r"]:
            kept = gain_r / peak_r if peak_r > 0 else 1.0
            if kept < (1.0 - policy["giveback_pct"] / 100.0):
                return {
                    "action": "EXIT_GIVEBACK", "reason": "GAVE_BACK_THE_MOVE",
                    "detail": (f"peaked at {peak_r:.2f}R and is back to {gain_r:+.2f}R "
                               f"({gain_pct:+.2f}%) — {1 - kept:.0%} of the move handed "
                               f"back, and intraday there is no tomorrow to recover it"),
                    "new_sl": None, "book_qty": 0,
                }

    # ── 5c. BREAKEVEN AS ITS OWN RUNG ───────────────────────────────────────
    #
    # Breakeven was reachable ONLY through BOOK_PARTIAL, which requires qty > 1.
    # With intraday_max_order_value at Rs 6,000, seven of the 48 names in the
    # traded universe size to a single share — NETWEB, GVT&D, KAYNES, ENRIN and
    # others above Rs 3,000 — and three (POWERINDIA, APARINDS, OFSS) size to
    # zero and cannot be traded at all. For every single-share position the
    # partial can never fire, so the stop never moved to entry and a trade that
    # paid a full R could still finish at -1R.
    #
    # Defaulting breakeven_at_r to partial_book_r means nothing changes for a
    # multi-share position: the partial still fires first and carries the stop
    # with it. This rung exists for the ones the partial cannot reach.
    #
    # Set above entry by the round trip, for the same reason as swing — a stop
    # at exactly the entry price still owes brokerage, STT and stamp duty on
    # both legs, and intraday that is 0.21% of a move worth maybe 0.7%.
    # Breakeven sits PAST entry in the direction of profit — above it for a long,
    # BELOW it for a short — because the round trip is owed either way.
    if policy["move_to_breakeven"] and gain_r >= policy["breakeven_at_r"]:
        be = round(entry * (1 + D.sign(d) * policy["cost_buffer_pct"] / 100.0), 2)
        if D.is_better_price(be, sl, d):
            return {
                "action": "TRAIL_SL", "reason": "BREAKEVEN",
                "detail": (f"{gain_r:.2f}R — sl {sl:.2f} -> {be:.2f}, entry plus the "
                           f"{policy['cost_buffer_pct']:.2f}% round trip. This trade "
                           f"can no longer cost money"),
                "new_sl": be, "book_qty": 0,
            }

    # ── 6. Trail ────────────────────────────────────────────────────────────
    if gain_r >= policy["trail_after_r"]:
        # The stop trails BEHIND the best price seen — below it for a long, above
        # it for a short. Written as `max(...) - trail_r * risk` this loosened a
        # short's stop by two risk units every single cycle.
        hwm = D.favourable_excursion(float(pos.get("high_water_mark") or 0),
                                     ltp, entry, d)
        trail = hwm - D.sign(d) * policy["trail_r"] * risk
        if D.is_better_price(trail, sl, d):
            return {
                "action": "TRAIL_SL", "reason": "TRAIL_UPDATE",
                "detail": (f"{gain_r:.2f}R — sl {sl:.2f} -> {trail:.2f} "
                           f"(locks {D.gain_r(entry, trail, risk, d):+.2f}R)"),
                "new_sl": round(trail, 2), "book_qty": 0,
            }

    # ── 6b. RUNWAY-AWARE TIGHTENING for open SHORTS, as the cover deadline
    #        approaches ──────────────────────────────────────────────────────
    #
    # can_short() refuses a NEW short once minutes_to_cover_deadline() drops
    # below intraday_short_min_runway_min (75) — an uncovered short goes to
    # the auction session. That gate protects ENTRIES. It says nothing about
    # a short already open: today, one rides its full stop width right up to
    # rung 1's hard EXIT_SQUAREOFF and is then closed at whatever price the
    # market happens to be at that instant — the exact forced, worst-possible
    # -moment exit the runway gate exists to avoid on the entry side.
    #
    # So the same number gates both sides. Once an OPEN short has less than
    # intraday_short_min_runway_min left before its own cover deadline, its
    # stop tightens continuously toward the live price rather than waiting
    # for the deadline to force a market exit — linear from full risk width
    # at `need` minutes left down to short_runway_tighten_floor_pct of it at
    # zero (rung 1 fires at zero anyway, so this never has to reach zero risk
    # on its own). Checked AFTER the ordinary trail so a short already
    # trailing tightly is never loosened back out by this rung —
    # is_better_price(tightened, sl, d) below enforces that either way, the
    # same invariant every other rung in this ladder holds.
    if (D.is_short(d) and policy.get("short_runway_tighten_enabled")
            and policy.get("short_runway_min")):
        need = policy["short_runway_min"]
        deadline_min_left = minutes_to_cover_deadline(now)
        if 0 < deadline_min_left < need:
            floor_frac = policy["short_runway_tighten_floor_pct"] / 100.0
            frac = floor_frac + (1.0 - floor_frac) * (deadline_min_left / need)
            tightened = ltp - D.sign(d) * risk * frac
            if D.is_better_price(tightened, sl, d):
                return {
                    "action": "TRAIL_SL", "reason": "RUNWAY_TIGHTEN",
                    "detail": (f"{deadline_min_left} min to the cover deadline (a fresh "
                               f"entry needs {need}) — tightening to {frac:.0%} of risk "
                               f"width rather than riding the full stop into a forced "
                               f"square-off ({gain_pct:+.2f}%, {gain_r:+.2f}R)"),
                    "new_sl": round(tightened, 2), "book_qty": 0,
                }

    # ── 7. Time stop, in MINUTES ────────────────────────────────────────────
    # A position that has gone nowhere is holding capital and attention that a
    # working setup could use, and intraday there is no tomorrow to wait for.
    held_min = 0
    try:
        opened = pos.get("partial_booked_at") or pos.get("synced_at") or pos.get("entry_date")
        if opened and "T" in str(opened):
            t0 = datetime.fromisoformat(str(opened).replace("Z", "+00:00")).astimezone(IST)
            held_min = int((now - t0).total_seconds() // 60)
    except Exception:
        held_min = 0

    if held_min >= policy["time_stop_min"] and gain_r < policy["time_stop_min_r"]:
        return {
            "action": "EXIT_TIME", "reason": "TIME_EXIT",
            "detail": (f"{held_min} min held at {gain_r:+.2f}R — going nowhere, and "
                       f"intraday there is no tomorrow to wait for"),
            "new_sl": None, "book_qty": 0,
        }

    return {"action": "HOLD", "reason": "holding",
            "detail": f"{gain_r:+.2f}R ({gain_pct:+.2f}%), {mins_left} min of session left",
            "new_sl": None, "book_qty": 0}


def invalidation_level_from(setup) -> tuple[float | None, str]:
    """
    The price whose loss kills the setup, taken from the engine's own meta.

    Each engine already names this in its invalidation string; this reads the
    numeric level out of the structured meta so it can be checked mechanically
    rather than by parsing prose.
    """
    m = getattr(setup, "meta", None) or {}

    # ── AN EXPLICIT DECLARATION WINS — 12-Aug-2026 ──────────────────────────
    #
    # Three engines set `meta["invalidation_level"]` outright (SDN's VWR, TRP
    # and ORB conditions) and NOTHING read it: this function only ever inferred
    # the level from the ordered key list below. The project's own landmine,
    # again — "a dict key and the key its consumer looks up are two different
    # claims" — and the inference got two engines actively wrong:
    #
    #   RNG   is a LONG at the range LOW. Its meta carries range_low AND
    #         range_high, and range_high is checked first, so the level came
    #         back ABOVE the entry. `_invalidated` then reads "price is below
    #         its invalidation level" on the very first evaluation and kills
    #         the trade instantly. RNG has never completed a trade.
    #   SDN/ORB is a SHORT that broke the range LOW, and carries both keys too,
    #         so it also resolved to range_high — the wrong end of the range.
    #   SDN/TRP declares prev_high; the list looks for `pdh`. No match at all,
    #         so the invalidation check was simply dead for every trap short.
    #   PBK   declares "losing VWAP" in prose and publishes no level key at
    #         all — also dead.
    #
    # The inference stays for engines that do not declare, because removing it
    # would silently disarm them instead. But an engine that states its own
    # level is the authority on it: it knows which end of its structure the
    # thesis lives at, and a positional key list cannot.
    explicit = m.get("invalidation_level")
    if explicit:
        try:
            prose = str(getattr(setup, "invalidation", "") or "")
            # The prose reads "a close below X — because ..."; the clause before
            # the dash is the label, the rest is the reasoning the alert prints.
            label = prose.split(" — ")[0].strip() or "lost its invalidation level"
            return float(explicit), label
        except (TypeError, ValueError):
            pass

    for key, label in (
        ("range_high", "closed back inside the opening range"),
        ("coil_high", "returned inside the coil"),
        ("or_low", "gave back the gap"),
        ("vwap", "lost VWAP"),
        ("pdh", "lost the previous day high"),
        ("range_low", "broke the range low"),
    ):
        v = m.get(key)
        if v:
            try:
                return float(v), label
            except (TypeError, ValueError):
                continue
    return None, ""
