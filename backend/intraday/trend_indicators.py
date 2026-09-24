"""
Daily trend indicators — pure, no I/O. Built for IGN's entry-quality study and
gate (docs/FINDINGS.md, 24-Sep-2026).

WHY THIS EXISTS
---------------
`sma_50/sma_200/supertrend/adx_minus_di` in `stock_data_daily` are Chartink
pass-throughs and the table now keeps only 8 days (migration 130), so nothing in
this repo could compute SuperTrend or Wilder DI/ADX, and nothing could compute a
200-day average from history. The operator's rule is that every input to a
buy/sell decision comes from Kite, so these are computed here from Kite daily
bars (intraday/daily_history.py fetches them).

EVERY FEATURE IS AS-OF THE LAST BAR PASSED IN. The caller decides which bar
that is; `daily_history.py` passes only COMPLETED sessions, never today's
forming candle. That is the quantity the replay study measures — a feature
recomputed with today's forming bar is a different, unvalidated signal (a +5%
spike drags price above its own SuperTrend by construction).

DEFINITIONS (ours, not Chartink's — parity with Chartink is informational)
--------------------------------------------------------------------------
  TR          max(high-low, |high-prev_close|, |low-prev_close|)
  ATR(n)      Wilder: mean of the first n TRs, then (prev*(n-1)+TR)/n
  SuperTrend  the standard final-band construction, ATR period 10, mult 3.0.
              STATEFUL — the bands only tighten while the trend holds, and a
              trend flips only when close crosses the opposing band. The seed
              washes out over a long history; callers pass 250+ bars.
  DI/ADX(n)   Wilder: sums of TR/+DM/-DM seeded with the first n, then
              s - s/n + x; ADX seeded with the mean of the first n DX values.
  RSI(n)      Wilder.

Every function returns None where the history is too short. It never guesses.
"""

from __future__ import annotations

from typing import NamedTuple, Sequence


class DailyBar(NamedTuple):
    date: object
    open: float
    high: float
    low: float
    close: float
    volume: float


PANEL_KEYS = (
    "above_st", "di_plus", "di_minus", "adx",
    "sma50_gt_200", "above_sma50", "dist_sma50",
    "rsi14", "prev_vol_ratio", "ret_1m",
)


def sma(values: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def _true_ranges(bars: Sequence[DailyBar]) -> list[float | None]:
    out: list[float | None] = [None] * len(bars)
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        out[i] = max(h - l, abs(h - pc), abs(l - pc))
    return out


def wilder_atr(bars: Sequence[DailyBar], n: int = 14) -> list[float | None]:
    """ATR aligned to `bars`; None until n true ranges exist (index n)."""
    out: list[float | None] = [None] * len(bars)
    if n <= 0 or len(bars) < n + 1:
        return out
    tr = _true_ranges(bars)
    atr = sum(tr[1:n + 1]) / n
    out[n] = atr
    for i in range(n + 1, len(bars)):
        atr = (atr * (n - 1) + tr[i]) / n
        out[i] = atr
    return out


def supertrend(bars: Sequence[DailyBar], period: int = 10,
               mult: float = 3.0) -> list[tuple[float, bool] | None]:
    """(line, close_above_line) aligned to `bars`; None before the ATR exists."""
    n = len(bars)
    atr = wilder_atr(bars, period)
    out: list[tuple[float, bool] | None] = [None] * n
    fub = flb = None
    down = True
    for i in range(n):
        a = atr[i]
        if a is None:
            continue
        hl2 = (bars[i].high + bars[i].low) / 2.0
        bub, blb = hl2 + mult * a, hl2 - mult * a
        c = bars[i].close
        if fub is None:
            fub, flb = bub, blb
            down = c <= fub
        else:
            pc = bars[i - 1].close
            nfub = bub if (bub < fub or pc > fub) else fub
            nflb = blb if (blb > flb or pc < flb) else flb
            if down:
                if c > nfub:
                    down = False
            else:
                if c < nflb:
                    down = True
            fub, flb = nfub, nflb
        line = fub if down else flb
        out[i] = (line, c > line)
    return out


def wilder_dmi(bars: Sequence[DailyBar],
               n: int = 14) -> tuple[float, float, float] | None:
    """(+DI, -DI, ADX) at the last bar, or None if fewer than 2n bars."""
    if n <= 0 or len(bars) < 2 * n:
        return None
    tr: list[float] = []
    pdm: list[float] = []
    mdm: list[float] = []
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        dn = bars[i - 1].low - bars[i].low
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dx: list[float] = []
    pdi = mdi = 0.0
    for k in range(n, len(tr) + 1):
        if k > n:
            s_tr = s_tr - s_tr / n + tr[k - 1]
            s_p = s_p - s_p / n + pdm[k - 1]
            s_m = s_m - s_m / n + mdm[k - 1]
        pdi = 100.0 * s_p / s_tr if s_tr > 0 else 0.0
        mdi = 100.0 * s_m / s_tr if s_tr > 0 else 0.0
        tot = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / tot if tot > 0 else 0.0)
    if len(dx) < n:
        return None
    adx = sum(dx[:n]) / n
    for v in dx[n:]:
        adx = (adx * (n - 1) + v) / n
    return pdi, mdi, adx


def rsi(closes: Sequence[float], n: int = 14) -> float | None:
    if n <= 0 or len(closes) < n + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def max_close_jump(bars: Sequence[DailyBar]) -> float | None:
    """Largest adjacent close-to-close move as a ratio >= 1 (2.0 == halved or
    doubled). A split/bonus the vendor did not adjust shows up here."""
    worst = None
    for i in range(1, len(bars)):
        a, b = bars[i - 1].close, bars[i].close
        if a > 0 and b > 0:
            r = max(a / b, b / a)
            if worst is None or r > worst:
                worst = r
    return worst


def feature_panel(bars: Sequence[DailyBar], *, st_period: int = 10,
                  st_mult: float = 3.0, dmi_n: int = 14,
                  rsi_n: int = 14) -> dict:
    """Every feature as-of the last bar. A field is None when its own history
    is too short — never a default that looks like a measurement."""
    out: dict = {k: None for k in PANEL_KEYS}
    out["n_bars"] = len(bars)
    out["as_of"] = str(bars[-1].date) if bars else None
    if not bars:
        return out
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    close = closes[-1]

    s50, s200 = sma(closes, 50), sma(closes, 200)
    if s50 is not None and s50 > 0:
        out["above_sma50"] = bool(close > s50)
        out["dist_sma50"] = round((close - s50) / s50 * 100.0, 4)
        if s200 is not None:
            out["sma50_gt_200"] = bool(s50 > s200)

    st = supertrend(bars, st_period, st_mult)
    if st and st[-1] is not None:
        out["above_st"] = bool(st[-1][1])

    dmi = wilder_dmi(bars, dmi_n)
    if dmi is not None:
        out["di_plus"] = round(dmi[0], 4)
        out["di_minus"] = round(dmi[1], 4)
        out["adx"] = round(dmi[2], 4)

    r = rsi(closes, rsi_n)
    if r is not None:
        out["rsi14"] = round(r, 4)

    if len(vols) >= 21:
        base = sum(vols[-21:-1]) / 20.0
        if base > 0:
            out["prev_vol_ratio"] = round(vols[-1] / base, 4)

    if len(closes) >= 22 and closes[-22] > 0:
        out["ret_1m"] = round((close / closes[-22] - 1.0) * 100.0, 4)
    return out
