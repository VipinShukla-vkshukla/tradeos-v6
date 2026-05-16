// Mock data for TradeOS Intelligence Console

export const mockPerformanceMetrics = {
  daily: Array.from({ length: 90 }, (_, i) => {
    const date = new Date()
    date.setDate(date.getDate() - (90 - i))
    return {
      date: date.toISOString().split('T')[0],
      win_rate_overall: 55 + Math.random() * 20,
      win_rate_prime: 60 + Math.random() * 18,
      avg_fwd_return: 2 + Math.random() * 4,
      score_return_r: 0.2 + Math.random() * 0.25,
      signals_count: Math.floor(Math.random() * 20) + 5,
      prime_count: Math.floor(Math.random() * 8) + 1,
      breakout_count: Math.floor(Math.random() * 6),
      outcome_coverage_pct: 30 + Math.random() * 50,
      ai_high_conv_wr: 65 + Math.random() * 15,
      ai_medium_conv_wr: 50 + Math.random() * 15,
      ai_low_conv_wr: 40 + Math.random() * 15,
    }
  }),
  weekly: Array.from({ length: 26 }, (_, i) => {
    const date = new Date()
    date.setDate(date.getDate() - (26 - i) * 7)
    return {
      date: date.toISOString().split('T')[0],
      win_rate_overall: 55 + Math.random() * 20,
      win_rate_prime: 60 + Math.random() * 18,
      avg_fwd_return: 2 + Math.random() * 4,
      score_return_r: 0.2 + Math.random() * 0.25,
      signals_count: Math.floor(Math.random() * 80) + 20,
      prime_count: Math.floor(Math.random() * 30) + 5,
      breakout_count: Math.floor(Math.random() * 25),
      outcome_coverage_pct: 40 + Math.random() * 50,
      ai_high_conv_wr: 65 + Math.random() * 15,
      ai_medium_conv_wr: 50 + Math.random() * 15,
      ai_low_conv_wr: 40 + Math.random() * 15,
    }
  }),
}

export const mockEngineStats = [
  { rank: 1, engine: 'momentum_rsi', win_rate: 72.3, count: 156, avg_return: 4.8, vs_baseline: 8.2, trend: 'up' },
  { rank: 2, engine: 'breakout_vol', win_rate: 65.1, count: 203, avg_return: 3.9, vs_baseline: 1.0, trend: 'up' },
  { rank: 3, engine: 'mean_reversion', win_rate: 58.4, count: 178, avg_return: 3.2, vs_baseline: -5.7, trend: 'down' },
  { rank: 4, engine: 'trend_following', win_rate: 61.2, count: 134, avg_return: 3.5, vs_baseline: -2.9, trend: 'up' },
  { rank: 5, engine: 'volume_spike', win_rate: 45.2, count: 89, avg_return: 1.8, vs_baseline: -18.9, trend: 'down' },
]

export const mockScoreCorrelation = Array.from({ length: 120 }, () => ({
  score_adjusted: Math.floor(Math.random() * 60) + 40,
  max_fwd_return: (Math.random() - 0.3) * 15,
  signal_type: ['PRIME', 'BREAKOUT', 'STAGED', 'REENTRY'][Math.floor(Math.random() * 4)],
}))

export const mockOpenPositions = [
  { id: 1, symbol: 'TIMKEN', entry_date: '2026-05-04', entry_price: 3595, actual_price: 3712, strategy: 'PRIME', stop_loss: 3420, target: 3950, regime: 'RISK ON', ai_conviction: 'HIGH', days_held: 8, notes: '' },
  { id: 2, symbol: 'RELIANCE', entry_date: '2026-05-06', entry_price: 2845, actual_price: 2790, strategy: 'BREAKOUT', stop_loss: 2700, target: 3100, regime: 'NEUTRAL', ai_conviction: 'MEDIUM', days_held: 6, notes: 'Watch volume' },
  { id: 3, symbol: 'TCS', entry_date: '2026-05-01', entry_price: 4120, actual_price: 4280, strategy: 'PRIME', stop_loss: 3950, target: 4500, regime: 'RISK ON', ai_conviction: 'HIGH', days_held: 11, notes: '' },
  { id: 4, symbol: 'INFY', entry_date: '2026-05-08', entry_price: 1890, actual_price: 1885, strategy: 'STAGED', stop_loss: 1800, target: 2050, regime: 'RECOVERING', ai_conviction: 'LOW', days_held: 4, notes: '' },
  { id: 5, symbol: 'HDFCBANK', entry_date: '2026-05-03', entry_price: 1650, actual_price: 1710, strategy: 'REENTRY', stop_loss: 1580, target: 1800, regime: 'RISK ON', ai_conviction: 'MEDIUM', days_held: 9, notes: 'Strong close' },
]

export const mockClosedPositions = [
  { id: 101, symbol: 'SBIN', entry_date: '2026-04-15', exit_date: '2026-04-28', entry_price: 780, exit_price: 845, pnl_pct: 8.3, pnl_abs: 6500, reason: 'TARGET_HIT', strategy: 'PRIME', days: 13 },
  { id: 102, symbol: 'ITC', entry_date: '2026-04-20', exit_date: '2026-05-02', entry_price: 420, exit_price: 405, pnl_pct: -3.6, pnl_abs: -1500, reason: 'STOP_HIT', strategy: 'BREAKOUT', days: 12 },
  { id: 103, symbol: 'BHARTIARTL', entry_date: '2026-04-10', exit_date: '2026-04-25', entry_price: 1120, exit_price: 1205, pnl_pct: 7.6, pnl_abs: 8500, reason: 'TARGET_HIT', strategy: 'PRIME', days: 15 },
  { id: 104, symbol: 'LT', entry_date: '2026-04-22', exit_date: '2026-05-05', entry_price: 3650, exit_price: 3580, pnl_pct: -1.9, pnl_abs: -7000, reason: 'MANUAL', strategy: 'STAGED', days: 13 },
  { id: 105, symbol: 'ASIANPAINT', entry_date: '2026-04-05', exit_date: '2026-04-22', entry_price: 3100, exit_price: 3350, pnl_pct: 8.1, pnl_abs: 25000, reason: 'TARGET_HIT', strategy: 'PRIME', days: 17 },
  { id: 106, symbol: 'HCLTECH', entry_date: '2026-04-18', exit_date: '2026-05-01', entry_price: 1520, exit_price: 1480, pnl_pct: -2.6, pnl_abs: -4000, reason: 'REGIME_CHANGE', strategy: 'REENTRY', days: 13 },
  { id: 107, symbol: 'WIPRO', entry_date: '2026-04-12', exit_price: 520, exit_date: '2026-04-26', entry_price: 480, pnl_pct: 8.3, pnl_abs: 4000, reason: 'TARGET_HIT', strategy: 'BREAKOUT', days: 14 },
  { id: 108, symbol: 'AXISBANK', entry_date: '2026-04-25', exit_date: '2026-05-07', entry_price: 1100, exit_price: 1080, pnl_pct: -1.8, pnl_abs: -2000, reason: 'STOP_HIT', strategy: 'PRIME', days: 12 },
]

export const mockMarketIntel = Array.from({ length: 15 }, (_, i) => {
  const date = new Date()
  date.setDate(date.getDate() - (15 - i) * 2)
  const tones = ['FULL', 'HALF', 'QUARTER', 'AVOID']
  const tone = tones[Math.floor(Math.random() * tones.length)]
  return {
    id: i + 1,
    created_at: date.toISOString(),
    tone,
    summary: `Market showing ${tone === 'FULL' ? 'strong breadth expansion' : tone === 'HALF' ? 'mixed signals with sector rotation' : tone === 'QUARTER' ? 'defensive posture warranted' : 'elevated risk, reduce exposure'}. Nifty ${tone === 'FULL' ? 'breaking above key resistance' : 'consolidating near support'}. FIIs ${Math.random() > 0.5 ? 'net buyers' : 'cautious'} in cash segment.`,
  }
})

export const mockAIProviders = [
  { provider: 'deepseek', win_rate: 68.5, count: 245 },
  { provider: 'claude', win_rate: 64.2, count: 189 },
  { provider: 'gemini', win_rate: 58.7, count: 156 },
  { provider: 'gpt4', win_rate: 61.3, count: 134 },
]

export const mockSelfImprovement = [
  { id: 1, recurrence_count: 4, confidence: 'HIGH', sectors: 'industrials', rule: 'When sector is EXTENDED, do not upgrade near-miss stocks from that sector — only take the top gate-passed stock', first_seen: '2026-04-28', last_seen: '2026-05-08' },
  { id: 2, recurrence_count: 3, confidence: 'MEDIUM', sectors: 'technology', rule: 'Breakout signals with ADX < 20 should be downgraded to STAGED regardless of score', first_seen: '2026-04-20', last_seen: '2026-05-06' },
  { id: 3, recurrence_count: 2, confidence: 'HIGH', sectors: 'financials', rule: 'Banking stocks require 2x volume confirmation vs other sectors for PRIME designation', first_seen: '2026-04-25', last_seen: '2026-05-05' },
]

export const mockLessons = [
  { id: 1, scenario: 'Low ADX breakout', rule: 'When ADX is below 20, breakout signals have <40% win rate. Downgrade to STAGED or skip.', confidence: 85, applied: true, strategy: 'BREAKOUT' },
  { id: 2, scenario: 'Extended sector entry', rule: 'Entering stocks from sectors >15% above 50DMA leads to mean reversion losses.', confidence: 78, applied: true, strategy: 'PRIME' },
  { id: 3, scenario: 'Earnings gap fade', rule: 'Shorting earnings gaps >5% in large caps within 2 days has 62% win rate.', confidence: 62, applied: false, strategy: 'MEAN_REVERSION' },
  { id: 4, scenario: 'FII + RSI confluence', rule: 'When FII buying coincides with RSI 50-60 bounce, PRIME signals show +12pp higher WR.', confidence: 91, applied: true, strategy: 'PRIME' },
  { id: 5, scenario: 'Volume dry-up', rule: 'Positions held through volume dry-up periods (>5 days below 20DMA vol) show 2x stop hit rate.', confidence: 73, applied: true, strategy: 'ALL' },
]

export const mockBrainRuns = [
  { run_id: 'brain-2026-05-09-1402', run_date: '2026-05-09T14:02:00Z', coverage_pct: 72, proposals_generated: 3, auto_applied: 1, elapsed_sec: 45, status: 'completed' },
  { run_id: 'brain-2026-05-08-1830', run_date: '2026-05-08T18:30:00Z', coverage_pct: 68, proposals_generated: 2, auto_applied: 0, elapsed_sec: 38, status: 'completed' },
  { run_id: 'brain-2026-05-07-1015', run_date: '2026-05-07T10:15:00Z', coverage_pct: 65, proposals_generated: 4, auto_applied: 2, elapsed_sec: 52, status: 'completed' },
  { run_id: 'brain-2026-05-06-0920', run_date: '2026-05-06T09:20:00Z', coverage_pct: 70, proposals_generated: 0, auto_applied: 0, elapsed_sec: 30, status: 'completed' },
  { run_id: 'brain-2026-05-05-1630', run_date: '2026-05-05T16:30:00Z', coverage_pct: 63, proposals_generated: 2, auto_applied: 1, elapsed_sec: 41, status: 'completed' },
]

export const mockProposals = [
  { id: 42, type: 'THRESHOLD_CHANGE', target: 'signal_threshold_prime_rsi_min', current_value: '48', new_value: '52', confidence: 82, wr_delta: 6.3, source: 'quant', status: 'PENDING', rationale: 'Backtesting shows 52 RSI min filters out 23% of false breakouts while retaining 94% of true signals.' },
  { id: 43, type: 'ENGINE_WEIGHT', target: 'regime_engine_weights', current_value: '{"momentum":0.3,"breakout":0.4}', new_value: '{"momentum":0.35,"breakout":0.35}', confidence: 71, wr_delta: 4.1, source: 'hybrid', status: 'PENDING', rationale: 'Rebalancing weights to favor momentum in current RISK ON regime based on 90-day rolling performance.' },
  { id: 44, type: 'SCRIPT_PATCH', target: 'compute/screen_stocks.py', current_value: 'v1.2', new_value: 'v1.3', confidence: 70, wr_delta: null, source: 'llm', status: 'PENDING', rationale: 'Add sector-relative strength filter to reduce false breakouts in weak sectors.', diff: '--- a/compute/screen_stocks.py\n+++ b/compute/screen_stocks.py\n@@ -45,6 +45,9 @@ def screen_stocks(universe):\n     for stock in universe:\n         score = compute_score(stock)\n+        if stock.sector_rsi < 45:\n+            score *= 0.8  # penalize weak sectors\n+\n         if score > threshold:\n             results.append(stock)' },
  { id: 45, type: 'INSIGHT', target: 'FII+RSI confluence', current_value: 'observation', new_value: 'observation', confidence: 75, wr_delta: null, source: 'llm', status: 'PENDING', rationale: 'FII buying days with RSI 50-60 bounce show 12pp higher win rate for PRIME signals. Consider adding as composite signal factor.' },
]

export const mockConfigChanges = [
  { id: 1, changed_at: '2026-05-09T14:02:00Z', key: 'signal_threshold_prime_rsi_daily_min', old_value: '48', new_value: '52', changed_by: 'auto_apply', proposal_id: 38 },
  { id: 2, changed_at: '2026-05-08T18:30:00Z', key: 'regime_engine_weights', old_value: '{"momentum":0.3}', new_value: '{"momentum":0.35}', changed_by: 'manual (cli)', proposal_id: 35 },
  { id: 3, changed_at: '2026-05-07T10:15:00Z', key: 'msl_final_score_weights', old_value: '{"technical":0.5}', new_value: '{"technical":0.55}', changed_by: 'dashboard_manual', proposal_id: 32 },
  { id: 4, changed_at: '2026-05-05T16:30:00Z', key: 'signal_threshold_breakout_adx_min', old_value: '20', new_value: '22', changed_by: 'auto_apply', proposal_id: 28 },
  { id: 5, changed_at: '2026-05-03T09:00:00Z', key: 'brain_auto_apply_threshold', old_value: '75', new_value: '80', changed_by: 'dashboard_manual', proposal_id: null },
]

export const mockSystemConfig = [
  { key: 'signal_threshold_prime_rsi_daily_min', value: '52', prefix: 'signal_threshold', last_changed_by: 'auto_apply', last_changed_at: '2026-05-09T14:02:00Z' },
  { key: 'signal_threshold_prime_rsi_daily_max', value: '82', prefix: 'signal_threshold', last_changed_by: 'system', last_changed_at: '2026-04-01T00:00:00Z' },
  { key: 'signal_threshold_breakout_adx_min', value: '22', prefix: 'signal_threshold', last_changed_by: 'auto_apply', last_changed_at: '2026-05-05T16:30:00Z' },
  { key: 'signal_threshold_breakout_volume_ratio', value: '1.5', prefix: 'signal_threshold', last_changed_by: 'system', last_changed_at: '2026-04-01T00:00:00Z' },
  { key: 'brain_auto_apply_threshold', value: '80', prefix: 'brain', last_changed_by: 'dashboard_manual', last_changed_at: '2026-05-03T09:00:00Z' },
  { key: 'brain_max_proposals_per_run', value: '5', prefix: 'brain', last_changed_by: 'system', last_changed_at: '2026-04-01T00:00:00Z' },
  { key: 'screener_min_market_cap_cr', value: '500', prefix: 'screener', last_changed_by: 'system', last_changed_at: '2026-04-01T00:00:00Z' },
  { key: 'screener_universe_nse500', value: 'true', prefix: 'screener', last_changed_by: 'system', last_changed_at: '2026-04-01T00:00:00Z' },
  { key: 'regime_engine_weights', value: '{"momentum":0.35,"breakout":0.35,"mean_reversion":0.2,"trend_following":0.1}', prefix: 'regime', last_changed_by: 'manual (cli)', last_changed_at: '2026-05-08T18:30:00Z' },
  { key: 'msl_final_score_weights', value: '{"technical":0.55,"fundamental":0.25,"ai":0.2}', prefix: 'msl', last_changed_by: 'dashboard_manual', last_changed_at: '2026-05-07T10:15:00Z' },
]

export const mockSignals = Array.from({ length: 50 }, (_, i) => {
  const date = new Date()
  date.setDate(date.getDate() - Math.floor(Math.random() * 90))
  const types = ['PRIME', 'BREAKOUT', 'STAGED', 'REENTRY']
  const regimes = ['RISK ON', 'NEUTRAL', 'RECOVERING', 'RISK OFF']
  const outcomes = ['WIN', 'LOSS', 'NEUTRAL', null]
  return {
    id: i + 1,
    date: date.toISOString().split('T')[0],
    symbol: ['TIMKEN', 'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'SBIN', 'ITC', 'BHARTIARTL', 'LT', 'ASIANPAINT'][Math.floor(Math.random() * 10)],
    type: types[Math.floor(Math.random() * 4)],
    score: Math.floor(Math.random() * 40) + 60,
    rsi: Math.floor(Math.random() * 40) + 40,
    adx: Math.floor(Math.random() * 30) + 15,
    vol_ratio: (Math.random() * 2 + 0.5).toFixed(2),
    regime: regimes[Math.floor(Math.random() * 4)],
    ai_conviction: ['HIGH', 'MEDIUM', 'LOW'][Math.floor(Math.random() * 3)],
    outcome: outcomes[Math.floor(Math.random() * 4)],
  }
})
