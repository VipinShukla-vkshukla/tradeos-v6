'use client';

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import { format, parseISO } from 'date-fns';
import type { WeeklyPerformance } from '@/types/database';

interface WinRateTrendChartProps {
  data: WeeklyPerformance[];
}

export function WinRateTrendChart({ data }: WinRateTrendChartProps) {
  // Transform data for chart
  const chartData = data.map((item) => ({
    ...item,
    week: format(parseISO(item.week_start), 'MMM d'),
    winRatePercent: item.win_rate * 100,
  }));

  // Calculate average win rate
  const avgWinRate = data.reduce((sum, item) => sum + item.win_rate, 0) / data.length * 100;

  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={chartData}
          margin={{ top: 10, right: 10, left: 0, bottom: 0 }}
        >
          <CartesianGrid 
            strokeDasharray="3 3" 
            stroke="var(--border-default)" 
            opacity={0.5}
          />
          <XAxis
            dataKey="week"
            stroke="var(--text-muted)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            stroke="var(--text-muted)"
            fontSize={11}
            tickLine={false}
            axisLine={false}
            domain={[40, 100]}
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
            itemStyle={{ color: 'var(--text-secondary)' }}
            formatter={(value: number) => [`${value.toFixed(1)}%`, 'Win Rate']}
          />
          <ReferenceLine
            y={avgWinRate}
            stroke="var(--text-muted)"
            strokeDasharray="5 5"
            label={{
              value: `Avg: ${avgWinRate.toFixed(1)}%`,
              position: 'right',
              fill: 'var(--text-muted)',
              fontSize: 10,
            }}
          />
          <ReferenceLine
            y={60}
            stroke="var(--warning)"
            strokeDasharray="3 3"
            opacity={0.5}
          />
          <Line
            type="monotone"
            dataKey="winRatePercent"
            stroke="var(--chart-1)"
            strokeWidth={2}
            dot={false}
            activeDot={{
              r: 4,
              fill: 'var(--chart-1)',
              stroke: 'var(--bg-panel)',
              strokeWidth: 2,
            }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export default WinRateTrendChart;
