"""
Unit economics — what a trade is worth, per clip, per target, per capital.

READ-ONLY. Writes nothing, places nothing, changes no config.

WHY THIS EXISTS
---------------
Stage 2's C.4 answered "what does the swing book earn at its design target?"
with a single friction constant (+0.204R) and got -0.004R. That constant is a
mean of eight ratios, and friction is not a constant — it is a function of two
things at once. This tool makes the function visible.

THE IDENTITY EVERYTHING HERE TURNS ON
-------------------------------------
    friction_R = statutory_rupees(clip) / risk_rupees
               = cost_pct(clip) / stop_pct

and because CNC's flat Rs 15.04 DP fee makes

    cost_pct(clip) = k + DP/clip          (k = the proportional part)

friction in R depends on the clip AND the stop width. Which of the two you hold
fixed decides whether a bigger clip helps or hurts, and the two answers point in
OPPOSITE directions:

    stop % fixed, clip free   -> risk grows with clip, DP amortises, friction FALLS
    risk Rs fixed, clip free  -> stop narrows as 1/clip, friction RISES

The system holds risk fixed (`risk_pct_per_trade` = 1.0). Under that sizing rule
a clip-size floor makes friction WORSE, not better. Both framings are printed
because acting on one while sized by the other is how a "fix friction" change
ends up adding friction.

WHY FRICTION HERE IS STATUTORY, NOT `round_trip()`
--------------------------------------------------
`round_trip()` adds `cost_slippage_bps` (5) on both legs. That is right for a
PRE-TRADE gate, which decides against a quoted price it has not yet paid. It is
a DOUBLE COUNT against a REALISED outcome, because slippage is already inside
the fill price on both books:

  · PAPER — `execution/paper_broker.py:90` fills MARKET orders at
            `ltp * (1 +/- slip)`, so every intraday row's entry_price and
            exit_price already carry it.
  · LIVE  — real broker fills embed real slippage by construction.

`tools/expectancy_ledger.py` uses `entry_leg + exit_leg` for this reason, and
that is what Stage 2's +0.204R / +0.124R are. This tool matches it, so every
number below is directly comparable to the ledger. Section 1 proves the match
row by row rather than asserting it.

Note the consequence, which is a real asymmetry and not a defect in either:
the intraday cost GATE prices an MIS round trip at 0.206% of position and the
expectancy LEDGER prices the same trade at 0.106%. Same trade, two numbers,
because they are answering two different questions.

    cd backend && python -m tools.unit_economics
    cd backend && python -m tools.unit_economics --hit 0.35
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import cfg_float, get_supabase, logger
from intraday import direction as D
from intraday.cost_model import entry_leg, exit_leg

BAR = "═" * 92

# Clip ladder. Spans the whole reachable range: below the smallest position this
# account has taken, up past the point where MIS brokerage stops being a
# percentage and becomes the Rs 20 cap.
CLIPS = (2_000, 2_733, 3_000, 4_000, 5_000, 6_500, 8_000, 10_000,
         12_500, 15_000, 20_000, 25_000, 33_000, 50_000, 100_000)

CAPITALS = (20_000, 50_000, 100_000)
TARGETS = (2.0, 2.5, 3.0)

# The band of stop widths this book has ever actually produced, so a grid cell
# implying a stop outside it can be marked unreachable rather than quietly
# offered as an option. Both ends are measured — see `measured()`.
STOP_BAND = (1.53, 10.43)


# ══════════════════════════════════════════════════════════════════════════
# friction
# ══════════════════════════════════════════════════════════════════════════
def statutory_rs(clip: float, product: str, price: float = 500.0) -> float:
    """
    Charges on both legs for a position worth `clip`.

    Quantised on a nominal Rs 500 share so the qty rounding is identical across
    the ladder. Price barely matters — none of the charge lines is per-share —
    but holding it fixed keeps the ladder monotonic, which a varying rounding
    error would not.
    """
    qty = max(int(round(clip / price)), 1)
    return entry_leg(price, qty, product=product) + exit_leg(price, qty, product=product)


def statutory_pct(clip: float, product: str, price: float = 500.0) -> float:
    qty = max(int(round(clip / price)), 1)
    return statutory_rs(clip, product, price) / (price * qty) * 100.0


def friction_r(clip: float, stop_pct: float, product: str) -> float:
    return statutory_pct(clip, product) / stop_pct


def gross_r(hit: float, target: float) -> float:
    """Expectancy before costs: winners pay `target` R, losers pay exactly 1R."""
    return hit * target - (1.0 - hit)


# ══════════════════════════════════════════════════════════════════════════
# 1 — the model against the book it claims to describe
# ══════════════════════════════════════════════════════════════════════════
def measured() -> dict:
    """
    Every closed row that has both a stop and a fill, reduced to the three
    quantities this tool needs: clip, stop width, friction in R.

    Returns per-book medians, which is what the grids are anchored on. MEDIAN
    and not mean, deliberately: friction_R is a RATIO, the sample is eight rows
    wide on the swing side, and one Rs 802 clip carries a friction of 0.767R.
    A mean over eight heterogeneous ratios is a statement about that one row.
    """
    sb = get_supabase()
    rows = (sb.table("closed_positions")
              .select("symbol,framework,product,entry_price,exit_price,actual_qty,"
                      "direction,planned_stop_at_entry,planned_target_at_entry")
              .execute().data)

    books: dict[str, list[dict]] = {}
    for r in rows:
        e, q, s = r.get("entry_price"), r.get("actual_qty"), r.get("planned_stop_at_entry")
        if not (e and q and s):
            continue
        e, q = float(e), int(q)
        rps = D.risk_per_share(e, float(s), r.get("direction"))
        if rps <= 0 or q <= 0:
            continue
        prod = (r.get("product") or "CNC").upper()
        xp = float(r["exit_price"]) if r.get("exit_price") else e
        t = r.get("planned_target_at_entry")
        books.setdefault(f"{r['framework']}/{prod}", []).append({
            "sym": r["symbol"], "clip": e * q, "risk": rps * q,
            "stop_pct": rps / e * 100.0,
            "cost_rs": entry_leg(e, q, product=prod) + exit_leg(xp, q, product=prod),
            "fric_r": (entry_leg(e, q, product=prod)
                       + exit_leg(xp, q, product=prod)) / (rps * q),
            "tgt_r": (D.reward_per_share(e, float(t), r.get("direction")) / rps
                      if t else None),
        })

    logger.info(BAR)
    logger.info("1. THE MODEL AGAINST THE MEASURED BOOK")
    logger.info(BAR)
    logger.info("   friction_R recomputed per row from the statutory schedule and each")
    logger.info("   row's OWN risk. If these do not reproduce expectancy_ledger's cost_r,")
    logger.info("   nothing below is trustworthy.")

    out = {}
    for key in sorted(books):
        t = books[key]
        logger.info("")
        logger.info(f"── {key}   n={len(t)} ──")
        if key.startswith("SWING"):
            logger.info(f"   {'symbol':12s} {'clip':>8s} {'stop%':>7s} {'risk Rs':>8s} "
                        f"{'cost Rs':>8s} {'cost%':>7s} {'friction R':>11s}")
            for x in sorted(t, key=lambda z: z["clip"]):
                logger.info(f"   {x['sym'][:12]:12s} {x['clip']:8.0f} {x['stop_pct']:6.2f}% "
                            f"{x['risk']:8.0f} {x['cost_rs']:8.2f} "
                            f"{x['cost_rs']/x['clip']*100:6.3f}% {x['fric_r']:11.3f}")
        fr = [x["fric_r"] for x in t]
        cl = [x["clip"] for x in t]
        sp = [x["stop_pct"] for x in t]
        tg = [x["tgt_r"] for x in t if x["tgt_r"]]
        logger.info(f"   friction R   mean {statistics.fmean(fr):+.3f}   "
                    f"median {statistics.median(fr):+.3f}")
        logger.info(f"   clip         mean Rs{statistics.fmean(cl):,.0f}   "
                    f"median Rs{statistics.median(cl):,.0f}")
        logger.info(f"   stop         mean {statistics.fmean(sp):.2f}%   "
                    f"median {statistics.median(sp):.2f}%")
        if tg:
            logger.info(f"   PLANNED target  mean {statistics.fmean(tg):.2f}R   "
                        f"median {statistics.median(tg):.2f}R   "
                        f"(the design assumes 2.00R)")
        out[key] = {"clip": statistics.median(cl), "stop": statistics.median(sp),
                    "fric_mean": statistics.fmean(fr), "fric_med": statistics.median(fr)}
    return out


def plan_geometry() -> tuple[float, float, int]:
    """
    The stop width and target multiple the pipeline is producing RIGHT NOW.

    The closed book is eight rows of history; `signal_output_daily` is the
    current plan set and an order of magnitude larger. They disagree — 6.23%
    against 4.34% — and the difference decides the answer, so both are carried
    through every grid rather than one being chosen.
    """
    sb = get_supabase()
    d = (sb.table("signal_output_daily").select("date")
           .order("date", desc=True).limit(1).execute().data[0]["date"])
    q = (sb.table("signal_output_daily")
           .select("entry_zone_low,current_price,planned_stop,planned_target")
           .eq("date", d).execute().data)
    sp, tg = [], []
    for x in q:
        e = x.get("entry_zone_low") or x.get("current_price")
        s = x.get("planned_stop")
        if not (e and s):
            continue
        e, s = float(e), float(s)
        if e <= 0 or s <= 0 or e <= s:
            continue
        sp.append((e - s) / e * 100.0)
        if x.get("planned_target"):
            tg.append((float(x["planned_target"]) - e) / (e - s))
    logger.info("")
    logger.info(f"── current plan geometry, signal_output_daily {d}   n={len(sp)} ──")
    logger.info(f"   stop    mean {statistics.fmean(sp):.2f}%   "
                f"median {statistics.median(sp):.2f}%   "
                f"min {min(sp):.2f}%   max {max(sp):.2f}%")
    logger.info(f"   target  mean {statistics.fmean(tg):.2f}R   "
                f"median {statistics.median(tg):.2f}R")
    return statistics.median(sp), statistics.median(tg), len(sp)


# ══════════════════════════════════════════════════════════════════════════
# 2 — friction by clip
# ══════════════════════════════════════════════════════════════════════════
def friction_ladder() -> None:
    logger.info("")
    logger.info(BAR)
    logger.info("2. STATUTORY ROUND TRIP BY CLIP")
    logger.info(BAR)
    logger.info(f"  {'clip':>9} │ {'CNC Rs':>8} {'CNC %':>8} {'DP share':>9} │ "
                f"{'MIS Rs':>8} {'MIS %':>8} │ {'CNC/MIS':>8}")
    for c in CLIPS:
        sc, sm = statutory_rs(c, "CNC"), statutory_rs(c, "MIS")
        dp = cfg_float("cost_dp_per_sell", 15.04)
        logger.info(f"  {c:>9,} │ {sc:8.2f} {statutory_pct(c,'CNC'):7.3f}% "
                    f"{dp/sc*100:8.1f}% │ {sm:8.2f} {statutory_pct(c,'MIS'):7.3f}% │ "
                    f"{sc/sm:7.2f}x")
    logger.info("")
    logger.info(f"  asymptote, clip → ∞:  CNC {statutory_pct(10**8,'CNC'):.4f}%   "
                f"MIS {statutory_pct(10**8,'MIS'):.4f}%")
    logger.info("  MIS cost%% is FLAT across this whole ladder. Brokerage is")
    logger.info("  min(Rs 20, 0.03%%) per order and the percentage branch wins below")
    logger.info("  Rs 66,667 per leg, so nothing amortises. CLIP IS NOT AN INTRADAY LEVER.")
    logger.info("  CNC's slope is almost entirely the flat DP fee, which is why it IS")
    logger.info("  a swing lever — but only in the framing of section 4.")


# ══════════════════════════════════════════════════════════════════════════
# 3 — the grid as the system actually sizes: risk fixed in rupees
# ══════════════════════════════════════════════════════════════════════════
def grid_risk_fixed(hit: float) -> None:
    risk_pct = cfg_float("risk_pct_per_trade", 1.0) / 100.0
    max_pos = cfg_float("max_position_pct", 20.0) / 100.0
    logger.info("")
    logger.info(BAR)
    logger.info(f"3. SWING CNC — RISK FIXED AT {risk_pct:.1%} OF CAPITAL "
                f"(risk_pct_per_trade), hit={hit:.0%}")
    logger.info(BAR)
    logger.info("   This is how the system sizes today. Risk in rupees is FIXED, so the")
    logger.info("   clip SETS the stop: stop%% = risk_Rs / clip. Friction therefore RISES")
    logger.info("   with clip — the stop narrows as 1/clip, faster than DP amortises.")
    logger.info(f"   (cap)  above max_position_pct {max_pos:.0%}, enforced at "
                f"analysis/portfolio_constraints.py:223")
    logger.info(f"   (geom) implied stop outside the measured {STOP_BAND[0]:.2f}"
                f"–{STOP_BAND[1]:.2f}%% band")
    for cap in CAPITALS:
        risk_rs, ceiling = cap * risk_pct, cap * max_pos
        logger.info("")
        logger.info(f"── capital Rs {cap:,}   risk/trade Rs {risk_rs:.0f}   "
                    f"clip ceiling Rs {ceiling:,.0f} ──")
        logger.info(f"  {'clip':>8} {'stop%':>7} {'cost Rs':>8} {'fricR':>7} │ "
                    f"{'@2.0R':>8} {'@2.5R':>8} {'@3.0R':>8} │ {'Rs/tr@2R':>9} {'':>11}")
        for c in CLIPS:
            if c > cap * 1.3:
                continue
            sp = risk_rs / c * 100.0
            fr = statutory_rs(c, "CNC") / risk_rs
            nets = [gross_r(hit, t) - fr for t in TARGETS]
            flags = ((["cap"] if c > ceiling else [])
                     + (["geom"] if not (STOP_BAND[0] <= sp <= STOP_BAND[1]) else []))
            logger.info(f"  {c:>8,} {sp:6.2f}% {statutory_rs(c,'CNC'):8.2f} {fr:7.3f} │ "
                        f"{nets[0]:+8.3f} {nets[1]:+8.3f} {nets[2]:+8.3f} │ "
                        f"{nets[0]*risk_rs:+9.2f} {','.join(flags):>11}")


# ══════════════════════════════════════════════════════════════════════════
# 4 — the grid with stop width held at what the book produces
# ══════════════════════════════════════════════════════════════════════════
def grid_stop_fixed(hit: float, stops: list[tuple[float, str]],
                    product: str, title: str) -> None:
    logger.info("")
    logger.info(BAR)
    logger.info(f"4. {title} — STOP HELD AT MEASURED WIDTH, hit={hit:.0%}")
    logger.info(BAR)
    logger.info("   Risk in rupees now RISES with the clip. This is the only framing in")
    logger.info("   which 'a bigger clip beats the DP fee' is a true statement, and it")
    logger.info("   costs a bigger rupee risk to say it. friction_R here is independent")
    logger.info("   of capital — capital enters only through the clip it permits.")
    for sp, lbl in stops:
        logger.info("")
        logger.info(f"── stop {sp:.2f}%  ({lbl}) ──")
        logger.info(f"  {'clip':>8} {'risk Rs':>8} {'cost%':>7} {'fricR':>7} │ "
                    f"{'@2.0R':>8} {'@2.5R':>8} {'@3.0R':>8} │ {'Rs/tr@2R':>9}")
        for c in CLIPS:
            cp = statutory_pct(c, product)
            fr, rr = cp / sp, c * sp / 100.0
            nets = [gross_r(hit, t) - fr for t in TARGETS]
            logger.info(f"  {c:>8,} {rr:8.0f} {cp:6.3f}% {fr:7.3f} │ "
                        f"{nets[0]:+8.3f} {nets[1]:+8.3f} {nets[2]:+8.3f} │ "
                        f"{nets[0]*rr:+9.2f}")


# ══════════════════════════════════════════════════════════════════════════
# 5 — the two questions, solved
# ══════════════════════════════════════════════════════════════════════════
def min_clip_for(target: float, stop_pct: float, product: str, hit: float):
    """
    Smallest clip whose friction still leaves positive expectancy at `target`.

    Returns None when no clip ever clears it — which is a real answer and the
    one that matters, because it means the target itself is the problem and no
    amount of size will rescue it. Returns 0.0 when the SMALLEST clip on the
    ladder already clears, i.e. clip is not a binding constraint at all.
    """
    g = gross_r(hit, target)
    lo, hi = 200.0, 20_000_000.0
    if statutory_pct(hi, product) / stop_pct >= g:
        return None
    if statutory_pct(lo, product) / stop_pct < g:
        return 0.0
    for _ in range(120):
        mid = (lo + hi) / 2.0
        if statutory_pct(mid, product) / stop_pct < g:
            hi = mid
        else:
            lo = mid
    return hi


def min_target_for(clip: float, stop_pct: float, product: str, hit: float) -> float:
    """Smallest target multiple whose gross expectancy covers friction."""
    return (friction_r(clip, stop_pct, product) + (1.0 - hit)) / hit


def solve(hit: float, swing_stops, swing_clip, mis_stop, mis_clip) -> None:
    logger.info("")
    logger.info(BAR)
    logger.info(f"5. SOLVED, at hit={hit:.0%}")
    logger.info(BAR)

    for label, product, stops, clips in (
            ("SWING CNC", "CNC", swing_stops,
             ((swing_clip, "measured median clip"), (4_000, "20% cap at Rs 20,000"))),
            ("INTRADAY MIS", "MIS", [(mis_stop, "measured MIS median")],
             ((mis_clip, "measured median clip"), (5_000, "25% of Rs 20,000")))):
        logger.info("")
        logger.info(f"── {label} ──")
        for sp, lbl in stops:
            mc = min_clip_for(2.0, sp, product, hit)
            answer = ("NO CLIP EVER CLEARS IT" if mc is None
                      else "ANY clip — not a binding constraint" if mc == 0.0
                      else f"Rs {mc:,.0f}")
            logger.info(f"   stop {sp:.2f}% ({lbl})")
            logger.info(f"     minimum clip clearing friction at 2R : {answer}")
            for c, cl in clips:
                fr = friction_r(c, sp, product)
                logger.info(f"     min target at Rs{c:>6,} ({cl:22s}): "
                            f"{min_target_for(c, sp, product, hit):.3f}R   "
                            f"friction {fr:.3f}R   net@2R {gross_r(hit,2.0)-fr:+.3f}R")

    # The same arithmetic asked the other way round, and the form that is
    # hardest to argue with: hold the target, solve for the win rate that just
    # pays for the friction. Compare it against what the book actually hits.
    #
    #   h*(T) = (1 + friction_R) / (1 + T)
    #
    # from  h*T - (1-h) = friction_R.  No assumption about the hit rate enters,
    # so this row survives even if the 40% design target never does.
    logger.info("")
    logger.info("── BREAK-EVEN HIT RATE   h* = (1 + friction_R) / (1 + target_R) ──")
    logger.info("   measured: SWING 39.7% (n=78) · INTRADAY MIS 25.5% (n=47), Stage 2 C.4")
    logger.info(f"   {'book':9s} {'clip':>7s} {'stop%':>6s} {'fricR':>6s} │ "
                f"{'h* @2.0R':>9s} {'h* @2.5R':>9s} {'h* @3.0R':>9s}")
    for bk, product, c, sp in (("SWING", "CNC", swing_clip, swing_stops[0][0]),
                               ("SWING", "CNC", swing_clip, swing_stops[1][0]),
                               ("SWING", "CNC", 4_000.0, swing_stops[0][0]),
                               ("SWING", "CNC", 4_000.0, swing_stops[1][0]),
                               ("INTRADAY", "MIS", mis_clip, mis_stop)):
        fr = friction_r(c, sp, product)
        logger.info(f"   {bk:9s} {c:>7,.0f} {sp:5.2f}% {fr:6.3f} │ "
                    + " ".join(f"{(1+fr)/(1+t):8.2%}" for t in TARGETS))


# ══════════════════════════════════════════════════════════════════════════
# 6 — the Rs 20,000 question
# ══════════════════════════════════════════════════════════════════════════
def at_20k(hit: float, swing_stops, swing_rate: float) -> None:
    cap = 20_000
    max_pos = cfg_float("max_position_pct", 20.0) / 100.0
    ceiling = cap * max_pos
    logger.info("")
    logger.info(BAR)
    logger.info("6. AT Rs 20,000 — every swing configuration the constraints permit")
    logger.info(BAR)
    logger.info(f"  clip ceiling Rs {ceiling:,.0f}  (max_position_pct {max_pos:.0%})")
    logger.info(f"  CNC is full cash, so concurrent slots = Rs {cap:,} / clip")
    logger.info(f"  monthly at the MEASURED close rate of {swing_rate:.1f} trades/month")
    logger.info(f"  {'clip':>7} {'stop%':>7} {'riskRs':>7} {'risk%':>6} {'fricR':>7} "
                f"{'@2.0R':>8} {'@2.5R':>8} {'reqTgt':>8} "
                f"{'Rs/mo@2R':>9} {'%/mo@2R':>8} {'Rs/mo@2.5R':>11} {'slots':>6}")
    for c in (2_000, 2_733, 3_000, 4_000):
        for sp, _ in swing_stops:
            rr = c * sp / 100.0
            fr = friction_r(c, sp, "CNC")
            n2, n25 = gross_r(hit, 2.0) - fr, gross_r(hit, 2.5) - fr
            m2, m25 = n2 * rr * swing_rate, n25 * rr * swing_rate
            logger.info(f"  {c:>7,} {sp:6.2f}% {rr:7.0f} {rr/cap*100:5.2f}% {fr:7.3f} "
                        f"{n2:+8.3f} {n25:+8.3f} "
                        f"{min_target_for(c, sp, 'CNC', hit):7.3f}R "
                        f"{m2:+9.0f} {m2/cap*100:+7.2f}% {m25:+11.0f} "
                        f"{int(cap//c):5d}{' (cap)' if c > ceiling else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hit", type=float, default=0.40,
                    help="win rate the grid assumes (default 0.40, the design target)")
    args = ap.parse_args()
    hit = args.hit

    logger.info("")
    logger.info(f"UNIT ECONOMICS — hit={hit:.0%}, loser = -1.00R, friction = statutory only")
    logger.info("")

    m = measured()
    plan_stop, plan_tgt, plan_n = plan_geometry()

    sw = m.get("SWING/CNC", {"clip": 2_733.0, "stop": 6.23})
    mis = m.get("INTRADAY/MIS", {"clip": 6_525.0, "stop": 0.90})
    swing_stops = [(round(sw["stop"], 2), "closed swing book median"),
                   (round(plan_stop, 2), f"current plans median, n={plan_n}")]

    friction_ladder()
    grid_risk_fixed(hit)
    grid_stop_fixed(hit, swing_stops, "CNC", "SWING CNC")
    grid_stop_fixed(hit, [(round(mis["stop"], 2), "closed MIS median")],
                    "MIS", "INTRADAY MIS")
    solve(hit, swing_stops, sw["clip"], round(mis["stop"], 2), mis["clip"])
    at_20k(hit, swing_stops, swing_rate=10.6)

    logger.info("")
    logger.info(BAR)
    logger.info("7. THE TARGET THE BOOK PLANS vs THE TARGET THE DESIGN ASSUMES")
    logger.info(BAR)
    logger.info("   Every grid above assumes the winner pays a FULL 2R. The plans do not")
    logger.info("   ask for one. This is what the book's own median target is worth.")
    for t, lbl in ((round(plan_tgt, 2), f"plans median n={plan_n}"), (2.00, "design")):
        for sp, sl in swing_stops:
            fr = friction_r(sw["clip"], sp, "CNC")
            g = gross_r(hit, t)
            logger.info(f"   target {t:.2f}R ({lbl:22s}) stop {sp:.2f}% : "
                        f"gross {g:+.3f}R  friction {fr:.3f}R  net {g-fr:+.3f}R")


if __name__ == "__main__":
    main()
