"""
Replay the swing trades actually taken, through today's production code. READ-ONLY.

    python -m tools.swing_fix_replay --label baseline
    python -m tools.swing_fix_replay --label fix1 --compare baseline
    python -m tools.swing_fix_replay --regime-audit

Entries are the real ones (closed_positions + open_positions, framework SWING).
Each is walked over 15-minute Kite bars through the real evaluate_exit(), with
the live 15-second cycle approximated by re-evaluating a bar close up to 60
times while the ladder keeps moving the stop. Entry-side rules (regime R:R and
slots via decide(), market exposure, daily cap, one position per symbol) are applied
to that same list by importing whatever the working tree provides.

What it cannot do, stated so a result is not over-read:
  - a trade removed by an entry rule is not replaced by another plan: the
    allocator's per-cycle history is not replayable;
  - sector-decay and participation-decay context is not point-in-time and is
    left out (multipliers stay 1.0);
  - the AI TIGHTEN_SL flag is taken from the recorded alert timestamps, so it
    exists only for positions the live book actually held.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date as _date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loguru import logger

from config import IST, get_supabase

CACHE = Path(__file__).resolve().parent / "replay" / "cache" / "span"
RESULTS = Path(__file__).resolve().parent / "replay" / "results" / "swing_fix"
CYCLES_PER_BAR = 60
#: Window boundary. The excluded history is still REPORTED, labelled
#: "reference", because without it every number describes one falling market.
_SPLIT_FALLBACK = "2026-08-14"


def split_date() -> str:
    from config import swing_data_since
    return swing_data_since() or _SPLIT_FALLBACK


def window_for(day: str) -> str:
    """"current" if the day is inside what the system learns from, else "reference"."""
    return "current" if str(day)[:10] >= split_date() else "reference"


# ── data ────────────────────────────────────────────────────────────────────

def _page(q):
    out, off = [], 0
    while True:
        rows = q().range(off, off + 999).execute().data or []
        out += rows
        if len(rows) < 1000:
            return out
        off += 1000


def load_trades(sb, since: str) -> list[dict]:
    cols = ("symbol,strategy,sector,entry_date,entry_price,actual_qty,planned_stop_at_entry,"
            "planned_target_at_entry,exit_date,exit_price,realized_pnl,charges,r_multiple,"
            "exit_reason,mode")
    closed = _page(lambda: sb.table("closed_positions").select(cols)
                   .eq("framework", "SWING").gte("entry_date", since).order("entry_date"))
    out = []
    for r in closed:
        if r["exit_reason"] == "MULTI_LEG":
            continue
        out.append({**r, "open": False, "qty": int(r["actual_qty"] or 0),
                    "stop": float(r["planned_stop_at_entry"] or 0),
                    "target": float(r["planned_target_at_entry"] or 0)})
    opened = (sb.table("open_positions").select(
        "symbol,strategy,sector,entry_date,entry_price,current_qty,planned_stop,planned_target,mode")
        .eq("framework", "SWING").eq("status", "ACTIVE").gte("entry_date", since)
        .execute().data or [])
    for r in opened:
        out.append({**r, "open": True, "qty": int(r["current_qty"] or 0),
                    "stop": float(r["planned_stop"] or 0),
                    "target": float(r["planned_target"] or 0),
                    "realized_pnl": None, "exit_reason": "OPEN"})
    out = [t for t in out if t["qty"] > 0 and t["stop"] and float(t["entry_price"]) > t["stop"]]
    out.sort(key=lambda t: (str(t["entry_date"])[:10], t["symbol"]))
    return out


def load_alert_times(sb, since: str) -> tuple[dict, dict]:
    """First ENTRY alert per (symbol, day) and every AI_TIGHTEN alert per symbol."""
    entry, tighten = {}, defaultdict(list)
    rows = _page(lambda: sb.table("intraday_alerts").select("ts,symbol,kind,meta")
                 .gte("ts", since).in_("kind", ["ENTRY", "TRAIL_SL"]).order("ts"))
    for r in rows:
        ts = datetime.fromisoformat(r["ts"]).astimezone(IST)
        if r["kind"] == "ENTRY":
            entry.setdefault((r["symbol"], ts.date().isoformat()), ts)
        elif "AI_TIGHTEN" in str(r.get("meta") or ""):
            tighten[r["symbol"]].append(ts)
    return entry, tighten


def load_plans(sb, since: str) -> dict:
    rows = _page(lambda: sb.table("signal_output_daily").select("*")
                 .gte("date", since).order("date"))
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    return dict(by_day)


def load_regime_rows(sb) -> list[dict]:
    return _page(lambda: sb.table("market_regime").select("*").order("date"))


def corrected_scores(rows: list[dict]) -> dict[str, float]:
    """
    date -> regime score with the frozen index inputs replaced by real history.

    Only the DIFFERENCE is applied (score with corrected inputs minus score with
    the stored inputs, both under today's pillar code), so pillar-code changes
    since April do not leak into the comparison.
    """
    import swing.compute.compute_regime as cr
    path = CACHE / "index_closes.json"
    if path.exists():
        idx = json.loads(path.read_text())
    else:
        idx = {t: cr.fetch_index_closes(t) for t in ("^NSEI", "^NSEBANK")}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(idx))
    nsei = [tuple(x) for x in idx["^NSEI"]]
    bank = [tuple(x) for x in idx["^NSEBANK"]]
    out = {}
    for r in rows:
        score = r.get("regime_score_computed")
        if score is None:
            continue
        d = r["date"]
        n = cr.index_indicators(nsei, float(r["nifty_price"]) if r.get("nifty_price") else None, d)
        b = cr.index_indicators([x for x in bank if x[0] <= d], None, d)
        fixed = dict(r, nifty_50dma=n.get("dma50"), nifty_200dma=n.get("dma200"),
                     nifty_weekly_rsi=n.get("weekly_rsi"), nifty_20d_chg_pct=n.get("ret_20d"),
                     nifty_5d_chg_pct=n.get("ret_5d"), banknifty_weekly_rsi=b.get("weekly_rsi"),
                     banknifty_price=b.get("price"))
        delta = ((cr.score_price_structure(fixed)[0] - cr.score_price_structure(r)[0])
                 + (cr.score_momentum(fixed)[0] - cr.score_momentum(r)[0]))
        out[d] = min(max(float(score) + delta, 0.0), 100.0)
    return out


@dataclass
class B:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


def _cached_cover(kind: str, symbol: str, start: str, end: str) -> Path | None:
    """
    The widest cached file for this symbol that OVERLAPS the asked-for window.

    The cache is keyed on symbol__start__end, so asking for a window that ends
    today never matches a file fetched yesterday — and with no broker session
    every study mode then silently reports "no data" while a perfectly good
    cache sits on disk. That is how --rank-study, --exposure-premise and
    --quality-study all returned zero rows the moment the Kite token expired:
    not one of them said the cache had been missed.

    Returns the covering file with the most days in it, or None. The caller
    says out loud when the coverage is short of what was asked.
    """
    d = CACHE / kind
    if not d.exists():
        return None
    pref = symbol.replace("&", "_") + "__"
    best, best_days = None, -1
    for f in d.glob(pref + "*.json.gz"):
        try:
            _sym, c_start, c_end = f.name[:-len(".json.gz")].split("__")
        except ValueError:
            continue
        if c_start > end or c_end < start:          # no overlap at all
            continue
        # Rank by how much of the ASKED-FOR window the file actually covers.
        # Ranking by end date alone picks the most recent file, which is
        # usually the narrowest one.
        try:
            lo = _date.fromisoformat(max(c_start, start))
            hi = _date.fromisoformat(min(c_end, end))
        except ValueError:
            continue
        days = (hi - lo).days
        if days > best_days:
            best, best_days = f, days
    return best


def bars_15m(kite, symbol: str, start: str, end: str) -> list[B]:
    path = CACHE / "15minute" / f"{symbol.replace('&', '_')}__{start}__{end}.json.gz"
    alt = None if path.exists() else _cached_cover("15minute", symbol, start, end)
    if path.exists() or alt:
        with gzip.open(alt or path, "rt", encoding="utf-8") as fh:
            raw = json.load(fh)
        if alt:
            raw = [r for r in raw if start <= r["ts"][:10] <= end]
    else:
        if kite is None:
            return []
        tok = _token(kite, symbol)
        if not tok:
            return []
        data = _fetch(kite, tok, start, end, "15minute")
        raw = [{"ts": d["date"].isoformat(), "o": d["open"], "h": d["high"],
                "l": d["low"], "c": d["close"]} for d in data]
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(raw, fh)
    out = []
    for r in raw:
        ts = datetime.fromisoformat(r["ts"])
        ts = IST.localize(ts) if ts.tzinfo is None else ts.astimezone(IST)
        out.append(B(ts, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])))
    return out


_TOKENS: dict = {}


def _fetch(kite, tok, start: str, end: str, interval: str) -> list:
    import time
    for attempt in range(5):
        time.sleep(0.4)
        try:
            return kite.historical_data(tok, _date.fromisoformat(start),
                                        _date.fromisoformat(end), interval) or []
        except Exception as e:
            if "Too many requests" not in str(e):
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"rate limited fetching {tok}")


def _token(kite, symbol: str):
    if not _TOKENS:
        for i in kite.instruments("NSE"):
            _TOKENS[i["tradingsymbol"]] = i["instrument_token"]
    return _TOKENS.get(symbol)


# ── regime, recomputed through production code ─────────────────────────────

def recompute_regimes(rows: list[dict], scores: dict | None = None) -> dict[str, str]:
    """date -> label, re-running compute_regime's own hysteresis over stored (or given) scores."""
    from swing.compute.compute_regime import apply_hysteresis, detect_recovering
    done: list[dict] = []
    labels = {}
    for r in rows:
        score = (scores or {}).get(r["date"], r.get("regime_score_computed"))
        r = dict(r, regime_score_computed=score)
        history = list(reversed(done[-10:]))
        if score is None:
            label = r.get("computed_regime") or r.get("regime") or "NEUTRAL"
        else:
            rec = detect_recovering(r, history, float(score))
            label = apply_hysteresis(float(score), r, history, rec)
        labels[r["date"]] = label
        done.append({**r, "computed_regime": label})
    return labels


def regime_before(labels: dict, day: str) -> str:
    prior = [d for d in labels if d < day]
    return labels[max(prior)] if prior else "NEUTRAL"


# ── exit replay ─────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    symbol: str
    entry_date: str
    qty: int
    entry: float
    stop: float
    exit_price: float
    exit_ts: str
    reason: str
    r: float
    gross: float
    charges: float
    net: float
    actual_reason: str
    actual_net: float | None
    removed_by: str = ""
    legs: list = field(default_factory=list)


def _charges(entry: float, exit_px: float, qty: int) -> float:
    from intraday.cost_model import round_trip
    return round_trip(entry, qty, exit_px, product="CNC").total


def replay_exit(t: dict, bars: list[B], entry_ts: datetime | None, policy: dict,
                labels: dict, tighten_ts: list[datetime], trend_by_day: dict,
                qty: int, calendar: list[str]) -> Outcome:
    from control.position_lifecycle import evaluate_exit
    entry = float(t["entry_price"])
    stop0 = t["stop"]
    day0 = str(t["entry_date"])[:10]
    pos = {"symbol": t["symbol"], "strategy": t.get("strategy"), "sector": t.get("sector"),
           "entry_price": entry, "planned_stop": stop0, "active_sl": stop0,
           "planned_target": t["target"], "high_water_mark": entry,
           "current_qty": qty, "actual_qty": qty, "direction": "LONG"}
    after = [b for b in bars if b.ts > entry_ts]
    legs: list[tuple[int, float]] = []
    remaining = qty
    reason, exit_px, exit_ts = "OPEN", None, None
    tstarts = sorted(tighten_ts)

    for b in after:
        d = b.ts.date().isoformat()
        # the daemon's own count: market_regime rows strictly after entry and
        # before today (today's row is written that evening)
        sessions = sum(1 for x in calendar if day0 < x < d)
        if any(entry_ts < ts <= b.ts for ts in tstarts):
            pos["ai_recommended_action"] = "TIGHTEN_SL"
            pos["ai_action_reason"] = "recorded AI_TIGHTEN_SL alert"
        pol = dict(policy)
        pol["_current_regime"] = regime_before(labels, d)
        ctx = trend_by_day.get(d)
        pol["_trend_ctx"] = {t["symbol"]: ctx} if ctx else {}

        sl = float(pos["active_sl"])
        if b.low <= sl:
            exit_px = min(b.open, sl)
            reason = "TRAIL_SL_HIT" if pos.get("trail_activated") else "STOP_LOSS_HIT"
            exit_ts = b.ts
            break
        pos["high_water_mark"] = max(float(pos["high_water_mark"]), b.high)

        terminal = False
        for _ in range(CYCLES_PER_BAR):
            dec = evaluate_exit(pos, b.close, sessions, pol)
            act = dec.get("action", "HOLD")
            if act.startswith("EXIT"):
                exit_px, reason, exit_ts, terminal = b.close, dec.get("reason", act), b.ts, True
                break
            if act == "BOOK_PARTIAL" and dec.get("book_qty"):
                n = min(int(dec["book_qty"]), remaining - 1)
                legs.append((n, b.close))
                remaining -= n
                pos["current_qty"] = remaining
                pos["partial_booked_qty"] = n
                if dec.get("new_sl"):
                    pos["active_sl"] = max(float(pos["active_sl"]), float(dec["new_sl"]))
                continue
            if act in ("TRAIL_SL", "RUN") and dec.get("new_sl"):
                new = float(dec["new_sl"])
                if new <= float(pos["active_sl"]) + 1e-9:
                    break
                pos["active_sl"] = new
                if dec.get("reason") == "BREAKEVEN":
                    pos["breakeven_moved"] = True
                else:
                    pos["trail_activated"] = True
                if new >= b.close:
                    break
                continue
            break
        if terminal:
            break

    if exit_px is None:
        exit_px = after[-1].close if after else entry
        exit_ts = after[-1].ts if after else None
    legs.append((remaining, exit_px))
    gross = sum(n * (px - entry) for n, px in legs)
    charges = sum(_charges(entry, px, n) for n, px in legs if n > 0)
    risk = entry - stop0
    avg_exit = sum(n * px for n, px in legs) / qty
    actual_net = (float(t["realized_pnl"]) - float(t.get("charges") or 0)
                  if t.get("realized_pnl") is not None else None)
    return Outcome(t["symbol"], day0, qty, entry, stop0, round(avg_exit, 2),
                   exit_ts.isoformat() if exit_ts else "", reason,
                   round((avg_exit - entry) / risk, 3), round(gross, 2), round(charges, 2),
                   round(gross - charges, 2), t["exit_reason"], actual_net,
                   legs=[(n, round(px, 2)) for n, px in legs])


# ── entry-side rules, imported when the working tree has them ──────────────

def entry_filter(t: dict, entry_ts, plans_by_day: dict, labels: dict, regime_rows: list[dict],
                 book: list[dict], taken_today: int) -> tuple[str, float]:
    """Returns (refusal reason or '', size multiplier) for one actual entry."""
    day = str(t["entry_date"])[:10]
    plan_days = [d for d in plans_by_day if d < day]
    plan = None
    if plan_days:
        plan = next((p for p in plans_by_day[max(plan_days)] if p["symbol"] == t["symbol"]), None)
    regime = regime_before(labels, day)
    size_mult = 1.0

    try:
        from analysis import market_exposure as mx
        from config import cfg_int
        exp = mx.for_entries(mx.exposure_for_day(regime, [r for r in regime_rows if r["date"] < day]))
        if exp.block_new:
            return f"exposure {exp.state}", 0.0
        cap = mx.daily_cap(cfg_int("swing_max_new_per_day", 2), exp)
        if taken_today >= cap:
            return (f"exposure {exp.state} cap {cap}" if exp.state != "NORMAL"
                    else f"daily cap {cap}"), 0.0
        size_mult = exp.size_mult
        if size_mult < 1.0:
            # decide()'s own floor: a scaled-down clip under the minimum is refused, not shrunk
            from config import capital_for, cfg_float
            floor = capital_for("SWING") * cfg_float("portfolio_min_position_pct", 3.0) / 100
            if int(t["qty"] * size_mult) * float(t["entry_price"]) < floor:
                return f"exposure {exp.state}: half size under the Rs{floor:,.0f} minimum", 0.0
        if plan is not None:
            field_rows = plans_by_day[max(plan_days)]
            why = mx.selection_refusal(exp, plan, field_rows, float(t["entry_price"]))
            if why:
                return why, 0.0
    except ImportError:
        pass

    if any(p["symbol"] == t["symbol"] for p in book):
        # the daemon skips a held name; replayed exits can run later than live ones
        return "already held in the replayed book", 0.0

    if plan is not None:
        from analysis.trade_decision import decide, regime_min_rr
        from config import capital_for

        def ok(reg: str):
            d = decide(plan, float(t["entry_price"]), total_capital=capital_for("SWING"),
                       open_positions=book, regime=reg, min_rr=regime_min_rr(reg),
                       max_chase_pct=plan.get("ai_max_chase_pct") or None)
            return d.action in ("BUY_NOW", "CHASE_LIMIT"), d

        # only a refusal the regime label causes counts; a refusal that also
        # happens under NEUTRAL is replay noise (stale plan, different capital)
        if regime != "NEUTRAL":
            passed, d = ok(regime)
            if not passed and ok("NEUTRAL")[0]:
                return f"regime {regime}: decide {d.action} {d.reason[:50]}", 0.0
    return "", size_mult


# ── run ─────────────────────────────────────────────────────────────────────

def _summ(rows: list[Outcome]) -> dict:
    kept = [o for o in rows if not o.removed_by]
    wins = [o for o in kept if o.net > 0]
    rs = [o.r for o in kept]
    eq, peak, dd = 0.0, 0.0, 0.0
    for o in sorted(kept, key=lambda o: o.exit_ts):
        eq += o.net
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": len(kept), "removed": len(rows) - len(kept),
            "win_pct": round(100 * len(wins) / len(kept), 1) if kept else 0.0,
            "sum_r": round(sum(rs), 2), "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
            "gross": round(sum(o.gross for o in kept)), "charges": round(sum(o.charges for o in kept)),
            "net": round(sum(o.net for o in kept)), "max_dd": round(dd)}


def run(label: str, since: str, compare: str | None, regime_mode: str = "stored") -> dict:
    from control.position_lifecycle import load_exit_policy
    from swing.signals.pace_calibration import build_family_stall_days
    sb = get_supabase()
    trades = load_trades(sb, since)
    entry_alerts, tighten = load_alert_times(sb, since)
    plans = load_plans(sb, "2026-06-01")
    regime_rows = load_regime_rows(sb)
    # "stored" is what the live book saw; "recomputed" runs today's
    # compute_regime hysteresis over the stored scores
    if regime_mode == "corrected":
        labels = recompute_regimes(regime_rows, corrected_scores(regime_rows))
    elif regime_mode == "recomputed":
        labels = recompute_regimes(regime_rows)
    else:
        labels = {r["date"]: (r.get("computed_regime") or r.get("regime") or "NEUTRAL")
                  for r in regime_rows}
    policy = load_exit_policy()
    try:
        policy["stall_days_by_family"] = build_family_stall_days(sb, global_default=policy["stall_days"])
    except Exception as e:
        logger.warning(f"stall calibration unavailable: {e}")
        policy["stall_days_by_family"] = {}

    kite = None
    try:
        from kite.kite_client import get_kite
        kite = get_kite()
    except Exception as e:
        logger.warning(f"no broker session — cache only ({e})")
    end = (datetime.now(IST).date() - timedelta(days=0)).isoformat()

    calendar = [r["date"] for r in regime_rows]
    prepared = []
    for t in trades:
        sym, day = t["symbol"], str(t["entry_date"])[:10]
        bars = bars_15m(kite, sym, since, end)
        if not bars:
            logger.warning(f"{sym}: no bars — skipped")
            continue
        entry = float(t["entry_price"])
        ets = entry_alerts.get((sym, day))
        b0 = None
        if ets:
            b0 = next((b for b in bars if b.ts <= ets < b.ts + timedelta(minutes=15)), None)
        if b0 is None:
            day_bars = [b for b in bars if b.ts.date().isoformat() == day]
            b0 = next((b for b in day_bars if b.low <= entry <= b.high),
                      day_bars[0] if day_bars else None)
        if b0 is None:
            logger.warning(f"{sym} {day}: no bar on entry day — skipped")
            continue
        prepared.append((b0.ts, t, bars))
    prepared.sort(key=lambda x: x[0])

    outcomes: list[Outcome] = []
    book: list[dict] = []
    by_day_taken: dict[str, int] = defaultdict(int)
    for ets, t, bars in prepared:
        sym, day = t["symbol"], str(t["entry_date"])[:10]
        book = [p for p in book if p["_exit"] > ets.isoformat()]
        why, mult = entry_filter(t, ets, plans, labels, regime_rows, book,
                                 by_day_taken[day])
        trend = {}
        for d, rows in plans.items():
            row = next((p for p in rows if p["symbol"] == sym), None)
            if row:
                trend[d] = row
        trend_by_day = {}
        for b in bars:
            d = b.ts.date().isoformat()
            if d not in trend_by_day:
                prior = [x for x in trend if x < d]
                trend_by_day[d] = trend[max(prior)] if prior else None
        qty = max(1, int(t["qty"] * mult)) if mult else t["qty"]
        o = replay_exit(t, bars, ets, policy, labels, tighten.get(sym, []), trend_by_day,
                        qty, calendar)
        if why:
            o.removed_by = why
        else:
            by_day_taken[day] += 1
            book.append({"symbol": sym, "sector": t.get("sector"), "entry_price": o.entry,
                         "current_qty": qty, "planned_stop": o.stop, "active_sl": o.stop,
                         "framework": "SWING", "_exit": o.exit_ts or "9999"})
        outcomes.append(o)

    recent = [o for o in outcomes if window_for(o.entry_date) == "current"]
    earlier = [o for o in outcomes if window_for(o.entry_date) == "reference"]
    res = {"label": label, "at": datetime.now(IST).isoformat(), "regime_mode": regime_mode,
           "split": split_date(),
           "recent": _summ(recent), "earlier": _summ(earlier), "all": _summ(outcomes),
           "trades": [asdict(o) for o in outcomes]}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{label}.json").write_text(json.dumps(res, indent=1, default=str))
    _print(res, compare)
    return res


def _print(res: dict, compare: str | None) -> None:
    prev = None
    if compare and (RESULTS / f"{compare}.json").exists():
        prev = json.loads((RESULTS / f"{compare}.json").read_text())
    print(f"\n=== {res['label']} ===  (learning from {res.get('split', _SPLIT_FALLBACK)} onward)")
    for w, shown in (("recent", "current"), ("earlier", "reference"), ("all", "both")):
        s = res[w]
        line = (f"{shown:9s} n={s['n']:3d} removed={s['removed']:2d} win%={s['win_pct']:5.1f} "
                f"sumR={s['sum_r']:6.2f} avgR={s['avg_r']:+.3f} gross={s['gross']:8,} "
                f"charges={s['charges']:6,} net={s['net']:8,} maxDD={s['max_dd']:8,}")
        if prev:
            p = prev[w]
            line += f"  | Δnet {s['net'] - p['net']:+,} ΔsumR {s['sum_r'] - p['sum_r']:+.2f} ΔmaxDD {s['max_dd'] - p['max_dd']:+,}"
        print(line)
    print(f"\n{'symbol':11s}{'entry':11s}{'actual':20s}{'act net':>8s} | {'replay':20s}{'R':>6s}{'net':>8s}  removed_by")
    for o in res["trades"]:
        an = f"{o['actual_net']:.0f}" if o["actual_net"] is not None else "open"
        print(f"{o['symbol']:11s}{o['entry_date']:11s}{o['actual_reason']:20s}{an:>8s} | "
              f"{o['reason']:20s}{o['r']:6.2f}{o['net']:8.0f}  {o['removed_by']}")


def regime_audit(corrected: bool = False) -> None:
    sb = get_supabase()
    rows = load_regime_rows(sb)
    scores = corrected_scores(rows) if corrected else None
    labels = recompute_regimes(rows, scores)
    agree = sum(1 for r in rows if r.get("regime_score_computed") is not None
                and (r.get("computed_regime") or r.get("regime")) == labels[r["date"]])
    scored = sum(1 for r in rows if r.get("regime_score_computed") is not None)
    print(f"recomputed vs stored label: {agree}/{scored} agree")
    for r in rows:
        if r.get("regime_score_computed") is None:
            continue
        stored = r.get("computed_regime") or r.get("regime")
        mark = "" if stored == labels[r["date"]] else "  <-- differs"
        sc = (scores or {}).get(r["date"], r["regime_score_computed"])
        print(f"{r['date']} score {r['regime_score_computed']:5.1f} -> {sc:5.1f} stored {stored:11s} "
              f"recomputed {labels[r['date']]:11s}{mark}")


def bars_daily(kite, symbol: str, start: str, end: str) -> dict[str, tuple]:
    path = CACHE / "day" / f"{symbol.replace('&', '_')}__{start}__{end}.json.gz"
    alt = None if path.exists() else _cached_cover("day", symbol, start, end)
    if path.exists() or alt:
        with gzip.open(alt or path, "rt", encoding="utf-8") as fh:
            rows = {k: tuple(v) for k, v in json.load(fh).items()}
        return {k: v for k, v in rows.items() if start <= k <= end} if alt else rows
    if kite is None:
        return {}
    tok = _token(kite, symbol)
    if not tok:
        return {}
    data = _fetch(kite, tok, start, end, "day")
    out = {str(d["date"])[:10]: (d["open"], d["high"], d["low"], d["close"]) for d in data}
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


def exposure_premise(since: str, until: str, regime_mode: str = "recomputed") -> None:
    """Plan outcomes by the exposure state the entry path would have seen."""
    import statistics as st
    import config
    from analysis import market_exposure as mx
    sb = get_supabase()
    plans = load_plans(sb, since)
    rows = load_regime_rows(sb)
    labels = recompute_regimes(rows, corrected_scores(rows) if regime_mode == "corrected" else None)
    try:
        from kite.kite_client import get_kite
        kite = get_kite()
    except Exception:
        kite = None
    end = datetime.now(IST).date().isoformat()
    live = dict(config.get_system_config())
    live["swing_exposure_enabled"] = "true"
    config._sys_config = live

    groups: dict = defaultdict(list)
    for day in sorted(d for d in plans if since <= d <= until):
        label = labels.get(day) or regime_before(labels, day)
        exp = mx.exposure_for_day(label, [r for r in rows if r["date"] <= day])
        window = window_for(day)
        for p in plans[day]:
            b = bars_daily(kite, p["symbol"], since, end)
            ds = sorted(x for x in b if x > day)
            if day not in b or len(ds) < 5:
                continue
            f5 = (b[ds[4]][3] / b[day][3] - 1) * 100
            pr = None
            try:
                stop, tgt, e = float(p["planned_stop"]), float(p["planned_target"]), b[ds[0]][0]
                if stop < e < tgt:
                    pr = (b[ds[min(9, len(ds) - 1)]][3] - e) / (e - stop)
                    for x in ds[:10]:
                        o, h, l, c = b[x]
                        if l <= stop:
                            pr = (min(o, stop) - e) / (e - stop)
                            break
                        if h >= tgt:
                            pr = (max(o, tgt) - e) / (e - stop)
                            break
            except (TypeError, ValueError):
                pass
            groups[(window, exp.state)].append((f5, pr))
            if exp.state == "CORRECTION":
                ok = not mx.selection_refusal(exp, p, plans[day], b[ds[0]][0])
                groups[(window, "CORRECTION pass" if ok else "CORRECTION refused")].append((f5, pr))

    for k in sorted(groups):
        v = groups[k]
        f5 = [a for a, _ in v]
        pr = [b for _, b in v if b is not None]
        days = ""
        print(f"{k[0]:8s} {k[1]:20s} n={len(v):4d} fwd5 med {st.median(f5):+5.2f}% "
              f"up {100 * sum(1 for x in f5 if x > 0) / len(f5):3.0f}% | planR n={len(pr):4d} "
              f"mean {st.fmean(pr) if pr else 0:+.2f} win {100 * sum(1 for x in pr if x > 0) / max(1, len(pr)):3.0f}%{days}")


def _plan_outcomes(since: str, until: str):
    """(window, day, plan, fwd5 %, planR) for every plan with enough bars after it."""
    sb = get_supabase()
    plans = load_plans(sb, since)
    end = datetime.now(IST).date().isoformat()
    out = []
    for day in sorted(d for d in plans if since <= d <= until):
        for p in plans[day]:
            b = bars_daily(None, p["symbol"], "2026-06-25", end)
            ds = sorted(x for x in b if x > day)
            if day not in b or len(ds) < 5:
                continue
            f5 = (b[ds[4]][3] / b[day][3] - 1) * 100
            pr = None
            try:
                stop, tgt, e = float(p["planned_stop"]), float(p["planned_target"]), b[ds[0]][0]
                if stop < e < tgt:
                    pr = (b[ds[min(9, len(ds) - 1)]][3] - e) / (e - stop)
                    for x in ds[:10]:
                        o, h, l, c = b[x]
                        if l <= stop:
                            pr = (min(o, stop) - e) / (e - stop)
                            break
                        if h >= tgt:
                            pr = (max(o, tgt) - e) / (e - stop)
                            break
            except (TypeError, ValueError):
                pass
            out.append((window_for(day), day, p, f5, pr))
    return out


def rank_study(since: str, until: str) -> None:
    """Do the entry-ranking tilts found on 14-Aug..08-Sep hold on the earlier window?"""
    import statistics as st
    recs = _plan_outcomes(since, until)

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    feats = {"vol_ratio": "vol_ratio", "rsi_daily": "rsi_daily", "rs_vs_nifty": "rs_vs_nifty",
             "sector_rank": "sector_rank_at_entry", "implied_rr": "implied_rr",
             "final_score": "final_score", "dist_entry_pct": "dist_entry_pct", "adx": "adx"}
    for w in ("reference", "current"):
        rows = [r for r in recs if r[0] == w]
        days = len({r[1] for r in rows})
        print(f"\n{w}: {len(rows)} plans over {days} days "
              f"(planR available on {sum(1 for r in rows if r[4] is not None)})")
        for name, col in feats.items():
            v = [(num(r[2].get(col)), r[3], r[4]) for r in rows if num(r[2].get(col)) is not None]
            if len(v) < 30:
                continue
            v.sort(key=lambda x: x[0])
            n = len(v)
            parts = [v[:n // 3], v[n // 3:2 * n // 3], v[2 * n // 3:]]
            line = f"  {name:15s}"
            for i, part in enumerate(parts):
                prs = [x[2] for x in part if x[2] is not None]
                line += (f" | T{i + 1} fwd5 {st.median([x[1] for x in part]):+5.2f}%"
                         f" R {st.fmean(prs) if prs else 0:+.2f}")
            print(line)


def edge_study(since: str, until: str, bar_mode: str = "day") -> None:
    """
    Where is the swing edge, if anywhere? Every plan the live entry gate
    (decide() at the next open, corrected regime labels) would buy, managed four
    ways on daily bars, net of CNC costs on a Rs 25,000 clip:

        ladder   production evaluate_exit()
        plan     planned stop and target only, 15-session time stop
        hold10   planned stop only, exit at the 10th session close
        hold5    planned stop only, exit at the 5th session close

    Split by window, engine family, signal type and planned risk tercile. A
    segment is only interesting if it is positive in BOTH windows.
    """
    import statistics as st
    import config
    from analysis.trade_decision import decide, regime_min_rr
    from control.position_lifecycle import load_exit_policy
    from intraday.cost_model import round_trip
    from allocation.scoring import swing_family

    sb = get_supabase()
    plans = load_plans(sb, since)
    rows = load_regime_rows(sb)
    labels = recompute_regimes(rows, corrected_scores(rows))
    calendar = [r["date"] for r in rows]
    policy = load_exit_policy()
    policy["stall_days_by_family"] = {}
    end = datetime.now(IST).date().isoformat()
    live = dict(config.get_system_config())
    config._sys_config = live
    kite = None
    if bar_mode == "15m":
        from kite.kite_client import get_kite
        kite = get_kite()

    def net_r(entry, exit_px, stop, qty):
        risk = entry - stop
        cost = round_trip(entry, qty, exit_px, product="CNC").total
        return ((exit_px - entry) * qty - cost) / (risk * qty)

    recs = []
    for day in sorted(d for d in plans if since <= d <= until):
        for pl in plans[day]:
            b = bars_daily(None, pl["symbol"], "2026-06-25", end)
            ds = sorted(x for x in b if x > day)
            if len(ds) < 5:
                continue
            try:
                stop, tgt = float(pl["planned_stop"]), float(pl["planned_target"])
            except (TypeError, ValueError):
                continue
            entry = float(b[ds[0]][0])
            if not stop < entry < tgt:
                continue
            reg = labels.get(day) or regime_before(labels, ds[0])
            d = decide(pl, entry, total_capital=300000, open_positions=[], regime=reg,
                       min_rr=regime_min_rr(reg), max_chase_pct=pl.get("ai_max_chase_pct") or None)
            if d.action not in ("BUY_NOW", "CHASE_LIMIT"):
                continue
            qty = max(1, int(25000 / entry))
            out = {}
            # plan / hold variants
            for name, tstop, use_tgt in (("plan", 15, True), ("hold10", 10, False), ("hold5", 5, False)):
                px = None
                for i, x in enumerate(ds[:tstop]):
                    o, h, l, c = b[x]
                    if l <= stop:
                        px = min(o, stop) if i else stop
                        break
                    if use_tgt and h >= tgt:
                        px = max(o, tgt) if i else tgt
                        break
                    px = c
                out[name] = net_r(entry, px, stop, qty)
            # production ladder, one evaluation per daily close
            if bar_mode == "15m":
                bars = [x for x in bars_15m(kite, pl["symbol"], "2026-06-25", end)
                        if x.ts.date().isoformat() >= ds[0]]
            else:
                bars = [B(IST.localize(datetime.strptime(x + " 15:30", "%Y-%m-%d %H:%M")),
                          *map(float, b[x])) for x in ds[:40]]
            t = {"symbol": pl["symbol"], "strategy": pl.get("strategy"), "sector": pl.get("sector"),
                 "entry_price": entry, "stop": stop, "target": tgt, "entry_date": ds[0],
                 "realized_pnl": None, "exit_reason": "", "qty": qty}
            ets = IST.localize(datetime.strptime(ds[0] + " 09:00", "%Y-%m-%d %H:%M"))
            if not bars:
                continue
            o = replay_exit(t, bars, ets, policy, labels, [], {}, qty, calendar)
            out["ladder"] = net_r(entry, o.exit_price, stop, qty) if o.reason != "OPEN" or len(ds) >= 15 else None
            recs.append({"window": window_for(day), "day": day,
                         "family": swing_family(pl.get("strategy")) or "?",
                         "strategy": (pl.get("strategy") or "?").split("+")[0],
                         "signal_type": pl.get("signal_type"),
                         "risk_pct": (entry - stop) / entry * 100, "ladder_reason": o.reason, **out})

    def show(label, rs):
        if len(rs) < 15:
            return
        cells = []
        for k in ("ladder", "plan", "hold10", "hold5"):
            v = [r[k] for r in rs if r.get(k) is not None]
            cells.append(f"{k} {st.fmean(v):+.3f} ({100 * sum(1 for x in v if x > 0) / len(v):.0f}%)")
        print(f"  {label:34s} n={len(rs):4d}  " + "  ".join(cells))

    for w in ("reference", "current"):
        rs = [r for r in recs if r["window"] == w]
        print(f"\n{w}: {len(rs)} buyable plans over {len({r['day'] for r in rs})} days  "
              f"[mean net R per trade (win%)]")
        show("ALL", rs)
        for key in ("family", "strategy", "signal_type"):
            for val in sorted({r[key] for r in rs}, key=str):
                show(f"{key}={val}", [r for r in rs if r[key] == val])
        rk = sorted(rs, key=lambda r: r["risk_pct"])
        n = len(rk)
        for i, part in enumerate((rk[:n // 3], rk[n // 3:2 * n // 3], rk[2 * n // 3:])):
            if part:
                show(f"risk T{i + 1} {part[0]['risk_pct']:.1f}-{part[-1]['risk_pct']:.1f}%", part)
        from collections import Counter
        print("  ladder exits:", dict(Counter(r["ladder_reason"] for r in rs).most_common(8)))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"edge_study_{bar_mode}.json").write_text(json.dumps(recs, default=str))


def quality_study(since: str, until: str) -> None:
    """
    setup_quality against forward outcomes, on whatever window is asked for.

    This is the permanent home of the comparison that decided setup_quality
    stays instrumentation (see analysis/setup_quality.py). It exists as a tool
    rather than as a scratch script because the decision has to be re-taken
    later on data the score has never seen, and a study that dies with the
    session gets rewritten from memory — badly.

    Scores are computed ON THE FLY from sector_strength/industry_strength as of
    each plan's own date, so the window can predate migration 148's stored
    column.
    """
    import statistics as st
    from analysis.setup_quality import FACTORS, features_from, merge_plan_rows, score
    sb = get_supabase()
    # signal_output_daily carries neither dist_vwap_20d_pct, ret_12m nor
    # base_score — see merge_plan_rows(). Without these two tables the study
    # scores too few factors and reports nothing.
    slog = {(r["date"], r["symbol"]): r for r in
            _page(lambda: sb.table("signal_log").select("*").gte("date", since))}
    msl = {(r["date"], r["symbol"]): r for r in
           _page(lambda: sb.table("master_shortlist").select("*").gte("date", since))}
    sec = {(r["date"], r.get("sector")): r
           for r in _page(lambda: sb.table("sector_strength").select("*").gte("date", since))}
    ind = {(r["date"], r.get("industry")): r
           for r in _page(lambda: sb.table("industry_strength").select("*").gte("date", since))}
    recs = _plan_outcomes(since, until)
    rows = []
    for w, day, plan, f5, pr in recs:
        key = (day, plan.get("symbol"))
        merged = merge_plan_rows(slog.get(key) or plan, msl.get(key))
        q = score(features_from(merged,
                                sec.get((day, merged.get("sector"))),
                                ind.get((day, merged.get("industry")))))
        if q.total is not None:
            rows.append((w, q.total, f5, pr, plan.get("symbol"), day, q.n_used))
    print(f"\n=== setup_quality vs forward outcomes, {since}..{until} "
          f"({len(FACTORS)} factors) ===")
    if not rows:
        print("  no plans could be scored — sector_strength/industry_strength missing "
              "for this window")
        return
    used = st.median([r[6] for r in rows])
    print(f"  {len(rows)} plans scored (median {used:.0f} of {len(FACTORS)} factors present)")
    for w in ("reference", "current"):
        g = sorted([r for r in rows if r[0] == w], key=lambda r: r[1])
        if len(g) < 50:
            print(f"  {w:10s} n={len(g)} — too few to split into quintiles")
            continue
        n = len(g)
        lo, hi = g[:n // 5], g[4 * n // 5:]
        prs = [r[3] for r in g if r[3] is not None]
        hi_pr = [r[3] for r in hi if r[3] is not None]
        lo_pr = [r[3] for r in lo if r[3] is not None]
        print(f"  {w:10s} n={n:4d}  fwd5 bottom {st.fmean([r[2] for r in lo]):+6.2f}%  "
              f"top {st.fmean([r[2] for r in hi]):+6.2f}%  "
              f"spread {st.fmean([r[2] for r in hi]) - st.fmean([r[2] for r in lo]):+6.2f}%")
        if len(hi_pr) >= 10 and len(lo_pr) >= 10:
            print(f"  {'':10s}       planR bottom {st.fmean(lo_pr):+6.2f}R  "
                  f"top {st.fmean(hi_pr):+6.2f}R  (all plans {st.fmean(prs):+.2f}R)")
    print("  REMINDER: separation here is necessary, not sufficient. The score already "
          "separated signals and still LOST to entry_ranking at the live caps, because "
          "the book takes five plans a day, not a quintile.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--since", default=None,
                    help="default: swing_data_since, else 2026-07-13")
    ap.add_argument("--compare")
    ap.add_argument("--regime", choices=["stored", "recomputed", "corrected"], default="stored")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a system_config key for this run only (never written)")
    ap.add_argument("--regime-audit", action="store_true")
    ap.add_argument("--exposure-premise", action="store_true")
    ap.add_argument("--rank-study", action="store_true")
    ap.add_argument("--quality-study", action="store_true")
    ap.add_argument("--edge-study", action="store_true")
    ap.add_argument("--bars", choices=["day", "15m"], default="day")
    ap.add_argument("--until", default="2026-09-08")
    a = ap.parse_args()
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    from config import swing_data_since
    floor = swing_data_since()
    plan_since = "2026-06-25"          # studies report BOTH windows; the cutoff labels them
    if a.since is None:
        a.since = "2026-07-13"
    if a.set:
        import config
        live = dict(config.get_system_config())
        live.update(dict(kv.split("=", 1) for kv in a.set))
        config._sys_config = live
    if a.edge_study:
        edge_study(plan_since, a.until, a.bars)
        return 0
    if a.rank_study:
        rank_study(plan_since, a.until)
        return 0
    if a.quality_study:
        quality_study(plan_since, a.until)
        return 0
    if a.exposure_premise:
        exposure_premise(plan_since, a.until,
                         "corrected" if a.regime == "corrected" else "recomputed")
        return 0
    if a.regime_audit:
        regime_audit(corrected=a.regime == "corrected")
        return 0
    run(a.label, a.since, a.compare, a.regime)
    return 0


if __name__ == "__main__":
    sys.exit(main())
