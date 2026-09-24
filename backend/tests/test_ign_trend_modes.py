"""
IGN trend logic beyond the long gate (24-Sep-2026): the SHORT gate, the confidence
score, and the forming-candle panel. All three ship OFF; the tests that matter most
are that off changes nothing and that each mode does what its switch says when on.

Direction is the recurring trap (a default of LONG once scored every short as a long),
so every mirrored rule is tested with the SAME panel read both ways, and the switches
of one direction are shown not to touch the other.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from config import IST
from intraday import ign_trend as T
from intraday.session import PRIME
from intraday.trend_indicators import DailyBar, feature_panel, forming_panel
from tests import cfg_ctx
from tests.test_ignition import _TIGHT_LOOKBACK, _long_ctx, _short_ctx

END = date(2026, 8, 4)          # the day before tests._fixtures' session (2026-08-05)


def _bars(kind: str, n: int = 260, last: float = 100.0, step: float = 0.3) -> list[DailyBar]:
    out = []
    for i in range(n):
        off = (n - 1 - i) * step
        c = last - off if kind == "up" else last + off
        out.append(DailyBar(END - timedelta(days=n - 1 - i), c, c + 0.5, c - 0.5, c, 1000.0))
    return out


def _panel(bars) -> dict:
    return dict(feature_panel(bars), ok=True, reason=None)


def _with(ctx, kind: str):
    bars = _bars(kind)
    ctx.daily_bars, ctx.daily_feats = bars, _panel(bars)
    return ctx


def _fire(ctx, extra=None):
    from intraday.strategies.ignition import IgnitionMomentum
    with cfg_ctx({**_TIGHT_LOOKBACK, **(extra or {})}):
        return IgnitionMomentum().evaluate(ctx, PRIME)


def _core(s):
    return (s.direction, s.entry, s.stop, s.target, s.confidence, s.rationale)


UP = dict(ok=True, above_st=True, adx=32.0, di_minus=12.0, prev_vol_ratio=1.4, sma50_gt_200=True)
DOWN = dict(ok=True, above_st=False, adx=32.0, di_minus=30.0, prev_vol_ratio=1.4, sma50_gt_200=False)


# ── the SHORT gate ──────────────────────────────────────────────────────────

def test_short_verdict_is_the_mirror_of_the_long_one():
    on_s = dict(require_below_st=True, min_adx=20.0, min_di_minus=20.0, require_sma50_lt_200=True)
    on_l = dict(require_above_st=True, min_adx=20.0, max_di_minus=20.0, require_sma50_gt_200=True)
    assert T.trend_verdict_short(DOWN, **on_s) == ("pass", 4, 4)
    assert T.trend_verdict_short(UP, **on_s)[0] == "refuse", "an uptrend is not a short's tailwind"
    assert T.trend_verdict(UP, **on_l) == ("pass", 4, 4)
    assert T.trend_verdict(DOWN, **on_l)[0] == "refuse", (
        "the SAME downtrend panel passes the short gate and refuses the long one")


def test_each_short_check_bites_on_its_own():
    base = dict(DOWN)
    assert T.trend_verdict_short(dict(base, above_st=True), require_below_st=True)[0] == "refuse"
    assert T.trend_verdict_short(dict(base, adx=15.0), min_adx=20.0)[0] == "refuse"
    assert T.trend_verdict_short(dict(base, adx=20.0), min_adx=20.0)[0] == "pass", ">= is inclusive"
    assert T.trend_verdict_short(dict(base, di_minus=19.9), min_di_minus=20.0)[0] == "refuse", (
        "for a SHORT, -DI is a HIGHER-is-better signal")
    assert T.trend_verdict_short(dict(base, prev_vol_ratio=3.0), max_prev_vol_ratio=2.0)[0] == "refuse"
    assert T.trend_verdict_short(dict(base, sma50_gt_200=True), require_sma50_lt_200=True)[0] == "refuse"


def test_short_verdict_abstains_like_the_long_one():
    on = dict(require_below_st=True, min_adx=20.0)
    assert T.trend_verdict_short(None, **on) == ("abstain", 0, 0)
    assert T.trend_verdict_short(dict(DOWN, ok=False), **on) == ("abstain", 0, 0)
    assert T.trend_verdict_short(DOWN) == ("abstain", 0, 0), "nothing enabled"
    assert T.trend_verdict_short(dict(DOWN, above_st=None, adx=None), **on) == ("abstain", 0, 0)
    assert T.trend_verdict_short(dict(DOWN, adx=None), **on) == ("pass", 1, 1), (
        "a missing ADX is no opinion; the readable check passed")


def test_an_unknown_direction_raises_instead_of_defaulting_to_long():
    for bad in ("", "long", "SIDEWAYS", None):
        try:
            T.trend_score(UP, bad)
        except ValueError:
            continue
        raise AssertionError(f"direction {bad!r} must raise, not silently score as LONG")


def test_short_gate_refuses_a_short_into_a_healthy_uptrend_only_when_armed():
    armed = {"ign_trend_short_gate_enabled": "true", "ign_trend_short_require_below_st": "true"}
    sh = _with(_short_ctx(), "up")                       # above_st True: wrong for a short
    assert _fire(sh, armed) is None, "armed short gate must refuse an uptrend panel"
    ok = _with(_short_ctx(), "down")
    s = _fire(ok, armed)
    assert s is not None and s.meta["trend"]["gate"] == "pass", s and s.meta["trend"]
    # thresholds SET but the master switch off: an empty gate would pass this trivially
    off = _fire(_with(_short_ctx(), "up"), {"ign_trend_short_require_below_st": "true"})
    assert off is not None and off.meta["trend"]["gate"] == "off"


def test_long_and_short_switches_do_not_leak_into_each_other():
    long_only = {"ign_trend_gate_enabled": "true", "ign_trend_require_above_st": "true"}
    short_only = {"ign_trend_short_gate_enabled": "true", "ign_trend_short_require_below_st": "true"}
    down_short = _with(_short_ctx(), "down")
    assert _fire(down_short, long_only) is not None, "the LONG gate must not refuse a short"
    down_long = _with(_long_ctx(), "down")               # a falling name with a long spike
    assert _fire(down_long, short_only) is not None, "the SHORT gate must not refuse a long"
    assert _fire(down_long, long_only) is None, "the long gate does refuse it"


def test_disarmed_short_gate_changes_no_trade():
    plain = _fire(_short_ctx())
    armed_off = _fire(_with(_short_ctx(), "up"),
                      {"ign_trend_short_require_below_st": "true", "ign_trend_short_min_adx": "20"})
    assert plain is not None and armed_off is not None
    assert _core(plain) == _core(armed_off), "master switch off: byte-identical setup"


# ── the confidence score ────────────────────────────────────────────────────

def test_every_config_key_is_wired_to_its_own_gate_parameter():
    """Each key gets a DISTINCT value, so a key read into the wrong slot (or the long
    key read for the short gate) shows up as a wrong number, not a coincidence."""
    long_keys = {"ign_trend_require_above_st": "true", "ign_trend_min_adx": "21",
                 "ign_trend_max_di_minus": "22", "ign_trend_max_prev_vol_ratio": "2.3",
                 "ign_trend_require_sma50_gt_200": "true", "ign_trend_min_agree": "4"}
    short_keys = {"ign_trend_short_require_below_st": "true", "ign_trend_short_min_adx": "31",
                  "ign_trend_short_min_di_minus": "32", "ign_trend_short_max_prev_vol_ratio": "3.3",
                  "ign_trend_short_require_sma50_lt_200": "true", "ign_trend_short_min_agree": "5"}
    with cfg_ctx({**long_keys, **short_keys}):
        assert T.gate_cfg() == dict(require_above_st=True, min_adx=21.0, max_di_minus=22.0,
                                    max_prev_vol_ratio=2.3, require_sma50_gt_200=True,
                                    min_agree=4)
        assert T.short_gate_cfg() == dict(require_below_st=True, min_adx=31.0,
                                          min_di_minus=32.0, max_prev_vol_ratio=3.3,
                                          require_sma50_lt_200=True, min_agree=5)
    with cfg_ctx({}):
        off = T.gate_cfg(), T.short_gate_cfg()
    assert off[0] == dict(require_above_st=False, min_adx=0.0, max_di_minus=0.0,
                          max_prev_vol_ratio=0.0, require_sma50_gt_200=False, min_agree=0)
    assert off[1] == dict(require_below_st=False, min_adx=0.0, min_di_minus=0.0,
                          max_prev_vol_ratio=0.0, require_sma50_lt_200=False, min_agree=0), (
        "every check defaults OFF")


def test_score_hand_computed():
    assert T.trend_score(UP, "LONG") == 1.0 and T.trend_score(DOWN, "SHORT") == 1.0
    # ADX and "yesterday was not a blow-off" are direction-agnostic, so the mirrored
    # panel still passes those two: (2 pass - 3 fail) / 5, not -1.
    assert abs(T.trend_score(DOWN, "LONG") + 0.2) < 1e-12
    assert abs(T.trend_score(UP, "SHORT") + 0.2) < 1e-12
    assert T.trend_score(dict(DOWN, adx=5.0, prev_vol_ratio=4.0), "LONG") == -1.0, (
        "all five fail only when the direction-agnostic checks fail too")
    # 3 of 5 pass, 2 fail -> (3-2)/5
    mixed = dict(UP, above_st=False, adx=10.0)
    assert abs(T.trend_score(mixed, "LONG") - 0.2) < 1e-12
    # missing fields are excluded from the denominator: 2 pass, 0 fail of 2 readable
    part = {"ok": True, "above_st": True, "adx": 30.0}
    assert T.trend_score(part, "LONG") == 1.0
    assert T.trend_score({"ok": True}, "LONG") is None and T.trend_score(None, "LONG") is None
    assert T.trend_score(dict(UP, ok=False), "LONG") is None


def test_score_thresholds_equal_the_pre_registered_ones():
    from tools.replay.ign_feature_stats import GATE_MAP
    assert T.SCORE_ADX == GATE_MAP["adx"][1]
    assert T.SCORE_DI_MINUS == GATE_MAP["di_minus"][1]
    assert T.SCORE_PREV_VOL_RATIO == GATE_MAP["prev_vol_ratio"][1]


def test_weight_zero_changes_nothing_and_records_the_score():
    plain = _fire(_long_ctx())
    good = _fire(_with(_long_ctx(), "up"))
    bad = _fire(_with(_long_ctx(), "down"))
    assert plain.confidence == good.confidence == bad.confidence, (
        "weight 0 (the default): a perfect or a terrible panel moves no confidence")
    assert good.meta["trend"]["score"] == 1.0 and bad.meta["trend"]["score"] == -0.2
    assert plain.meta["trend"]["score"] is None


def test_weight_moves_confidence_in_the_score_direction_for_each_side():
    base = _fire(_long_ctx()).confidence
    w = {"ign_trend_confidence_weight": "0.1"}
    hi = _fire(_with(_long_ctx(), "up"), w).confidence
    lo = _fire(_with(_long_ctx(), "down"), w).confidence
    assert abs(hi - min(0.85, base + 0.1)) < 0.011, (base, hi)
    assert abs(lo - (base - 0.02)) < 0.011, (base, lo)   # 0.1 x score(-0.2)
    s_base = _fire(_short_ctx()).confidence
    s_good = _fire(_with(_short_ctx(), "down"), w).confidence
    s_bad = _fire(_with(_short_ctx(), "up"), w).confidence
    assert s_good > s_base > s_bad, "a SHORT is scored on the SHORT mirror, not the long one"
    assert _fire(_long_ctx(), w).confidence == base, "no panel: unchanged, not penalised"


def test_confidence_stays_inside_its_bounds():
    hi = _fire(_with(_long_ctx(), "up"), {"ign_trend_confidence_weight": "5"}).confidence
    lo = _fire(_with(_long_ctx(), "down"), {"ign_trend_confidence_weight": "5"}).confidence
    assert hi == 0.85, "the existing 0.85 cap still holds"
    assert lo == 0.05, "a floor, so a negative score cannot push confidence below zero"


def test_the_score_reaches_the_allocators_confidence_band_through_its_own_lookup():
    """Asserted through the CONSUMER (Allocator._prior_for), never by reading a dict: the
    score changes IGN's confidence, native_rank carries it, and the band rung prices the
    proposal from a different realised-R sample."""
    from allocation import scoring as S
    from allocation.allocator import Allocator
    from allocation.proposal import from_intraday
    from allocation.scoring import Prior

    base = _fire(_long_ctx()).confidence
    edges = f"{base + 0.02:.2f},{base + 0.08:.2f}"
    priors = {
        "INTRADAY/IGN": Prior("INTRADAY/IGN", 300, -0.10, -0.10, 0.02, -1.0, 1.4),
        f"INTRADAY/IGN{S.BAND_SEP}C0": Prior(f"INTRADAY/IGN{S.BAND_SEP}C0", 300, -0.50, -0.5, 0.02, -1.0, 1.0),
        f"INTRADAY/IGN{S.BAND_SEP}C2": Prior(f"INTRADAY/IGN{S.BAND_SEP}C2", 300, +0.40, 0.4, 0.02, -1.0, 1.6),
        "INTRADAY/ALL": Prior("INTRADAY/ALL", 300, -0.1, -0.1, 0.02, -1.0, 1.4),
    }
    bands = {"alloc_intraday_confidence_bands": "true",
             "intraday_prior_confidence_band_edges": edges}

    def prior_key(setup):
        setup.meta.update({"sub_engine": "IGN", "family": "IGN"})
        p = from_intraday(setup, 60)
        assert p is not None
        with cfg_ctx(bands):
            a = Allocator.__new__(Allocator)
            a._priors = priors
            return a._prior_for(p)

    off = prior_key(_fire(_with(_long_ctx(), "up")))
    on = prior_key(_fire(_with(_long_ctx(), "up"), {"ign_trend_confidence_weight": "0.1"}))
    assert off.key.endswith(f"IGN{S.BAND_SEP}C0"), off.key
    assert on.key.endswith(f"IGN{S.BAND_SEP}C2"), (on.key, base)
    assert on.mean_r > off.mean_r, "a healthier trend is priced from the better band"


# ── the forming-candle panel ────────────────────────────────────────────────

def test_forming_panel_is_a_different_quantity_from_the_as_of_panel():
    down = _bars("down")
    spike = dict(date=END + timedelta(days=1), open=100.0, high=110.0, low=99.8,
                 close=109.0, volume=9e6)
    as_of = feature_panel(down)
    live = forming_panel(down, **spike)
    assert as_of["above_st"] is False, "as of yesterday's close the name is in a downtrend"
    assert live["above_st"] is True, (
        "with today's +9% candle appended it sits above its own SuperTrend line: the "
        "spike drags the signal by construction, which is why it is a different signal")
    assert live["n_bars"] == as_of["n_bars"] + 1
    assert live["as_of"] == str(spike["date"]) and live["forming"] is True
    assert live["prev_vol_ratio"] is None, "partial-day volume vs a full-day baseline is not comparable"


def test_forming_panel_refuses_unusable_input():
    bars = _bars("up")
    ok = dict(date=END + timedelta(days=1), open=100.0, high=101.0, low=99.0, close=100.5, volume=1e6)
    assert forming_panel(bars, **ok)["above_st"] is not None
    for bad in (dict(ok, close=0), dict(ok, high=98.0), dict(ok, date=END)):
        assert forming_panel(bars, **bad)["above_st"] is None, bad
    assert forming_panel([], **ok)["above_st"] is None


def _forming_ctx(kind="down"):
    ctx = _with(_long_ctx(), kind)          # ltp 106 vs prev_close 100: a +6% spike today
    return ctx


def test_forming_feats_builds_from_the_context_and_guards_its_inputs():
    ctx = _forming_ctx()
    p = T.forming_feats(ctx)
    assert p is not None and p["ok"] is True and p["forming"] is True
    assert p["as_of"] == "2026-08-05", "as of TODAY, from ctx.as_of, not the wall clock"
    assert p["above_st"] is True, "the +6% candle reclaims the SuperTrend of a falling name"
    ctx2 = _forming_ctx()
    ctx2.daily_bars = None
    assert T.forming_feats(ctx2) is None, "no bars -> cannot build"
    ctx3 = _forming_ctx()
    ctx3.daily_feats = dict(ctx3.daily_feats, ok=False)
    assert T.forming_feats(ctx3) is None, "a failed-integrity as-of panel blocks the forming one too"
    ctx4 = _forming_ctx()
    ctx4.daily_bars = ctx4.daily_bars + [DailyBar(date(2026, 8, 5), 1, 1, 1, 1, 1)]
    assert T.forming_feats(ctx4) is None, (
        "a completed bar dated TODAY means the as-of side is already contaminated: refuse")
    ctx5 = _forming_ctx()
    ctx5.day_open = None
    assert T.forming_feats(ctx5) is None


def test_active_feats_uses_the_switch_and_never_silently_falls_back():
    ctx = _forming_ctx()
    with cfg_ctx({}):
        assert T.active_feats(ctx) is ctx.daily_feats, "default: the as-of panel"
    with cfg_ctx({"ign_trend_use_forming": "true"}):
        live = T.active_feats(ctx)
        assert live is not None and live.get("forming") is True
        ctx.daily_bars = None
        assert T.active_feats(ctx) is None, (
            "forming asked for but unavailable: abstain, do NOT fall back to the other quantity")


def test_the_gate_reads_the_forming_panel_only_when_told_to():
    armed = {"ign_trend_gate_enabled": "true", "ign_trend_require_above_st": "true"}
    as_of_bad = _forming_ctx("down")                     # as-of above_st False, forming True
    assert _fire(as_of_bad, armed) is None, "default: yesterday's panel decides -> refused"
    s = _fire(_forming_ctx("down"), {**armed, "ign_trend_use_forming": "true"})
    assert s is not None and s.meta["trend"]["gate"] == "pass", (
        "forming switch on: today's candle decides -> passes")
    # forming switch on but it cannot be built: abstain (allowed), never a silent refusal
    ctx = _forming_ctx("down")
    ctx.daily_bars = None
    s2 = _fire(ctx, {**armed, "ign_trend_use_forming": "true"})
    assert s2 is not None and s2.meta["trend"]["gate"] == "abstain"


def test_trend_live_is_recorded_when_buildable_and_absent_when_not():
    s = _fire(_forming_ctx("down"))
    live = s.meta["trend_live"]
    assert set(live) == set(T.LIVE_KEYS) | {"as_of"}, live
    assert live["above_st"] is True and s.meta["trend"]["above_st"] is False, (
        "the two records disagree on purpose: they are different quantities")
    assert "trend_live" not in _fire(_long_ctx()).meta, "no bars -> no key, no wasted bytes"


def test_recording_forming_changes_no_trade():
    plain = _fire(_long_ctx())
    rec = _fire(_forming_ctx("down"))
    assert _core(plain) == _core(rec), "record-only: the setup itself is untouched"


TESTS = [
    ("short verdict is the mirror of the long one", test_short_verdict_is_the_mirror_of_the_long_one),
    ("each short check bites on its own", test_each_short_check_bites_on_its_own),
    ("short verdict abstains like the long one", test_short_verdict_abstains_like_the_long_one),
    ("an unknown direction raises instead of defaulting to long",
     test_an_unknown_direction_raises_instead_of_defaulting_to_long),
    ("short gate refuses an uptrend only when armed",
     test_short_gate_refuses_a_short_into_a_healthy_uptrend_only_when_armed),
    ("long and short switches do not leak into each other",
     test_long_and_short_switches_do_not_leak_into_each_other),
    ("disarmed short gate changes no trade", test_disarmed_short_gate_changes_no_trade),
    ("every config key is wired to its own gate parameter",
     test_every_config_key_is_wired_to_its_own_gate_parameter),
    ("score, hand-computed", test_score_hand_computed),
    ("score thresholds equal the pre-registered ones",
     test_score_thresholds_equal_the_pre_registered_ones),
    ("weight zero changes nothing and records the score",
     test_weight_zero_changes_nothing_and_records_the_score),
    ("weight moves confidence in the score direction for each side",
     test_weight_moves_confidence_in_the_score_direction_for_each_side),
    ("confidence stays inside its bounds", test_confidence_stays_inside_its_bounds),
    ("the score reaches the allocator's confidence band through its own lookup",
     test_the_score_reaches_the_allocators_confidence_band_through_its_own_lookup),
    ("forming panel is a different quantity from the as-of panel",
     test_forming_panel_is_a_different_quantity_from_the_as_of_panel),
    ("forming panel refuses unusable input", test_forming_panel_refuses_unusable_input),
    ("forming feats builds from the context and guards its inputs",
     test_forming_feats_builds_from_the_context_and_guards_its_inputs),
    ("active feats uses the switch and never silently falls back",
     test_active_feats_uses_the_switch_and_never_silently_falls_back),
    ("the gate reads the forming panel only when told to",
     test_the_gate_reads_the_forming_panel_only_when_told_to),
    ("trend_live is recorded when buildable and absent when not",
     test_trend_live_is_recorded_when_buildable_and_absent_when_not),
    ("recording forming changes no trade", test_recording_forming_changes_no_trade),
]
