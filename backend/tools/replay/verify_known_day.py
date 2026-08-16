"""
Prove the harness against a day already recorded. THE GATE ON THE WHOLE PHASE.

If the harness cannot reproduce a session the live system already recorded,
nothing it says about an unrecorded one is worth reading. This module is
therefore the only place in the package permitted to read `intraday_setups` —
it COMPARES against that table and never computes from it — and
`independence.py` whitelists exactly this file.

    python -m tools.replay.verify_known_day --date 2026-08-14
    python -m tools.replay.verify_known_day --date 2026-08-14 --outcome-only

TWO CHECKS, DELIBERATELY SEPARATE
-----------------------------------
1. `check_outcome_rule()` — feeds the PORTED resolver the stored
   `(entry, stop, target, direction)` and the harness's own bars, and requires
   it to reproduce the recorded `outcome`. This isolates the outcome rule from
   the detection path: if detections do not reproduce but outcomes do, you know
   which half is broken. That is the difference between a debuggable failure and
   a shrug.

2. `check_detections()` — replays the day cold from bars and compares the
   detections it finds to the stored ones.

DEDUP MUST USE THE SAME RULE ON BOTH SIDES
--------------------------------------------
`(trade_date, symbol, engine)`, each key represented by its FIRST detection by
timestamp. The ledger records that the two available constructions (first
detection vs first TAKEN) disagree on 3 of 1102 keys, so picking the wrong one
chases a 0.2 pp ghost.

**The stored `strategy` column holds the FAMILY, not the sub-engine.**
`engine.py:3733` writes `s.meta.get("family") or s.strategy`, while
`_setup_is_new` dedups on `s.strategy` (the sub-engine). Comparing the harness's
sub-engine against the stored `strategy` would mismatch every GAP and PDL row
(both stored as family ORB) and every PBK row (stored as VWR). The comparison
below therefore matches on FAMILY, which is what the column actually contains.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import get_supabase, fetch_all
from intraday import direction as D
from tools.replay.bars import BarSource
from tools.replay.conventions import ALL as CONVENTIONS, BAR_CLOSE, Convention
from tools.replay.detect import replay_symbol_day
from tools.replay.outcomes_port import resolve
from tools.replay.universe import (build_universe_for_session,
                                   prev_day_reference)


# ── stored side ─────────────────────────────────────────────────────────────
def load_stored(day: str, sb=None) -> list[dict]:
    """
    Every recorded detection for one date. Paged — this table crossed 1000 rows
    PER SESSION on 12-Aug-2026 and an unpaged read here would silently compare
    against the first thousand.
    """
    sb = sb or get_supabase()
    return fetch_all(
        lambda: sb.table("intraday_setups")
                  # `ts` is the detection timestamp — there is no `created_at`
                  # on this table, and asking for one raises 42703 on page one
                  # rather than returning a degraded read.
                  .select("id,trade_date,symbol,strategy,phase,direction,entry,"
                          "stop,target,rr,confidence,outcome,outcome_pct,"
                          "cost_verdict,meta,ts")
                  .eq("trade_date", day),
        order_by="id")


def dedupe(rows: list[dict], key_fn, ts_fn) -> dict:
    """First detection per key, by timestamp. `weekly_review.dedupe_setups`' rule."""
    best: dict = {}
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        if k not in best or (ts_fn(r) or "") < (ts_fn(best[k]) or ""):
            best[k] = r
    return best


# ── check 1: the outcome rule ───────────────────────────────────────────────
@dataclass
class OutcomeCheck:
    total: int = 0
    compared: int = 0
    matched: int = 0
    mismatches: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)
    # Residuals CLASSIFIED rather than counted. REPLAY_DESIGN §10.2 requires
    # every non-reproduced row to land in a named cause with a count, so that a
    # mismatch is diagnosed instead of explained away afterwards.
    from_session_open: int = 0     # stored value matches a WHOLE-DAY window
    unexplained: int = 0

    @property
    def pct(self) -> float:
        return (self.matched / self.compared * 100.0) if self.compared else 0.0


def check_outcome_rule(day: str, src: BarSource, sb=None,
                       limit: int | None = None) -> OutcomeCheck:
    """
    Does the PORTED resolver reproduce the recorded outcome? Bar >= 99%.

    Only rows with a stored outcome are compared; unresolved ones are counted
    and reported, never treated as agreement.
    """
    rows = load_stored(day, sb)
    chk = OutcomeCheck(total=len(rows))

    for r in rows[:limit] if limit else rows:
        stored = (r.get("outcome") or "").upper()
        if stored not in ("TARGET", "STOP", "TIMEOUT"):
            chk.skipped["unresolved"] = chk.skipped.get("unresolved", 0) + 1
            continue
        entry, stop, tgt = (r.get("entry"), r.get("stop"), r.get("target"))
        if not (entry and stop and tgt):
            chk.skipped["no_levels"] = chk.skipped.get("no_levels", 0) + 1
            continue

        bars = src.get(r["symbol"], day)
        if not bars:
            chk.skipped["no_bars"] = chk.skipped.get("no_bars", 0) + 1
            continue

        created = r.get("ts")
        after = None
        if created:
            from datetime import datetime
            try:
                after = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                after = after.astimezone(bars[0].ts.tzinfo)
            except Exception:
                after = None

        got = resolve(float(entry), float(stop), float(tgt),
                      r.get("direction") or "LONG", bars, after=after)
        chk.compared += 1
        if got.outcome == stored:
            chk.matched += 1
            continue

        # CLASSIFY the residual. The one cause seen so far is a stored value
        # produced by a window starting at the SESSION OPEN rather than at the
        # setup's own timestamp — i.e. scored partly on price action that
        # happened before the setup existed. Re-running with that window
        # identifies it positively rather than by elimination.
        from_open = resolve(float(entry), float(stop), float(tgt),
                            r.get("direction") or "LONG", bars, after=None)
        cause = ("stored value matches a WHOLE-DAY window"
                 if from_open.outcome == stored else "UNEXPLAINED")
        if from_open.outcome == stored:
            chk.from_session_open += 1
        else:
            chk.unexplained += 1

        chk.mismatches.append({
            "symbol": r["symbol"], "strategy": r.get("strategy"),
            "direction": r.get("direction"), "stored": stored,
            "got": got.outcome, "from_open": from_open.outcome,
            "entry": entry, "stop": stop, "target": tgt, "cause": cause,
        })
    return chk


# ── residual classification (REPLAY_DESIGN §10.2) ───────────────────────────
#
# THE LEVER THAT MAKES THIS POSSIBLE. Every one of the nine engines sets
# `entry=round(ctx.ltp, 2)` — checked at orb.py:146, gap_and_go.py:122,
# prev_day_levels.py:157, squeeze.py:126, pullback.py:137, vwap_reclaim.py:162,
# range_fade.py:121, short_distribution.py:195/270/314, gap_down_bounce.py:187.
# So a stored `entry` is not a derived level, it IS the live LTP at that
# instant. Asking whether that number appears as a completed-bar close near its
# own timestamp turns "tick versus bar" from a story into a count.
NEARBY_BARS = 2


def _entry_is_a_bar_close(bars, ts_raw, entry, window: int = NEARBY_BARS) -> bool:
    """Did the stored LTP print as the close of a bar within ±`window` of its ts?"""
    if not bars or entry is None:
        return False
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")
                                    ).astimezone(bars[0].ts.tzinfo)
    except Exception:
        return False
    i = None
    for j, b in enumerate(bars):
        if b.ts <= ts:
            i = j
        else:
            break
    if i is None:
        return False
    e = round(float(entry), 2)
    lo, hi = max(0, i - window), min(len(bars) - 1, i + window)
    return any(abs(bars[j].close - e) <= 0.005 for j in range(lo, hi + 1))


# ── check 2: detections ─────────────────────────────────────────────────────
@dataclass
class DetectionCheck:
    day: str = ""
    convention: str = ""
    stored_keys: int = 0
    replay_keys: int = 0
    reproduced: int = 0
    missed: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    level_mismatches: list = field(default_factory=list)
    symbols_replayed: int = 0
    coverage: str = ""
    scan_date: str | None = None
    miss_causes: dict = field(default_factory=dict)
    extra_causes: dict = field(default_factory=dict)
    entry_bps: dict = field(default_factory=dict)
    # Per family, because the residual's SIGN is what every downstream number
    # inherits and it is not the same sign in every engine. A pooled "43 missed,
    # 18 extra" hides that VCE is net short of detections while SDN is net long.
    by_family: dict = field(default_factory=dict)

    @property
    def pct(self) -> float:
        return (self.reproduced / self.stored_keys * 100.0) if self.stored_keys else 0.0


def check_detections(day: str, src: BarSource, sb=None,
                     universe_limit: int | None = None,
                     convention: Convention = BAR_CLOSE) -> DetectionCheck:
    """
    Replay `day` cold and compare its detections to the stored record.

    THE UNIVERSE IS RANKED ON THE PRIOR SESSION, not on `day`. The daemon builds
    its list at 09:15, when `day`'s own bhavcopy does not exist —
    `universe.scan_date_for_session` carries the measurement and the reasoning.
    This module used `day`'s own rows until 2026-08-16 and that was lookahead as
    well as infidelity.

    The replayed symbol set is the union of that universe and the symbols the
    stored record names. That is NOT circular: the stored record chooses which
    symbols to fetch bars for and contributes nothing to whether a detection is
    found on them. It exists because `scanner.live_rerank` promoted names
    intraday from quotes that were never stored, so without the union those
    misses would be uncountable rather than merely unreproducible.
    """
    sb = sb or get_supabase()
    chk = DetectionCheck(day=day, convention=convention.name)

    stored_rows = load_stored(day, sb)
    stored = dedupe(stored_rows,
                    key_fn=lambda r: (r["trade_date"], r["symbol"],
                                      (r.get("strategy") or "").upper()),
                    ts_fn=lambda r: str(r.get("ts") or ""))
    chk.stored_keys = len(stored)

    uni = build_universe_for_session(day, sb, limit=universe_limit)
    chk.scan_date = uni.scan_date
    watched = set(uni.symbols)
    live_symbols = {r["symbol"] for r in stored_rows}
    symbols = sorted(watched | live_symbols)
    chk.symbols_replayed = len(symbols)

    prev = prev_day_reference(day, symbols, sb)

    replayed: dict = {}
    bars_by_symbol: dict = {}
    for sym in symbols:
        bars = src.get(sym, day)
        if not bars:
            continue
        bars_by_symbol[sym] = bars
        for det in replay_symbol_day(sym, day, bars, prev=prev.get(sym),
                                     convention=convention):
            k = (det.trade_date, det.symbol, det.family.upper())
            if k not in replayed or det.ts < replayed[k].ts:
                replayed[k] = det
    chk.replay_keys = len(replayed)
    chk.coverage = src.coverage.line()

    miss_causes: dict[str, int] = defaultdict(int)
    bps_buckets: dict[str, int] = defaultdict(int)

    for k, srow in stored.items():
        det = replayed.get(k)
        sym = k[1]
        if det is None:
            # Classified in priority order — the first cause that applies is the
            # binding one. A symbol with no bars cannot be judged on its price,
            # and a price that never printed cannot be blamed on a silent engine.
            #
            # UNIVERSE MEMBERSHIP IS NOT A CAUSE HERE and an earlier version of
            # this classifier wrongly made it one. The replayed symbol set is the
            # union of the universe and every symbol the live record names, so a
            # name outside the reconstructed 40 was still replayed, still had
            # bars, and still had every engine run against it. Labelling its
            # miss "not in the universe" would have credited a universe fix with
            # 25 misses it does not touch. Membership is reported beside the
            # cause, never as one.
            bars = bars_by_symbol.get(sym)
            if not bars:
                cause = "no_bars_for_symbol"
            elif not _entry_is_a_bar_close(bars, srow.get("ts"), srow.get("entry")):
                cause = "live_ltp_is_intra_minute"           # §10.2 cause 1
            else:
                cause = "engine_silent_on_a_reachable_price"
            miss_causes[cause] += 1
            chk.missed.append({"key": k, "strategy": srow.get("strategy"),
                               "direction": srow.get("direction"),
                               "entry": srow.get("entry"),
                               "verdict": srow.get("cost_verdict"),
                               "cause": cause, "in_universe": sym in watched})
            continue
        chk.reproduced += 1
        problems = []
        if D.normalise(det.direction) != D.normalise(srow.get("direction") or "LONG"):
            problems.append(f"direction {det.direction} vs {srow.get('direction')}")
        for fld, got in (("entry", det.entry), ("stop", det.stop),
                         ("target", det.target)):
            exp = srow.get(fld)
            if exp is not None and abs(float(exp) - float(got)) > 0.01:
                problems.append(f"{fld} {got:.2f} vs {float(exp):.2f}")
        # RELATIVE, alongside the absolute check. A 2 dp match on a Rs 37,550
        # stock demands the identical tick; the same 0.01 on a Rs 100 stock is a
        # basis point. Both are reported because they answer different questions
        # — "is it the same number" and "is it the same trade".
        exp_entry = srow.get("entry")
        if exp_entry:
            bps = abs(det.entry - float(exp_entry)) / float(exp_entry) * 10_000.0
            for label, edge in (("<=5bps", 5), ("<=25bps", 25),
                                ("<=100bps", 100), (">100bps", 1e18)):
                if bps <= edge:
                    bps_buckets[label] += 1
                    break
        if problems:
            chk.level_mismatches.append({"key": k, "problems": problems})

    extra_causes: dict[str, int] = defaultdict(int)
    stored_families_by_symbol: dict[str, set] = defaultdict(set)
    for r in stored_rows:
        stored_families_by_symbol[r["symbol"]].add((r.get("strategy") or "").upper())

    for k, det in replayed.items():
        if k in stored:
            continue
        sym = k[1]
        if sym not in live_symbols:
            cause = "symbol_absent_from_live_record"
        elif det.look != "bar_close":
            cause = "sub_bar_look_only"
        elif stored_families_by_symbol.get(sym):
            cause = "symbol_watched_live_but_this_family_never_fired"
        else:
            cause = "unexplained"
        extra_causes[cause] += 1
        chk.extras.append({"key": k, "engine": det.engine,
                           "sub_engine": det.sub_engine, "look": det.look,
                           "entry": det.entry, "phase": det.phase,
                           "cause": cause})

    chk.miss_causes = dict(sorted(miss_causes.items(), key=lambda kv: -kv[1]))
    chk.extra_causes = dict(sorted(extra_causes.items(), key=lambda kv: -kv[1]))
    chk.entry_bps = dict(bps_buckets)

    fam: dict[str, dict] = defaultdict(
        lambda: {"stored": 0, "repro": 0, "missed": 0, "extra": 0})
    for k in stored:
        fam[k[2]]["stored"] += 1
        fam[k[2]]["repro" if k in replayed else "missed"] += 1
    for m in chk.extras:
        fam[m["key"][2]]["extra"] += 1
    for f, d in fam.items():
        d["net"] = d["extra"] - d["missed"]
    chk.by_family = dict(sorted(fam.items(), key=lambda kv: -kv[1]["stored"]))
    return chk


# ── report ──────────────────────────────────────────────────────────────────
def report(day: str, oc: OutcomeCheck | None, dc: DetectionCheck | None) -> int:
    print()
    print("=" * 74)
    print(f"HARNESS VERIFICATION — {day}")
    print("=" * 74)

    rc = 0
    if oc is not None:
        print(f"\n1. OUTCOME RULE (ported resolver vs stored outcome)")
        print(f"   stored rows      : {oc.total}")
        print(f"   compared         : {oc.compared}")
        print(f"   reproduced       : {oc.matched}  ({oc.pct:.1f}%)   bar: >= 99%")
        if oc.skipped:
            print(f"   skipped          : {dict(sorted(oc.skipped.items()))}")
        if oc.mismatches:
            print(f"\n   RESIDUALS ({len(oc.mismatches)}), classified:")
            print(f"     stored value matches a WHOLE-DAY window : "
                  f"{oc.from_session_open}")
            print(f"     UNEXPLAINED                             : "
                  f"{oc.unexplained}")
            print(f"\n   first 10:")
            print(f"     {'symbol':<14}{'eng':<6}{'dir':<6}{'stored':<8}"
                  f"{'harness':<9}{'whole-day':<10}")
            for m in oc.mismatches[:10]:
                print(f"     {m['symbol']:<14}{str(m['strategy']):<6}"
                      f"{m['direction']:<6}{m['stored']:<8}{m['got']:<9}"
                      f"{m['from_open']:<10}")
        # The harness is trusted when nothing is left UNEXPLAINED. A residual
        # that reproduces exactly under a different, identifiable window is a
        # property of the STORED value, not an error in the rule under test —
        # and it is reported as its own finding rather than absorbed.
        verdict = ("PASS" if oc.compared and oc.unexplained == 0 else "FAIL")
        rc |= 0 if verdict == "PASS" else 1
        print(f"\n   -> {verdict}   "
              f"({oc.matched} reproduced, {oc.from_session_open} stored-side "
              f"anomalies, {oc.unexplained} unexplained)")

    if dc is not None:
        print(f"\n2. DETECTIONS (cold replay vs stored record)")
        print(f"   convention       : {dc.convention}"
              + ("   [UPPER BOUND, not a reproduction]"
                 if CONVENTIONS[dc.convention].is_bound else ""))
        print(f"   universe ranked  : {dc.scan_date}  (the prior session — the "
              f"daemon has no bhavcopy for {dc.day} at 09:15)")
        print(f"   {dc.coverage}")
        print(f"   symbols replayed : {dc.symbols_replayed}")
        print(f"   stored keys      : {dc.stored_keys}")
        print(f"   replayed keys    : {dc.replay_keys}")
        print(f"   reproduced       : {dc.reproduced}  ({dc.pct:.1f}%)   bar: >= 85%")
        print(f"   missed           : {len(dc.missed)}")
        print(f"   extras           : {len(dc.extras)}   bar: 0 "
              f"(an extra means the replay is MORE permissive than live)")
        print(f"   level mismatches : {len(dc.level_mismatches)}")
        if dc.entry_bps:
            order = ["<=5bps", "<=25bps", "<=100bps", ">100bps"]
            print("   entry agreement  : "
                  + "  ".join(f"{l} {dc.entry_bps.get(l, 0)}" for l in order))
        if dc.by_family:
            print("   PER FAMILY (net = extras - misses; the sign every "
                  "downstream n inherits):")
            print(f"     {'fam':<6}{'stored':>8}{'repro':>7}{'missed':>8}"
                  f"{'extra':>7}{'net':>6}")
            for f, d in dc.by_family.items():
                print(f"     {f:<6}{d['stored']:>8}{d['repro']:>7}"
                      f"{d['missed']:>8}{d['extra']:>7}{d['net']:>+6}")
        if dc.miss_causes:
            print(f"   MISSES classified ({len(dc.missed)}):")
            for c, n in dc.miss_causes.items():
                print(f"     {n:>4}  {c}")
        if dc.extra_causes:
            print(f"   EXTRAS classified ({len(dc.extras)}):")
            for c, n in dc.extra_causes.items():
                print(f"     {n:>4}  {c}")
        if dc.missed:
            print(f"   MISSED, first 15:")
            for m in dc.missed[:15]:
                _, sym, fam = m["key"]
                print(f"     {sym:<13}{fam:<5}{str(m['direction']):<5} "
                      f"entry={str(m['entry']):<9} {m['cause']}")
        if dc.extras:
            print(f"   EXTRAS, first 15:")
            for e in dc.extras[:15]:
                _, sym, fam = e["key"]
                print(f"     {sym:<13}{fam:<5}via {e['engine']}/{e['sub_engine']:<5} "
                      f"entry={e['entry']:<10.2f}{e['cause']}")
        if dc.level_mismatches:
            print(f"   LEVEL MISMATCHES, first 10:")
            for m in dc.level_mismatches[:10]:
                _, sym, fam = m["key"]
                print(f"     {sym:<14} {fam:<6} {'; '.join(m['problems'])}")
        verdict = "PASS" if dc.stored_keys and dc.pct >= 85.0 and not dc.extras else "FAIL"
        rc |= 0 if verdict == "PASS" else 1
        print(f"   -> {verdict}")

    print()
    print("=" * 74)
    print("HARNESS TRUSTED" if rc == 0 else
          "HARNESS NOT TRUSTED — do not run a full replay on these results")
    print("=" * 74)
    return rc


def sweep(day: str, src: BarSource, sb=None,
          universe_limit: int | None = None) -> int:
    """
    Every convention against the same day, side by side. A DIAGNOSTIC.

    The exit code is the SHIPPED convention's, never the best column's. A sweep
    that could pass the gate by finding a permissive enough convention would be
    the acceptance bar moving itself, which is the one thing this stage was told
    not to do.
    """
    results = []
    for name in ("bar_close", "cadence_15s", "next_open",
                 "ltp_bracket", "full_overlay"):
        conv = CONVENTIONS[name]
        dc = check_detections(day, src, sb, universe_limit=universe_limit,
                              convention=conv)
        results.append((conv, dc))

    print()
    print("=" * 78)
    print(f"CONVENTION SWEEP — {day}   (diagnostic; the gate is bar_close)")
    print("=" * 78)
    print(f"{'convention':<14}{'repro':>8}{'pct':>8}{'missed':>8}{'extras':>8}"
          f"{'keys':>7}   bound?")
    for conv, dc in results:
        print(f"{conv.name:<14}{dc.reproduced:>8}{dc.pct:>7.1f}%{len(dc.missed):>8}"
              f"{len(dc.extras):>8}{dc.replay_keys:>7}   "
              f"{'YES' if conv.is_bound else '-'}")
    print()
    for conv, dc in results:
        print(f"  {conv.name}: {conv.note}")
        print(f"      misses  {dc.miss_causes}")
        print(f"      extras  {dc.extra_causes}")
    print()

    shipped = next(dc for conv, dc in results if conv.name == "bar_close")
    print("=" * 78)
    print(f"GATE (bar_close): {shipped.pct:.1f}% vs 85%, "
          f"{len(shipped.extras)} extras vs 0  -> "
          f"{'PASS' if shipped.pct >= 85.0 and not shipped.extras else 'FAIL'}")
    print("=" * 78)
    return 0 if (shipped.pct >= 85.0 and not shipped.extras) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="trade_date, YYYY-MM-DD")
    ap.add_argument("--outcome-only", action="store_true")
    ap.add_argument("--detections-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap rows in the outcome check (for a fast smoke test)")
    ap.add_argument("--universe-limit", type=int, default=None)
    ap.add_argument("--convention", default="bar_close",
                    choices=sorted(CONVENTIONS),
                    help="when the replay looks and what it may know — see "
                         "tools/replay/conventions.py")
    ap.add_argument("--sweep", action="store_true",
                    help="run every convention and print the comparison. "
                         "Diagnostic: the exit code still comes from the "
                         "SHIPPED convention, never from the best one.")
    args = ap.parse_args()

    try:
        from kite.kite_client import get_kite
        kite = get_kite()
    except Exception as e:
        logger.warning(f"no broker session ({e}) — cache-only")
        kite = None

    src = BarSource(kite)
    sb = get_supabase()

    if args.sweep:
        return sweep(args.date, src, sb, universe_limit=args.universe_limit)

    oc = None if args.detections_only else check_outcome_rule(
        args.date, src, sb, limit=args.limit)
    dc = None if args.outcome_only else check_detections(
        args.date, src, sb, universe_limit=args.universe_limit,
        convention=CONVENTIONS[args.convention])
    return report(args.date, oc, dc)


if __name__ == "__main__":
    raise SystemExit(main())
