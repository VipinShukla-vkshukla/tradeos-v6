"""
A below-floor class prior must fall back to the book's own distribution.

WHAT THIS CATCHES
------------------
`Prior` is a frozen dataclass with no `__bool__`/`__len__` override, so a
below-floor (`usable=False`) instance is still truthy. `Allocator._prior_for`
used `pri.get(class) or pri.get(book)`, which only falls through when `class`
is ABSENT from the dict — never when it is present-but-unusable. Since
`swing_priors()` populates a key for every `signal_type` with at least one
observation, a thin-but-present class prior (exactly the case the module's
own docstring describes falling back) could never reach the fallback. With
`planned_stop` only populated from 28-Jul-2026, almost every swing engine
bucket sits below the 30-sample floor today, so every one of them silently
resolved to a NEUTRAL prior (always a negative edge after costs) instead of
the book's usable distribution.

Demonstrated against the pre-fix code before trusting this: with the old
`or` chain, `test_below_floor_class_prior_falls_back_to_book` fails, matching
`SWING/CTL` (below floor) instead of `SWING/ALL` (usable).
"""

from __future__ import annotations

from tests import cfg_ctx


def _proposal(source: str, direction: str = "LONG"):
    from allocation.proposal import Proposal
    if direction == "SHORT":
        return Proposal(symbol="ZOMATO", framework="INTRADAY", product="MIS",
                        entry=250.0, stop=253.0, target=244.0, quantity=40,
                        source=source, native_rank=70.0, direction="SHORT")
    return Proposal(symbol="RELIANCE", framework="SWING", product="CNC",
                    entry=2500.0, stop=2450.0, target=2650.0, quantity=8,
                    source=source, native_rank=80.0, direction="LONG")


class NoDB:
    def table(self, *a, **k):
        raise RuntimeError("this test must not touch the database")


def test_below_floor_class_prior_falls_back_to_book():
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    with cfg_ctx():
        alloc = Allocator(sb=NoDB())
        alloc._priors = {
            # n=13 < the default floor of 30 -> below_floor=True, usable=False
            "SWING/CTL": Prior("SWING/CTL", 13, -0.208, -0.15, 0.09,
                               -0.6, 0.3, trigger_rate=0.4, below_floor=True,
                               note="needs 30 observations to be trusted"),
            "SWING/ALL": Prior("SWING/ALL", 340, 0.212, 0.18, 0.03,
                               -0.5, 1.1, trigger_rate=0.4),
        }
        prior = alloc._prior_for(_proposal("CTL"))
        assert prior.key == "SWING/ALL", (
            f"a below-floor class prior ({prior.key!r}) was used directly "
            f"instead of falling back to the usable book prior SWING/ALL — "
            f"the truthy-Prior bug is back")


def test_below_floor_short_class_prior_falls_back_to_short_book():
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    with cfg_ctx():
        alloc = Allocator(sb=NoDB())
        alloc._priors = {
            "INTRADAY/SDN/SHORT": Prior("INTRADAY/SDN/SHORT", 9, -0.30, -0.20,
                                        0.11, -1.2, 0.4, below_floor=True,
                                        note="needs 30 observations to be trusted"),
            "INTRADAY/ALL/SHORT": Prior("INTRADAY/ALL/SHORT", 90, 0.05, 0.02,
                                        0.05, -1.0, 1.0),
        }
        prior = alloc._prior_for(_proposal("SDN", direction="SHORT"))
        assert prior.key == "INTRADAY/ALL/SHORT", (
            f"a below-floor SHORT class prior ({prior.key!r}) was used "
            f"directly instead of falling back to INTRADAY/ALL/SHORT — and it "
            f"must never fall back to the LONG book either")


def test_usable_class_prior_is_still_preferred_over_book():
    """Regression guard: the fix must not over-correct to always using the book."""
    from allocation.allocator import Allocator
    from allocation.scoring import Prior

    with cfg_ctx():
        alloc = Allocator(sb=NoDB())
        alloc._priors = {
            "SWING/CTL": Prior("SWING/CTL", 45, 0.35, 0.30, 0.04, -0.4, 1.4,
                               trigger_rate=0.5),
            "SWING/ALL": Prior("SWING/ALL", 340, 0.05, 0.04, 0.03, -0.5, 1.1,
                               trigger_rate=0.4),
        }
        prior = alloc._prior_for(_proposal("CTL"))
        assert prior.key == "SWING/CTL", (
            f"a USABLE class prior was bypassed in favour of {prior.key!r} — "
            f"the most-specific-first ladder must still prefer it")


TESTS = [
    ("below-floor class prior falls back to book",
     test_below_floor_class_prior_falls_back_to_book),
    ("below-floor SHORT class prior falls back to SHORT book",
     test_below_floor_short_class_prior_falls_back_to_short_book),
    ("usable class prior still preferred over book",
     test_usable_class_prior_is_still_preferred_over_book),
]
