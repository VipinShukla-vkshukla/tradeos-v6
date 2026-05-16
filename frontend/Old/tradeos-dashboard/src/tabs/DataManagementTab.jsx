import React, { useState, useMemo } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card'
import { DataGuard } from '../components/DataGuard'
import { Button } from '../components/ui/Button'
import { Modal } from '../components/ui/Modal'
import { Input } from '../components/ui/Input'
import { Select } from '../components/ui/Select'
import { Badge } from '../components/ui/Badge'
import { mockSystemConfig, mockOpenPositions, mockSignals, mockLessons } from '../data/mockData'
import { formatCurrency, formatDate, formatPercent, cn, getSignalTypeBadge, getRegimeBadge } from '../lib/utils'
import { ChevronDown, ChevronRight, Edit, Save, X, Plus, Download, Search, Filter } from 'lucide-react'

function ConfigEditor() {
  const [config, setConfig] = useState(mockSystemConfig)
  const [expandedGroups, setExpandedGroups] = useState({})
  const [editingKey, setEditingKey] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [confirmModalOpen, setConfirmModalOpen] = useState(false)
  const [pendingChange, setPendingChange] = useState(null)
  const [changeReason, setChangeReason] = useState('Manual update via dashboard')
  const [jsonModalOpen, setJsonModalOpen] = useState(false)
  const [jsonKey, setJsonKey] = useState(null)
  const [jsonValue, setJsonValue] = useState('')

  const groups = useMemo(() => {
    const g = {}
    config.forEach(item => {
      const prefix = item.prefix || 'Other'
      if (!g[prefix]) g[prefix] = []
      g[prefix].push(item)
    })
    return g
  }, [config])

  const groupLabels = {
    signal_threshold: 'Signal Thresholds',
    brain: 'Brain Settings',
    screener: 'Screener Settings',
    regime: 'Regime Weights',
    msl: 'MSL Settings',
    Other: 'Other',
  }

  const toggleGroup = (prefix) => {
    setExpandedGroups(prev => ({ ...prev, [prefix]: !prev[prefix] }))
  }

  const startEdit = (item) => {
    if (item.value.startsWith('{') || item.value.startsWith('[')) {
      setJsonKey(item.key)
      setJsonValue(item.value)
      setJsonModalOpen(true)
    } else {
      setEditingKey(item.key)
      setEditValue(item.value)
    }
  }

  const saveEdit = () => {
    const item = config.find(c => c.key === editingKey)
    if (item && editValue !== item.value) {
      setPendingChange({ key: editingKey, oldValue: item.value, newValue: editValue })
      setConfirmModalOpen(true)
    } else {
      setEditingKey(null)
    }
  }

  const saveJsonEdit = () => {
    const item = config.find(c => c.key === jsonKey)
    if (item && jsonValue !== item.value) {
      setPendingChange({ key: jsonKey, oldValue: item.value, newValue: jsonValue })
      setConfirmModalOpen(true)
      setJsonModalOpen(false)
    } else {
      setJsonModalOpen(false)
    }
  }

  const confirmChange = () => {
    setConfig(prev => prev.map(c =>
      c.key === pendingChange.key
        ? { ...c, value: pendingChange.newValue, last_changed_by: 'dashboard_manual', last_changed_at: new Date().toISOString() }
        : c
    ))
    setConfirmModalOpen(false)
    setEditingKey(null)
    setPendingChange(null)
    setChangeReason('Manual update via dashboard')
  }

  const timeAgo = (dateStr) => {
    const diff = Date.now() - new Date(dateStr).getTime()
    const days = Math.floor(diff / (1000 * 60 * 60 * 24))
    if (days === 0) return 'today'
    if (days === 1) return '1 day ago'
    return `${days} days ago`
  }

  return (
    <div className="space-y-2">
      {Object.entries(groups).map(([prefix, items]) => (
        <div key={prefix} className="border border-border-custom rounded-lg overflow-hidden">
          <button
            onClick={() => toggleGroup(prefix)}
            className="w-full flex items-center justify-between px-4 py-3 bg-bg-elevated/30 hover:bg-bg-elevated/50 transition-colors"
          >
            <div className="flex items-center gap-2">
              {expandedGroups[prefix] ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
              <span className="font-medium">{groupLabels[prefix] || prefix}</span>
              <Badge variant="grey">{items.length} keys</Badge>
            </div>
          </button>
          {expandedGroups[prefix] && (
            <div className="divide-y divide-border-custom/50">
              {items.map(item => (
                <div key={item.key} className="flex items-center justify-between px-4 py-2.5 hover:bg-bg-elevated/20">
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-xs text-text-muted truncate">{item.key}</div>
                    {editingKey === item.key ? (
                      <div className="flex items-center gap-2 mt-1">
                        <Input value={editValue} onChange={e => setEditValue(e.target.value)} className="w-48 h-7 text-sm" />
                        <Button size="sm" variant="success" onClick={saveEdit}><Save size={14} /></Button>
                        <Button size="sm" variant="ghost" onClick={() => setEditingKey(null)}><X size={14} /></Button>
                      </div>
                    ) : (
                      <div className="text-sm font-medium mt-0.5">{item.value}</div>
                    )}
                    <div className="text-xs text-text-dim mt-0.5">
                      Last changed: {item.last_changed_by} {timeAgo(item.last_changed_at)}
                    </div>
                  </div>
                  {editingKey !== item.key && (
                    <Button size="sm" variant="ghost" onClick={() => startEdit(item)}>
                      <Edit size={14} />
                    </Button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      ))}

      {/* Confirm Modal */}
      <Modal open={confirmModalOpen} onClose={() => setConfirmModalOpen(false)} title="Confirm Config Change">
        {pendingChange && (
          <div className="space-y-4">
            <p className="text-sm">
              Change <span className="font-mono text-trade-blue">{pendingChange.key}</span> from{' '}
              <span className="font-mono">{pendingChange.oldValue}</span> to{' '}
              <span className="font-mono text-trade-green">{pendingChange.newValue}</span>?
            </p>
            <div>
              <label className="text-sm text-text-muted block mb-1">Reason (optional)</label>
              <Input value={changeReason} onChange={e => setChangeReason(e.target.value)} placeholder="Why are you making this change?" />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setConfirmModalOpen(false)}>Cancel</Button>
              <Button onClick={confirmChange}>Confirm</Button>
            </div>
          </div>
        )}
      </Modal>

      {/* JSON Modal */}
      <Modal open={jsonModalOpen} onClose={() => setJsonModalOpen(false)} title={`Edit ${jsonKey}`} className="max-w-xl">
        <div className="space-y-4">
          <textarea
            value={jsonValue}
            onChange={e => setJsonValue(e.target.value)}
            className="w-full h-64 bg-bg-base border border-border-custom rounded-md p-3 text-sm font-mono text-text-primary focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-trade-blue resize-none"
            spellCheck={false}
          />
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setJsonModalOpen(false)}>Cancel</Button>
            <Button onClick={saveJsonEdit}>Save JSON</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

function OpenPositionsManager() {
  const [positions, setPositions] = useState(mockOpenPositions)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [newPosition, setNewPosition] = useState({
    symbol: '', entry_date: '', entry_price: '', strategy: 'PRIME', stop_loss: '', target: '', ai_conviction: 'HIGH', notes: ''
  })

  const handleAdd = () => {
    const pos = {
      id: Date.now(),
      symbol: newPosition.symbol,
      entry_date: newPosition.entry_date,
      entry_price: parseFloat(newPosition.entry_price),
      actual_price: parseFloat(newPosition.entry_price),
      strategy: newPosition.strategy,
      stop_loss: parseFloat(newPosition.stop_loss),
      target: parseFloat(newPosition.target),
      regime: 'NEUTRAL',
      ai_conviction: newPosition.ai_conviction,
      days_held: 0,
      notes: newPosition.notes,
    }
    setPositions(prev => [...prev, pos])
    setAddModalOpen(false)
    setNewPosition({ symbol: '', entry_date: '', entry_price: '', strategy: 'PRIME', stop_loss: '', target: '', ai_conviction: 'HIGH', notes: '' })
  }

  return (
    <div>
      <div className="flex justify-end mb-3">
        <Button onClick={() => setAddModalOpen(true)}><Plus size={16} className="mr-1" /> Add New Position</Button>
      </div>
      <DataGuard data={positions} minRows={1} name="open positions">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border-custom text-text-muted">
                <th className="text-left py-2 px-2">Symbol</th>
                <th className="text-left py-2 px-2">Entry Date</th>
                <th className="text-left py-2 px-2">Entry ₹</th>
                <th className="text-left py-2 px-2">Actual ₹</th>
                <th className="text-left py-2 px-2">Strategy</th>
                <th className="text-left py-2 px-2">SL</th>
                <th className="text-left py-2 px-2">Target</th>
                <th className="text-left py-2 px-2">AI Conv</th>
                <th className="text-left py-2 px-2">Notes</th>
              </tr>
            </thead>
            <tbody>
              {positions.map(pos => (
                <tr key={pos.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                  <td className="py-2 px-2 font-semibold">{pos.symbol}</td>
                  <td className="py-2 px-2 text-text-muted">{formatDate(pos.entry_date)}</td>
                  <td className="py-2 px-2 font-mono">{formatCurrency(pos.entry_price)}</td>
                  <td className="py-2 px-2 font-mono">{formatCurrency(pos.actual_price)}</td>
                  <td className="py-2 px-2"><Badge variant={getSignalTypeBadge(pos.strategy)}>{pos.strategy}</Badge></td>
                  <td className="py-2 px-2 font-mono text-trade-red">{formatCurrency(pos.stop_loss)}</td>
                  <td className="py-2 px-2 font-mono text-trade-green">{formatCurrency(pos.target)}</td>
                  <td className="py-2 px-2">{pos.ai_conviction}</td>
                  <td className="py-2 px-2 text-text-muted max-w-[200px] truncate">{pos.notes || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataGuard>

      <Modal open={addModalOpen} onClose={() => setAddModalOpen(false)} title="Add New Position">
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-text-muted block mb-1">Symbol</label>
              <Input value={newPosition.symbol} onChange={e => setNewPosition(p => ({ ...p, symbol: e.target.value.toUpperCase() }))} placeholder="e.g. RELIANCE" />
            </div>
            <div>
              <label className="text-xs text-text-muted block mb-1">Entry Date</label>
              <Input type="date" value={newPosition.entry_date} onChange={e => setNewPosition(p => ({ ...p, entry_date: e.target.value }))} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-text-muted block mb-1">Entry Price (₹)</label>
              <Input type="number" value={newPosition.entry_price} onChange={e => setNewPosition(p => ({ ...p, entry_price: e.target.value }))} />
            </div>
            <div>
              <label className="text-xs text-text-muted block mb-1">Strategy</label>
              <Select
                value={newPosition.strategy}
                onChange={e => setNewPosition(p => ({ ...p, strategy: e.target.value }))}
                options={[{ value: 'PRIME', label: 'PRIME_SETUP' }, { value: 'BREAKOUT', label: 'BREAKOUT' }, { value: 'STAGED', label: 'STAGED' }, { value: 'REENTRY', label: 'REENTRY' }]}
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-text-muted block mb-1">Stop Loss (₹)</label>
              <Input type="number" value={newPosition.stop_loss} onChange={e => setNewPosition(p => ({ ...p, stop_loss: e.target.value }))} />
            </div>
            <div>
              <label className="text-xs text-text-muted block mb-1">Target (₹)</label>
              <Input type="number" value={newPosition.target} onChange={e => setNewPosition(p => ({ ...p, target: e.target.value }))} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-text-muted block mb-1">AI Conviction</label>
              <Select
                value={newPosition.ai_conviction}
                onChange={e => setNewPosition(p => ({ ...p, ai_conviction: e.target.value }))}
                options={[{ value: 'HIGH', label: 'HIGH' }, { value: 'MEDIUM', label: 'MEDIUM' }, { value: 'LOW', label: 'LOW' }]}
              />
            </div>
          </div>
          <div>
            <label className="text-xs text-text-muted block mb-1">Notes</label>
            <Input value={newPosition.notes} onChange={e => setNewPosition(p => ({ ...p, notes: e.target.value }))} placeholder="Optional notes..." />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={() => setAddModalOpen(false)}>Cancel</Button>
            <Button onClick={handleAdd} disabled={!newPosition.symbol || !newPosition.entry_date || !newPosition.entry_price}>Add Position</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

function SignalLogExplorer() {
  const [signals] = useState(mockSignals)
  const [filters, setFilters] = useState({
    dateFrom: '', dateTo: '', types: [], regime: '', sector: '', scoreMin: '', scoreMax: ''
  })

  const filtered = useMemo(() => {
    return signals.filter(s => {
      if (filters.dateFrom && s.date < filters.dateFrom) return false
      if (filters.dateTo && s.date > filters.dateTo) return false
      if (filters.types.length > 0 && !filters.types.includes(s.type)) return false
      if (filters.regime && s.regime !== filters.regime) return false
      if (filters.scoreMin && s.score < parseInt(filters.scoreMin)) return false
      if (filters.scoreMax && s.score > parseInt(filters.scoreMax)) return false
      return true
    })
  }, [signals, filters])

  const exportCSV = () => {
    const headers = ['Date', 'Symbol', 'Type', 'Score', 'RSI', 'ADX', 'Vol Ratio', 'Regime', 'AI Conv', 'Outcome']
    const rows = filtered.map(s => [s.date, s.symbol, s.type, s.score, s.rsi, s.adx, s.vol_ratio, s.regime, s.ai_conviction, s.outcome || '—'])
    const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `signals_${new Date().toISOString().split('T')[0]}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const toggleType = (type) => {
    setFilters(prev => ({
      ...prev,
      types: prev.types.includes(type)
        ? prev.types.filter(t => t !== type)
        : [...prev.types, type]
    }))
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Input type="date" value={filters.dateFrom} onChange={e => setFilters(p => ({ ...p, dateFrom: e.target.value }))} className="w-36" placeholder="From" />
        <Input type="date" value={filters.dateTo} onChange={e => setFilters(p => ({ ...p, dateTo: e.target.value }))} className="w-36" placeholder="To" />
        <div className="flex gap-1">
          {['PRIME', 'BREAKOUT', 'STAGED', 'REENTRY'].map(type => (
            <button
              key={type}
              onClick={() => toggleType(type)}
              className={cn(
                'px-2 py-1 text-xs rounded border transition-colors',
                filters.types.includes(type)
                  ? 'bg-trade-blue/20 border-trade-blue text-trade-blue'
                  : 'border-border-custom text-text-muted hover:text-text-primary'
              )}
            >
              {type}
            </button>
          ))}
        </div>
        <Select
          value={filters.regime}
          onChange={e => setFilters(p => ({ ...p, regime: e.target.value }))}
          options={[{ value: '', label: 'All Regimes' }, { value: 'RISK ON', label: 'RISK ON' }, { value: 'NEUTRAL', label: 'NEUTRAL' }, { value: 'RECOVERING', label: 'RECOVERING' }, { value: 'RISK OFF', label: 'RISK OFF' }]}
          className="w-36"
        />
        <div className="flex items-center gap-1">
          <Input type="number" value={filters.scoreMin} onChange={e => setFilters(p => ({ ...p, scoreMin: e.target.value }))} className="w-20" placeholder="Min" />
          <span className="text-text-muted">-</span>
          <Input type="number" value={filters.scoreMax} onChange={e => setFilters(p => ({ ...p, scoreMax: e.target.value }))} className="w-20" placeholder="Max" />
        </div>
        <Button variant="outline" onClick={exportCSV}><Download size={14} className="mr-1" /> Export CSV</Button>
      </div>

      <DataGuard data={filtered} minRows={1} name="signals">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border-custom text-text-muted">
                <th className="text-left py-2 px-2">Date</th>
                <th className="text-left py-2 px-2">Symbol</th>
                <th className="text-left py-2 px-2">Type</th>
                <th className="text-left py-2 px-2">Score</th>
                <th className="text-left py-2 px-2">RSI</th>
                <th className="text-left py-2 px-2">ADX</th>
                <th className="text-left py-2 px-2">Vol Ratio</th>
                <th className="text-left py-2 px-2">Regime</th>
                <th className="text-left py-2 px-2">AI Conv</th>
                <th className="text-left py-2 px-2">Outcome</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(s => (
                <tr key={s.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                  <td className="py-2 px-2 text-text-muted">{formatDate(s.date)}</td>
                  <td className="py-2 px-2 font-semibold">{s.symbol}</td>
                  <td className="py-2 px-2"><Badge variant={getSignalTypeBadge(s.type)}>{s.type}</Badge></td>
                  <td className="py-2 px-2 font-mono">{s.score}</td>
                  <td className="py-2 px-2 font-mono">{s.rsi}</td>
                  <td className="py-2 px-2 font-mono">{s.adx}</td>
                  <td className="py-2 px-2 font-mono">{s.vol_ratio}</td>
                  <td className="py-2 px-2"><Badge variant={getRegimeBadge(s.regime)}>{s.regime}</Badge></td>
                  <td className="py-2 px-2">{s.ai_conviction}</td>
                  <td className="py-2 px-2">
                    {s.outcome ? (
                      <Badge variant={s.outcome === 'WIN' ? 'green' : s.outcome === 'LOSS' ? 'red' : 'yellow'}>{s.outcome}</Badge>
                    ) : (
                      <span className="text-text-dim">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataGuard>
    </div>
  )
}

function LessonsManager() {
  const [lessons, setLessons] = useState(mockLessons)
  const [addModalOpen, setAddModalOpen] = useState(false)
  const [newLesson, setNewLesson] = useState({ scenario: '', rule: '', confidence: 50, strategy: 'PRIME' })

  const handleAdd = () => {
    setLessons(prev => [...prev, { id: Date.now(), ...newLesson, applied: false }])
    setAddModalOpen(false)
    setNewLesson({ scenario: '', rule: '', confidence: 50, strategy: 'PRIME' })
  }

  return (
    <div>
      <div className="flex justify-end mb-3">
        <Button onClick={() => setAddModalOpen(true)}><Plus size={16} className="mr-1" /> Add Lesson</Button>
      </div>
      <DataGuard data={lessons} minRows={1} name="lessons">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border-custom text-text-muted">
                <th className="text-left py-2 px-2">Scenario</th>
                <th className="text-left py-2 px-2">Rule</th>
                <th className="text-left py-2 px-2">Confidence</th>
                <th className="text-left py-2 px-2">Applied</th>
                <th className="text-left py-2 px-2">Strategy</th>
              </tr>
            </thead>
            <tbody>
              {lessons.map(lesson => (
                <tr key={lesson.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                  <td className="py-2 px-2 font-medium">{lesson.scenario}</td>
                  <td className="py-2 px-2 text-text-muted max-w-xs truncate">{lesson.rule}</td>
                  <td className="py-2 px-2">
                    <div className="flex items-center gap-2">
                      <div className="w-16 h-2 bg-bg-elevated rounded-full overflow-hidden">
                        <div
                          className={cn('h-full rounded-full',
                            lesson.confidence > 70 ? 'bg-trade-green' :
                            lesson.confidence >= 40 ? 'bg-trade-yellow' : 'bg-trade-red'
                          )}
                          style={{ width: `${lesson.confidence}%` }}
                        />
                      </div>
                      <span className={cn('font-medium',
                        lesson.confidence > 70 ? 'text-trade-green' :
                        lesson.confidence >= 40 ? 'text-trade-yellow' : 'text-trade-red'
                      )}>{lesson.confidence}%</span>
                    </div>
                  </td>
                  <td className="py-2 px-2">{lesson.applied ? <span className="text-trade-green">✓</span> : <span className="text-text-dim">—</span>}</td>
                  <td className="py-2 px-2"><Badge variant="grey">{lesson.strategy}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </DataGuard>

      <Modal open={addModalOpen} onClose={() => setAddModalOpen(false)} title="Add New Lesson">
        <div className="space-y-3">
          <div>
            <label className="text-xs text-text-muted block mb-1">Scenario</label>
            <Input value={newLesson.scenario} onChange={e => setNewLesson(p => ({ ...p, scenario: e.target.value }))} placeholder="e.g. Low ADX breakout" />
          </div>
          <div>
            <label className="text-xs text-text-muted block mb-1">Rule</label>
            <Input value={newLesson.rule} onChange={e => setNewLesson(p => ({ ...p, rule: e.target.value }))} placeholder="Describe the rule..." />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="text-xs text-text-muted block mb-1">Confidence (0-100)</label>
              <Input type="number" min="0" max="100" value={newLesson.confidence} onChange={e => setNewLesson(p => ({ ...p, confidence: parseInt(e.target.value) || 0 }))} />
            </div>
            <div>
              <label className="text-xs text-text-muted block mb-1">Strategy</label>
              <Select
                value={newLesson.strategy}
                onChange={e => setNewLesson(p => ({ ...p, strategy: e.target.value }))}
                options={[{ value: 'PRIME', label: 'PRIME' }, { value: 'BREAKOUT', label: 'BREAKOUT' }, { value: 'STAGED', label: 'STAGED' }, { value: 'REENTRY', label: 'REENTRY' }, { value: 'ALL', label: 'ALL' }]}
              />
            </div>
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={() => setAddModalOpen(false)}>Cancel</Button>
            <Button onClick={handleAdd} disabled={!newLesson.scenario || !newLesson.rule}>Add Lesson</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

export function DataManagementTab() {
  const [activeSection, setActiveSection] = useState('config')

  const sections = [
    { id: 'config', label: 'Config Editor', component: <ConfigEditor /> },
    { id: 'positions', label: 'Open Positions Manager', component: <OpenPositionsManager /> },
    { id: 'signals', label: 'Signal Log Explorer', component: <SignalLogExplorer /> },
    { id: 'lessons', label: 'Lessons Manager', component: <LessonsManager /> },
  ]

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-1 bg-bg-surface border border-border-custom rounded-lg p-1 w-fit">
        {sections.map(sec => (
          <button
            key={sec.id}
            onClick={() => setActiveSection(sec.id)}
            className={cn(
              'px-4 py-2 text-sm font-medium rounded-md transition-colors',
              activeSection === sec.id
                ? 'bg-trade-blue/10 text-trade-blue'
                : 'text-text-muted hover:text-text-primary'
            )}
          >
            {sec.label}
          </button>
        ))}
      </div>

      <div className="text-xs text-text-muted mb-2">
        {activeSection === 'config' && '⚠️ Writable — All changes are logged to config_change_log'}
        {activeSection === 'positions' && '⚠️ Writable — Full CRUD for open positions'}
        {activeSection === 'signals' && 'ℹ️ Read-only — Signal log with filtering and export'}
        {activeSection === 'lessons' && '⚠️ Writable — Lesson library management'}
      </div>

      {sections.find(s => s.id === activeSection)?.component}
    </div>
  )
}
