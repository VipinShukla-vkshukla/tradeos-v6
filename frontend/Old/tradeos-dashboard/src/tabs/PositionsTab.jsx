import React, { useState, useMemo } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  LineChart, Line, Legend
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card'
import { DataGuard } from '../components/DataGuard'
import { Button } from '../components/ui/Button'
import { Modal } from '../components/ui/Modal'
import { Input } from '../components/ui/Input'
import { Select } from '../components/ui/Select'
import { Badge } from '../components/ui/Badge'
import { mockOpenPositions, mockClosedPositions } from '../data/mockData'
import { formatCurrency, formatPercent, formatDate, cn, getSignalTypeBadge } from '../lib/utils'
import { ArrowUpDown, Filter } from 'lucide-react'

function PriceUpdateModal({ open, onClose, position, onUpdate }) {
  const [price, setPrice] = useState('')
  if (!position) return null

  return (
    <Modal open={open} onClose={onClose} title={`Update Entry Price — ${position.symbol}`}>
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-text-muted">Signal price:</span>
            <div className="font-mono text-lg">{formatCurrency(position.entry_price)}</div>
          </div>
          <div>
            <span className="text-text-muted">Current actual:</span>
            <div className="font-mono text-lg">{formatCurrency(position.actual_price)}</div>
          </div>
        </div>
        <div>
          <label className="text-sm text-text-muted block mb-1">New actual fill</label>
          <div className="flex items-center gap-2">
            <span className="text-text-muted">₹</span>
            <Input
              type="number"
              value={price}
              onChange={e => setPrice(e.target.value)}
              placeholder="Enter price"
              className="flex-1"
            />
          </div>
        </div>
        <p className="text-xs text-text-muted">P&L calculations will use this price.</p>
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={() => { onUpdate(position.id, parseFloat(price)); onClose() }} disabled={!price}>
            Update Price
          </Button>
        </div>
      </div>
    </Modal>
  )
}

function ExitModal({ open, onClose, position, onExit }) {
  const [exitPrice, setExitPrice] = useState('')
  const [reason, setReason] = useState('TARGET_HIT')
  if (!position) return null

  const estPnl = exitPrice ? ((parseFloat(exitPrice) - position.actual_price) / position.actual_price * 100) : null

  return (
    <Modal open={open} onClose={onClose} title={`Close Position — ${position.symbol}`}>
      <div className="space-y-4">
        <div className="flex gap-4 text-sm">
          <div>
            <span className="text-text-muted">Entry:</span>
            <span className="font-mono ml-1">{formatCurrency(position.entry_price)}</span>
          </div>
          <div>
            <span className="text-text-muted">Days:</span>
            <span className="font-mono ml-1">{position.days_held}</span>
          </div>
        </div>
        <div>
          <label className="text-sm text-text-muted block mb-1">Exit Price</label>
          <div className="flex items-center gap-2">
            <span className="text-text-muted">₹</span>
            <Input
              type="number"
              value={exitPrice}
              onChange={e => setExitPrice(e.target.value)}
              placeholder="Enter exit price"
              className="flex-1"
            />
          </div>
        </div>
        <div>
          <label className="text-sm text-text-muted block mb-1">Reason</label>
          <Select
            value={reason}
            onChange={e => setReason(e.target.value)}
            options={[
              { value: 'TARGET_HIT', label: 'TARGET_HIT' },
              { value: 'STOP_HIT', label: 'STOP_HIT' },
              { value: 'MANUAL', label: 'MANUAL' },
              { value: 'REGIME_CHANGE', label: 'REGIME_CHANGE' },
              { value: 'SIGNAL_DEGRADED', label: 'SIGNAL_DEGRADED' },
            ]}
          />
        </div>
        {estPnl !== null && (
          <div className={cn('text-sm font-medium', estPnl >= 0 ? 'text-trade-green' : 'text-trade-red')}>
            Est. P&L: {estPnl >= 0 ? '+' : ''}{estPnl.toFixed(2)}%
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button variant="danger" onClick={() => { onExit(position.id, parseFloat(exitPrice), reason); onClose() }} disabled={!exitPrice}>
            Confirm Exit
          </Button>
        </div>
      </div>
    </Modal>
  )
}

export function PositionsTab() {
  const [openPositions, setOpenPositions] = useState(mockOpenPositions)
  const [priceModalOpen, setPriceModalOpen] = useState(false)
  const [exitModalOpen, setExitModalOpen] = useState(false)
  const [selectedPosition, setSelectedPosition] = useState(null)
  const [sortConfig, setSortConfig] = useState({ key: null, direction: 'asc' })
  const [dateFilter, setDateFilter] = useState('')
  const [strategyFilter, setStrategyFilter] = useState('')
  const [reasonFilter, setReasonFilter] = useState('')

  const handleSort = (key) => {
    const direction = sortConfig.key === key && sortConfig.direction === 'asc' ? 'desc' : 'asc'
    setSortConfig({ key, direction })
  }

  const sortedOpen = useMemo(() => {
    if (!sortConfig.key) return openPositions
    return [...openPositions].sort((a, b) => {
      const aVal = a[sortConfig.key]
      const bVal = b[sortConfig.key]
      if (aVal < bVal) return sortConfig.direction === 'asc' ? -1 : 1
      if (aVal > bVal) return sortConfig.direction === 'asc' ? 1 : -1
      return 0
    })
  }, [openPositions, sortConfig])

  const filteredClosed = useMemo(() => {
    let data = [...mockClosedPositions]
    if (dateFilter) data = data.filter(d => d.exit_date >= dateFilter)
    if (strategyFilter) data = data.filter(d => d.strategy === strategyFilter)
    if (reasonFilter) data = data.filter(d => d.reason === reasonFilter)
    if (sortConfig.key) {
      data.sort((a, b) => {
        const aVal = a[sortConfig.key]
        const bVal = b[sortConfig.key]
        if (aVal < bVal) return sortConfig.direction === 'asc' ? -1 : 1
        if (aVal > bVal) return sortConfig.direction === 'asc' ? 1 : -1
        return 0
      })
    }
    return data
  }, [dateFilter, strategyFilter, reasonFilter, sortConfig])

  const monthlyPnl = useMemo(() => {
    const grouped = {}
    filteredClosed.forEach(trade => {
      const month = trade.exit_date.slice(0, 7)
      grouped[month] = (grouped[month] || 0) + trade.pnl_abs
    })
    return Object.entries(grouped).map(([month, pnl]) => ({ month, pnl })).sort((a, b) => a.month.localeCompare(b.month))
  }, [filteredClosed])

  const cumulativePnl = useMemo(() => {
    let sum = 0
    return filteredClosed
      .sort((a, b) => a.exit_date.localeCompare(b.exit_date))
      .map(trade => {
        sum += trade.pnl_abs
        return { date: trade.exit_date, pnl: sum }
      })
  }, [filteredClosed])

  const closedSummary = useMemo(() => {
    const total = filteredClosed.length
    const avgPnl = total > 0 ? filteredClosed.reduce((s, t) => s + t.pnl_pct, 0) / total : 0
    const wins = filteredClosed.filter(t => t.pnl_pct > 0).length
    const winRate = total > 0 ? (wins / total) * 100 : 0
    const totalPnl = filteredClosed.reduce((s, t) => s + t.pnl_abs, 0)
    return { total, avgPnl, winRate, totalPnl }
  }, [filteredClosed])

  return (
    <div className="space-y-6">
      {/* Open Positions */}
      <Card>
        <CardHeader>
          <CardTitle>Open Positions</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={openPositions} minRows={1} name="open positions">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border-custom text-text-muted">
                    {['Symbol', 'Entry Date', 'Entry Price', 'Actual Price', 'Diff', 'Strategy', 'Stop Loss', 'Target', 'Regime', 'AI Conv', 'Days Held', 'Est. P&L', 'Actions'].map(col => (
                      <th key={col} className="text-left py-2 px-2 whitespace-nowrap cursor-pointer hover:text-text-primary" onClick={() => handleSort(col.toLowerCase().replace(' ', '_'))}>
                        <div className="flex items-center gap-1">{col} <ArrowUpDown size={12} /></div>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {sortedOpen.map(pos => {
                    const diff = ((pos.actual_price - pos.entry_price) / pos.entry_price * 100)
                    const estPnl = diff.toFixed(2)
                    return (
                      <tr key={pos.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                        <td className="py-2 px-2 font-semibold">{pos.symbol}</td>
                        <td className="py-2 px-2 text-text-muted">{formatDate(pos.entry_date)}</td>
                        <td className="py-2 px-2 font-mono">{formatCurrency(pos.entry_price)}</td>
                        <td className="py-2 px-2 font-mono">{formatCurrency(pos.actual_price)}</td>
                        <td className={cn('py-2 px-2 font-mono', diff >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                          {diff >= 0 ? '+' : ''}{estPnl}%
                        </td>
                        <td className="py-2 px-2"><Badge variant={getSignalTypeBadge(pos.strategy)}>{pos.strategy}</Badge></td>
                        <td className="py-2 px-2 font-mono text-trade-red">{formatCurrency(pos.stop_loss)}</td>
                        <td className="py-2 px-2 font-mono text-trade-green">{formatCurrency(pos.target)}</td>
                        <td className="py-2 px-2"><Badge variant={pos.regime === 'RISK ON' ? 'green' : pos.regime === 'NEUTRAL' ? 'yellow' : pos.regime === 'RECOVERING' ? 'blue' : 'red'}>{pos.regime}</Badge></td>
                        <td className="py-2 px-2">{pos.ai_conviction}</td>
                        <td className="py-2 px-2 text-text-muted">{pos.days_held}</td>
                        <td className={cn('py-2 px-2 font-mono font-medium', diff >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                          {diff >= 0 ? '+' : ''}{estPnl}%
                        </td>
                        <td className="py-2 px-2">
                          <div className="flex gap-1">
                            <Button size="sm" variant="outline" onClick={() => { setSelectedPosition(pos); setPriceModalOpen(true) }}>💰</Button>
                            <Button size="sm" variant="danger" onClick={() => { setSelectedPosition(pos); setExitModalOpen(true) }}>🔴</Button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      <PriceUpdateModal
        open={priceModalOpen}
        onClose={() => setPriceModalOpen(false)}
        position={selectedPosition}
        onUpdate={(id, price) => setOpenPositions(prev => prev.map(p => p.id === id ? { ...p, actual_price: price } : p))}
      />
      <ExitModal
        open={exitModalOpen}
        onClose={() => setExitModalOpen(false)}
        position={selectedPosition}
        onExit={(id, price, reason) => setOpenPositions(prev => prev.filter(p => p.id !== id))}
      />

      {/* Closed Positions */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Closed Positions</CardTitle>
            <div className="flex gap-2">
              <Input type="date" value={dateFilter} onChange={e => setDateFilter(e.target.value)} className="w-40" />
              <Select
                value={strategyFilter}
                onChange={e => setStrategyFilter(e.target.value)}
                options={[{ value: '', label: 'All Strategies' }, { value: 'PRIME', label: 'PRIME' }, { value: 'BREAKOUT', label: 'BREAKOUT' }, { value: 'STAGED', label: 'STAGED' }, { value: 'REENTRY', label: 'REENTRY' }]}
                className="w-40"
              />
              <Select
                value={reasonFilter}
                onChange={e => setReasonFilter(e.target.value)}
                options={[{ value: '', label: 'All Reasons' }, { value: 'TARGET_HIT', label: 'TARGET_HIT' }, { value: 'STOP_HIT', label: 'STOP_HIT' }, { value: 'MANUAL', label: 'MANUAL' }, { value: 'REGIME_CHANGE', label: 'REGIME_CHANGE' }]}
                className="w-40"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <DataGuard data={filteredClosed} minRows={0} name="closed positions">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border-custom text-text-muted">
                    {['Symbol', 'Entry', 'Exit', 'Days', 'Entry ₹', 'Exit ₹', 'P&L %', 'P&L ₹', 'Reason', 'Strategy'].map(col => (
                      <th key={col} className="text-left py-2 px-2 whitespace-nowrap">{col}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filteredClosed.map(pos => (
                    <tr key={pos.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                      <td className="py-2 px-2 font-semibold">{pos.symbol}</td>
                      <td className="py-2 px-2 text-text-muted">{formatDate(pos.entry_date)}</td>
                      <td className="py-2 px-2 text-text-muted">{formatDate(pos.exit_date)}</td>
                      <td className="py-2 px-2 text-text-muted">{pos.days}</td>
                      <td className="py-2 px-2 font-mono">{formatCurrency(pos.entry_price)}</td>
                      <td className="py-2 px-2 font-mono">{formatCurrency(pos.exit_price)}</td>
                      <td className={cn('py-2 px-2 font-mono font-bold', pos.pnl_pct >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                        {pos.pnl_pct >= 0 ? '+' : ''}{pos.pnl_pct}%
                      </td>
                      <td className={cn('py-2 px-2 font-mono', pos.pnl_abs >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                        {pos.pnl_abs >= 0 ? '+' : ''}{formatCurrency(Math.abs(pos.pnl_abs))}
                      </td>
                      <td className="py-2 px-2"><Badge variant="grey">{pos.reason}</Badge></td>
                      <td className="py-2 px-2"><Badge variant={getSignalTypeBadge(pos.strategy)}>{pos.strategy}</Badge></td>
                    </tr>
                  ))}
                  <tr className="bg-bg-elevated/50 font-semibold">
                    <td className="py-2 px-2" colSpan={6}>Summary</td>
                    <td className="py-2 px-2">{closedSummary.total} trades</td>
                    <td className={cn('py-2 px-2 font-mono', closedSummary.avgPnl >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                      Avg {closedSummary.avgPnl >= 0 ? '+' : ''}{closedSummary.avgPnl.toFixed(1)}%
                    </td>
                    <td className="py-2 px-2">{formatPercent(closedSummary.winRate)} WR</td>
                    <td className={cn('py-2 px-2 font-mono', closedSummary.totalPnl >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                      {closedSummary.totalPnl >= 0 ? '+' : ''}{formatCurrency(Math.abs(closedSummary.totalPnl))}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* P&L Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Card>
          <CardHeader><CardTitle>Monthly P&L</CardTitle></CardHeader>
          <CardContent>
            <DataGuard data={filteredClosed} minRows={3} name="closed trades">
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={monthlyPnl}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis dataKey="month" stroke="#6b7280" fontSize={12} />
                    <YAxis stroke="#6b7280" fontSize={12} tickFormatter={v => `₹${v/1000}k`} />
                    <Tooltip contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }} formatter={v => formatCurrency(v)} />
                    <Bar dataKey="pnl" fill="#3b82f6" radius={[4, 4, 0, 0]}>
                      {monthlyPnl.map((entry, index) => (
                        <Cell key={index} fill={entry.pnl >= 0 ? '#10b981' : '#ef4444'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </DataGuard>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Cumulative P&L</CardTitle></CardHeader>
          <CardContent>
            <DataGuard data={filteredClosed} minRows={3} name="closed trades">
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={cumulativePnl}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis dataKey="date" stroke="#6b7280" fontSize={12} />
                    <YAxis stroke="#6b7280" fontSize={12} tickFormatter={v => `₹${v/1000}k`} />
                    <Tooltip contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }} formatter={v => formatCurrency(v)} />
                    <Line type="monotone" dataKey="pnl" stroke="#3b82f6" strokeWidth={2} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </DataGuard>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
