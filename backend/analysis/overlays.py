"""
Structural overlays — cheap edge and left-tail removal. Not signal generators.

    day_type()          expiry mechanics condition intraday priors
    vol_exposure()      book-level exposure scales inversely to volatility
    liquidity_ok()      can this position actually be exited at plan?

WHY THESE ARE OVERLAYS AND NOT ENGINES
--------------------------------------
None of them finds a trade. Each takes trades the engines already found and
answers a question the engines do not ask: is today a different kind of day, is
the whole book too large for this volatility, and can this specific name be got
out of. They generate approximately zero gross alpha and a meaningful amount of
net alpha, entirely by removing realisations from the left tail.

That is worth saying plainly because it makes them easy to under-value. Nothing
here will ever appear as a winning trade. Their contribution is the losses that
did not happen, which no P&L line reports.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_bool, cfg_float, cfg_int, get_supabase, today_ist


# ─────────────────────────────────────────────────────────────────────────────
# 1. EXPIRY DAY-TYPE
# ─────────────────────────────────────────────────────────────────────────────

NORMAL, EXPIRY_WEEK, EXPIRY_DAY, MONTHLY_EXPIRY = (
    "NORMAL", "EXPIRY_WEEK", "EXPIRY_DAY", "MONTHLY_EXPIRY")


def day_type(d: date | None = None, sb=None) -> str:
    """
    What kind of session this is, from derivative settlement mechanics.

    WHY A CONTINUATION ENGINE IS MIS-CALIBRATED WITHOUT THIS.

    On index expiry the cash tape is a different animal: pinning holds price to
    strikes, unwinding flows dominate the last hour, and gamma effects
    systematically alter exactly the two archetypes the intraday sleeve trades —
    opening-range breakouts and VWAP reversion. An engine calibrated across all
    days is calibrated for the ~80% of days when settlement flows do not
    dominate, and quietly wrong on the rest.

    The point is NOT to predict expiry-day direction. It is to stop applying
    normal-day priors to an abnormal day. Conditioning is the deliverable;
    a separate expiry strategy is not.

    THIS IS A HEURISTIC AND IT IS APPROXIMATELY RIGHT, NOT EXACT.

    Say so plainly, because a day-type flag that is quietly wrong on some days
    is worse than none — it would condition priors on a mislabelled population
    and the miscalibration would look like engine noise.

    The system does not ingest an F&O expiry calendar, so the day is inferred
    from the holiday-aware trading calendar: last trading day of the week is
    treated as weekly expiry, last trading day of the month as monthly. NSE
    weekly index expiry has moved between weekdays more than once, so reading it
    from the calendar survives a rule change that a hardcoded Thursday does not.

    Where it is wrong: NSE monthly F&O expiry is the last THURSDAY, which is not
    always the last trading day — 31-Aug-2026 is a Monday and this will call it
    MONTHLY_EXPIRY when the real settlement was the previous Thursday. Expect a
    one-to-three session error on the monthly flag most months.

    That is tolerable for its purpose and not for a stronger one. The overlay
    only SIZES DOWN, so a false positive costs a little participation on an
    ordinary day and a false negative leaves that day at normal size — the
    behaviour it had before. It would NOT be tolerable as an input to a prior
    fitted per day-type, which is where this needs a real expiry calendar first.
    """
    d = d or today_ist()
    if not cfg_bool("overlay_expiry_enabled", False):
        return NORMAL

    holidays = _holidays(sb)

    def is_session(x: date) -> bool:
        return x.weekday() < 5 and x.isoformat() not in holidays

    if not is_session(d):
        return NORMAL

    # Last trading day of this calendar month?
    nxt = d + timedelta(days=1)
    last_of_month = True
    while nxt.month == d.month:
        if is_session(nxt):
            last_of_month = False
            break
        nxt += timedelta(days=1)
    if last_of_month:
        return MONTHLY_EXPIRY

    # Last trading day of this week (Mon-Fri block)?
    last_of_week = True
    nxt = d + timedelta(days=1)
    while nxt.weekday() < 5:
        if is_session(nxt):
            last_of_week = False
            break
        nxt += timedelta(days=1)
    if last_of_week:
        return EXPIRY_DAY

    # Somewhere in a week whose last session is a monthly expiry.
    end = d
    while end.weekday() < 4:
        end += timedelta(days=1)
    if end.month != d.month or (end - d).days <= 4 and end.day >= 25:
        return EXPIRY_WEEK
    return NORMAL


def _holidays(sb=None) -> set[str]:
    try:
        sb = sb or get_supabase()
        return {str(r["date"])[:10] for r in
                (sb.table("nse_holidays").select("date").execute().data or [])}
    except Exception:
        return set()


def expiry_size_multiplier(dt: str | None = None) -> float:
    """
    How much of normal size an intraday setup gets, given the day type.

    Reduction rather than exclusion, deliberately. Expiry days are not
    untradeable — they are differently distributed, with fatter tails in both
    directions. Excluding them would forfeit the ~20% of sessions where the
    largest intraday ranges occur; sizing down keeps the participation and
    removes the part that hurts.
    """
    dt = dt or day_type()
    return {
        NORMAL:          1.0,
        EXPIRY_WEEK:     cfg_float("overlay_size_expiry_week", 0.85),
        EXPIRY_DAY:      cfg_float("overlay_size_expiry_day", 0.70),
        MONTHLY_EXPIRY:  cfg_float("overlay_size_monthly_expiry", 0.55),
    }.get(dt, 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. VOLATILITY-REGIME EXPOSURE SCALING
# ─────────────────────────────────────────────────────────────────────────────

def vol_exposure(sb=None) -> tuple[float, str]:
    """
    Book-level exposure multiplier, inverse to volatility. Returns (mult, why).

    THIS CONTRIBUTES ALMOST NOTHING TO RETURN AND A GREAT DEAL TO DRAWDOWN.

    A momentum book bleeds in high-volatility regimes — stops are hit by noise
    rather than by thesis failure, and the same 1% risk per trade buys a much
    wider distribution of outcomes. Shrinking exposure into exactly those
    regimes is the cheapest drawdown control available, and it costs return only
    in the minority of high-VIX periods that also trend.

    Bands are set from THIS ACCOUNT'S OWN observed VIX distribution rather than
    from textbook levels: 94 observations to 04-Aug-2026 gave p25 13.4, median
    16.6, p75 19.6. A textbook "VIX above 20 is high" would have called the last
    quarter almost entirely calm and never fired.

    India VIX is ingested already (market_regime.india_vix). If it is missing
    this returns 1.0 and says so — an unknown volatility regime is not a reason
    to shrink, but it IS a reason to say the guard is not operating.
    """
    if not cfg_bool("overlay_vol_scaling_enabled", False):
        return 1.0, "volatility scaling disabled"
    try:
        sb = sb or get_supabase()
        rows = (sb.table("market_regime").select("date,india_vix")
                  .not_.is_("india_vix", "null")
                  .order("date", desc=True).limit(1).execute().data) or []
    except Exception as e:
        return 1.0, f"VIX unavailable ({type(e).__name__}) — exposure NOT scaled"
    if not rows:
        return 1.0, "no VIX reading — exposure NOT scaled"

    vix = float(rows[0]["india_vix"])
    calm = cfg_float("overlay_vix_calm", 14.0)      # ~p25 of observed
    high = cfg_float("overlay_vix_high", 20.0)      # ~p75 of observed
    fear = cfg_float("overlay_vix_fear", 26.0)      # ~p95 of observed

    if vix <= calm:
        return cfg_float("overlay_exposure_calm", 1.0), f"VIX {vix:.1f} calm — full exposure"
    if vix <= high:
        return cfg_float("overlay_exposure_normal", 0.85), f"VIX {vix:.1f} normal — 85% exposure"
    if vix <= fear:
        return cfg_float("overlay_exposure_high", 0.60), f"VIX {vix:.1f} elevated — 60% exposure"
    return cfg_float("overlay_exposure_fear", 0.35), (
        f"VIX {vix:.1f} extreme — 35% exposure. A momentum book in this regime is "
        f"paying noise to find out its stops were too tight")


# ─────────────────────────────────────────────────────────────────────────────
# 3. LIQUIDITY AND CIRCUIT-BAND ELIGIBILITY
# ─────────────────────────────────────────────────────────────────────────────

def liquidity_ok(stock: dict, planned_value: float) -> tuple[bool, str]:
    """
    Can this position be got OUT of at something like the plan?

    A STOP IS A PLAN FOR CONTINUOUS PRICES.

    Indian small caps do not always offer continuous prices. A breakout signal
    into a 5% circuit band cannot travel far enough to reach its target, and a
    held position that gaps through its stop realises a loss that is a multiple
    of the planned R — the broker-side stop converts to a market order and fills
    wherever the gap opens. Neither failure is visible in the entry logic, and
    both are pure left tail.

    This generates zero gross alpha. Its entire contribution is realisations
    that do not happen, which is why it is easy to under-value and why it is
    stated here rather than assumed.

    The test is the position against the name's ORDINARY traded value, not
    against an absolute rupee floor. Rs 4,000 is nothing in RELIANCE and is a
    meaningful share of a day's turnover in a micro-cap.
    """
    if not cfg_bool("overlay_liquidity_enabled", False):
        return True, "liquidity gate disabled"

    value_cr = float(stock.get("value_cr") or 0)
    if value_cr <= 0:
        # Unknown liquidity is refused rather than waved through. The names
        # whose turnover is unrecorded are disproportionately the illiquid ones.
        if cfg_bool("overlay_liquidity_strict", True):
            return False, "no traded-value data — cannot confirm an exit is possible"
        return True, "no traded-value data (strict mode off)"

    daily_value = value_cr * 1e7
    share = planned_value / daily_value if daily_value else 1.0
    max_share = cfg_float("overlay_max_share_of_daily_value", 0.005)   # 0.5%
    if share > max_share:
        return False, (f"position is {share:.2%} of the name's daily traded value "
                       f"(Rs {daily_value/1e7:.1f} cr) against a {max_share:.2%} limit — "
                       f"exiting at plan is not realistic")

    min_cr = cfg_float("overlay_min_daily_value_cr", 5.0)
    if value_cr < min_cr:
        return False, (f"daily traded value Rs {value_cr:.1f} cr is below the "
                       f"Rs {min_cr:.1f} cr floor — a stop here is a suggestion")

    # Circuit-band proxy. NSE does not publish the band in any feed this system
    # ingests, so it is inferred from realised range: a name repeatedly closing
    # at a round percentage from its open is band-limited. atr_pct is the
    # cheapest available signal and the threshold is deliberately loose.
    atr = float(stock.get("atr_pct") or 0)
    band_hint = cfg_float("overlay_band_atr_pct", 9.0)
    if atr and atr > band_hint:
        return False, (f"ATR {atr:.1f}% suggests a band-limited or gap-prone name — "
                       f"a stop cannot be relied on to fill near its level")
    return True, f"Rs {value_cr:.1f} cr daily value, position is {share:.3%} of it"
