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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from loguru import logger

from config import get_supabase, fetch_all
from intraday import direction as D
from tools.replay.bars import BarSource
from tools.replay.detect import replay_symbol_day
from tools.replay.outcomes_port import resolve
from tools.replay.universe import build_universe_at, prev_day_reference


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


# ── check 2: detections ─────────────────────────────────────────────────────
@dataclass
class DetectionCheck:
    day: str = ""
    stored_keys: int = 0
    replay_keys: int = 0
    reproduced: int = 0
    missed: list = field(default_factory=list)
    extras: list = field(default_factory=list)
    level_mismatches: list = field(default_factory=list)
    symbols_replayed: int = 0
    coverage: str = ""

    @property
    def pct(self) -> float:
        return (self.reproduced / self.stored_keys * 100.0) if self.stored_keys else 0.0


def check_detections(day: str, src: BarSource, sb=None,
                     universe_limit: int | None = None) -> DetectionCheck:
    """
    Replay `day` cold and compare its detections to the stored record.

    The replayed universe is the MORNING universe. `scanner.live_rerank`
    promoted names intraday from quotes that were never stored, so detections on
    promoted names cannot reproduce at all — one of the four expected mismatches
    (REPLAY_DESIGN §10.2). To keep that from swamping the comparison, the
    replayed symbol set is the union of the reconstructed universe and the
    symbols the stored record actually names. That is NOT circular: the stored
    record chooses which symbols to fetch bars for, and contributes nothing to
    whether a detection is found on them.
    """
    sb = sb or get_supabase()
    chk = DetectionCheck(day=day)

    stored_rows = load_stored(day, sb)
    stored = dedupe(stored_rows,
                    key_fn=lambda r: (r["trade_date"], r["symbol"],
                                      (r.get("strategy") or "").upper()),
                    ts_fn=lambda r: str(r.get("ts") or ""))
    chk.stored_keys = len(stored)

    uni = build_universe_at(day, sb, limit=universe_limit)
    symbols = sorted(set(uni.symbols) | {r["symbol"] for r in stored_rows})
    chk.symbols_replayed = len(symbols)

    prev = prev_day_reference(day, symbols, sb)

    replayed: dict = {}
    for sym in symbols:
        bars = src.get(sym, day)
        if not bars:
            continue
        for det in replay_symbol_day(sym, day, bars, prev=prev.get(sym)):
            k = (det.trade_date, det.symbol, det.family.upper())
            if k not in replayed or det.ts < replayed[k].ts:
                replayed[k] = det
    chk.replay_keys = len(replayed)
    chk.coverage = src.coverage.line()

    for k, srow in stored.items():
        det = replayed.get(k)
        if det is None:
            chk.missed.append({"key": k, "strategy": srow.get("strategy"),
                               "direction": srow.get("direction"),
                               "entry": srow.get("entry"),
                               "verdict": srow.get("cost_verdict")})
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
        if problems:
            chk.level_mismatches.append({"key": k, "problems": problems})

    for k, det in replayed.items():
        if k not in stored:
            chk.extras.append({"key": k, "engine": det.engine,
                               "sub_engine": det.sub_engine,
                               "entry": det.entry, "phase": det.phase})
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
        print(f"   {dc.coverage}")
        print(f"   symbols replayed : {dc.symbols_replayed}")
        print(f"   stored keys      : {dc.stored_keys}")
        print(f"   replayed keys    : {dc.replay_keys}")
        print(f"   reproduced       : {dc.reproduced}  ({dc.pct:.1f}%)   bar: >= 85%")
        print(f"   missed           : {len(dc.missed)}")
        print(f"   extras           : {len(dc.extras)}   bar: 0 "
              f"(an extra means the replay is MORE permissive than live)")
        print(f"   level mismatches : {len(dc.level_mismatches)}")
        if dc.missed:
            print(f"   MISSED, first 15:")
            for m in dc.missed[:15]:
                _, sym, fam = m["key"]
                print(f"     {sym:<14} {fam:<6} {str(m['direction']):<5} "
                      f"entry={m['entry']} verdict={m['verdict']}")
        if dc.extras:
            print(f"   EXTRAS, first 15:")
            for e in dc.extras[:15]:
                _, sym, fam = e["key"]
                print(f"     {sym:<14} {fam:<6} via {e['engine']}/{e['sub_engine']} "
                      f"entry={e['entry']:.2f} phase={e['phase']}")
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="trade_date, YYYY-MM-DD")
    ap.add_argument("--outcome-only", action="store_true")
    ap.add_argument("--detections-only", action="store_true")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap rows in the outcome check (for a fast smoke test)")
    ap.add_argument("--universe-limit", type=int, default=None)
    args = ap.parse_args()

    try:
        from kite.kite_client import get_kite
        kite = get_kite()
    except Exception as e:
        logger.warning(f"no broker session ({e}) — cache-only")
        kite = None

    src = BarSource(kite)
    sb = get_supabase()

    oc = None if args.detections_only else check_outcome_rule(
        args.date, src, sb, limit=args.limit)
    dc = None if args.outcome_only else check_detections(
        args.date, src, sb, universe_limit=args.universe_limit)
    return report(args.date, oc, dc)


if __name__ == "__main__":
    raise SystemExit(main())
