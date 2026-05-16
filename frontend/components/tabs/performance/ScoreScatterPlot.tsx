'use client';

import {
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ZAxis,
  ReferenceLine,
  Cell,
} from 'recharts';
import type { EngineStats } from '@/types/database';

interface ScoreScatterPlotProps {
  data: EngineStats[];
}

// Generate mock scatter data from engine stats
function generateScatterData(engines: EngineStats[]) {
  const points: Array<{
    x: number; // conviction score
    y: number; // actual P&L %
    engine: string;
    color: string;
  }> = [];

  const colors = [
    'var(--chart-1)',
    'var(--chart-2)',
    'var(--chart-3)',
    'var(--chart-4)',
    'var(--chart-5)',
  ];

  engines.forEach((engine, engineIndex) => {
    // Generate points based on engine characteristics
    const baseWinRate = engine.win_rate;
    const basePnl = engine.avg_pnl_percent;
    
    for (let i = 0; i < engine.executed_signals; i++) {
      // Conviction score correlates somewhat with win rate
      const convictionBase = 50 + baseWinRate * 30;
      const conviction = Math.max(30, Math.min(95, convictionBase + (Math.random() - 0.5) * 40));
      
      // P&L has noise but correlates with conviction
      const isWin = Math.random() < baseWinRate;
      const pnl = isWin 
        ? basePnl * (0.5 + Math.random()) 
        : -Math.abs(basePnl) * (0.3 + Math.random() * 0.7);

      points.push({
        x: conviction,
        y: pnl,
        engine: engine.engine_name,
        color: colors[engineIndex % colors.length],
      });
    }
  });

  return points;
}

export function ScoreScatterPlot({ data }: ScoreScatterPlotProps) {
  const scatterData = generateScatterData(data);

  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--border-default)"
            opacity={0.5}
          />
          <XAxis
            type="number"
            dataKey="x"
            name="Conviction"
            stroke="var(--text-muted)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            domain={[20, 100]}
            tickFormatter={(value) => `${value}%`}
            label={{
              value: 'Conviction Score',
              position: 'bottom',
              offset: -5,
              fill: 'var(--text-muted)',
              fontSize: 10,
            }}
          />
          <YAxis
            type="number"
            dataKey="y"
            name="P&L"
            stroke="var(--text-muted)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            domain={[-15, 15]}
            tickFormatter={(value) => `${value}%`}
            label={{
              value: 'Actual P&L %',
              angle: -90,
              position: 'insideLeft',
              fill: 'var(--text-muted)',
              fontSize: 10,
            }}
          />
          <ZAxis range={[20, 60]} />
          <Tooltip
            cursor={{ strokeDasharray: '3 3' }}
            contentStyle={{
              backgroundColor: 'var(--bg-tooltip)',
              border: '1px solid var(--border-default)',
              borderRadius: 'var(--radius-md)',
              fontSize: '12px',
            }}
            formatter={(value: number, name: string) => {
              if (name === 'x') return [`${value.toFixed(0)}%`, 'Conviction'];
              if (name === 'y') return [`${value.toFixed(1)}%`, 'P&L'];
              return [value, name];
            }}
            labelFormatter={(_, payload) => {
              const item = payload?.[0]?.payload;
              return item?.engine || '';
            }}
          />
          <ReferenceLine
            y={0}
            stroke="var(--border-default)"
            strokeDasharray="3 3"
          />
          <ReferenceLine
            x={60}
            stroke="var(--warning)"
            strokeDasharray="3 3"
            opacity={0.5}
          />
          <Scatter data={scatterData}>
            {scatterData.map((entry, index) => (
              <Cell
                key={`cell-${index}`}
                fill={entry.y >= 0 ? 'var(--profit-green)' : 'var(--loss-red)'}
                fillOpacity={0.6}
              />
            ))}
          </Scatter>
        </ScatterChart>
      </ResponsiveContainer>

      {/* Legend */}
      <div className="flex items-center justify-center gap-4 mt-2 text-xs text-muted-foreground">
        <div className="flex items-center gap-1">
          <div className="h-2 w-2 rounded-full bg-profit opacity-60" />
          <span>Profitable</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-2 w-2 rounded-full bg-loss opacity-60" />
          <span>Loss</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="h-px w-4 bg-warning opacity-50" style={{ borderTop: '1px dashed' }} />
          <span>60% threshold</span>
        </div>
      </div>
    </div>
  );
}

export default ScoreScatterPlot;
