"""
Per-engine exit curation — load_intraday_policy(engine=...) (docs/FINDINGS.md,
10-Sep-2026). Extends the per-engine config-prefix pattern every engine
already uses for its own ENTRY parameters (gap_min_pct, ign_target_r, ...)
to the previously-pooled MANAGEMENT rungs (giveback, trail, partial-book,
breakeven, time stop).

The regression that matters most: an engine with no override reads back
byte-identical to the pooled call. That's the literal proof "cannot impact
other engines," not a description of intent.
"""

from __future__ import annotations

from tests import cfg_ctx

SCOPED_KEYS = [
    "giveback_pct", "giveback_min_r", "trail_r", "trail_after_r",
    "partial_book_r", "partial_book_pct", "breakeven_at_r",
    "time_stop_min", "time_stop_min_r",
]

POOLED_ONLY_KEYS = [
    "target_r", "use_setup_target", "move_to_breakeven", "check_invalidation",
    "must_exit_time", "squareoff_buffer", "cost_buffer_pct",
    "short_runway_tighten_enabled", "volume_decay_enabled",
]


def test_no_engine_argument_matches_no_engine_call():
    """load_intraday_policy() and load_intraday_policy(engine=None) must be
    the same call — the default parameter must not change existing callers."""
    from intraday.exit_policy import load_intraday_policy
    assert load_intraday_policy() == load_intraday_policy(engine=None)


def test_unset_engine_is_byte_identical_to_pooled():
    """An engine with no override anywhere in system_config reads back
    EXACTLY the pooled policy -- the literal proof this cannot impact any
    engine that hasn't opted in."""
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"intraday_giveback_pct": "30", "intraday_trail_r": "1.0"}):
        pooled = load_intraday_policy()
        for eng in ("ORB", "GAP", "PDL", "VCE", "PBK", "VWR", "RNG", "SDN",
                   "GDB", "IGN"):
            assert load_intraday_policy(engine=eng) == pooled, (
                f"{eng} with no override diverged from the pooled policy")


def test_empty_string_engine_behaves_like_none():
    from intraday.exit_policy import load_intraday_policy
    assert load_intraday_policy(engine="") == load_intraday_policy()


def test_engine_override_is_used_for_scoped_keys():
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"intraday_giveback_pct": "30", "ign_giveback_pct": "0"}):
        ign = load_intraday_policy(engine="IGN")
        assert ign["giveback_pct"] == 0.0


def test_engine_override_is_case_insensitive():
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"intraday_giveback_pct": "30", "ign_giveback_pct": "0"}):
        assert load_intraday_policy(engine="ign")["giveback_pct"] == 0.0
        assert load_intraday_policy(engine="Ign")["giveback_pct"] == 0.0


def test_one_engines_override_does_not_leak_into_another():
    """The explicit cross-contamination check: an IGN-only override must
    never be visible to a different engine, or the pooled call."""
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"intraday_giveback_pct": "30", "ign_giveback_pct": "0"}):
        assert load_intraday_policy(engine="IGN")["giveback_pct"] == 0.0
        assert load_intraday_policy(engine="GAP")["giveback_pct"] == 30.0
        assert load_intraday_policy(engine="SDN")["giveback_pct"] == 30.0
        assert load_intraday_policy()["giveback_pct"] == 30.0


def test_every_scoped_key_is_independently_overridable():
    from intraday.exit_policy import load_intraday_policy
    overrides = {
        "ign_giveback_pct": "10", "ign_giveback_min_r": "0.8",
        "ign_trail_r": "2.0", "ign_trail_after_r": "1.0",
        "ign_partial_book_r": "1.5", "ign_partial_book_pct": "40",
        "ign_breakeven_at_r": "1.5", "ign_time_stop_minutes": "60",
        "ign_time_stop_min_r": "0.2",
    }
    with cfg_ctx(overrides):
        p = load_intraday_policy(engine="IGN")
        assert p["giveback_pct"] == 10.0
        assert p["giveback_min_r"] == 0.8
        assert p["trail_r"] == 2.0
        assert p["trail_after_r"] == 1.0
        assert p["partial_book_r"] == 1.5
        assert p["partial_book_pct"] == 40.0
        assert p["breakeven_at_r"] == 1.5
        assert p["time_stop_min"] == 60
        assert p["time_stop_min_r"] == 0.2


def test_pooled_only_keys_never_read_an_engine_prefix():
    """target_r and the rest stay pooled-only by design (no replay evidence
    to scope them yet) -- an engine-prefixed key for one of these must be
    silently ignored, not accidentally picked up."""
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"intraday_target_r": "2.0", "ign_target_r": "99.0"}):
        assert load_intraday_policy(engine="IGN")["target_r"] == 2.0


def test_breakeven_at_r_fallback_chain_preserved_per_engine():
    """breakeven_at_r's own nested fallback (defaults to partial_book_r when
    unset) must still work THROUGH an engine override on partial_book_r,
    exactly as it does for the pooled call today."""
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"ign_partial_book_r": "1.7"}):
        p = load_intraday_policy(engine="IGN")
        assert p["breakeven_at_r"] == 1.7


def test_breakeven_at_r_own_key_still_wins_over_the_fallback():
    """An explicit ign_breakeven_at_r must still win over the
    partial_book_r-derived fallback, engine-scoped exactly as pooled."""
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"ign_partial_book_r": "1.7", "ign_breakeven_at_r": "2.2"}):
        assert load_intraday_policy(engine="IGN")["breakeven_at_r"] == 2.2


def test_pooled_breakeven_fallback_unchanged_by_this_change():
    """The pooled (no engine) call's own nested fallback -- unrelated to any
    engine override -- must read exactly as it did before this existed."""
    from intraday.exit_policy import load_intraday_policy
    with cfg_ctx({"intraday_partial_book_r": "1.3"}):
        assert load_intraday_policy()["breakeven_at_r"] == 1.3
    with cfg_ctx({"intraday_partial_book_r": "1.3", "intraday_breakeven_at_r": "1.9"}):
        assert load_intraday_policy()["breakeven_at_r"] == 1.9


TESTS = [
    ("load_intraday_policy() matches engine=None", test_no_engine_argument_matches_no_engine_call),
    ("unset engine is byte-identical to pooled, every engine", test_unset_engine_is_byte_identical_to_pooled),
    ("empty-string engine behaves like None", test_empty_string_engine_behaves_like_none),
    ("engine override is used for scoped keys", test_engine_override_is_used_for_scoped_keys),
    ("engine override is case-insensitive", test_engine_override_is_case_insensitive),
    ("one engine's override does not leak into another", test_one_engines_override_does_not_leak_into_another),
    ("every scoped key is independently overridable", test_every_scoped_key_is_independently_overridable),
    ("pooled-only keys never read an engine prefix", test_pooled_only_keys_never_read_an_engine_prefix),
    ("breakeven_at_r fallback chain preserved per engine", test_breakeven_at_r_fallback_chain_preserved_per_engine),
    ("breakeven_at_r's own key still wins over the fallback", test_breakeven_at_r_own_key_still_wins_over_the_fallback),
    ("pooled breakeven fallback unchanged by this change", test_pooled_breakeven_fallback_unchanged_by_this_change),
]
