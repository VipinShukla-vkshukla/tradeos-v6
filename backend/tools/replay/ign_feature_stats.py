"""
IGN feature study — statistics and pre-registration. Pure, no I/O.

The five signals behind the first IGN filter proposal were measured on 3,824 rows that were
54 independent symbol-days; at the independent unit (n=35 LONG) none was significant and two
changed sign. This module fixes ONE row per (symbol, day), the hypotheses and decision rule
BEFORE any replay result, a permutation p with a Holm correction, and a time-ordered holdout
looked at once.

Everything between the PRE-REGISTERED markers below was committed before the replay ran;
changing it after seeing a result invalidates the study. R is gross R from the live IGN exit
ladder over real Kite minute bars; costs (~0.2R on MIS) are excluded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time as dtime

import numpy as np
from scipy.stats import rankdata

# ── PRE-REGISTERED — 24-Sep-2026, before any replay result existed ───────────

# (feature, expected sign of Spearman(feature, gross R); 0 = two-sided because
# the earlier evidence for its sign was contradictory). Order is the Holm order.
HYPOTHESES: tuple[tuple[str, int], ...] = (
    ("above_st", +1),         # price above its daily SuperTrend(10,3) line
    ("adx", +1),              # a trending market carries a move
    ("di_minus", -1),         # selling pressure under the spike
    ("prev_vol_ratio", -1),   # yesterday already a blow-off -> exhausted
    ("sma50_gt_200", 0),      # flipped sign between the row-level and symbol-day views
    ("dist_sma50", 0),        # extension above the 50DMA: momentum or exhaustion
    ("rsi14", 0),             # likewise
)
# Recorded and reported, never used to arm anything.
EXPLORATORY: tuple[str, ...] = ("di_plus", "above_sma50", "ret_1m")

# Fixed round/conventional thresholds — NOT tuned on the data. A feature can only
# arm if it has a gate mapping here; dist_sma50 and rsi14 deliberately do not.
GATE_MAP: dict[str, tuple[str, object]] = {
    "above_st": ("require_above_st", True),
    "adx": ("min_adx", 20.0),
    "di_minus": ("max_di_minus", 20.0),
    "prev_vol_ratio": ("max_prev_vol_ratio", 2.0),
    "sma50_gt_200": ("require_sma50_gt_200", True),
}

HOLDOUT_FRAC = 1.0 / 3.0       # newest third of the trading days
MIN_N_TOTAL = 150              # symbol-days in the live-config population
HOLM_ALPHA = 0.10              # on TRAIN, across the seven pre-registered features
CI_LEVEL = 0.95                # holdout CI on rho must exclude zero
MIN_IMPROVEMENT_R = 0.10       # kept-group mean gross R beats dropped-group's by this
MIN_KEPT_FRAC = 0.40           # and the gate may not remove more than 60% of trades
N_PERM = 4000
N_BOOT = 2000
GAP_LIMIT_PCT = 22.0           # open vs prior close beyond this = split/bonus, skip the day
MIN_TURNOVER_CR = 25.0         # intraday_min_turnover_cr
MIN_PRICE = 50.0               # intraday_min_price
PREFILTER_VOL_MULT = 1.2       # loose: day's volume vs prior day's

# ── end of pre-registration ──────────────────────────────────────────────────


@dataclass
class Row:
    """One independent observation: the first detection of a symbol-day."""
    symbol: str
    day: str
    r: float
    feats: dict
    hour_bucket: str = ""
    action: str = ""
    extra: dict = field(default_factory=dict)


def _num(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def feature_pairs(rows: list[Row], feature: str) -> tuple[np.ndarray, np.ndarray]:
    xs, ys = [], []
    for r in rows:
        f = r.feats or {}
        if not f.get("ok"):
            continue
        x = _num(f.get(feature))
        if x is not None:
            xs.append(x)
            ys.append(r.r)
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    """NaN when either side is constant — no variation is not a zero effect."""
    if len(x) < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return float("nan")
    rx, ry = rankdata(x), rankdata(y)
    return float(np.corrcoef(rx, ry)[0, 1])


def perm_pvalue(x: np.ndarray, y: np.ndarray, n_perm: int | None = None,
                seed: int = 0) -> float:
    """Two-sided permutation p for Spearman. Valid with ties and booleans.
    `n_perm` resolves to N_PERM at CALL time so a test may lower it."""
    n_perm = N_PERM if n_perm is None else n_perm
    obs = spearman(x, y)
    if obs != obs:
        return float("nan")
    rng = np.random.default_rng(seed)
    rx, ry = rankdata(x), rankdata(y)
    rx_c, ry_c = rx - rx.mean(), ry - ry.mean()
    denom = np.sqrt((rx_c ** 2).sum() * (ry_c ** 2).sum())
    perms = np.argsort(rng.random((n_perm, len(x))), axis=1)
    stat = (ry_c[perms] @ rx_c) / denom
    return float((1 + (np.abs(stat) >= abs(obs) - 1e-12).sum()) / (n_perm + 1))


def bootstrap_ci(x: np.ndarray, y: np.ndarray, n_boot: int | None = None,
                 level: float = CI_LEVEL, seed: int = 1) -> tuple[float, float]:
    n_boot = N_BOOT if n_boot is None else n_boot
    if len(x) < 5:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    m = len(x)
    idx = rng.integers(0, m, (n_boot, m))
    rx = rankdata(x[idx], axis=1)
    ry = rankdata(y[idx], axis=1)
    rx -= rx.mean(axis=1, keepdims=True)
    ry -= ry.mean(axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        vals = (rx * ry).sum(axis=1) / np.sqrt((rx ** 2).sum(axis=1) * (ry ** 2).sum(axis=1))
    vals = vals[np.isfinite(vals)]
    if len(vals) < n_boot // 2:
        return float("nan"), float("nan")
    a = (1 - level) / 2
    return float(np.quantile(vals, a)), float(np.quantile(vals, 1 - a))


def holm(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in the input order. NaN stays NaN and
    does not count toward the number of tests."""
    idx = [i for i, p in enumerate(pvals) if p == p]
    order = sorted(idx, key=lambda i: pvals[i])
    k = len(order)
    adj = [float("nan")] * len(pvals)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (k - rank) * pvals[i])
        adj[i] = min(1.0, running)
    return adj


def time_split(rows: list[Row], holdout_frac: float = HOLDOUT_FRAC
               ) -> tuple[list[Row], list[Row]]:
    """Older trading days train, newest holdout. Cut on DAYS so no day straddles."""
    days = sorted({r.day for r in rows})
    if len(days) < 3:
        return list(rows), []
    cut = max(1, int(round(len(days) * (1.0 - holdout_frac))))
    train_days = set(days[:cut])
    return ([r for r in rows if r.day in train_days],
            [r for r in rows if r.day not in train_days])


def first_detection(dets: list, population: str):
    """First LONG detection of a symbol-day for a population.

    "live"    — what the live system would record with the armed open-hour gate:
                the first LONG at or after 10:00.
    "ungated" — the first LONG at any time.
    Detections must be in time order and un-deduplicated (dedup_pct < 0 in the
    replay) so the first post-10:00 detection exists even when an open-hour one
    would have de-duplicated it.
    """
    for d in sorted(dets, key=lambda d: d.ts):
        if d.direction != "LONG":
            continue
        if population == "live" and d.ts.time() < dtime(10, 0):
            continue
        return d
    return None


def hour_bucket_of(ts) -> str:
    t = ts.time()
    if t < dtime(10, 0):
        return "OPEN"
    if t < dtime(13, 0):
        return "MID"
    return "LATE"


def candidate_days(daily: dict[str, list[dict]], start: str, end: str, *,
                   min_price: float = MIN_PRICE,
                   min_turnover_cr: float = MIN_TURNOVER_CR,
                   vol_mult: float = PREFILTER_VOL_MULT,
                   up_pct: float = 3.0) -> list[tuple[str, str]]:
    """(symbol, day) pairs worth fetching minute bars for. `daily` maps symbol ->
    date-ordered rows with date/high/close/volume. A deliberately LOOSE superset
    of what IGN can fire on: the rules themselves decide, this only avoids
    fetching a full session for names that never moved."""
    out = []
    for sym, rows in daily.items():
        for i in range(1, len(rows)):
            d = str(rows[i]["date"])[:10]
            if d < start or d > end:
                continue
            pc, pv = float(rows[i - 1]["close"] or 0), float(rows[i - 1]["volume"] or 0)
            if pc < min_price or pv <= 0:
                continue
            if pc * pv / 1e7 < min_turnover_cr:
                continue
            hi, vol = float(rows[i]["high"] or 0), float(rows[i]["volume"] or 0)
            if hi / pc - 1.0 < up_pct / 100.0 or vol < vol_mult * pv:
                continue
            out.append((sym, d))
    return sorted(out, key=lambda t: (t[1], t[0]))


def gap_ok(prev_close: float, first_open: float, limit_pct: float = GAP_LIMIT_PCT) -> bool:
    if not prev_close or not first_open or prev_close <= 0:
        return False
    return abs(first_open / prev_close - 1.0) * 100.0 <= limit_pct


def prev_from_daily(bars: list, day: date) -> dict | None:
    """The `prev` dict tools.replay.contexts.build_context reads, built from Kite
    daily bars strictly BEFORE `day`. `volume` is the prior session's raw volume,
    matching what live puts in `avg_volume_20d` (see SymbolContext)."""
    from intraday.trend_indicators import wilder_atr
    prior = [b for b in bars if b.date < day]
    if len(prior) < 15:
        return None
    last = prior[-1]
    atr = wilder_atr(prior, 14)[-1]
    if atr is None or last.close <= 0:
        return None
    return {"close": last.close, "high": last.high, "low": last.low,
            "atr_pct": atr / last.close * 100.0, "volume": last.volume,
            "value_cr": last.close * last.volume / 1e7, "sector": ""}


# ── analysis ────────────────────────────────────────────────────────────────

def describe(rows: list[Row]) -> dict:
    rs = np.asarray([r.r for r in rows], dtype=float)
    n = len(rs)
    if n == 0:
        return {"n": 0}
    se = float(rs.std(ddof=1) / np.sqrt(n)) if n > 1 else float("nan")
    return {"n": n, "mean_r": float(rs.mean()), "se": se,
            "median_r": float(np.median(rs)), "win": float((rs > 0).mean())}


def test_feature(rows: list[Row], feature: str, sign: int, *, n_perm: int | None = None,
                 n_boot: int | None = None, seed: int = 0, with_ci: bool = True) -> dict:
    x, y = feature_pairs(rows, feature)
    rho = spearman(x, y)
    nan2 = (float("nan"), float("nan"))
    return {"feature": feature, "sign": sign, "n": int(len(x)), "rho": rho,
            "p": perm_pvalue(x, y, n_perm, seed) if rho == rho else float("nan"),
            "ci": bootstrap_ci(x, y, n_boot, seed=seed + 1)
            if (with_ci and rho == rho) else nan2}


def sign_agrees(rho: float, sign: int) -> bool:
    if rho != rho:
        return False
    return True if sign == 0 else (rho > 0) == (sign > 0)


def gate_config(features: list[str]) -> dict:
    cfg = dict(require_above_st=False, min_adx=0.0, max_di_minus=0.0,
               max_prev_vol_ratio=0.0, require_sma50_gt_200=False, min_agree=0)
    for f in features:
        key, val = GATE_MAP[f]
        cfg[key] = val
    return cfg


def gate_partition(rows: list[Row], cfg: dict) -> dict[str, list[Row]]:
    from intraday.strategies.ignition import trend_verdict
    out = {"pass": [], "refuse": [], "abstain": []}
    for r in rows:
        verdict, _, _ = trend_verdict(r.feats, **cfg)
        out[verdict].append(r)
    return out


def train_stage(train: list[Row]) -> dict:
    """Pre-registered features tested on TRAIN only; Holm across the whole list."""
    tests = [test_feature(train, f, s, with_ci=False) for f, s in HYPOTHESES]
    adj = holm([t["p"] for t in tests])
    for t, a in zip(tests, adj):
        t["p_holm"] = a
        t["candidate"] = bool(a == a and a <= HOLM_ALPHA
                              and sign_agrees(t["rho"], t["sign"]))
    return {"tests": tests,
            "candidates": [t["feature"] for t in tests if t["candidate"]]}


def holdout_stage(hold: list[Row], candidates: list[tuple[str, int]]) -> dict:
    """One look. `candidates` is (feature, sign) with the sign already resolved
    (a two-sided feature carries the sign it showed on TRAIN — see
    resolve_signs). CONFIRMED when the holdout sign agrees and the holdout
    bootstrap CI on rho excludes zero."""
    res = []
    for f, s in candidates:
        t = test_feature(hold, f, s)
        lo, hi = t["ci"]
        t["confirmed"] = bool(t["rho"] == t["rho"] and sign_agrees(t["rho"], s)
                              and lo == lo and (lo > 0 or hi < 0))
        res.append(t)
    return {"tests": res, "confirmed": [t["feature"] for t in res if t["confirmed"]]}


def resolve_signs(train_tests: list[dict]) -> list[tuple[str, int]]:
    """A two-sided candidate is carried into the holdout with the sign it showed
    on TRAIN — that is the direction being confirmed."""
    out = []
    for t in train_tests:
        if not t["candidate"]:
            continue
        s = t["sign"] or (1 if t["rho"] > 0 else -1)
        out.append((t["feature"], s))
    return out


def gate_verdict(hold: list[Row], confirmed: list[str]) -> dict:
    """Would the confirmed composite have helped on the holdout?"""
    usable = [f for f in confirmed if f in GATE_MAP]
    if not usable:
        return {"arm": False, "why": "no confirmed signal has a gate check"}
    cfg = gate_config(usable)
    part = gate_partition(hold, cfg)
    kept, dropped = part["pass"] + part["abstain"], part["refuse"]
    if not dropped or not kept:
        return {"arm": False, "cfg": cfg, "why": "gate separates nothing on the holdout",
                "kept": len(kept), "dropped": len(dropped)}
    kr = np.asarray([r.r for r in kept])
    dr = np.asarray([r.r for r in dropped])
    diff = float(kr.mean() - dr.mean())
    frac = len(kept) / len(hold)
    rng = np.random.default_rng(7)
    boots = []
    for _ in range(N_BOOT):
        a = kr[rng.integers(0, len(kr), len(kr))].mean()
        b = dr[rng.integers(0, len(dr), len(dr))].mean()
        boots.append(a - b)
    lo = float(np.quantile(boots, (1 - CI_LEVEL) / 2))
    ok = bool(diff >= MIN_IMPROVEMENT_R and frac >= MIN_KEPT_FRAC and lo > 0)
    return {"arm": ok, "cfg": cfg, "features": usable, "kept": len(kept),
            "dropped": len(dropped), "kept_frac": frac,
            "kept_mean_r": float(kr.mean()), "dropped_mean_r": float(dr.mean()),
            "diff": diff, "diff_ci_lo": lo,
            "why": "arm" if ok else
            f"diff {diff:+.3f}R (need >= {MIN_IMPROVEMENT_R}), kept {frac:.0%} "
            f"(need >= {MIN_KEPT_FRAC:.0%}), CI lo {lo:+.3f} (need > 0)"}


def hour_effect(rows: list[Row]) -> dict:
    """OPEN vs MID+LATE on the ungated first detection — checks migration 141's
    armed open-hour gate at the independent unit. Reported, never armed here."""
    o = [r for r in rows if r.hour_bucket == "OPEN"]
    m = [r for r in rows if r.hour_bucket in ("MID", "LATE")]
    if len(o) < 5 or len(m) < 5:
        return {"n_open": len(o), "n_later": len(m), "note": "too few to say"}
    ro = np.asarray([r.r for r in o])
    rm = np.asarray([r.r for r in m])
    rng = np.random.default_rng(11)
    boots = [rm[rng.integers(0, len(rm), len(rm))].mean()
             - ro[rng.integers(0, len(ro), len(ro))].mean() for _ in range(N_BOOT)]
    return {"n_open": len(o), "n_later": len(m), "open_mean_r": float(ro.mean()),
            "later_mean_r": float(rm.mean()), "diff": float(rm.mean() - ro.mean()),
            "ci": (float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975)))}
