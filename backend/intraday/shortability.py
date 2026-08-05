"""
Can this name be shorted TODAY — and, more importantly, can it be COVERED?

    can_short(ctx, stock_row, minutes_left) -> (ok, reason, notes)

THE QUESTION IS NOT "WILL IT FALL"
-----------------------------------
The engines answer that. This module answers the one that has no long
equivalent: if the trade goes wrong, can you get out at all?

A long that goes wrong is sold. Worst case you take delivery, need the cash,
and sell tomorrow. A short that goes wrong may be IMPOSSIBLE to close:

  · A stock locked at its UPPER circuit has buyers queued and no sellers. There
    is no price at which you can buy to cover. Your stop is a number in a
    database; the exchange does not care.
  · You are then carried to the close with an open short, which is SHORT
    DELIVERY. The exchange buys it back for you in the auction session and
    charges you the difference plus a penalty — commonly ~20% of the trade
    value, and there is no cap that helps you.

So this is not a quality filter, it is a solvency filter, and it runs before
conviction is ever considered.

WHAT THIS SYSTEM CAN AND CANNOT SEE — STATED, NOT ASSUMED
----------------------------------------------------------
**NSE circuit bands are in no feed this system ingests.** That is not a
guess — migration 040 records it explicitly while introducing
`overlay_band_atr_pct` as a deliberate proxy. So the upper-circuit guard here is
a PROXY and is labelled as one everywhere it appears. It uses two things that
ARE available:

    today's move from prev_close     how much of a plausible band is spent
    daily ATR%                       how wide that band is likely to be

A name already up 6% intraday is either in a band-limited move or in one strong
enough that the marginal short is a bet against demonstrated demand. Both are
reasons to stand aside, so the proxy does not need to be precise to be useful —
it needs to be conservative, and it is.

Anything here that would be better with real band data is marked `PROXY:` in its
note, so a future feed upgrade has a checklist rather than an archaeology
project.

WHY DELIVERY PERCENTAGE INVERTS
--------------------------------
`scanner.py` treats high delivery as a QUALITY signal — a name where most volume
is delivered is being accumulated rather than churned, which is what you want to
be long. For a short it is the opposite reading of the same number: stock that
has gone into delivery is stock that is no longer available to be sold, and a
tightly held name with a small tradeable float is the definition of a squeeze
candidate. The same column, read in the opposite direction, because the question
is different.

WHY THE LIQUIDITY BAR IS HIGHER THAN FOR A LONG
------------------------------------------------
Covering is COMPULSORY and time-boxed; selling a long is neither. A name you can
enter and not exit is a bad long and an unacceptable short, so the traded-value
floor is multiplied rather than reused.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import cfg_bool, cfg_float, cfg_int


def can_short(ctx, stock: dict | None = None,
              minutes_left: int | None = None) -> tuple[bool, str, list[str]]:
    """
    Returns (allowed, reason, notes).

    `reason` is the FIRST disqualification, phrased for the operator. `notes`
    carries every observation made, including the ones that passed, so a refusal
    is auditable and a promotion argument has evidence behind it.

    Ordered by what disqualifies fastest and most cheaply, and — where two
    checks are equally cheap — by which one being wrong costs more.
    """
    stock = stock or {}
    notes: list[str] = []
    sym = getattr(ctx, "symbol", "?")

    # ── 1. SURVEILLANCE. Hard exclusion, and not for the usual reason ───────
    #
    # The scanner already drops ASM and F&O-ban names from the universe, so this
    # is belt-and-braces for any caller that bypasses it. It is repeated here
    # because the consequence differs: for a long, ASM means a wider spread and
    # a margin penalty. For a short it means the exchange has already identified
    # the name as one where price is being manipulated — which is precisely the
    # population where an engineered squeeze happens, and where an upper-circuit
    # lock is most likely.
    if stock.get("asm_flag") or stock.get("gsm_flag"):
        return False, (f"{sym} is under ASM/GSM surveillance — the exchange has "
                       f"flagged this name for exactly the price behaviour that "
                       f"traps a short"), notes
    if stock.get("fo_ban_flag"):
        return False, (f"{sym} is in the F&O ban period — cash and futures "
                       f"decouple while the ban runs, and the unwind is violent "
                       f"in both directions"), notes
    notes.append("surveillance: clear")

    # ── 2. RUNWAY. A short must be covered, and covering takes time ─────────
    #
    # A long opened with twenty minutes left is a mediocre trade. A short opened
    # with twenty minutes left is a race against the auction, and the cover
    # deadline is already earlier than the long exit by
    # intraday_short_cover_lead_min. So a short needs MORE runway than a long,
    # not the same amount, and it is checked before anything expensive.
    if minutes_left is not None:
        need = cfg_int("intraday_short_min_runway_min", 75)
        if minutes_left < need:
            return False, (f"{minutes_left} min to the cover deadline, and a short "
                           f"needs {need} — an uncovered short goes to auction, so "
                           f"the runway requirement is not the same as a long's"), notes
        notes.append(f"runway: {minutes_left} min (needs {need})")

    # ── 3. THE UPPER-CIRCUIT PROXY. The one that cannot be undone ───────────
    #
    # PROXY — see the module docstring. Real bands are in no feed here.
    prev_close = getattr(ctx, "prev_close", None)
    ltp = getattr(ctx, "ltp", None)
    if prev_close and ltp:
        up_pct = (ltp - prev_close) / prev_close * 100.0
        band_hint = cfg_float("overlay_band_atr_pct", 9.0)
        # Two thresholds, and the tighter one wins. The absolute figure catches
        # a quiet name approaching a 5% or 10% band; the ATR-relative one
        # catches a volatile name whose band is wide but whose move is already
        # extreme for its own behaviour.
        cap_abs = cfg_float("intraday_short_max_up_pct", 4.0)
        atr = getattr(ctx, "atr_pct_daily", None)
        cap_atr = (atr * cfg_float("intraday_short_max_up_atr_mult", 1.5)) if atr else None
        cap = min(x for x in (cap_abs, cap_atr) if x) if cap_atr else cap_abs

        if up_pct >= cap:
            return False, (
                f"{sym} is +{up_pct:.1f}% today against a {cap:.1f}% ceiling — "
                f"PROXY for circuit-band risk, which this system has no feed for. "
                f"A name locked at its upper circuit has buyers queued and NO "
                f"sellers, so there is no price at which a short can be covered "
                f"and the position is carried into the auction regardless of its "
                f"stop"), notes
        notes.append(f"PROXY upper-band: +{up_pct:.1f}% of a {cap:.1f}% ceiling"
                     + (f" (ATR {atr:.1f}%)" if atr else " (no ATR — absolute cap only)"))

        # A name that has ALREADY collapsed is a different refusal: the easy
        # money is gone and what remains is bounce risk. Shorting the fourth
        # consecutive down-leg is where a short book gives back its month.
        floor_pct = cfg_float("intraday_short_max_down_pct", 6.0)
        if -up_pct >= floor_pct:
            return False, (f"{sym} is already {up_pct:.1f}% down — the move this "
                           f"setup wants has largely happened, and what is left is "
                           f"a bounce that covers into a thin book"), notes

    # ── 4. SQUEEZE RISK — the same delivery number, read the other way ──────
    dp = stock.get("delivery_pct")
    if dp is not None:
        try:
            dp = float(dp)
            ceiling = cfg_float("intraday_short_max_delivery_pct", 75.0)
            if dp >= ceiling:
                return False, (
                    f"{sym} delivers {dp:.0f}% of its volume against a {ceiling:.0f}% "
                    f"ceiling — stock in delivery is stock no longer available to "
                    f"trade, and a tightly held float is the definition of a "
                    f"squeeze candidate. The scanner reads this same number as "
                    f"QUALITY for a long; for a short it is the risk"), notes
            notes.append(f"delivery: {dp:.0f}% (squeeze ceiling {ceiling:.0f}%)")
        except (TypeError, ValueError):
            pass

    # ── 5. LIQUIDITY, at a higher bar than a long requires ──────────────────
    #
    # Covering is compulsory and time-boxed. Reuses the existing traded-value
    # measure so the two books cannot disagree about what "liquid" means, then
    # multiplies the floor, because the same name can be an acceptable long and
    # an unacceptable short.
    value_cr = getattr(ctx, "value_cr", None) or stock.get("value_cr")
    if value_cr is not None:
        try:
            value_cr = float(value_cr)
            base  = cfg_float("intraday_min_value_cr", 25.0)
            mult  = cfg_float("intraday_short_liquidity_mult", 2.0)
            floor = base * mult
            if value_cr < floor:
                return False, (f"{sym} trades Rs {value_cr:.0f}cr against a Rs "
                               f"{floor:.0f}cr floor for shorts ({mult:g}x the long "
                               f"floor) — a name you can enter and not exit is a bad "
                               f"long and an unacceptable short"), notes
            notes.append(f"liquidity: Rs {value_cr:.0f}cr (short floor Rs {floor:.0f}cr)")
        except (TypeError, ValueError):
            pass

    return True, "shortable", notes


def describe_gates() -> list[tuple[str, str, str]]:
    """
    (key, current value, what it protects) for every switch this module reads.

    Exists so `tools/health.py` can assert that each one is actually consulted,
    rather than trusting that a CRITICAL key in system_config is doing something.
    Config keys nobody reads are this project's most-repeated defect and two of
    them have already reached production wearing a CRITICAL label.
    """
    from config import cfg
    return [
        ("intraday_short_min_runway_min", cfg("intraday_short_min_runway_min", "75"),
         "minutes needed to cover before the deadline"),
        ("intraday_short_max_up_pct", cfg("intraday_short_max_up_pct", "4.0"),
         "PROXY upper-circuit ceiling — absolute"),
        ("intraday_short_max_up_atr_mult", cfg("intraday_short_max_up_atr_mult", "1.5"),
         "PROXY upper-circuit ceiling — in the name's own ATR"),
        ("intraday_short_max_down_pct", cfg("intraday_short_max_down_pct", "6.0"),
         "how far already fallen before the bounce outweighs the trend"),
        ("intraday_short_max_delivery_pct", cfg("intraday_short_max_delivery_pct", "75.0"),
         "squeeze risk from a tightly held float"),
        ("intraday_short_liquidity_mult", cfg("intraday_short_liquidity_mult", "2.0"),
         "how much more liquid a short must be than a long"),
    ]
