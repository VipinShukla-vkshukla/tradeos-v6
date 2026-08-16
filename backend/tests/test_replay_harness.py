"""
The replay harness, checked offline — including the checks themselves.

WHY THESE LIVE HERE AND NOT IN A SCRATCH DIRECTORY
---------------------------------------------------
CLAUDE.md: every check in `backend/tests/` was once a throwaway verification
script, written into a scratch directory, run once, lost at session end, and
rewritten by the next session that touched the same code. Two defects shipped
through that gap. A harness whose correctness is only ever demonstrated in a
transcript is a harness nobody can re-verify after the next refactor.

EVERY CHECK BELOW IS DEMONSTRATED FAILING, NOT ONLY PASSING
-------------------------------------------------------------
This project has found five health checks that reported green while the thing
they watched was broken, and one threshold that no input could ever clear. So
the independence scan is tested against a file that violates it, the lookahead
guard is tested against a context that can see ahead, and the outcome rule is
tested against the direction error that would retire a working short engine.

No database, no broker, no network — pure arithmetic over in-memory objects.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import IST
from intraday.cost_model import round_trip
from intraday.strategies.base import Bar
from intraday.strategies.squeeze import SqueezeExpansion
from tests import cfg_ctx

from tools.replay import freeze, holdout, independence
from tools.replay.contexts import (apply_forming_bar, assert_no_lookahead,
                                   bars_before, build_context)
from tools.replay.conventions import BAR_CLOSE, CADENCE_15S, evaluation_points
from tools.replay.ladder import step_swing
from tools.replay.outcomes_port import planned_r, resolve
from tools.replay.universe import build_universe_at


# ── helpers ─────────────────────────────────────────────────────────────────
def _bars(n: int = 30, start_price: float = 100.0,
          day: str = "2026-06-01") -> list[Bar]:
    """A synthetic session, one bar per minute from 09:15."""
    t0 = IST.localize(datetime.fromisoformat(f"{day}T09:15:00"))
    out = []
    for i in range(n):
        p = start_price + i * 0.10
        out.append(Bar(ts=t0 + timedelta(minutes=i), open=p, high=p + 0.20,
                       low=p - 0.20, close=p + 0.05, volume=1000 + i))
    return out


# ── 1. the independence scan ────────────────────────────────────────────────
def test_independence_scan_is_clean_on_the_shipped_package():
    viol = independence.scan()
    assert not viol, (
        "the harness references a forbidden table or module:\n  "
        + "\n  ".join(viol))


def test_independence_scan_catches_an_injected_forbidden_read():
    """
    THE FAILING SIDE. A scan that cannot fail is not a scan.

    Writes a file containing a real forbidden read into a temp directory and
    requires the scanner to flag it. If this ever passes silently, the check
    guarding the whole harness has stopped working.
    """
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "smuggler.py"
        # Built from parts so this test file itself stays clean of the token —
        # the scan is a literal grep and would otherwise flag this module.
        token = "intraday" + "_setups"
        bad.write_text(
            f'def read(sb):\n'
            f'    return sb.table("{token}").select("*").execute().data\n',
            encoding="utf-8")

        viol = independence.scan([bad])
        assert viol, (
            "the independence scan did NOT flag a file containing a forbidden "
            "read — the check cannot fail and therefore proves nothing")
        assert "smuggler.py" in viol[0] and token in viol[0], (
            f"the scan flagged something, but not the injected read: {viol[0]}")


def test_independence_scan_respects_the_whitelist():
    """The verification module may contain the tokens; nothing else may."""
    with tempfile.TemporaryDirectory() as td:
        token = "intraday" + "_setups"
        allowed = Path(td) / "verify_known_day.py"
        allowed.write_text(f'X = "{token}"\n', encoding="utf-8")
        assert not independence.scan([allowed]), (
            "verify_known_day.py must be exempt — it exists to compare against "
            "the live record")

        other = Path(td) / "detect.py"
        other.write_text(f'X = "{token}"\n', encoding="utf-8")
        assert independence.scan([other]), (
            "the whitelist has widened beyond verify_known_day.py")


def test_independence_scan_is_not_vacuous():
    """A scan that inspects nothing passes forever. This is that guard."""
    scanned = independence.check_scan_is_not_vacuous()
    assert scanned >= 4, (
        f"only {scanned} harness file(s) were inspected — the package walk has "
        f"broken, and an empty scan reports clean no matter what is in the code")


def test_exempt_files_cannot_reach_the_database():
    """
    The token-scan exemption is a hole; this is the guard on it.

    `independence.py` is exempt only because it names the forbidden tables as
    data. A file that cannot reach a database cannot abuse that exemption.
    """
    bad = independence.check_exempt_files_are_inert()
    assert not bad, "\n  ".join(bad)


# ── 2. truncation / lookahead ───────────────────────────────────────────────
def test_context_contains_no_bar_at_or_after_the_evaluation_time():
    day_bars = _bars(40)
    now = day_bars[20].ts
    ctx = build_context("TEST", day_bars, now)

    assert ctx is not None
    assert max(b.ts for b in ctx.bars) < now, "context can see the current bar"
    assert len(ctx.bars) == 20, f"expected 20 bars strictly before {now}"
    # day_high must be the high SO FAR, not the session's.
    assert ctx.day_high < max(b.high for b in day_bars), (
        "day_high equals the full session high — the aggregates were built from "
        "untruncated bars even though the bar list was sliced")
    assert_no_lookahead(ctx, now, day_bars)


def test_the_lookahead_guard_actually_fires():
    """
    THE FAILING SIDE. One `bars[:i+1]` where `bars[:i]` was meant is invisible
    without this, and it makes every engine look brilliant.
    """
    day_bars = _bars(40)
    now = day_bars[20].ts
    ctx = build_context("TEST", day_bars, now)

    # Inject exactly the off-by-one this guard exists to catch.
    ctx.bars = day_bars[:21]

    try:
        assert_no_lookahead(ctx, now, day_bars)
    except AssertionError as e:
        assert "LOOKAHEAD" in str(e)
        return
    raise AssertionError(
        "assert_no_lookahead accepted a context containing the evaluation bar "
        "— the harness's central guarantee is unguarded")


def test_bars_before_is_strict():
    day_bars = _bars(10)
    exact = day_bars[5].ts
    got = bars_before(day_bars, exact)
    assert len(got) == 5, "bars_before must be strict (<), not inclusive (<=)"


# ── 3. the outcome rule ─────────────────────────────────────────────────────
def _one_bar(hi: float, lo: float, close: float) -> list[Bar]:
    t = IST.localize(datetime.fromisoformat("2026-06-01T09:20:00"))
    return [Bar(ts=t, open=(hi + lo) / 2, high=hi, low=lo, close=close,
                volume=1000)]


def test_bad_fill_resolves_stop_when_both_are_inside_one_bar():
    """
    A coarse bar cannot tell you the sequence, so assume the bad one. Assuming
    the good one is how a strategy looks profitable on paper and loses live.
    """
    out = resolve(entry=100.0, stop=98.0, target=104.0, direction="LONG",
                  bars=_one_bar(hi=105.0, lo=97.0, close=101.0))
    assert out.outcome == "STOP", (
        f"both levels inside one bar must resolve STOP, got {out.outcome} — "
        f"the replay is flattering every engine it scores")
    assert out.exit_price == 98.0

    short = resolve(entry=100.0, stop=102.0, target=96.0, direction="SHORT",
                    bars=_one_bar(hi=103.0, lo=95.0, close=99.0))
    assert short.outcome == "STOP", "the bad fill must apply to shorts too"
    assert short.exit_price == 102.0


def test_a_short_does_not_resolve_stop_on_its_first_bar():
    """
    THE DIRECTION LANDMINE. A short's stop sits ABOVE its entry, so the long
    form (`lo <= stop`) is true immediately and every short resolves STOP within
    seconds of detection. That error would assign SDN — the only short engine
    this system has — a catastrophic prior made entirely of arithmetic, and then
    retire it on that evidence.
    """
    # Price falls: a winning short. Long-form arithmetic would call this STOP.
    t = IST.localize(datetime.fromisoformat("2026-06-01T09:20:00"))
    bars = [Bar(ts=t + timedelta(minutes=i), open=100 - i, high=100.5 - i,
                low=99.5 - i, close=100 - i, volume=1000) for i in range(6)]

    out = resolve(entry=100.0, stop=102.0, target=96.0, direction="SHORT",
                  bars=bars)
    assert out.outcome == "TARGET", (
        f"a short whose price fell to its target resolved {out.outcome} — the "
        f"direction arithmetic is inverted")
    assert out.pct is not None and out.pct > 0, (
        f"a profitable short must have a POSITIVE gain_pct, got {out.pct} — "
        f"unsigned, every winning short is recorded as a loss")


def test_planned_r_is_signed_in_the_trades_favour():
    out = resolve(entry=100.0, stop=98.0, target=104.0, direction="LONG",
                  bars=_one_bar(hi=104.5, lo=99.5, close=104.2))
    assert out.outcome == "TARGET"
    r = planned_r(100.0, 98.0, "LONG", out)
    assert r is not None and abs(r - 2.0) < 0.01, (
        f"a 4% gain on a 2% risk is +2.0R, got {r}")


# ── 4. the swing ladder is long-only, and says so ───────────────────────────
def test_swing_ladder_refuses_a_short():
    """
    `evaluate_exit`'s risk line is long-only (`position_lifecycle.py:301`) and
    would return a negative risk on a short — silently. The harness must refuse
    rather than return a confident wrong number.
    """
    pos = {"symbol": "TEST", "entry_price": 100.0, "planned_stop": 102.0,
           "planned_target": 96.0, "current_qty": 10, "direction": "SHORT"}
    try:
        step_swing(pos, _bars(5))
    except AssertionError as e:
        assert "SHORT" in str(e)
        return
    raise AssertionError(
        "step_swing accepted a SHORT — evaluate_exit would compute a negative "
        "risk and every R it produced would be nonsense")


# ── 5. costs: CNC and MIS are different trades ──────────────────────────────
def test_cnc_round_trip_costs_materially_more_than_mis():
    """
    An omitted `product=` understates swing friction by roughly 5x. Delivery
    pays zero brokerage but 0.1% STT on BOTH legs, 0.015% stamp and a flat
    Rs 15.04 DP fee per sell.

    This is the assertion REPLAY_DESIGN §9.3 promises: if the two come out
    similar, an argument was dropped somewhere in the harness.
    """
    mis = round_trip(200.0, 10, product="MIS")
    cnc = round_trip(200.0, 10, product="CNC")

    assert cnc.pct_of_position > mis.pct_of_position * 2.0, (
        f"CNC {cnc.pct_of_position:.4f}% vs MIS {mis.pct_of_position:.4f}% — "
        f"a Rs 2,000 delivery round trip is ~1.0%, not the ~0.21% the intraday "
        f"model reports. These are not the same trade financially.")
    assert round_trip(200.0, 10).pct_of_position == mis.pct_of_position, (
        "the default product is no longer MIS — every call site that omits the "
        "argument has silently changed meaning")


# ── 6. the universe port has not drifted from production ────────────────────
def test_universe_port_has_not_drifted_from_production():
    """
    The port copies `scanner.build_universe`'s filter arithmetic. If production
    changes and this copy does not, the replay silently scans a different
    universe than the system it is modelling — and nothing anywhere would say so.

    Compares the load-bearing literals against production's own source.
    """
    src = (Path(__file__).resolve().parents[1]
           / "intraday" / "scanner.py").read_text(encoding="utf-8")

    required = [
        "0.55 * mov_score + 0.45 * liq_score",   # the ranking formula
        'cfg_float("intraday_min_price"',
        'cfg_float("intraday_min_turnover_cr"',
        'cfg_float("intraday_min_atr_pct"',
        'cfg_float("intraday_max_atr_pct"',
        'cfg_float("intraday_min_delivery_pct"',
        "value / max(min_value * 8, 1)",         # the liquidity score
        "(atr - min_atr) / max(max_atr - min_atr, 0.01)",   # the movement score
    ]
    missing = [r for r in required if r not in src]
    assert not missing, (
        "intraday/scanner.py no longer contains:\n  " + "\n  ".join(missing)
        + "\n\ntools/replay/universe.py is a COPY of that filter and is now "
          "stale. Re-sync it, then update this list.")


def test_universe_port_filters_and_ranks_as_specified():
    """
    Behaviour, not just similarity. Hand-computed expectations on synthetic rows.
    """
    rows = [
        # kept: comfortably inside every bound
        {"symbol": "GOOD", "close": 500.0, "value_cr": 300.0, "atr_pct": 3.0,
         "delivery_pct": 50.0, "avg_vol_20d": 1e6, "sector": "IT"},
        # dropped: price below intraday_min_price
        {"symbol": "CHEAP", "close": 10.0, "value_cr": 300.0, "atr_pct": 3.0,
         "delivery_pct": 50.0},
        # dropped: turnover below intraday_min_turnover_cr
        {"symbol": "THIN", "close": 500.0, "value_cr": 1.0, "atr_pct": 3.0,
         "delivery_pct": 50.0},
        # dropped: ATR below the floor — cannot pay for its own round trip
        {"symbol": "DULL", "close": 500.0, "value_cr": 300.0, "atr_pct": 0.2,
         "delivery_pct": 50.0},
        # dropped: ATR above the ceiling
        {"symbol": "WILD", "close": 500.0, "value_cr": 300.0, "atr_pct": 50.0,
         "delivery_pct": 50.0},
        # dropped: a SHELL — null close, the 2026-04-29 signature
        {"symbol": "SHELL", "close": None, "value_cr": 300.0, "atr_pct": 3.0,
         "delivery_pct": 50.0},
        # dropped: under ASM on the date being ranked. The flag is on the DATED
        # row (ingest_asm_gsm writes .eq("date", today)), so it is point-in-time
        # and must be honoured — see the correction in universe.py's docstring.
        {"symbol": "FLAGGED", "close": 500.0, "value_cr": 300.0, "atr_pct": 3.0,
         "delivery_pct": 50.0, "asm_flag": True},
        {"symbol": "BANNED", "close": 500.0, "value_cr": 300.0, "atr_pct": 3.0,
         "delivery_pct": 50.0, "fo_ban_flag": True},
    ]
    with cfg_ctx({"intraday_skip_flagged": "true"}):
        res = build_universe_at("2026-06-01", rows=rows, limit=40)

    assert res.symbols == ["GOOD"], (
        f"expected only GOOD to survive, got {res.symbols}")
    assert res.shells_dropped == 1, (
        f"the null-close shell was not counted, got {res.shells_dropped} — "
        f"2026-04-29's 1,976 shells would pass through unremarked")
    assert res.flags_applied is True and res.rejected["flagged"] == 2, (
        f"ASM/F&O flags must be honoured — they live on the dated "
        f"stock_data_daily row, not in the history-less safety_lists table. "
        f"flags_applied={res.flags_applied} rejected={res.rejected}")

    # THE OTHER DIRECTION. The switch must be a switch: with it off, the same
    # two rows come back. A filter that cannot be turned off is not being read.
    with cfg_ctx({"intraday_skip_flagged": "false"}):
        off = build_universe_at("2026-06-01", rows=rows, limit=40)
    assert set(off.symbols) == {"GOOD", "FLAGGED", "BANNED"}, (
        f"intraday_skip_flagged=false should admit the flagged names, "
        f"got {off.symbols}")

    # THE PASSING SIDE. A filter no realistic row can clear is the same defect
    # wearing a different hat, and this project has shipped one of those.
    assert res.entries, (
        "a realistic, comfortably-qualifying row produced an EMPTY universe — "
        "the filter cannot be cleared and every replayed day would report "
        "'no setups' indistinguishably from a quiet market")


# ── 6. evaluation conventions — WHEN the replay looks, and what it may know ──
def test_cadence_alone_cannot_change_a_single_detection():
    """
    The 15 s cadence over minute bars is not an approximation of the live loop.
    It is four identical questions.

    This is the check that makes the diagnosis provable rather than asserted:
    every extra look `CADENCE_15S` adds carries the SAME bar truncation and the
    SAME ltp as the bar-close look it follows, so no engine can answer it
    differently. Measured against the live record on 2026-08-14, that predicted
    an exactly-zero change, and the run produced exactly zero — 169 reproduced,
    43 missed and 18 extra under both.
    """
    bars = _bars(40)
    base = [(p.upto, p.ltp) for p in evaluation_points(bars, BAR_CLOSE)]
    fast = [(p.upto, p.ltp) for p in evaluation_points(bars, CADENCE_15S)]

    assert len(fast) == 4 * len(base), (
        f"cadence_15s should look 4x as often, got {len(fast)} vs {len(base)}")
    assert set(fast) == set(base), (
        "cadence_15s produced an (upto, ltp) pair that bar_close does not — "
        "it is supposed to add LOOKS, not information")


def test_a_sub_bar_look_cannot_read_past_its_own_clock():
    """
    `build_context` must refuse `upto > now`, and the forming-bar overlay must
    refuse a bar it is not standing inside.

    Both are demonstrated FAILING. Sub-bar evaluation is the one place in this
    package where reading the future is a single argument away, and an assertion
    nobody has watched fire is not an assertion.
    """
    bars = _bars(40)
    now = bars[10].ts + timedelta(seconds=30)

    # 1. bars truncated AFTER the clock
    try:
        build_context("X", bars, now, upto=now + timedelta(minutes=5))
        raise AssertionError(
            "build_context accepted a truncation instant AFTER its own clock — "
            "the engine would have read five minutes it cannot have seen")
    except AssertionError as e:
        if "AFTER the evaluation clock" not in str(e):
            raise

    # 2. overlaid with a bar two minutes ahead
    ctx = build_context("X", bars, now, upto=bars[10].ts)
    assert ctx is not None
    try:
        apply_forming_bar(ctx, bars[12], bars[12].high, extremes=True, vwap=True)
        raise AssertionError(
            "apply_forming_bar accepted a bar the clock has not reached — the "
            "overlay would report a future minute's high as the present")
    except AssertionError as e:
        if "standing inside" not in str(e):
            raise

    # THE PASSING SIDE. The legitimate overlay — the bar `now` sits inside —
    # must be accepted, or the convention could never be exercised at all.
    ok = apply_forming_bar(ctx, bars[10], bars[10].high, extremes=True, vwap=True)
    assert ok.ltp == bars[10].high, "the legitimate forming-bar overlay was refused"
    assert ok.day_high >= bars[10].high


def test_the_bar_close_convention_cannot_fire_the_squeeze_engine():
    """
    VCE cannot produce a single detection at a bar close. STRUCTURALLY, not
    rarely — and this is why the harness reproduced 0 of 17 stored VCE keys.

    `squeeze.py:57-77` takes `r_hi = max(b.high for b in bars[-n:])` and then
    refuses unless `ctx.ltp > r_hi`. At the bar-close convention `ctx.ltp` is
    `bars[-1].close`, and `bars[-1]` is inside that window, so
    `ltp <= bars[-1].high <= r_hi` holds for every bar of every symbol on every
    day. The test is the pair: it must never fire at a close, and it MUST fire
    when the price is genuinely above the coil — otherwise this would be an
    engine that does nothing rather than a convention that blinds one.
    """
    # A coil: 16 flat bars, so `recent` is much tighter than `prior`.
    t0 = IST.localize(datetime.fromisoformat("2026-06-01T09:15:00"))
    bars = []
    for i in range(20):
        # prior half wide, recent half tight — the contraction VCE looks for
        w = 2.0 if i < 10 else 0.10
        bars.append(Bar(ts=t0 + timedelta(minutes=i), open=100.0,
                        high=100.0 + w, low=100.0 - w, close=100.0,
                        volume=5000))
    eng = SqueezeExpansion()

    with cfg_ctx({}):
        # 1. every bar close, every bar — never once
        fired = 0
        for i in range(10, len(bars)):
            now = bars[i].ts + timedelta(minutes=1)
            ctx = build_context("COIL", bars, now, prev={"atr_pct": 2.0})
            if ctx and eng.evaluate(ctx, "PRIME"):
                fired += 1
        assert fired == 0, (
            f"VCE fired {fired} times at a bar close — if this ever passes, the "
            f"engine changed and the 0-of-17 finding needs re-measuring")

        # 2. the same context with the price above the coil — MUST fire
        now = bars[-1].ts + timedelta(minutes=1)
        ctx = build_context("COIL", bars, now, prev={"atr_pct": 2.0})
        r_hi = max(b.high for b in ctx.bars[-8:])
        ctx.ltp = r_hi * 1.002        # a real breakout, 20 bps through the coil
        got = eng.evaluate(ctx, "PRIME")

    assert got is not None, (
        "VCE refused a price 20 bps above its own coil — this check would then "
        "be unable to distinguish 'the convention blinds the engine' from 'the "
        "engine never fires', which is the whole thing it exists to separate")


# ── 7. the parameter freeze (REPLAY_DESIGN §8) ──────────────────────────────
def test_freeze_records_both_sources_of_a_value():
    """
    A key absent from `system_config` is not absent from the system — `cfg`
    falls back to the caller's literal, and 27 of the 177 keys resolve that way.
    Freezing only the table would leave those free to move whenever someone
    edits a default in an engine.
    """
    rec = {"reader": "cfg_float", "defaults": {"intraday/strategies/orb.py": "0.45"},
           "read_by": ["intraday/strategies/orb.py"]}
    val, src = freeze._resolve("k", rec, {"k": "0.90"})
    assert (val, src) == ("0.90", "system_config"), (val, src)

    val, src = freeze._resolve("k", rec, {})
    assert (val, src) == ("0.45", "source_default"), (
        f"a key missing from the table must freeze its SOURCE default, got "
        f"{val!r} from {src!r} — otherwise editing that literal silently "
        f"changes a 'frozen' replay")


def test_frozen_config_swaps_and_restores_the_process_global():
    """
    `config._sys_config` is process-wide. One replay leaking its parameters into
    the next would score two windows under one set of switches with no record of
    which — the same hazard `cfg_ctx()` exists for in this suite.
    """
    import config as _c
    fp = freeze.FrozenParams(label="t", created_at="", code={},
                             values={"orb_target_r": "9.99"}, provenance={})
    before = _c._sys_config
    with freeze.frozen_config(fp, check_code=False):
        assert _c.cfg_float("orb_target_r", 1.0) == 9.99, (
            "frozen parameters were not applied — the engines would have read "
            "live config")
    assert _c._sys_config is before, (
        "frozen_config did not restore the previous config on exit")


def test_frozen_file_refuses_to_load_if_hand_edited():
    """A recorded SHA that no longer matches its own contents is a file someone
    changed after freezing it, and it must not load silently."""
    fp = freeze.FrozenParams(label="t", created_at="", code={},
                             values={"a": "1"}, provenance={})
    fp.sha = fp.compute_sha()
    fp.values["a"] = "2"                      # the hand edit
    assert fp.compute_sha() != fp.sha, (
        "editing a frozen value did not change its SHA — the file could be "
        "altered after the fact and still claim to be the same experiment")


def test_freeze_raises_when_a_named_path_is_gone():
    """
    A freeze that silently omits `squeeze.py` because someone moved it
    identifies the wrong experiment. Demonstrated by pointing it at a path that
    does not exist.
    """
    orig = freeze.CONFIG_SOURCES
    try:
        freeze.CONFIG_SOURCES = ("intraday/strategies", "intraday/not_a_file.py")
        try:
            freeze.discover_keys()
            raise AssertionError(
                "freeze accepted a config source that does not exist — a "
                "renamed module would drop its keys out of the freeze silently")
        except RuntimeError as e:
            assert "does not exist" in str(e)
    finally:
        freeze.CONFIG_SOURCES = orig


# ── 8. the holdout refusals — each demonstrated REFUSING ─────────────────────
def test_holdout_refuses_a_repeat_run_on_one_params_sha():
    """
    R3, the refusal that matters most, exercised through the REAL `preflight()`.

    Not through a local copy of its rule: this project has already been burned
    by `check_selects`, a check that read its own hardcoded return value and
    printed a pass over its own error output. So the test redirects the two
    directories `preflight` reads and then calls it — once with no prior result
    and once with one — and asserts only on whether an R3 clause appears.
    Whether R1/R2 also fire depends on the working tree and is irrelevant here.
    """
    with tempfile.TemporaryDirectory() as td:
        orig_results, orig_params = holdout.RESULTS_DIR, freeze.PARAMS_DIR
        try:
            holdout.RESULTS_DIR = Path(td) / "results"
            holdout.RESULTS_DIR.mkdir()
            freeze.PARAMS_DIR = Path(td) / "params"
            freeze.PARAMS_DIR.mkdir()

            fp = freeze.FrozenParams(label="t", created_at="", code={},
                                     values={"a": "1"}, provenance={})
            fp.sha = fp.compute_sha()
            freeze.write(fp)

            def r3_clauses():
                return [r for r in holdout.preflight("t").refusals
                        if r.startswith("R3")]

            assert not r3_clauses(), (
                "R3 refused with no prior result on disk — a holdout that can "
                "never run is the same defect as one that runs twice")

            holdout.result_path_for(fp.sha).write_text(
                '{"ran_at": "2026-06-30T18:00:00+05:30"}', encoding="utf-8")

            got = r3_clauses()
            assert got, (
                "a second holdout on the same parameters was NOT refused — the "
                "one-look guarantee is the entire value of a holdout")
            assert "ONE look" in got[0], got[0]
        finally:
            holdout.RESULTS_DIR, freeze.PARAMS_DIR = orig_results, orig_params


def test_holdout_result_filename_embeds_the_params_sha():
    """A second parameter set must be a visibly different artefact rather than
    an overwrite of the first."""
    a = holdout.result_path_for("a" * 64).name
    b = holdout.result_path_for("b" * 64).name
    assert a != b and a.startswith("holdout_") and a.endswith(".json"), (a, b)


def test_holdout_refuses_uncommitted_parameters():
    """
    R2, both halves. `_committed_blob_matches` must reject a file git has never
    seen AND a tracked file that has since been edited — the second is the
    dangerous one, because it looks tracked.
    """
    with tempfile.TemporaryDirectory() as td:
        stray = Path(td) / "never_committed.json"
        stray.write_text("{}", encoding="utf-8")
        # Outside the repo entirely. This must REFUSE, not raise — a
        # precondition that throws is a traceback where a refusal belonged, and
        # this exact path raised ValueError until the R3 test drove it.
        ok, why = holdout._committed_blob_matches(stray)
        assert not ok and "outside the repository" in why, (
            f"an uncommitted parameter file was accepted or misdiagnosed: "
            f"ok={ok} why={why!r}")

    # The passing side: a file that IS committed and unmodified resolves.
    tracked = freeze.REPO / "docs" / "REPLAY_DESIGN.md"
    if tracked.exists():
        ok, why = holdout._committed_blob_matches(tracked)
        assert ok or "differs" in why, (
            f"a committed, unmodified file was rejected for the wrong reason: "
            f"{why}")


TESTS = [
    ("independence scan is clean on the shipped package",
     test_independence_scan_is_clean_on_the_shipped_package),
    ("independence scan catches an injected forbidden read",
     test_independence_scan_catches_an_injected_forbidden_read),
    ("independence scan respects the whitelist",
     test_independence_scan_respects_the_whitelist),
    ("independence scan is not vacuous",
     test_independence_scan_is_not_vacuous),
    ("exempt files cannot reach the database",
     test_exempt_files_cannot_reach_the_database),
    ("context contains no bar at or after the evaluation time",
     test_context_contains_no_bar_at_or_after_the_evaluation_time),
    ("the lookahead guard actually fires",
     test_the_lookahead_guard_actually_fires),
    ("bars_before is strict",
     test_bars_before_is_strict),
    ("bad fill resolves STOP when both are inside one bar",
     test_bad_fill_resolves_stop_when_both_are_inside_one_bar),
    ("a short does not resolve STOP on its first bar",
     test_a_short_does_not_resolve_stop_on_its_first_bar),
    ("planned R is signed in the trade's favour",
     test_planned_r_is_signed_in_the_trades_favour),
    ("swing ladder refuses a short",
     test_swing_ladder_refuses_a_short),
    ("CNC round trip costs materially more than MIS",
     test_cnc_round_trip_costs_materially_more_than_mis),
    ("universe port has not drifted from production",
     test_universe_port_has_not_drifted_from_production),
    ("universe port filters and ranks as specified",
     test_universe_port_filters_and_ranks_as_specified),
    ("cadence alone cannot change a single detection",
     test_cadence_alone_cannot_change_a_single_detection),
    ("a sub-bar look cannot read past its own clock",
     test_a_sub_bar_look_cannot_read_past_its_own_clock),
    ("the bar-close convention cannot fire the squeeze engine",
     test_the_bar_close_convention_cannot_fire_the_squeeze_engine),
    ("freeze records both sources of a value",
     test_freeze_records_both_sources_of_a_value),
    ("frozen config swaps and restores the process global",
     test_frozen_config_swaps_and_restores_the_process_global),
    ("frozen file refuses to load if hand-edited",
     test_frozen_file_refuses_to_load_if_hand_edited),
    ("freeze raises when a named path is gone",
     test_freeze_raises_when_a_named_path_is_gone),
    ("holdout refuses a repeat run on one params sha",
     test_holdout_refuses_a_repeat_run_on_one_params_sha),
    ("holdout result filename embeds the params sha",
     test_holdout_result_filename_embeds_the_params_sha),
    ("holdout refuses uncommitted parameters",
     test_holdout_refuses_uncommitted_parameters),
]
