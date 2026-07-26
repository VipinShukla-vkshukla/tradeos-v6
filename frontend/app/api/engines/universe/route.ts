// app/api/engines/universe/route.ts
//
// GET /api/engines/universe
//   Today's full stock universe plus the sector-rank map, trimmed to only the
//   fields any engine gate references.
//
// WHY SHIP THE WHOLE UNIVERSE TO THE BROWSER
//   The Control Room re-evaluates every gate on every slider movement. Doing
//   that server-side would put a network round-trip between the operator's
//   hand and the number they are reasoning about, which turns a live
//   instrument back into a form. ~499 rows x 25 fields is roughly 200KB —
//   cheap to send once, then free to re-evaluate thousands of times.
//
//   PostgREST caps a response at 1000 rows, so this pages explicitly. The
//   universe is ~499 today, but a cap silently truncating the universe would
//   make every preview subtly wrong, which is worse than being slow.

import { NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

function getServiceClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY ?? process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) throw new Error('Supabase not configured');
  return createClient(url, key);
}

// Every field referenced by any gate in engine_registry.DEFAULT_ENGINE_CONFIG,
// plus what the results table displays.
//
// NOTE: bb_squeeze is NOT a column on stock_data_daily. run_sbs derives it
// inline from bb_upper/bb_lower/close, so it must be derived here too — see
// deriveFields below. Selecting it directly returns
// "column stock_data_daily.bb_squeeze does not exist".
const FIELDS = [
  'symbol', 'company_name', 'sector', 'industry', 'close', 'market_cap',
  'rsi_daily', 'rsi_weekly', 'rsi_monthly', 'adx', 'di_plus', 'di_minus',
  'vol_ratio', 'delivery_pct', 'atr_pct', 'consol_range', 'pct_change',
  'rs_vs_nifty', 'dist_sma50', 'ret_1m', 'ret_3m', 'ret_6m', 'macd_hist',
  'above_sma50', 'sma50_gt_200', 'above_st', 'wk_hi_high', 'wk_hi_low',
  'bb_upper', 'bb_lower', 'high_30d', 'value_cr',
].join(',');

const BB_SQUEEZE_MAX_WIDTH_PCT = 7.0;   // must match run_sbs in screen_stocks.py

/**
 * Add fields the engines compute in Python but that do not exist as columns.
 * Keeping this identical to the Python is what makes the Control Room preview
 * trustworthy — a derived field that drifts would silently mispredict which
 * stocks a gate admits.
 */
function deriveFields(rows: Record<string, unknown>[]): Record<string, unknown>[] {
  for (const r of rows) {
    const up = Number(r.bb_upper ?? 0);
    const lo = Number(r.bb_lower ?? 0);
    const close = Number(r.close ?? 0);
    const widthPct = close > 0 && up > lo ? ((up - lo) / close) * 100 : 999;
    r.bb_width_pct = Number.isFinite(widthPct) ? Number(widthPct.toFixed(2)) : null;
    r.bb_squeeze = widthPct < BB_SQUEEZE_MAX_WIDTH_PCT;
  }
  return rows;
}

export async function GET() {
  try {
    const sb = getServiceClient();

    const { data: latest } = await sb
      .from('stock_data_daily')
      .select('date')
      .order('date', { ascending: false })
      .limit(1);
    const tradeDate = latest?.[0]?.date;
    if (!tradeDate) {
      return NextResponse.json({ message: 'stock_data_daily is empty' }, { status: 404 });
    }

    const stocks: unknown[] = [];
    for (let page = 0; page < 10; page++) {
      const { data, error } = await sb
        .from('stock_data_daily')
        .select(FIELDS)
        .eq('date', tradeDate)
        .range(page * 1000, page * 1000 + 999);
      if (error) throw error;
      // Double cast via unknown: supabase-js types a .select() with a runtime
      // string as GenericStringError[], which does not overlap our row shape.
      stocks.push(...deriveFields((data ?? []) as unknown as Record<string, unknown>[]));
      if (!data || data.length < 1000) break;
    }

    // sector_strength for the SAME date. A single explicit date, never an
    // unfiltered ordered grab — mixing dates here is exactly the bug that made
    // screen_stocks read ranks from three different days.
    let sectorRows: { sector: string; rank: number }[] = [];
    const { data: ss } = await sb
      .from('sector_strength')
      .select('sector,rank,stock_count,composite_score,rotation_score,rank_delta_5d,sector_state,qualified')
      .eq('date', tradeDate);
    sectorRows = (ss ?? []) as never;

    if (!sectorRows.length) {
      const { data: ls } = await sb
        .from('sector_strength')
        .select('date')
        .order('date', { ascending: false })
        .limit(1);
      if (ls?.[0]?.date) {
        const { data: ss2 } = await sb
          .from('sector_strength')
          .select('sector,rank,stock_count,composite_score,rotation_score,rank_delta_5d,sector_state,qualified')
          .eq('date', ls[0].date);
        sectorRows = (ss2 ?? []) as never;
      }
    }

    const sectorRank: Record<string, number> = {};
    for (const r of sectorRows) if (r.rank) sectorRank[r.sector] = r.rank;

    return NextResponse.json({
      trade_date: tradeDate,
      count: stocks.length,
      stocks,
      sectorRank,
      sectors: sectorRows,
    });
  } catch (e) {
    return NextResponse.json({ message: (e as Error).message }, { status: 500 });
  }
}
