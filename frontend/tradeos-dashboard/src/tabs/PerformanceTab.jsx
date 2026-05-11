import React, { useState, useMemo } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  BarChart, Bar, Legend, ScatterChart, Scatter, ZAxis, Cell, ReferenceLine
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card'
import { DataGuard } from '../components/DataGuard'
import { Badge } from '../components/ui/Badge'
import {
  mockPerformanceMetrics, mockEngineStats, mockScoreCorrelation
} from '../data/mockData'
import { formatPercent, formatDelta, formatDate, cn } from '../lib/utils'
import { ArrowUp, ArrowDown, TrendingUp, AlertTriangle } from 'lucide-react'

function KPICard({ title, value, delta, deltaLabel, warning }) {
  const isPositive = delta >= 0
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="text-sm text-text-muted mb-1">{title}</div>
        <div className="text-2xl font-bold text-text-primary">{value}</div>
        {delta !== undefined && (
          <div className={cn('flex items-center gap-1 text-sm mt-1', isPositive ? 'text-trade-green' : 'text-trade-red')}>
            {isPositive ? <ArrowUp size={14} /> : <ArrowDown size={14} />}
            <span>{formatDelta(delta)}</span>
            <span className="text-text-muted ml-1">{deltaLabel}</span>
          </div>
        )}
        {warning && (
          <div className="flex items-center gap-1 text-trade-yellow text-xs mt-2">
            <AlertTriangle size={12} />
            {warning}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function PerformanceTab() {
  const [grain] = useState('weekly')
  const data = mockPerformanceMetrics[grain]
  const latest = data[data.length - 1]
  const prev = data[data.length - 2]

  const winRateDelta = latest.win_rate_overall - prev.win_rate_overall
  const avgReturnDelta = latest.avg_fwd_return - prev.avg_fwd_return
  const rDelta = latest.score_return_r - prev.score_return_r

  const signalTypeData = useMemo(() => {
    const types = ['PRIME', 'BREAKOUT', 'STAGED', 'REENTRY']
    return types.map(type => {
      const filtered = mockScoreCorrelation.filter(d => d.signal_type === type)
      const wins = filtered.filter(d => d.max_fwd_return > 0).length
      return {
        type,
        winRate: filtered.length > 0 ? (wins / filtered.length) * 100 : 0,
        count: filtered.length,
      }
    })
  }, [])

  const chartData = data.map(d => ({
    ...d,
    dateLabel: formatDate(d.date),
  }))

  const pearsonR = latest.score_return_r

  return (
    <div className="space-y-6">
      {/* KPI Row */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard
          title="Win Rate (D+10)"
          value={formatPercent(latest.win_rate_overall)}
          delta={winRateDelta}
          deltaLabel="vs last week"
        />
        <KPICard
          title="Avg Fwd Return"
          value={`+${formatPercent(latest.avg_fwd_return)}`}
          delta={avgReturnDelta}
          deltaLabel="vs prev"
        />
        <KPICard
          title="Score→Return r"
          value={`r=${pearsonR.toFixed(3)}`}
          delta={rDelta}
          deltaLabel="improving"
          warning={pearsonR < 0.15 ? "Score not predicting returns" : null}
        />
        <KPICard
          title="Signals Today"
          value={`${latest.signals_count}`}
          deltaLabel={`${latest.prime_count} PRIME, ${latest.breakout_count} BREAKOUT`}
        />
      </div>

      {pearsonR < 0.15 && (
        <div className="bg-trade-red/10 border border-trade-red/20 rounded-lg p-3 text-trade-red text-sm flex items-center gap-2">
          <AlertTriangle size={16} />
          Score not predicting returns — review scoring model
        </div>
      )}

      {/* Win Rate Trend */}
      <Card>
        <CardHeader>
          <CardTitle>Win Rate Trend (26 weeks)</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={chartData} minRows={8} name="weekly performance">
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis dataKey="dateLabel" stroke="#6b7280" fontSize={12} />
                  <YAxis domain={[0, 100]} stroke="#6b7280" fontSize={12} tickFormatter={v => `${v}%`} />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }}
                    labelStyle={{ color: '#f9fafb' }}
                  />
                  <Line type="monotone" dataKey="win_rate_overall" stroke="#3b82f6" strokeWidth={2} dot={false} name="Overall" />
                  <Line type="monotone" dataKey="win_rate_prime" stroke="#10b981" strokeWidth={2} dot={false} strokeOpacity={0.7} name="PRIME" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Signal Type Breakdown */}
      <Card>
        <CardHeader>
          <CardTitle>Signal Type Breakdown</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={signalTypeData} minRows={1} name="signal types">
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={signalTypeData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis dataKey="type" stroke="#6b7280" fontSize={12} />
                  <YAxis yAxisId="left" domain={[0, 100]} stroke="#6b7280" fontSize={12} tickFormatter={v => `${v}%`} />
                  <YAxis yAxisId="right" orientation="right" stroke="#6b7280" fontSize={12} />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }}
                  />
                  <Legend />
                  <Bar yAxisId="left" dataKey="winRate" name="Win Rate %" fill="#3b82f6" radius={[4, 4, 0, 0]}>
                    {signalTypeData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fillOpacity={entry.count < 10 ? 0.4 : 1} />
                    ))}
                  </Bar>
                  <Bar yAxisId="right" dataKey="count" name="Count" fill="#6b7280" radius={[4, 4, 0, 0]} opacity={0.5} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Engine Leaderboard */}
      <Card>
        <CardHeader>
          <CardTitle>Engine Leaderboard</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={mockEngineStats} minRows={3} name="engine stats">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border-custom text-text-muted">
                    <th className="text-left py-2 px-3">Rank</th>
                    <th className="text-left py-2 px-3">Engine</th>
                    <th className="text-left py-2 px-3">Win Rate</th>
                    <th className="text-left py-2 px-3">Count</th>
                    <th className="text-left py-2 px-3">Avg Return</th>
                    <th className="text-left py-2 px-3">vs Baseline</th>
                    <th className="text-left py-2 px-3">Trend</th>
                  </tr>
                </thead>
                <tbody>
                  {mockEngineStats.map(engine => (
                    <tr key={engine.engine} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                      <td className="py-2 px-3 font-mono">{engine.rank}</td>
                      <td className="py-2 px-3 font-medium">{engine.engine}</td>
                      <td className="py-2 px-3">
                        <span className={cn(
                          engine.win_rate > 60 ? 'text-trade-green' : engine.win_rate >= 40 ? 'text-trade-yellow' : 'text-trade-red',
                          'font-semibold'
                        )}>
                          {formatPercent(engine.win_rate)}
                        </span>
                      </td>
                      <td className="py-2 px-3 text-text-muted">{engine.count}</td>
                      <td className="py-2 px-3">+{engine.avg_return}%</td>
                      <td className={cn('py-2 px-3 font-medium', engine.vs_baseline >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                        {engine.vs_baseline >= 0 ? '+' : ''}{engine.vs_baseline}pp
                      </td>
                      <td className="py-2 px-3">
                        {engine.trend === 'up' ? (
                          <span className="text-trade-green">▲</span>
                        ) : (
                          <span className="text-trade-red">▼</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Score Predictiveness Scatter */}
      <Card>
        <CardHeader>
          <CardTitle>Score Predictiveness</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={mockScoreCorrelation} minRows={30} name="score correlation">
            <div className="relative">
              <div className="absolute top-2 right-4 bg-bg-elevated border border-border-custom rounded px-3 py-1 text-sm font-mono z-10">
                r = {pearsonR.toFixed(3)}
              </div>
              <div className="h-80">
                <ResponsiveContainer width="100%" height="100%">
                  <ScatterChart>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis type="number" dataKey="score_adjusted" name="Score" stroke="#6b7280" fontSize={12} domain={[35, 105]} />
                    <YAxis type="number" dataKey="max_fwd_return" name="Fwd Return" stroke="#6b7280" fontSize={12} tickFormatter={v => `${v}%`} />
                    <Tooltip
                      cursor={{ strokeDasharray: '3 3' }}
                      contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }}
                      formatter={(value, name) => [name === 'max_fwd_return' ? `${value.toFixed(2)}%` : value, name === 'max_fwd_return' ? 'Fwd Return' : 'Score']}
                    />
                    <ReferenceLine y={0} stroke="#6b7280" strokeDasharray="3 3" />
                    <Scatter data={mockScoreCorrelation} fill="#3b82f6" opacity={0.6}>
                      {mockScoreCorrelation.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={
                          entry.signal_type === 'PRIME' ? '#3b82f6' :
                          entry.signal_type === 'BREAKOUT' ? '#10b981' :
                          entry.signal_type === 'STAGED' ? '#f59e0b' : '#8b5cf6'
                        } />
                      ))}
                    </Scatter>
                  </ScatterChart>
                </ResponsiveContainer>
              </div>
            </div>
            {pearsonR < 0.15 && (
              <div className="mt-3 bg-trade-red/10 border border-trade-red/20 rounded p-3 text-trade-red text-sm flex items-center gap-2">
                <AlertTriangle size={16} />
                Score not predicting returns — correlation too low
              </div>
            )}
          </DataGuard>
        </CardContent>
      </Card>
    </div>
  )
}
