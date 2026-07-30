"""
Find edges nobody has coded yet.

    python -m tools.discover_engines            both passes
    python -m tools.discover_engines --days 10  wider window

WHY THE WEEKLY REVIEW CANNOT DO THIS
------------------------------------
weekly_review.py scores the seven engines that exist. It can retire one and it
can loosen a gate, but it cannot propose a strategy nobody wrote — it only ever
looks where the engines already look. That makes it structurally blind to the
thing worth the most: a move the system did not see at all.

TWO PASSES, TWO DIFFERENT BLIND SPOTS

  A. REFUSED BUT RIGHT.  Every setup a gate declined is recorded with its
     outcome. If refusals in some slice reach target MORE often than the setups
     actually taken, that slice is not a mis-tuned gate — it is an unnamed edge
     living in the rejected population. Cheap: the data and the comparison
     already exist, only the segmentation was missing.

  B. MOVED BUT UNSEEN.  Sweep the universe for stocks that made a real intraday
     move and ask which produced NO detection from ANY engine. Those are the
     misses that cost nothing visible and never appear in any P&L — there is no
     losing trade to notice, only a winner that never happened. Characterise
     what preceded them and look for a repeat.

WHAT A FINDING IS AND IS NOT
----------------------------
A pattern here is a HYPOTHESIS, never a strategy. It is proposed at SHADOW —
detected and scored, never traded — and must clear the same 20-outcome bar every
existing engine clears before it can be promoted. A system that invents engines
and trades them is one nobody can audit, which is the opposite of what this
project has spent its time building.

The bar for reporting at all is deliberately high. Given enough slices, noise
produces a "pattern" every time; MIN_OCCURRENCES and the lift threshold exist to
make sure a finding has to be surprising, not merely present.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from config import IST, cfg_float, get_supabase, today_ist

# A cluster must recur before it is a pattern. Below this it is an anecdote, and
# anecdotes are what backtests are made of.
MIN_OCCURRENCES = 6

# How much better than the taken-setup baseline a refused slice must do before
# it is worth naming. 1.5x, not 1.05x — with enough slices something always
# looks slightly better.
MIN_LIFT = 1.5

# What counts as a move worth having caught, net of a 0.21% round trip.
MOVE_PCT = 1.5


def _hdr(t: str) -> None:
    logger.info("")
    logger.info("─" * 72)
    logger.info(t)
    logger.info("─" * 72)


def _propose(sb, subject: str, evidence: str, confidence: float) -> None:
    """
    Raise a SHADOW candidate. Same table as every other proposal, so a discovery
    is reviewed exactly like a retirement rather than through a side channel.
    """
    from tools.weekly_review import _RUN_ID
    try:
        existing = (sb.table("brain_proposals").select("id")
                      .eq("proposal_type", "ENGINE_CANDIDATE")
                      .eq("target_key", subject).eq("status", "PENDING")
                      .execute().data or [])
        row = {"analysis_run_id": _RUN_ID, "proposal_type": "ENGINE_CANDIDATE",
               "target_key": subject, "current_value": "does not exist",
               "proposed_value": "build as SHADOW", "evidence": evidence,
               "rationale": evidence, "confidence": confidence,
               "status": "PENDING", "source": "discover_engines", "priority": 3}
        if existing:
            sb.table("brain_proposals").update(row).eq("id", existing[0]["id"]).execute()
        else:
            sb.table("brain_proposals").insert(row).execute()
    except Exception as e:
        logger.warning(f"  could not record candidate ({subject}): {e}")


# ── Pass A ──────────────────────────────────────────────────────────────────
def refused_but_right(sb, days: int) -> int:
    """
    Slices of the REFUSED population that outperformed the taken one.

    Segmented by (gate, strategy) and by (gate, session phase), because a gate
    is rarely wrong everywhere — it is wrong in a corner. "Structure blocks PDL
    during PRIME" is a sentence you can build an engine from; "structure is too
    strict" is not.
    """
    _hdr(f"A · REFUSED BUT RIGHT — gates that decline winners ({days}d)")
    since = (today_ist() - timedelta(days=days)).isoformat()
    rows = (sb.table("intraday_setups")
              .select("strategy,cost_verdict,outcome,outcome_pct,phase,confidence")
              .gte("trade_date", since).execute().data or [])
    done = [r for r in rows if r.get("outcome")]
    if not done:
        logger.info("  nothing resolved in the window")
        return 0

    taken = [r for r in done if r.get("cost_verdict") == "TAKEN"]
    if not taken:
        logger.info("  no taken setups to form a baseline against")
        return 0
    base = sum(1 for r in taken if r["outcome"] == "TARGET") / len(taken)
    logger.info(f"  baseline: taken setups reach target {base:.0%} "
                f"(n={len(taken)}) — a refused slice must beat {base * MIN_LIFT:.0%}")

    slices = defaultdict(list)
    for r in done:
        v = r.get("cost_verdict")
        if not v or v == "TAKEN":
            continue
        slices[(v, r.get("strategy") or "?")].append(r)
        slices[(v, f"phase:{r.get('phase') or '?'}")].append(r)

    found = 0
    for (gate, seg), rs in sorted(slices.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        if n < MIN_OCCURRENCES:
            continue
        hit = sum(1 for r in rs if r["outcome"] == "TARGET") / n
        if hit <= base * MIN_LIFT:
            continue
        found += 1
        subject = f"{gate}/{seg}"
        why = (f"{gate} refused {n} setups in {seg} and {hit:.0%} reached target, "
               f"against {base:.0%} for setups actually taken — the refused "
               f"population in this slice is BETTER than the traded one")
        logger.warning(f"  ! {subject:<34} {hit:.0%} of {n}  (baseline {base:.0%})")
        logger.info(f"      {why}")
        _propose(sb, subject, why, 0.6 if n >= MIN_OCCURRENCES * 2 else 0.4)

    if not found:
        logger.success("  no refused slice beats the taken baseline — the gates are "
                       "declining worse setups than they allow, which is their job")
    return found


# ── Pass B ──────────────────────────────────────────────────────────────────
def moved_but_unseen(sb, days: int) -> int:
    """
    Real intraday moves that produced no detection from any engine.

    These are the expensive misses precisely because they are invisible: no
    losing trade, no bad exit, nothing in the P&L — only a winner that never
    happened. Nothing else in this system looks for them.

    Characterised on features available BEFORE the move, because a pattern
    described by what happened afterwards is not a signal, it is a description.
    """
    _hdr(f"B · MOVED BUT UNSEEN — real moves no engine detected ({days}d)")

    # stock_data_daily, not the broker.
    #
    # The swing pipeline already ingests OHLC for the whole universe every
    # evening. Re-fetching it from kite.historical_data would mean one
    # rate-limited call per symbol per day for data this database already holds,
    # and would make discovery depend on a live broker session for a question
    # about the past. A second source of truth for the same bars is also how
    # two parts of this project came to disagree about the same number.
    since = (today_ist() - timedelta(days=days)).isoformat()

    from intraday import scanner
    universe = set(scanner.symbols(sb))

    # FILTERED AND PAGED, both deliberately.
    #
    # PostgREST returns at most 1000 rows per request and says nothing when it
    # truncates. Asking for 499 symbols over 25 days is ~12,000 rows, so the
    # first version silently analysed the first 1000 — mostly names outside the
    # universe — and reported 40 symbol-days as if that were the whole window.
    # A discovery tool drawing conclusions from 8% of its data is worse than one
    # that does not run.
    bars, page = [], 0
    while True:
        chunk = (sb.table("stock_data_daily")
                   .select("symbol,date,open,high,low,close,volume,vol_ratio,adx,"
                           "atr_pct,delivery_pct,rs_vs_nifty,dist_sma50,sector")
                   .in_("symbol", sorted(universe))
                   .gte("date", since)
                   .order("date")
                   .range(page * 1000, page * 1000 + 999).execute().data or [])
        bars.extend(chunk)
        if len(chunk) < 1000:
            break
        page += 1
    if not bars:
        logger.warning("  no bars in stock_data_daily for the window")
        return 0
    logger.info(f"  {len(bars)} symbol-days loaded for {len(universe)} universe names")

    seen = defaultdict(set)      # trade_date -> symbols any engine detected
    for r in (sb.table("intraday_setups").select("symbol,trade_date")
                .gte("trade_date", since).execute().data or []):
        seen[str(r.get("trade_date"))].add(r.get("symbol"))

    # Previous close per symbol, so the gap is knowable at the open.
    by_sym = defaultdict(list)
    for b in bars:
        by_sym[b["symbol"]].append(b)
    for v in by_sym.values():
        v.sort(key=lambda x: str(x["date"]))

    # `seen` and `by_sym` are assembled above; the clustering pass below does
    # the counting, because a miss only means something next to a denominator.
    # ── Clustering, with a denominator ──────────────────────────────────────
    #
    # Counting misses per bucket is not evidence. "28 flat opens ran 3.5%" says
    # nothing until you know how many flat opens there were in total — a common
    # bucket produces the most misses simply by being common, and reporting that
    # as a pattern is how a discovery tool manufactures strategies out of base
    # rates.
    #
    # So every bucket is scored as LIFT: how often a big move followed this
    # setup, against how often one followed ANY setup. A bucket only earns a
    # candidate if the move rate is materially higher than the background AND
    # the engines missed it.
    #
    # Features are all from the PRIOR row — knowable at the open, before the
    # move. A feature read from the same day describes the outcome and would
    # make every bucket look predictive.
    def _f(v, d=0.0):
        try:
            return float(v) if v is not None else d
        except (TypeError, ValueError):
            return d

    feats = {
        "gap down > 1%":        lambda p, m: m["gap"] <= -1.0,
        "flat open +/-0.3%":    lambda p, m: abs(m["gap"]) <= 0.3,
        "gap up > 1%":          lambda p, m: m["gap"] >= 1.0,
        "prior volume > 1.5x":  lambda p, m: _f(p.get("vol_ratio")) > 1.5,
        "prior volume < 0.8x":  lambda p, m: 0 < _f(p.get("vol_ratio")) < 0.8,
        "ADX > 25 (trending)":  lambda p, m: _f(p.get("adx")) > 25,
        "ADX < 18 (choppy)":    lambda p, m: 0 < _f(p.get("adx")) < 18,
        "ATR > 3% (volatile)":  lambda p, m: _f(p.get("atr_pct")) > 3.0,
        "delivery > 60%":       lambda p, m: _f(p.get("delivery_pct")) > 60,
        "RS vs NIFTY > 5":      lambda p, m: _f(p.get("rs_vs_nifty")) > 5,
        "extended > 8% o/50MA": lambda p, m: _f(p.get("dist_sma50")) > 8,
    }

    # Denominators: every symbol-day, whether or not it moved or was detected.
    tot = defaultdict(int)          # bucket -> all symbol-days
    moved = defaultdict(int)        # bucket -> days a big move followed
    missed = defaultdict(list)      # bucket -> the ones no engine saw
    for sym, rows in by_sym.items():
        if sym not in universe:
            continue
        for i in range(1, len(rows)):
            prior, b = rows[i - 1], rows[i]
            try:
                o, h, c = _f(b["open"]), _f(b["high"]), _f(b["close"])
                pc = _f(prior["close"])
            except Exception:
                continue
            if not o or not h or not pc:
                continue
            m = {"symbol": sym, "date": str(b["date"])[:10],
                 "move": round((h - o) / o * 100.0, 2),
                 "gap": round((o - pc) / pc * 100.0, 2),
                 "closed_strong": c > o}
            big = m["move"] >= MOVE_PCT
            unseen = big and sym not in seen.get(m["date"], set())
            for name, fn in feats.items():
                try:
                    if not fn(prior, m):
                        continue
                except Exception:
                    continue
                tot[name] += 1
                if big:
                    moved[name] += 1
                if unseen:
                    missed[name].append(m)

    all_days = sum(1 for sym, rows in by_sym.items() if sym in universe
                   for _ in rows[1:])
    all_moves = sum(1 for sym, rows in by_sym.items() if sym in universe
                    for i in range(1, len(rows))
                    if _f(rows[i].get("open")) and
                    (_f(rows[i]["high"]) - _f(rows[i]["open"])) /
                    max(_f(rows[i]["open"]), 1e-9) * 100.0 >= MOVE_PCT)
    base_rate = all_moves / all_days if all_days else 0.0
    logger.info(f"  background: {all_moves} of {all_days} symbol-days produced a "
                f"{MOVE_PCT}%+ move ({base_rate:.0%})")

    found = 0
    for name in sorted(feats, key=lambda k: -len(missed.get(k, []))):
        n_tot, n_miss = tot.get(name, 0), len(missed.get(name, []))
        if n_tot < MIN_OCCURRENCES * 3 or n_miss < MIN_OCCURRENCES:
            continue
        rate = moved.get(name, 0) / n_tot
        lift = rate / base_rate if base_rate else 0.0
        if lift < MIN_LIFT:
            continue          # common, not predictive
        hits = missed[name]
        strong = sum(1 for m in hits if m["closed_strong"]) / len(hits)
        avg = sum(m["move"] for m in hits) / len(hits)
        found += 1
        why = (f"after '{name}', {rate:.0%} of {n_tot} symbol-days produced a "
               f"{MOVE_PCT}%+ move against a {base_rate:.0%} background "
               f"({lift:.1f}x lift). {n_miss} of those went undetected by every "
               f"engine, averaging {avg:.2f}% with {strong:.0%} closing strong")
        logger.warning(f"  ! {name:<24} lift {lift:.1f}x  "
                       f"({rate:.0%} of {n_tot})  {n_miss} missed, avg {avg:.2f}%")
        logger.info("      e.g. " + ", ".join(
            f"{m['symbol']} {m['date']} +{m['move']:.1f}%" for m in hits[:3]))
        _propose(sb, f"UNSEEN/{name}", why,
                 0.6 if n_miss >= MIN_OCCURRENCES * 2 else 0.4)

    if not found:
        logger.info("  no prior-day condition predicts these moves better than "
                    f"{MIN_LIFT}x background — the misses are not a pattern yet")
    return found


def main(days: int = 14, skip_bars: bool = False) -> int:
    sb = get_supabase()
    logger.info("═" * 72)
    logger.info("TradeOS — engine discovery (proposes SHADOW candidates only)")
    logger.info("═" * 72)
    n = refused_but_right(sb, days)
    if not skip_bars:
        n += moved_but_unseen(sb, days)
    logger.info("")
    if n:
        logger.warning(f"  {n} candidate(s) raised — read them with `tradeos learn show`")
        logger.info("  Each is a HYPOTHESIS. Build as SHADOW, score 20 outcomes, "
                    "then decide.")
    else:
        logger.success("  no candidates — nothing in the data is asking for a new engine")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discover engines that do not exist yet")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--skip-bars", action="store_true",
                    help="pass A only; skips the historical sweep")
    a = ap.parse_args()
    sys.exit(main(a.days, a.skip_bars))
