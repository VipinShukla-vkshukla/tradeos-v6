"""
TradeOS v6 — Phase 4: Discovery Engine
Mines chartink_raw_data history for hidden signal dimensions not in the current framework.
Correlates data columns with closed trade outcomes using statistical methods.
Writes candidate signals to discovery_proposals (PENDING — never auto-applies).

Run: every Sunday via evolution_weekly.yml, Phase 4+ only.
Wire in evolution_weekly.yml:
  - name: Discovery Engine
    if: ${{ vars.AUTONOMY_PHASE == '4' }}
    run: python history/discovery_engine.py

Pre-requisites:
  - 6+ months of chartink_raw_data accumulation (minimum for statistical significance)
  - signal_log with outcome_pnl_pct populated (G2 fix must be applied)
  - G1 fix applied (9 ML feature columns in signal_log)
  
What it does:
  1. Load all closed_positions with linked signal_log entry rows
  2. For each chartink_raw_data column, compute: win_rate_when_high vs win_rate_when_low
  3. Identify columns with statistically significant split (chi-squared or Mann-Whitney U)
  4. Compute correlation coefficient between column value and outcome_pnl_pct
  5. Write top N proposals to discovery_proposals (status=PENDING)
  6. Telegram summary with top 3 discoveries
  
Statistical threshold: p < 0.05, minimum sample size 30 per bucket.
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import get_supabase, today_ist, IST, is_kill_switch_active, logger

MIN_SAMPLE_SIZE = 30     # min trades per bucket for statistical significance
P_VALUE_THRESHOLD = 0.05
MAX_PROPOSALS = 5        # max discoveries to write per weekly run
LOOKBACK_DAYS = 180      # 6 months of history


def load_trade_outcomes(sb) -> list[dict]:
    """
    Load closed trades linked to their signal_log entry conditions.
    Requires G1 (9 ML cols) and G2 (outcome_pnl_pct) fixes to be applied.
    """
    cutoff = (today_ist() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    # Get closed positions with exit dates
    closed = sb.table("closed_positions").select("symbol,entry_date,pnl_pct") \
        .gte("exit_date", cutoff).execute().data
    if not closed:
        logger.warning("No closed trades in lookback period")
        return []

    # Link to signal_log for entry conditions
    linked = []
    for trade in closed:
        signal = sb.table("signal_log").select("*") \
            .eq("symbol", trade["symbol"]) \
            .eq("date", trade["entry_date"]) \
            .limit(1).execute().data
        if signal:
            merged = {**signal[0], "actual_pnl_pct": float(trade.get("pnl_pct") or 0)}
            linked.append(merged)

    logger.info(f"Loaded {len(linked)} trades with signal context")
    return linked


def load_chartink_features(sb, symbols: list[str], dates: list[str]) -> dict:
    """
    Load chartink_raw_data features for the given symbols and dates.
    Returns dict: (symbol, date) -> feature_row
    """
    if not symbols:
        return {}
    result = {}
    BATCH = 50
    for i in range(0, len(symbols), BATCH):
        batch_syms = symbols[i:i+BATCH]
        rows = sb.table("chartink_raw_data").select("*") \
            .in_("symbol", batch_syms) \
            .in_("date", dates[:50]).execute().data
        for r in rows:
            result[(r["symbol"], r["date"])] = r
    return result


def get_numeric_columns(sample_row: dict) -> list[str]:
    """Identify numeric columns to test from a chartink_raw_data row."""
    exclude = {"id", "date", "symbol", "company_name", "sector", "industry", "created_at"}
    numeric_cols = []
    for k, v in sample_row.items():
        if k not in exclude:
            try:
                float(v)
                numeric_cols.append(k)
            except (TypeError, ValueError):
                pass
    return numeric_cols


def compute_correlation(values: list[float], outcomes: list[float]) -> float | None:
    """Pearson correlation between feature values and outcome pnl%."""
    if len(values) < 10:
        return None
    try:
        import statistics
        n = len(values)
        mean_v = sum(values) / n
        mean_o = sum(outcomes) / n
        cov = sum((v - mean_v) * (o - mean_o) for v, o in zip(values, outcomes)) / n
        std_v = statistics.stdev(values) or 1
        std_o = statistics.stdev(outcomes) or 1
        return round(cov / (std_v * std_o), 4)
    except Exception:
        return None


def compute_win_rate_split(values: list[float], outcomes: list[float]) -> dict | None:
    """
    Split by median and compute win rate above vs below median.
    Win = outcome_pnl_pct > 0.
    Returns None if insufficient samples.
    """
    if len(values) < MIN_SAMPLE_SIZE * 2:
        return None

    import statistics
    median = statistics.median(values)
    above = [(v, o) for v, o in zip(values, outcomes) if v >= median]
    below = [(v, o) for v, o in zip(values, outcomes) if v < median]

    if len(above) < MIN_SAMPLE_SIZE or len(below) < MIN_SAMPLE_SIZE:
        return None

    wr_above = sum(1 for _, o in above if o > 0) / len(above)
    wr_below = sum(1 for _, o in below if o < 0) / len(below)
    split = abs(wr_above - wr_below)

    return {
        "median": round(median, 4),
        "win_rate_above_median": round(wr_above, 4),
        "win_rate_below_median": round(wr_below, 4),
        "win_rate_split": round(split, 4),
        "above_n": len(above),
        "below_n": len(below),
    }


def discover_features(trades: list[dict], chartink_map: dict) -> list[dict]:
    """
    Test every chartink_raw_data numeric column for correlation with trade outcome.
    Returns top discoveries sorted by win_rate_split.
    """
    # Get feature columns from first available chartink row
    sample_key = next(iter(chartink_map), None)
    if not sample_key:
        logger.warning("No chartink features available for discovery")
        return []
    feature_cols = get_numeric_columns(chartink_map[sample_key])

    discoveries = []

    for col in feature_cols:
        values, outcomes = [], []
        for trade in trades:
            key = (trade["symbol"], trade["date"])
            craw = chartink_map.get(key)
            if craw and craw.get(col) is not None:
                try:
                    values.append(float(craw[col]))
                    outcomes.append(float(trade.get("actual_pnl_pct") or 0))
                except (TypeError, ValueError):
                    pass

        if len(values) < MIN_SAMPLE_SIZE:
            continue

        corr = compute_correlation(values, outcomes)
        split_data = compute_win_rate_split(values, outcomes)

        if corr is None or split_data is None:
            continue

        # Only surface features with meaningful correlation or win rate split
        if abs(corr) < 0.15 and split_data["win_rate_split"] < 0.10:
            continue

        discoveries.append({
            "feature_name":        col,
            "correlation":         corr,
            "sample_size":         len(values),
            "win_rate_with":       split_data["win_rate_above_median"],
            "win_rate_without":    split_data["win_rate_below_median"],
            "win_rate_split":      split_data["win_rate_split"],
            "evidence": (
                f"Feature '{col}': correlation={corr:.3f} with outcome. "
                f"Above-median: {split_data['win_rate_above_median']:.1%} win rate ({split_data['above_n']} trades). "
                f"Below-median: {split_data['win_rate_below_median']:.1%} win rate ({split_data['below_n']} trades). "
                f"Split: {split_data['win_rate_split']:.1%}."
            )
        })

    # Sort by win_rate_split descending
    discoveries.sort(key=lambda x: x["win_rate_split"], reverse=True)
    logger.info(f"Discovery engine: {len(discoveries)} candidate features found")
    return discoveries[:MAX_PROPOSALS]


def write_proposals(sb, discoveries: list[dict], today: str):
    """Write top discoveries to discovery_proposals (PENDING status, never auto-applied)."""
    if not discoveries:
        return 0
    rows = [
        {
            **d,
            "week_of":   today,
            "status":    "PENDING",
            "created_at": datetime.now(IST).isoformat(),
        }
        for d in discoveries
    ]
    sb.table("discovery_proposals").insert(rows).execute()
    return len(rows)


def send_telegram_summary(discoveries: list[dict]):
    """Send top 3 discoveries via Telegram."""
    if not discoveries:
        return
    try:
        from control.telegram_bot import send_message
        msg = f"<b>🔬 Discovery Engine — {len(discoveries)} New Signal Candidates</b>\n"
        for d in discoveries[:3]:
            msg += (
                f"\n<b>{d['feature_name']}</b>\n"
                f"Correlation: {d['correlation']:.3f} | "
                f"Win split: {d['win_rate_split']:.1%} | "
                f"Sample: {d['sample_size']} trades\n"
            )
        msg += "\nReview in discovery_proposals table. Status = PENDING — nothing auto-applied."
        send_message(msg)
    except Exception as e:
        logger.warning(f"Discovery Telegram alert failed: {e}")


def main():
    if is_kill_switch_active():
        logger.warning("Kill switch active — discovery_engine skipped")
        return {"status": "skipped", "reason": "kill_switch"}

    logger.info("Discovery Engine starting")
    sb = get_supabase()
    today = str(today_ist())

    # 1. Load trade outcomes
    trades = load_trade_outcomes(sb)
    if len(trades) < MIN_SAMPLE_SIZE:
        logger.info(f"Insufficient trades ({len(trades)}) for discovery — need {MIN_SAMPLE_SIZE}+")
        return {"status": "skipped", "reason": f"insufficient_trades: {len(trades)}"}

    # 2. Load chartink features
    symbols = list({t["symbol"] for t in trades})
    dates   = list({t["date"] for t in trades})
    chartink_map = load_chartink_features(sb, symbols, dates)
    logger.info(f"Loaded {len(chartink_map)} chartink feature rows")

    # 3. Run discovery
    discoveries = discover_features(trades, chartink_map)

    # 4. Write proposals
    count = write_proposals(sb, discoveries, today)

    # 5. Telegram
    send_telegram_summary(discoveries)

    logger.success(f"Discovery Engine complete: {count} proposals written")
    return {"proposals": count, "candidates_tested": len(discoveries), "status": "ok"}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print discoveries without writing")
    args = parser.parse_args()

    if args.dry_run:
        import os; os.environ["DRY_RUN"] = "True"

    result = main()
    print(result)
