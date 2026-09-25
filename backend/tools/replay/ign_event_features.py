"""
Pure feature and outcome extraction for the IGN event study. numpy in, dicts out, no I/O.

An EVENT is one (symbol, day, trigger) where a stock's minute bar CLOSES at least `move_pct`
above the prior close (and, optionally, IGN's volume ratio clears `vr_min`). Every feature is
computed from data up to and including that bar's close and from prior sessions only; every
outcome uses bars strictly after it. The first tests in tests/test_ign_event_features.py are the
look-ahead tests, because a leak here would make everything downstream look better than it is.

IGN PARITY. `first_trigger` with move 3.5, vr 2.0, the 8-bar minimum and the structural-stop test
reproduces `IgnitionMomentum.evaluate` on the same bars (tests compare it to the live class).
  volume ratio   cumulative volume through bar i / (prior-day volume * max(1, i) / 375)   [as live]
  structural stop  min low of the last 20 bars * (1 - 0.12%), refused above 1.75% risk    [as live]

Bars are indexed by minute since 09:15 (bar 0 = 09:15). Bar i closes at 09:16 + i.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence

import numpy as np

SESSION_MIN = 375
LAST_EXIT_IDX = 359                  # the 15:14 bar, the last close before the 15:15 square-off
MIN_BARS = 8                         # ign_min_bars
STOP_LOOKBACK = 20                   # ign_stop_lookback_bars
STOP_BUFFER = 0.0012                 # ign_stop_buffer_pct / 100
MAX_RISK_PCT = 1.75                  # ign_max_risk_pct
OPEN_HOUR_END_IDX = 44               # first bar whose close is >= 10:00 (bar 44 closes 10:00)


class DayBars(NamedTuple):
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray

    @property
    def n(self) -> int:
        return len(self.c)


def day_from_rows(rows: Sequence[dict | tuple]) -> DayBars:
    """(open, high, low, close, volume) per minute bar, oldest first."""
    a = np.asarray([[r["open"], r["high"], r["low"], r["close"], r["volume"]]
                    if isinstance(r, dict) else list(r) for r in rows], dtype=float)
    return DayBars(a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4])


# ── the trigger and the stop ────────────────────────────────────────────────

def ign_volume_ratio(d: DayBars, prev_day_volume: float) -> np.ndarray:
    """IGN's own time-adjusted ratio at every bar, exactly as SymbolContext.volume_ratio()."""
    idx = np.arange(d.n)
    mins = np.maximum(1.0, idx.astype(float))
    return np.cumsum(d.v) / (prev_day_volume * mins / SESSION_MIN)


def first_trigger(d: DayBars, prev_close: float, prev_day_volume: float, move_pct: float,
                  vr_min: float = 0.0, *, min_idx: int = MIN_BARS - 1,
                  require_feasible: bool = False) -> int | None:
    """First bar index whose CLOSE is >= move_pct above the prior close, with IGN's volume
    ratio >= vr_min (0 = no volume test) and at least min_idx+1 bars seen. With
    require_feasible the structural stop must also exist within IGN's max risk, which is how
    the live engine's first DETECTION is defined."""
    if d.n == 0 or prev_close <= 0:
        return None
    ok = (d.c / prev_close - 1.0) * 100.0 >= move_pct
    ok &= np.arange(d.n) >= min_idx
    if vr_min > 0:
        if prev_day_volume <= 0:
            return None
        ok &= ign_volume_ratio(d, prev_day_volume) >= vr_min
    for i in np.flatnonzero(ok):
        if not require_feasible:
            return int(i)
        risk = structural_risk_pct(d, int(i))
        if risk is not None and 0 < risk <= MAX_RISK_PCT:
            return int(i)
    return None


def structural_stop(d: DayBars, i: int, lookback: int = STOP_LOOKBACK,
                    buffer: float = STOP_BUFFER) -> float:
    lo = d.l[max(0, i - lookback + 1): i + 1].min()
    return float(lo * (1.0 - buffer))


def structural_risk_pct(d: DayBars, i: int) -> float | None:
    """Risk as a % of the trigger close, or None if the stop is not below it."""
    stop = structural_stop(d, i)
    c = float(d.c[i])
    if c <= 0 or stop >= c:
        return None
    return (c - stop) / c * 100.0


# ── the intraday volume profile ─────────────────────────────────────────────

def volume_profile(days: Sequence[DayBars]) -> np.ndarray:
    """f[i] = median over days of (volume through bar i) / (the day's total volume): the
    fraction of a normal session's volume that has usually traded by bar i. A time-of-day
    benchmark, so relative volume at 09:45 and at 13:00 mean the same thing."""
    fr = []
    for d in days:
        tot = d.v.sum()
        if d.n >= SESSION_MIN - 5 and tot > 0:
            f = np.cumsum(d.v) / tot
            fr.append(np.pad(f, (0, max(0, SESSION_MIN - len(f))), constant_values=1.0)[:SESSION_MIN])
    if not fr:
        raise ValueError("no full sessions to build a volume profile from")
    return np.median(np.vstack(fr), axis=0)


# ── intraday features at the trigger bar ────────────────────────────────────

def intraday_features(d: DayBars, i: int, prev_close: float, prev_day_volume: float,
                      avg20_volume: float, profile: np.ndarray) -> dict:
    """Everything the stock's own minute bars say at bar i's close. Uses bars 0..i only."""
    o, h, l, c, v = d.o[:i + 1], d.h[:i + 1], d.l[:i + 1], d.c[:i + 1], d.v[:i + 1]
    ci = float(c[-1])
    hi, lo = float(h.max()), float(l.min())
    typ = (h + l + c) / 3.0
    cumv = float(v.sum())
    vwap = float((typ * v).sum() / cumv) if cumv > 0 else float(typ.mean())
    f: dict = {}
    f["idx"] = i
    f["move_pct"] = (ci / prev_close - 1.0) * 100.0
    f["gap_pct"] = (float(o[0]) / prev_close - 1.0) * 100.0
    f["move_from_open_pct"] = (ci / float(o[0]) - 1.0) * 100.0
    f["from_high_pct"] = (ci / hi - 1.0) * 100.0                   # <= 0: how far off the day high
    f["pos_in_range"] = (ci - lo) / (hi - lo) if hi > lo else 1.0
    f["vwap_dev_pct"] = (ci / vwap - 1.0) * 100.0
    f["pct_bars_above_vwap"] = _pct_above_running_vwap(h, l, c, v)
    # opening range = first 15 bars; only meaningful once it is complete
    if i >= 15:
        orh = float(h[:15].max())
        f["above_orh"] = bool(ci > orh)
        f["orh_dist_pct"] = (ci / orh - 1.0) * 100.0
    else:
        f["above_orh"] = None
        f["orh_dist_pct"] = None
    # speed and shape of the move
    over1 = np.flatnonzero((c / prev_close - 1.0) * 100.0 >= 1.0)
    f["bars_since_1pct"] = int(i - over1[0]) if len(over1) else None
    win = c[max(0, i - 29): i + 1]
    steps = np.abs(np.diff(win))
    f["path_eff_30"] = float(abs(win[-1] - win[0]) / steps.sum()) if len(win) > 1 and steps.sum() > 0 else None
    f["ret_30_pct"] = (ci / float(c[max(0, i - 30)]) - 1.0) * 100.0
    f["ret_5_pct"] = (ci / float(c[max(0, i - 5)]) - 1.0) * 100.0
    pre = slice(max(0, i - 10), i)
    f["pre_range10_pct"] = ((float(h[pre].max()) - float(l[pre].min())) / ci * 100.0) if i > 0 else None
    rng_now = float(h[-1] - l[-1])
    rng_prior = float((h[max(0, i - 20):i] - l[max(0, i - 20):i]).mean()) if i > 0 else 0.0
    f["trig_bar_range_x"] = rng_now / rng_prior if rng_prior > 0 else None
    vol_prior = float(v[max(0, i - 20):i].mean()) if i > 0 else 0.0
    f["trig_bar_vol_x"] = float(v[-1]) / vol_prior if vol_prior > 0 else None
    v5 = float(v[max(0, i - 4):i + 1].mean())
    v20 = float(v[max(0, i - 24):max(1, i - 4)].mean()) if i > 4 else 0.0
    f["vol_last5_x"] = v5 / v20 if v20 > 0 else None
    # volume, two benchmarks: IGN's own (prior-day based) and the time-of-day profile (20-day)
    f["vr_ign"] = float(cumv / (prev_day_volume * max(1.0, float(i)) / SESSION_MIN)) if prev_day_volume > 0 else None
    exp_frac = float(profile[min(i, len(profile) - 1)])
    f["rvol_profile"] = float(cumv / (avg20_volume * exp_frac)) if avg20_volume > 0 and exp_frac > 0 else None
    f["value_so_far_cr"] = float((typ * v).sum() / 1e7)
    risk = structural_risk_pct(d, i)
    f["risk_pct"] = risk
    f["feasible"] = bool(risk is not None and 0 < risk <= MAX_RISK_PCT)
    return f


def _pct_above_running_vwap(h, l, c, v) -> float:
    typ = (h + l + c) / 3.0
    cv = np.cumsum(v)
    with np.errstate(invalid="ignore", divide="ignore"):
        rv = np.where(cv > 0, np.cumsum(typ * v) / cv, typ)
    return float((c > rv).mean())


# ── the market around the trigger ───────────────────────────────────────────

def market_features(nifty: DayBars | None, n500: DayBars | None, vix: DayBars | None, i: int,
                    nifty_prev_close: float | None, n500_prev_close: float | None,
                    vix_prev_close: float | None, stock_move_pct: float) -> dict:
    """Index state at the same bar. Any input may be missing; its features are then None,
    never a default."""
    f: dict = {"nifty_move_pct": None, "nifty_ret_30_pct": None, "nifty_from_open_pct": None,
               "n500_move_pct": None, "vix": None, "vix_move_pct": None,
               "rs_nifty": None, "rs_n500": None, "n500_minus_nifty": None}
    if nifty is not None and i < nifty.n and nifty_prev_close:
        cn = float(nifty.c[i])
        f["nifty_move_pct"] = (cn / nifty_prev_close - 1.0) * 100.0
        f["nifty_ret_30_pct"] = (cn / float(nifty.c[max(0, i - 30)]) - 1.0) * 100.0
        f["nifty_from_open_pct"] = (cn / float(nifty.o[0]) - 1.0) * 100.0
        f["rs_nifty"] = stock_move_pct - f["nifty_move_pct"]
    if n500 is not None and i < n500.n and n500_prev_close:
        f["n500_move_pct"] = (float(n500.c[i]) / n500_prev_close - 1.0) * 100.0
        f["rs_n500"] = stock_move_pct - f["n500_move_pct"]
    if f["nifty_move_pct"] is not None and f["n500_move_pct"] is not None:
        f["n500_minus_nifty"] = f["n500_move_pct"] - f["nifty_move_pct"]
    if vix is not None and i < vix.n:
        f["vix"] = float(vix.c[i])
        if vix_prev_close:
            f["vix_move_pct"] = (float(vix.c[i]) / vix_prev_close - 1.0) * 100.0
    return f


# ── prior-session (daily) features ──────────────────────────────────────────

def daily_features(bars: Sequence, day_index_hint=None) -> dict:
    """Stock context as of the last COMPLETED session. `bars` are DailyBar-like (date, open,
    high, low, close, volume), oldest first, ALREADY truncated to before the event day."""
    from intraday.trend_indicators import feature_panel, wilder_atr
    out: dict = {}
    n = len(bars)
    if n < 25:
        return out
    closes = np.asarray([b.close for b in bars], dtype=float)
    highs = np.asarray([b.high for b in bars], dtype=float)
    lows = np.asarray([b.low for b in bars], dtype=float)
    vols = np.asarray([b.volume for b in bars], dtype=float)
    panel = feature_panel(bars)
    for k in ("above_st", "adx", "di_plus", "di_minus", "sma50_gt_200", "above_sma50",
              "dist_sma50", "rsi14", "prev_vol_ratio", "ret_1m"):
        out[k] = panel.get(k)
    atr = wilder_atr(bars, 14)[-1]
    out["atr14_pct"] = float(atr / closes[-1] * 100.0) if atr else None
    for w in (20,):
        out[f"dist_sma{w}"] = float((closes[-1] / closes[-w:].mean() - 1.0) * 100.0)
    out["ret_5d"] = float((closes[-1] / closes[-6] - 1.0) * 100.0)
    out["ret_20d"] = float((closes[-1] / closes[-21] - 1.0) * 100.0)
    out["ret_60d"] = float((closes[-1] / closes[-61] - 1.0) * 100.0) if n >= 61 else None
    win = min(252, n)
    out["hi52_dist"] = float((closes[-1] / highs[-win:].max() - 1.0) * 100.0)
    out["lo52_dist"] = float((closes[-1] / lows[-win:].min() - 1.0) * 100.0)
    rng = highs - lows
    out["prev_range_pct"] = float(rng[-1] / closes[-1] * 100.0)
    out["prev_clv"] = float((closes[-1] - lows[-1]) / rng[-1]) if rng[-1] > 0 else None
    out["prev_ret1"] = float((closes[-1] / closes[-2] - 1.0) * 100.0)
    avg_rng20 = rng[-20:].mean()
    out["range_ratio"] = float(rng[-1] / avg_rng20) if avg_rng20 > 0 else None
    out["nr7"] = bool(rng[-1] <= rng[-7:].min())
    out["avg20_volume"] = float(vols[-20:].mean())
    out["prev_day_volume"] = float(vols[-1])
    out["avg20_turnover_cr"] = float((vols[-20:] * closes[-20:]).mean() / 1e7)
    out["prev_close"] = float(closes[-1])
    out["price_level"] = float(closes[-1])
    prior_ign = highs[-20:] / closes[-21:-1] - 1.0 >= 0.03
    out["n_ign_20d"] = int(prior_ign.sum())
    out["prev_day_ign"] = bool(prior_ign[-1])
    up = 0
    for k in range(n - 1, 0, -1):
        if closes[k] > closes[k - 1]:
            up += 1
        else:
            break
    out["up_streak"] = up
    out["prev_high"] = float(highs[-1])
    out["prev_low"] = float(lows[-1])
    return out


def index_daily_features(rows: Sequence[dict], day: str) -> dict:
    """Trend of an index as of the last completed session before `day` (rows have an ISO
    `date` and a `close`)."""
    prior = [r for r in rows if r["date"] < day]
    if len(prior) < 25:
        return {}
    cl = np.asarray([r["close"] for r in prior], dtype=float)
    return {"prev_close": float(cl[-1]),
            "dist_sma20": float((cl[-1] / cl[-20:].mean() - 1.0) * 100.0),
            "ret_5d": float((cl[-1] / cl[-6] - 1.0) * 100.0),
            "ret_20d": float((cl[-1] / cl[-21] - 1.0) * 100.0)}


# ── outcomes: strictly after the trigger bar ────────────────────────────────

HORIZONS = (15, 30, 60, 120)


def forward_outcomes(d: DayBars, i: int, *, fill: str = "next_open") -> dict | None:
    """Gross forward path summary from the entry fill. `fill` is 'next_open' (the first
    executable price after the signal: bar i+1's open) or 'trigger_close' (bar i's close, as
    IGN's replay assumes). Returns None if there is no bar to enter on."""
    if fill == "next_open":
        e = i + 1
        if e >= d.n:
            return None
        entry = float(d.o[e])
    elif fill == "trigger_close":
        e = i + 1
        if e >= d.n:
            return None
        entry = float(d.c[i])
    else:
        raise ValueError(f"fill must be 'next_open' or 'trigger_close', got {fill!r}")
    end = min(d.n - 1, LAST_EXIT_IDX)
    if end < e:
        return None
    hh, ll = d.h[e:end + 1], d.l[e:end + 1]
    out = {"entry": entry, "entry_idx": e,
           "eod_ret_pct": (float(d.c[end]) / entry - 1.0) * 100.0,
           "mfe_pct": (float(hh.max()) / entry - 1.0) * 100.0,
           "mae_pct": (float(ll.min()) / entry - 1.0) * 100.0,
           "bars_to_mfe": int(np.argmax(hh)), "bars_to_mae": int(np.argmin(ll))}
    for hz in HORIZONS:
        j = min(e + hz - 1, end)
        out[f"ret_{hz}_pct"] = (float(d.c[j]) / entry - 1.0) * 100.0
    return out
