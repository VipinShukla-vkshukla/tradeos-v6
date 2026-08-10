"""
`config.capital_for()` is the mechanism that answers the operator's own
question, 07-Aug-2026: when intraday goes LIVE, does TradeOS reserve each
book's capital first so neither can spend the other's, and does it respect
the real account rather than a number that has drifted from it?

The function already does this — sleeve reserved for intraday, swing takes
the remainder, refuses rather than going negative — and `tools/validate_config`
already anticipates the exact configuration live today (a ₹1,00,000 paper
sleeve on a ~₹30,000 account) as a WARN that becomes an ERROR the moment
`intraday_trading_mode` flips to LIVE. What was missing is a permanent check
that this branching logic itself stays correct — the same gap this session
found repeatedly elsewhere (alloc_max_slots pooling, check_new_entry pooling)
was never a "does the design make sense" problem, it was "does the code still
do what the design says" going unchecked. This is that check, added before
the mechanism has ever been exercised live rather than after.
"""

from __future__ import annotations

from contextlib import contextmanager

from tests import cfg_ctx


@contextmanager
def _total_capital(value: float):
    """capital_for() reads config.TOTAL_CAPITAL, a module constant set once at
    import time from the environment — not a system_config key, so cfg_ctx()
    alone cannot vary it. Patched directly and restored, the same discipline
    cfg_ctx applies to _sys_config."""
    import config
    saved = config.TOTAL_CAPITAL
    config.TOTAL_CAPITAL = value
    try:
        yield
    finally:
        config.TOTAL_CAPITAL = saved


def test_intraday_gets_its_own_sleeve_regardless_of_swing():
    from config import capital_for
    with _total_capital(30000.0), cfg_ctx({"intraday_capital": "100000"}):
        assert capital_for("INTRADAY") == 100000.0, (
            "intraday must size against its own configured sleeve even when "
            "it is far larger than the real account — harmless while PAPER, "
            "and exactly the configuration live today")


def test_swing_gets_the_whole_account_while_intraday_is_paper():
    """Today's actual state: a simulated position holds no rupees, so
    reserving a sleeve against it would idle real capital for nothing."""
    from config import capital_for
    with _total_capital(30000.0), cfg_ctx({"intraday_capital": "100000",
                                            "intraday_trading_mode": "PAPER"}):
        assert capital_for("SWING") == 30000.0, (
            f"swing must size against the full account while intraday is "
            f"PAPER, got {capital_for('SWING')} — an oversized paper sleeve "
            f"must not shrink the live book")


def test_swing_takes_the_remainder_once_intraday_is_live():
    from config import capital_for
    with _total_capital(30000.0), cfg_ctx({"intraday_capital": "12000",
                                            "intraday_trading_mode": "LIVE"}):
        assert capital_for("SWING") == 18000.0, (
            f"expected TOTAL_CAPITAL(30000) - intraday_capital(12000) = "
            f"18000, got {capital_for('SWING')} — the two books must never "
            f"be able to size against the same rupee")


def test_the_books_never_sum_to_more_than_total_capital_once_live():
    """The property the operator actually asked for: neither book can block
    or overdraw the other, and together they never exceed the real account."""
    from config import capital_for
    with _total_capital(30000.0), cfg_ctx({"intraday_capital": "12000",
                                            "intraday_trading_mode": "LIVE"}):
        total = capital_for("SWING") + capital_for("INTRADAY")
        assert total <= 30000.0, (
            f"swing ({capital_for('SWING')}) + intraday ({capital_for('INTRADAY')}) "
            f"= {total}, which exceeds TOTAL_CAPITAL 30000 — the two books "
            f"can double-commit the same rupee")


def test_oversized_sleeve_starves_swing_to_zero_not_negative():
    """The guardrail config.py's own docstring describes: refuse loudly
    rather than let swing size against negative capital.

    THE WARNING IS CAPTURED, NOT LET LOOSE — 10-Aug-2026. This test sets
    intraday LIVE on purpose to make `capital_for` emit its starvation ERROR,
    and that ERROR was printing into every `tools.verify` run. The operator
    read a green 150/150 with a red "intraday_capital >= TOTAL_CAPITAL while
    intraday is LIVE" in the middle of it and reasonably took it for a live
    misconfiguration; the live config has intraday on PAPER and is fine.

    An ERROR line inside a PASSING run is not a harmless cosmetic issue — it
    is how a reader is trained to scroll past red text, which is exactly the
    habit that lets a real failure hide. Suppressed for the duration of the
    assertion only, so the guard is still exercised and still proven to fire.
    """
    from loguru import logger
    from config import capital_for
    import config as _cfg
    _cfg._capital_warned.discard("swing_starved")
    logger.disable("config")
    try:
        with _total_capital(30000.0), cfg_ctx({"intraday_capital": "100000",
                                                "intraday_trading_mode": "LIVE"}):
            starved = capital_for("SWING")
    finally:
        logger.enable("config")
    with _total_capital(30000.0), cfg_ctx({"intraday_capital": "100000",
                                            "intraday_trading_mode": "LIVE"}):
        assert starved == 0.0, (
            f"an intraday sleeve >= the whole account, while LIVE, must "
            f"leave swing exactly 0 (refuse every entry), got "
            f"{capital_for('SWING')} — a negative sleeve would be worse "
            f"than a loud refusal")


def test_explicit_swing_capital_override_wins_even_while_paper():
    """swing_capital, if set, is the dashboard's exact-split escape hatch —
    must override the derived 'whole account while paper' default too."""
    from config import capital_for
    with _total_capital(30000.0), cfg_ctx({"swing_capital": "20000",
                                            "intraday_capital": "10000",
                                            "intraday_trading_mode": "PAPER"}):
        assert capital_for("SWING") == 20000.0, (
            f"an explicit swing_capital override must win over the derived "
            f"whole-account default, got {capital_for('SWING')}")


TESTS = [
    ("intraday gets its own sleeve regardless of swing",
     test_intraday_gets_its_own_sleeve_regardless_of_swing),
    ("swing gets the whole account while intraday is paper",
     test_swing_gets_the_whole_account_while_intraday_is_paper),
    ("swing takes the remainder once intraday is live",
     test_swing_takes_the_remainder_once_intraday_is_live),
    ("the books never sum to more than TOTAL_CAPITAL once live",
     test_the_books_never_sum_to_more_than_total_capital_once_live),
    ("oversized sleeve starves swing to zero, not negative",
     test_oversized_sleeve_starves_swing_to_zero_not_negative),
    ("explicit swing_capital override wins even while paper",
     test_explicit_swing_capital_override_wins_even_while_paper),
]
