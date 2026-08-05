"""
LONG and SHORT, defined once, so eleven modules cannot each get it wrong.

WHY THIS MODULE EXISTS AT ALL
-----------------------------
`direction` has been a field on `Setup` and a column on `intraday_setups` since
migration 014. Every engine hardcoded "LONG" and nothing anywhere read it. That
is this project's dominant failure mode — a field nobody consumes — and it made
shorting look like a small change when it is not.

Shorting is not an engine. It is a sign convention that the following were all
written to assume away, each of them silently:

    Setup.risk_pct/reward_pct/rr   negative for a short, and rr returns 0.0
    cost_model.is_worth_taking     "target is at or below entry" -> refuse
    scoring.score                  stop >= entry -> edge None -> allocator DECLINE
    scoring.intraday_priors        skips rows where stop >= entry, so shorts
                                   never enter the prior at all
    exit_policy                    risk = entry - stop is NEGATIVE, so it falls
                                   back to a 0.5% default, and gain_r = (ltp -
                                   entry)/risk goes POSITIVE as price rises —
                                   a short bleeding money reads as a winner and
                                   the engine trails its stop the wrong way
    outcomes.resolve_day           lo <= stop is true on the first bar when the
                                   stop sits above entry, so EVERY short would
                                   resolve as an instant STOP and poison the
                                   learning loop
    market_context                 allow_longs, with no short equivalent
    market_structure               gate_long only — a short would be blocked by
                                   the downtrend it wants
    engine._track_excursion        high_water_mark; a short's best excursion is
                                   a LOW
    square_off_paper               sells to flatten; a short is flattened by
                                   BUYING

Adding a short engine on top of that would produce a system that finds shorts,
scores them as incoherent, refuses them at the allocator, and — if any survived
— trailed them backwards and recorded every one as a loss.

THE TRANSFORM
-------------
`sign(direction)` is +1 for LONG and -1 for SHORT, and every piece of arithmetic
above is expressed in terms of it. The long path is bit-identical at sign=+1,
which `tools/health.py::check_direction_symmetry` asserts by running both paths
and comparing, rather than by anyone believing this paragraph.

    risk    = sign * (entry - stop)          positive in both directions
    gain_r  = sign * (ltp - entry) / risk    positive when the trade is winning
    better  = sign * (a - b) > 0             "a is a better price than b"
    mfe     = max/min favourable price       one concept, two implementations

WHAT IS NOT SYMMETRIC, AND MUST NEVER BE MADE TO LOOK SYMMETRIC
----------------------------------------------------------------
Two things about an Indian cash-market short have no long equivalent, and both
are about being unable to get out rather than about being wrong:

  1. FAILING TO COVER IS NOT A BAD TRADE, IT IS A PENALTY. A long left open at
     the close becomes delivery — you need the cash, which is inconvenient. A
     short left open is SHORT DELIVERY: the exchange buys it back for you in the
     auction session and the penalty runs to ~20% of the value, dwarfing any
     stop. So a short's square-off is a safety control, not a policy, and it
     gets its own earlier deadline (`intraday_short_cover_buffer_min`).

  2. A LONG'S WORST CASE IS BOUNDED AND A SHORT'S IS NOT. A long can lose 100%.
     A short's loss is unbounded in theory and, far more practically, a stock
     locked at its UPPER circuit cannot be covered at any price — you are held
     to the close and into the auction regardless of your stop. NSE circuit
     bands are in no feed this system ingests (see migration 040), so this is
     guarded by proxy in `shortability.py`, and the proxy is named as a proxy.

`is_short()` is deliberately explicit everywhere rather than defaulted. A
missing direction reads as LONG — which is the SAFE default here, because the
long path is the one with a bounded loss and no auction penalty.
"""

from __future__ import annotations

LONG, SHORT = "LONG", "SHORT"


def normalise(direction: str | None) -> str:
    """
    Anything unrecognised becomes LONG.

    Deliberately, and this is the one place the asymmetry favours a default. A
    row whose direction is NULL, empty, or misspelled is far more likely to be
    a pre-shorting record — every setup written before this module existed — than
    a short whose label was lost. Reading those as SHORT would invert the sign on
    the entire history at once. Reading a genuine short as LONG is caught by the
    coherence check in `validate()`, because its stop sits above its entry.
    """
    d = (direction or "").strip().upper()
    return SHORT if d == SHORT else LONG


def sign(direction: str | None) -> int:
    """+1 for LONG, -1 for SHORT. The only place this mapping is written."""
    return -1 if normalise(direction) == SHORT else 1


def is_short(direction: str | None) -> bool:
    return normalise(direction) == SHORT


def entry_side(direction: str | None) -> str:
    """The order side that OPENS this position."""
    return "SELL" if is_short(direction) else "BUY"


def exit_side(direction: str | None) -> str:
    """The order side that CLOSES it. A short is flattened by buying."""
    return "BUY" if is_short(direction) else "SELL"


def risk_per_share(entry: float, stop: float, direction: str | None) -> float:
    """
    Distance from entry to stop, always positive when the stop is on the correct
    side. Returns 0.0 when it is not, so callers can refuse rather than divide
    by a negative and get a confidently wrong R.
    """
    r = sign(direction) * (entry - stop)
    return r if r > 0 else 0.0


def reward_per_share(entry: float, target: float, direction: str | None) -> float:
    """Distance from entry to target, positive when the target is profitable."""
    r = sign(direction) * (target - entry)
    return r if r > 0 else 0.0


def gain_r(entry: float, ltp: float, risk: float, direction: str | None) -> float:
    """How many R this position is up. Positive means winning, both directions."""
    if not risk:
        return 0.0
    return sign(direction) * (ltp - entry) / risk


def gain_pct(entry: float, ltp: float, direction: str | None) -> float:
    """Percent move in the position's favour. Positive means winning."""
    if not entry:
        return 0.0
    return sign(direction) * (ltp - entry) / entry * 100.0


def is_better_price(a: float, b: float, direction: str | None) -> bool:
    """
    Is `a` a more favourable price than `b` for this position?

    For a long a higher stop is tighter; for a short a lower one is. Every
    "only move the stop forwards" comparison in the exit ladder goes through
    here, because that is the comparison that silently loosens a short's stop
    every cycle if it is written as `>`.
    """
    return sign(direction) * (a - b) > 0


def better_of(a: float, b: float, direction: str | None) -> float:
    """The more favourable of two prices."""
    return a if is_better_price(a, b, direction) else b


def favourable_excursion(prev: float | None, ltp: float,
                         entry: float, direction: str | None) -> float:
    """
    The best price this position has seen — a HIGH for a long, a LOW for a short.

    `high_water_mark` keeps its column name because renaming a live column to
    say "most favourable excursion" would be a migration touching every writer
    for no behavioural gain. What it MEANS is direction-dependent, and that
    meaning lives here rather than in each caller's head.
    """
    base = prev if prev else entry
    return better_of(base, ltp, direction)


def validate(entry: float, stop: float, target: float,
             direction: str | None) -> tuple[bool, str]:
    """
    Are these levels coherent for this direction?

    The check every consumer needs and each was writing for itself in the long
    form (`stop >= entry or target <= entry`). Stated once, so a short is
    refused for being genuinely incoherent and never for being a short.
    """
    d = normalise(direction)
    if entry <= 0:
        return False, "entry is not a price"
    if risk_per_share(entry, stop, d) <= 0:
        return False, (f"{d}: stop {stop} is on the wrong side of entry {entry} "
                       f"— a {d} stops {'above' if d == SHORT else 'below'} entry")
    if reward_per_share(entry, target, d) <= 0:
        return False, (f"{d}: target {target} is on the wrong side of entry {entry} "
                       f"— a {d} targets {'below' if d == SHORT else 'above'} entry")
    return True, "coherent"
