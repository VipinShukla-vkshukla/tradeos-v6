"""
Entry budgets get an EARLY RESERVE, not a minimum gap (11-Aug-2026, revised
the same day it first shipped).

WHAT THE FIRST VERSION GOT WRONG
----------------------------------
swing_entry_min_gap_minutes blocked any entry arriving within a fixed
window of the LAST one. Checked against the actual 10-Aug sequence it was
built from, that was the wrong rule: the allocator scored 8 proposals
together at 09:36:23 ("3 to take, 5 refused") and picked AUBANK and SCI as
two of that day's three best trades in ONE joint, opportunity-cost-aware
decision — two seconds apart. A 20-minute gap would have let AUBANK through
and refused SCI for 20 minutes for no reason at all, overriding a decision
the allocator had already made well, at zero benefit. (VIJAYA, the third
slot, genuinely DID need to out-compete rivals across three separate
allocator passes over the next ~40 seconds before winning it — the rising
bar was already doing that job correctly on its own.)

What actually went wrong was that ALL THREE of the day's slots were spent
on a single snapshot of the first ~21 minutes (itself mostly a symptom of
the Kite token being stale until then), leaving nothing for the remaining
~5.5 hours regardless of what showed up later. order_manager.entry_reserved()
targets THAT directly: a cap on how many of today's entries may land before
a cutoff clock time, with NO restriction on how close together they are
otherwise, before or after the cutoff.

WHAT THIS CATCHES
------------------
These tests pin: the reserve blocks only when the count is already at the
cap AND the clock is still before cutoff; it never blocks after cutoff,
however many entries have landed; two or three candidates arriving in the
same instant are NOT penalised relative to each other — only the COUNT
before cutoff matters, never the gap between them (the direct fix for the
AUBANK/SCI case); reserve_n=0 (or an unparseable cutoff) is the exact
rollback lever, restoring count-only behaviour; and parse_hhmm() degrades
to None rather than raising on a bad config value.
"""

from __future__ import annotations

from datetime import datetime, time

from config import IST


# ── parse_hhmm() ─────────────────────────────────────────────────────────

def test_parse_hhmm_reads_a_clean_value():
    from execution.order_manager import parse_hhmm
    assert parse_hhmm("10:30") == time(10, 30)


def test_parse_hhmm_degrades_to_none_on_garbage():
    from execution.order_manager import parse_hhmm
    assert parse_hhmm("not a time") is None
    assert parse_hhmm("") is None
    assert parse_hhmm(None) is None


# ── entry_reserved(), pure ───────────────────────────────────────────────

def test_blocked_when_at_cap_before_cutoff():
    from execution.order_manager import entry_reserved
    now = datetime(2026, 8, 11, 9, 40, 0, tzinfo=IST)
    blocked, why = entry_reserved(1, now, max_before_cutoff=1, cutoff=time(10, 30))
    assert blocked is True
    assert "10:30" in why


def test_clear_once_past_cutoff_however_many_taken():
    from execution.order_manager import entry_reserved
    now = datetime(2026, 8, 11, 10, 31, 0, tzinfo=IST)
    blocked, _ = entry_reserved(5, now, max_before_cutoff=1, cutoff=time(10, 30))
    assert blocked is False, "past the cutoff, the reserve has no opinion at all"


def test_exactly_at_cutoff_is_past_it():
    from execution.order_manager import entry_reserved
    now = datetime(2026, 8, 11, 10, 30, 0, tzinfo=IST)
    blocked, _ = entry_reserved(5, now, max_before_cutoff=1, cutoff=time(10, 30))
    assert blocked is False


def test_clear_below_the_cap_even_before_cutoff():
    from execution.order_manager import entry_reserved
    now = datetime(2026, 8, 11, 9, 40, 0, tzinfo=IST)
    blocked, _ = entry_reserved(0, now, max_before_cutoff=1, cutoff=time(10, 30))
    assert blocked is False, "n_today=0 must never be refused — nothing spent yet"


def test_zero_reserve_restores_count_only_behaviour():
    from execution.order_manager import entry_reserved
    now = datetime(2026, 8, 11, 9, 16, 0, tzinfo=IST)
    blocked, _ = entry_reserved(99, now, max_before_cutoff=0, cutoff=time(10, 30))
    assert blocked is False, "max_before_cutoff=0 must be the exact rollback lever"


def test_none_cutoff_restores_count_only_behaviour():
    """An unparseable config value must fail OPEN (no restriction), not closed."""
    from execution.order_manager import entry_reserved
    now = datetime(2026, 8, 11, 9, 16, 0, tzinfo=IST)
    blocked, _ = entry_reserved(99, now, max_before_cutoff=1, cutoff=None)
    assert blocked is False


def test_two_simultaneous_entries_are_not_paced_against_each_other():
    """THE DIRECT FIX for the AUBANK/SCI case: with a reserve of 2, taking
    a first and then immediately a second (same second, same allocator
    pass) is exactly as allowed as taking them minutes apart — only the
    THIRD, early, separate one is refused."""
    from execution.order_manager import entry_reserved
    now = datetime(2026, 8, 10, 9, 36, 24, tzinfo=IST)
    # First entry: n_today=0 going in.
    blocked1, _ = entry_reserved(0, now, max_before_cutoff=2, cutoff=time(10, 30))
    assert blocked1 is False
    # Second entry, TWO SECONDS LATER, n_today now 1: still clear, because
    # the reserve counts HOW MANY, not HOW SOON.
    now2 = datetime(2026, 8, 10, 9, 36, 26, tzinfo=IST)
    blocked2, _ = entry_reserved(1, now2, max_before_cutoff=2, cutoff=time(10, 30))
    assert blocked2 is False, (
        "a gap-based rule would have refused this — the reserve must not")
    # A third, n_today now 2, at the cap: refused, regardless of gap.
    now3 = datetime(2026, 8, 10, 9, 37, 3, tzinfo=IST)
    blocked3, _ = entry_reserved(2, now3, max_before_cutoff=2, cutoff=time(10, 30))
    assert blocked3 is True


def test_the_actual_10aug_trace_with_the_shipped_default():
    """Regression pin for the real incident, with swing's actual shipped
    default (max_before_cutoff=1, cutoff=10:30): AUBANK (first, n_today=0)
    clears; SCI (n_today=1, two seconds later) is now refused — but ONLY
    because the default reserve is 1, not because of the gap. Documents
    the real, intentional behaviour change from the first version: the
    first version let AUBANK through and blocked SCI for 20 minutes for
    the WRONG reason (timing); this version blocks SCI for the RIGHT
    reason (reserve exhausted) and would instead allow it if
    swing_max_entries_before_time were raised to 2."""
    from execution.order_manager import entry_reserved
    aubank_at = datetime(2026, 8, 10, 9, 36, 24, tzinfo=IST)
    sci_at = datetime(2026, 8, 10, 9, 36, 26, tzinfo=IST)
    blocked_aubank, _ = entry_reserved(0, aubank_at, max_before_cutoff=1, cutoff=time(10, 30))
    blocked_sci, _ = entry_reserved(1, sci_at, max_before_cutoff=1, cutoff=time(10, 30))
    assert blocked_aubank is False
    assert blocked_sci is True
    # But raise the reserve to 2 (the operator's call, a one-line config
    # change, not a code change) and both go through:
    blocked_sci_at_2, _ = entry_reserved(1, sci_at, max_before_cutoff=2, cutoff=time(10, 30))
    assert blocked_sci_at_2 is False


TESTS = [
    ("parse_hhmm reads a clean value", test_parse_hhmm_reads_a_clean_value),
    ("parse_hhmm degrades to None on garbage", test_parse_hhmm_degrades_to_none_on_garbage),
    ("blocked when at cap before cutoff", test_blocked_when_at_cap_before_cutoff),
    ("clear once past cutoff however many taken",
     test_clear_once_past_cutoff_however_many_taken),
    ("exactly at cutoff is past it", test_exactly_at_cutoff_is_past_it),
    ("clear below the cap even before cutoff", test_clear_below_the_cap_even_before_cutoff),
    ("zero reserve restores count-only behaviour",
     test_zero_reserve_restores_count_only_behaviour),
    ("None cutoff restores count-only behaviour",
     test_none_cutoff_restores_count_only_behaviour),
    ("two simultaneous entries are not paced against each other",
     test_two_simultaneous_entries_are_not_paced_against_each_other),
    ("the actual 10-Aug trace with the shipped default",
     test_the_actual_10aug_trace_with_the_shipped_default),
]
