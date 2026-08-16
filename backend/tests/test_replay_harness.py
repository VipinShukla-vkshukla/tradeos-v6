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
from tests import cfg_ctx

from tools.replay import independence
from tools.replay.contexts import assert_no_lookahead, bars_before, build_context
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
    ]
    with cfg_ctx({}):
        res = build_universe_at("2026-06-01", rows=rows, limit=40)

    assert res.symbols == ["GOOD"], (
        f"expected only GOOD to survive, got {res.symbols}")
    assert res.shells_dropped == 1, (
        f"the null-close shell was not counted, got {res.shells_dropped} — "
        f"2026-04-29's 1,976 shells would pass through unremarked")
    assert res.flags_applied is False, (
        "safety flags must be OFF: safety_lists has no history, so applying "
        "today's ASM/F&O state to a past date is lookahead")

    # THE PASSING SIDE. A filter no realistic row can clear is the same defect
    # wearing a different hat, and this project has shipped one of those.
    assert res.entries, (
        "a realistic, comfortably-qualifying row produced an EMPTY universe — "
        "the filter cannot be cleared and every replayed day would report "
        "'no setups' indistinguishably from a quiet market")


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
]
