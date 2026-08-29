"""
What is AI actually costing, by call site — 29-Aug-2026, migration 127.

WHY THIS EXISTS
---------------
Before this session's audit, the three highest-volume AI call sites
(evening decision engine, evening market intel, intraday's advisor firing
up to ~75x/trading day) reported zero token or cost usage anywhere. The
only existing budget mechanism (`post_trade_analysis.PROVIDER_COST_INR`)
is a hardcoded per-call guess, not derived from real usage, and scoped to
the smallest, cheapest, least-frequent path. `ai_usage_log`
(`ai/usage_tracker.py`) now records real `resp.usage` for every call;
this report is how that becomes a number a human can actually look at,
rather than rows sitting in a table nobody queries.

Cost is an ESTIMATE — `PROVIDER_COST_INR` is still the only per-token/
per-call rate this codebase has on hand (no real billing API is wired
up), reused here rather than inventing a second guess. Token counts
themselves are real, read from `resp.usage` at call time, not estimated.

Usage:
    python -m tools.ai_usage_report
    python -m tools.ai_usage_report --days 7
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from config import get_supabase, today_ist, fetch_all

PAGE = 1000

# Same rates post_trade_analysis.py already uses — reused, not duplicated,
# so the two never quietly disagree about what a provider costs.
from ai.post_trade_analysis import PROVIDER_COST_INR


def _fetch_rows(sb, since: str) -> list[dict]:
    # order_by="id", not "ts" — ai_usage_log.id is the table's own BIGSERIAL
    # PRIMARY KEY, guaranteed unique by construction; ts is a timestamp and
    # not guaranteed unique (two calls in the same second would page with no
    # error and let rows repeat/vanish across page boundaries). Caught by
    # this project's own static_analysis check before it ever shipped.
    return fetch_all(lambda: sb.table("ai_usage_log")
                     .select("call_site,framework,provider,model,prompt_tokens,"
                             "completion_tokens,total_tokens,finish_reason,ts")
                     .gte("ts", since), page=PAGE, order_by="id")


def report(days: int, sb=None) -> None:
    sb = sb or get_supabase()
    since = (today_ist() - timedelta(days=max(days, 1))).isoformat()
    rows = _fetch_rows(sb, since)

    logger.info("=" * 78)
    logger.info(f"AI USAGE REPORT — last {days} day(s), since {since}")
    logger.info("=" * 78)

    if not rows:
        logger.info("  no AI calls logged in this window")
        return

    by_site: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_site[r.get("call_site") or "unknown"].append(r)

    total_calls = len(rows)
    total_tokens = sum(r.get("total_tokens") or 0 for r in rows)
    truncated = sum(1 for r in rows if r.get("finish_reason") == "length")
    unmeasured = sum(1 for r in rows if r.get("total_tokens") is None)

    logger.info(f"  {'call_site':<28}{'calls':>7}{'tokens':>10}{'trunc':>7}"
               f"{'~cost(INR)':>12}")
    grand_cost = 0.0
    for site, rs in sorted(by_site.items(), key=lambda kv: -len(kv[1])):
        calls = len(rs)
        tokens = sum(r.get("total_tokens") or 0 for r in rs)
        trunc = sum(1 for r in rs if r.get("finish_reason") == "length")
        # Cost estimate: PROVIDER_COST_INR is a flat per-CALL rate, the only
        # rate this codebase has — not a per-token rate, so it cannot be
        # scaled by this site's own token count without inventing a second
        # number. Flat-rate x calls, same assumption post_trade_analysis.py
        # already makes for its own budget check.
        providers = {r.get("provider") for r in rs if r.get("provider")}
        rate = max((PROVIDER_COST_INR.get(p, 0.30) for p in providers), default=0.30)
        site_cost = calls * rate
        grand_cost += site_cost
        logger.info(f"  {site:<28}{calls:>7}{tokens:>10}{trunc:>7}"
                   f"{site_cost:>12.2f}")

    logger.info("-" * 78)
    logger.info(f"  {'TOTAL':<28}{total_calls:>7}{total_tokens:>10}"
               f"{truncated:>7}{grand_cost:>12.2f}")
    if unmeasured:
        logger.warning(f"  {unmeasured} of {total_calls} calls have no usable "
                       f"token data (provider response shape not recognised, "
                       f"or the call itself failed before a response came back)")
    if truncated:
        logger.warning(f"  {truncated} call(s) hit their token limit "
                       f"(finish_reason=length) — their output is incomplete")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args()
    report(args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
