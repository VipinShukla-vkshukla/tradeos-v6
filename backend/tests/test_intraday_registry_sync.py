"""
intraday.strategies.registry.sync_to_db() — 09-Sep-2026.

WHY THIS EXISTS
-----------------
sync_to_db() has existed since migration 014 and was never called from
anywhere in this codebase (grep confirmed) — intraday_strategy_config, the
table the frontend's engine cards read their "conditions" text from, had
zero rows for every intraday engine, not only the ones added since. Wired
into intraday/run.py's own startup now. Fixed alongside that: `"lifecycle":
"ACTIVE"` was hardcoded rather than read from the real, config-overridable
`engine_lifecycle()`, and `description` used `e.__class__.__doc__`, empty
for 9 of this project's 10 engines because this codebase documents at the
MODULE level, not the class level. See docs/FINDINGS.md, 09-Sep-2026.
"""

from __future__ import annotations

from types import SimpleNamespace

from tests import cfg_ctx


class _FakeSyncSB:
    """Records every insert; `.select().eq()` reads back either a seeded
    "already exists" set or a row this same fake already inserted — so
    sync_to_db()'s own `if existing: continue` guard is exercised for
    real, not stubbed away."""
    def __init__(self, existing_strategies=()):
        self.existing = set(existing_strategies)
        self.inserted: dict[str, dict] = {}
        self._filter_val = None
        self._pending = None

    def table(self, name):
        assert name == "intraday_strategy_config"
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, _col, val):
        self._filter_val = val
        return self

    def insert(self, row):
        self._pending = row
        return self

    def execute(self):
        if self._pending is not None:
            self.inserted[self._pending["strategy"]] = self._pending
            self._pending = None
            return SimpleNamespace(data=[])
        present = self._filter_val in self.existing or self._filter_val in self.inserted
        return SimpleNamespace(data=([{"strategy": self._filter_val}] if present else []))


def test_sync_skips_an_engine_that_already_has_a_row():
    from intraday.strategies import registry
    sb = _FakeSyncSB(existing_strategies={"ORB"})
    registry.sync_to_db(sb)
    assert "ORB" not in sb.inserted, "an existing row must never be touched"


def test_sync_reads_the_real_lifecycle_not_a_hardcoded_active():
    """THE BUG. sync_to_db() used to write 'ACTIVE' unconditionally — a
    SHADOW or RETIRED engine would have been seeded into this operator-
    facing table reading as fully live regardless of its real state."""
    from intraday.strategies import registry
    sb = _FakeSyncSB(existing_strategies=set(registry.engine_names()) - {"RNG"})
    with cfg_ctx({"intraday_engine_rng_lifecycle": "SHADOW"}):
        registry.sync_to_db(sb)
    assert "RNG" in sb.inserted, "the one missing engine must still be inserted"
    assert sb.inserted["RNG"]["lifecycle"] == "SHADOW", (
        f"must read engine_lifecycle(), not hardcode ACTIVE — got "
        f"{sb.inserted['RNG']['lifecycle']!r}")


def test_sync_description_comes_from_the_module_docstring_not_the_class():
    """THE OTHER BUG. ORB's class body carries no docstring of its own —
    confirmed directly, not assumed — so e.__class__.__doc__ was always
    None/blank for it. All of this engine's real documentation lives in
    the MODULE docstring at the top of orb.py."""
    from intraday.strategies import registry
    from intraday.strategies.orb import OpeningRangeBreakout
    assert OpeningRangeBreakout.__doc__ is None, (
        "fixture assumption broke — ORB's class now carries its own "
        "docstring, re-check whether the module-level extraction is "
        "still the right fix")
    sb = _FakeSyncSB(existing_strategies=set(registry.engine_names()) - {"ORB"})
    registry.sync_to_db(sb)
    desc = sb.inserted["ORB"]["description"]
    assert desc, "description must not be blank"
    assert "opening range" in desc.lower(), (
        f"expected ORB's real module-docstring title, got {desc!r}")


def test_sync_label_is_the_engine_class_name():
    from intraday.strategies import registry
    sb = _FakeSyncSB(existing_strategies=set(registry.engine_names()) - {"IGN"})
    registry.sync_to_db(sb)
    assert sb.inserted["IGN"]["label"] == "IgnitionMomentum"


def test_sync_phases_are_comma_joined_from_the_real_engine():
    from intraday.strategies import registry
    sb = _FakeSyncSB(existing_strategies=set(registry.engine_names()) - {"ORB"})
    registry.sync_to_db(sb)
    assert sb.inserted["ORB"]["phases"] == "PRIME"


def test_sync_covers_every_registered_engine_when_the_table_is_empty():
    from intraday.strategies import registry
    sb = _FakeSyncSB(existing_strategies=set())
    registry.sync_to_db(sb)
    assert set(sb.inserted.keys()) == set(registry.engine_names())


TESTS = [
    ("sync skips an engine that already has a row",
     test_sync_skips_an_engine_that_already_has_a_row),
    ("sync reads the real lifecycle, not a hardcoded ACTIVE",
     test_sync_reads_the_real_lifecycle_not_a_hardcoded_active),
    ("sync description comes from the module docstring, not the class",
     test_sync_description_comes_from_the_module_docstring_not_the_class),
    ("sync label is the engine class name",
     test_sync_label_is_the_engine_class_name),
    ("sync phases are comma-joined from the real engine",
     test_sync_phases_are_comma_joined_from_the_real_engine),
    ("sync covers every registered engine when the table is empty",
     test_sync_covers_every_registered_engine_when_the_table_is_empty),
]

if __name__ == "__main__":
    fails = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL  {name} — {e}")
    print(f"\n{len(TESTS) - fails}/{len(TESTS)} passed")
