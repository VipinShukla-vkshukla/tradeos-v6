'use client';

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { EngineStats } from '@/types/database';

interface SignalTypeBreakdownProps {
  data: EngineStats[];
}

export function SignalTypeBreakdown({ data }: SignalTypeBreakdownProps) {
  // Transform data for grouped bar chart
  const chartData = data.map((engine) => ({
    name: engine.engine_name.split(' ')[0], // Shorten names
    fullName: engine.engine_name,
    winRate: Math.round(engine.win_rate * 100),
    avgPnl: engine.avg_pnl_percent,
    totalSignals: engine.total_signals,
  }));

  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={chartData}
          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
        >
          <CartesianGrid
            strokeDasharray="3 3"
            stroke="var(--border-default)"
            opacity={0.5}
            vertical={false}
          />
          <XAxis
            dataKey="name"
            stroke="var(--text-muted)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
          />
          <YAxis
            yAxisId="left"
            stroke="var(--text-muted)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            domain={[0, 100]}
            tickFormatter={(value) => `${value}%`}
          />
          <YAxis
            yAxisId="right"
            orientation="right"
            stroke="var(--text-muted)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value) => `${value}%`}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: 'var(--bg-tooltip)',
              border: '1px solid var(--border-default)',
              borderRadius: 'var(--radius-md)',
              fontSize: '12px',
            }}
            labelStyle={{ color: 'var(--text-primary)', fontWeight: 500 }}
            formatter={(value: number, name: string) => {
              if (name === 'winRate') return [`${value}%`, 'Win Rate'];
              if (name === 'avgPnl') return [`${value.toFixed(1)}%`, 'Avg P&L'];
              return [value, name];
            }}
            labelFormatter={(label, payload) => {
              const item = payload?.[0]?.payload;
              return item?.fullName || label;
            }}
          />
          <Legend
            verticalAlign="top"
            height={36}
            formatter={(value) => {
              if (value === 'winRate') return 'Win Rate';
              if (value === 'avgPnl') return 'Avg P&L %';
              return value;
            }}
          />
          <Bar
            yAxisId="left"
            dataKey="winRate"
            fill="var(--chart-1)"
            radius={[4, 4, 0, 0]}
            maxBarSize={40}
          />
          <Bar
            yAxisId="right"
            dataKey="avgPnl"
            fill="var(--chart-2)"
            radius={[4, 4, 0, 0]}
            maxBarSize={40}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export default SignalTypeBreakdown;
