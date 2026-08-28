'use client';

import { useMemo } from 'react';
import { Trophy, TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { formatCurrency, formatPercent } from '@/lib/formatters';
import type { EngineStats } from '@/types/database';

interface EngineLeaderboardProps {
  data: EngineStats[];
  onSelect?: (engine: EngineStats) => void;
}

function getRankBadge(rank: number) {
  if (rank === 1) return <Trophy className="h-4 w-4 text-yellow-500" />;
  if (rank === 2) return <Trophy className="h-4 w-4 text-gray-400" />;
  if (rank === 3) return <Trophy className="h-4 w-4 text-amber-600" />;
  return <span className="text-xs text-muted-foreground w-4 text-center">{rank}</span>;
}

function getTrendIcon(value: number) {
  if (value > 0) return <TrendingUp className="h-3.5 w-3.5 text-profit" />;
  if (value < 0) return <TrendingDown className="h-3.5 w-3.5 text-loss" />;
  return <Minus className="h-3.5 w-3.5 text-muted-foreground" />;
}

export function EngineLeaderboard({ data, onSelect }: EngineLeaderboardProps) {
  // Sort by a composite score (win rate * 0.4 + profit factor * 0.3 + sharpe * 0.3)
  const rankedData = useMemo(() => {
    return [...data]
      .map((engine) => ({
        ...engine,
        score: engine.win_rate * 40 + 
               (engine.sharpe_ratio || 1) * 30 + 
               (engine.avg_pnl_percent > 0 ? 30 : 0),
      }))
      .sort((a, b) => b.score - a.score)
      .map((engine, index) => ({ ...engine, rank: index + 1 }));
  }, [data]);

  return (
    <div className="space-y-1">
      {/* Header */}
      <div className="grid grid-cols-12 gap-2 px-2 py-1.5 text-xs font-medium text-muted-foreground border-b border-border">
        <div className="col-span-1">#</div>
        <div className="col-span-4">Engine</div>
        <div className="col-span-2 text-right">Win Rate</div>
        <div className="col-span-2 text-right">Avg P&L</div>
        <div className="col-span-3 text-right">Total P&L</div>
      </div>

      {/* Rows */}
      {rankedData.map((engine) => (
        <div
          key={engine.engine_name}
          onClick={() => onSelect?.(engine)}
          className={`grid grid-cols-12 gap-2 px-2 py-2.5 text-sm hover:bg-panel-hover rounded-md transition-colors ${onSelect ? 'cursor-pointer' : ''}`}
          title={onSelect ? 'View factor breakdown — what separates winners from losers' : undefined}
        >
          <div className="col-span-1 flex items-center">
            {getRankBadge(engine.rank)}
          </div>
          <div className="col-span-4 flex flex-col">
            <span className="font-medium text-foreground truncate">
              {engine.engine_name}
            </span>
            <span className="text-xs text-muted-foreground">
              {engine.total_signals} signals
            </span>
          </div>
          <div className="col-span-2 flex items-center justify-end gap-1">
            <span className={engine.win_rate >= 0.6 ? 'text-profit' : engine.win_rate >= 0.5 ? 'text-foreground' : 'text-loss'}>
              {formatPercent(engine.win_rate * 100, { showSign: false })}
            </span>
          </div>
          <div className="col-span-2 flex items-center justify-end gap-1">
            {getTrendIcon(engine.avg_pnl_percent)}
            <span className={engine.avg_pnl_percent >= 0 ? 'text-profit' : 'text-loss'}>
              {formatPercent(engine.avg_pnl_percent)}
            </span>
          </div>
          <div className="col-span-3 text-right">
            <span className={engine.total_pnl >= 0 ? 'text-profit' : 'text-loss'}>
              {formatCurrency(engine.total_pnl, { compact: true, showSign: true })}
            </span>
          </div>
        </div>
      ))}

      {/* Summary row */}
      <div className="grid grid-cols-12 gap-2 px-2 py-2.5 text-sm font-medium border-t border-border mt-2">
        <div className="col-span-1" />
        <div className="col-span-4 text-muted-foreground">Total</div>
        <div className="col-span-2 text-right text-muted-foreground">
          {formatPercent(
            (data.reduce((sum, e) => sum + e.win_rate, 0) / data.length) * 100,
            { showSign: false }
          )}
        </div>
        <div className="col-span-2 text-right text-muted-foreground">
          {formatPercent(
            data.reduce((sum, e) => sum + e.avg_pnl_percent, 0) / data.length
          )}
        </div>
        <div className="col-span-3 text-right">
          <span className={data.reduce((sum, e) => sum + e.total_pnl, 0) >= 0 ? 'text-profit' : 'text-loss'}>
            {formatCurrency(
              data.reduce((sum, e) => sum + e.total_pnl, 0),
              { compact: true, showSign: true }
            )}
          </span>
        </div>
      </div>
    </div>
  );
}

export default EngineLeaderboard;
