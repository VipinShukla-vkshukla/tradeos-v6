import React, { useState } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
  ScatterChart, Scatter
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card'
import { DataGuard } from '../components/DataGuard'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Modal } from '../components/ui/Modal'
import { Input } from '../components/ui/Input'
import { Select } from '../components/ui/Select'
import {
  mockBrainRuns, mockProposals, mockConfigChanges
} from '../data/mockData'
import { formatDateTime, formatPercent, cn } from '../lib/utils'
import { Check, X, RotateCcw, Eye, AlertTriangle, TrendingUp, Settings, Brain } from 'lucide-react'

function KPICard({ title, value, subtitle, icon: Icon }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="flex items-center gap-2 mb-2">
          {Icon && <Icon size={16} className="text-trade-blue" />}
          <span className="text-sm text-text-muted">{title}</span>
        </div>
        <div className="text-2xl font-bold text-text-primary">{value}</div>
        <div className="text-xs text-text-muted mt-1">{subtitle}</div>
      </CardContent>
    </Card>
  )
}

export function BrainEngineTab() {
  const [proposals, setProposals] = useState(mockProposals)
  const [configChanges, setConfigChanges] = useState(mockConfigChanges)
  const [diffModalOpen, setDiffModalOpen] = useState(false)
  const [selectedDiff, setSelectedDiff] = useState('')
  const [approveModalOpen, setApproveModalOpen] = useState(false)
  const [selectedProposal, setSelectedProposal] = useState(null)
  const [rollbackModalOpen, setRollbackModalOpen] = useState(false)
  const [selectedChange, setSelectedChange] = useState(null)
  const [keyFilter, setKeyFilter] = useState('')
  const [byFilter, setByFilter] = useState('')

  const handleApprove = (id) => {
    setProposals(prev => prev.filter(p => p.id !== id))
    setApproveModalOpen(false)
  }

  const handleReject = (id) => {
    setProposals(prev => prev.filter(p => p.id !== id))
  }

  const handleRollback = (change) => {
    setConfigChanges(prev => [...prev, {
      ...change,
      id: Date.now(),
      changed_at: new Date().toISOString(),
      old_value: change.new_value,
      new_value: change.old_value,
      changed_by: 'rollback'
    }])
    setRollbackModalOpen(false)
  }

  const filteredChanges = configChanges.filter(c => {
    if (keyFilter && !c.key.includes(keyFilter)) return false
    if (byFilter && !c.changed_by.includes(byFilter)) return false
    return true
  })

  const appliedProposals = proposals.filter(p => p.status === 'APPLIED')
  const hasSufficientHistory = appliedProposals.length >= 2

  const typeColors = {
    THRESHOLD_CHANGE: 'blue',
    ENGINE_WEIGHT: 'purple',
    SCRIPT_PATCH: 'orange',
    INSIGHT: 'grey',
  }

  const byColors = {
    auto_apply: 'blue',
    dashboard_manual: 'orange',
    manual: 'green',
    rollback: 'red',
  }

  return (
    <div className="space-y-6">
      {/* Brain Health KPIs */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <KPICard title="Config Coverage" value="68%" subtitle="of threshold keys reviewed" icon={Settings} />
        <KPICard title="Acceptance Rate" value="72.7%" subtitle="8 of 11 approved/total" icon={Check} />
        <KPICard title="Avg WR Improve" value="+4.8pp" subtitle="per apply (applied ones)" icon={TrendingUp} />
        <KPICard title="Proposals Today" value="3 pending" subtitle="1 auto-applied" icon={Brain} />
      </div>

      {/* Brain Run Timeline */}
      <Card>
        <CardHeader><CardTitle>Brain Run Timeline</CardTitle></CardHeader>
        <CardContent>
          <DataGuard data={mockBrainRuns} minRows={2} name="brain runs">
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <ScatterChart>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis
                    type="number"
                    dataKey="run_date"
                    domain={['dataMin', 'dataMax']}
                    tickFormatter={v => new Date(v).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' })}
                    stroke="#6b7280"
                    fontSize={12}
                  />
                  <YAxis type="number" dataKey="coverage_pct" name="Coverage" stroke="#6b7280" fontSize={12} tickFormatter={v => `${v}%`} />
                  <Tooltip
                    contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }}
                    formatter={(value, name) => {
                      if (name === 'run_date') return formatDateTime(value)
                      if (name === 'coverage_pct') return `${value}%`
                      return value
                    }}
                  />
                  <Scatter data={mockBrainRuns}>
                    {mockBrainRuns.map((entry, index) => (
                      <Cell
                        key={index}
                        fill={
                          entry.auto_applied > 0 ? '#10b981' :
                          entry.proposals_generated > 0 ? '#f59e0b' : '#6b7280'
                        }
                        r={Math.max(6, entry.proposals_generated * 3 + 4)}
                      />
                    ))}
                  </Scatter>
                </ScatterChart>
              </ResponsiveContainer>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Proposal Queue */}
      <Card>
        <CardHeader><CardTitle>Proposal Queue</CardTitle></CardHeader>
        <CardContent>
          <DataGuard data={proposals} minRows={1} name="proposals">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border-custom text-text-muted">
                    <th className="text-left py-2 px-2">ID</th>
                    <th className="text-left py-2 px-2">Type</th>
                    <th className="text-left py-2 px-2">Target</th>
                    <th className="text-left py-2 px-2">Cur → New</th>
                    <th className="text-left py-2 px-2">Conf</th>
                    <th className="text-left py-2 px-2">WR Δ</th>
                    <th className="text-left py-2 px-2">Source</th>
                    <th className="text-left py-2 px-2">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {proposals.filter(p => p.status === 'PENDING').map(p => (
                    <tr key={p.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                      <td className="py-2 px-2 font-mono">{p.id}</td>
                      <td className="py-2 px-2"><Badge variant={typeColors[p.type] || 'grey'}>{p.type}</Badge></td>
                      <td className="py-2 px-2 text-text-muted max-w-xs truncate" title={p.target}>{p.target}</td>
                      <td className="py-2 px-2 font-mono text-xs">
                        <span className="text-text-muted">{p.current_value}</span>
                        <span className="text-text-dim mx-1">→</span>
                        <span className="text-trade-blue">{p.new_value}</span>
                      </td>
                      <td className="py-2 px-2 font-semibold">{p.confidence}%</td>
                      <td className={cn('py-2 px-2', p.wr_delta >= 0 ? 'text-trade-green' : 'text-trade-red')}>
                        {p.wr_delta ? `${p.wr_delta >= 0 ? '+' : ''}${p.wr_delta}pp` : 'n/a'}
                      </td>
                      <td className="py-2 px-2"><Badge variant="grey">{p.source}</Badge></td>
                      <td className="py-2 px-2">
                        <div className="flex gap-1">
                          <Button size="sm" variant="success" onClick={() => { setSelectedProposal(p); setApproveModalOpen(true) }}>
                            <Check size={14} />
                          </Button>
                          <Button size="sm" variant="danger" onClick={() => handleReject(p.id)}>
                            <X size={14} />
                          </Button>
                          {p.type === 'SCRIPT_PATCH' && (
                            <Button size="sm" variant="outline" onClick={() => { setSelectedDiff(p.diff); setDiffModalOpen(true) }}>
                              <Eye size={14} />
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Config Change Audit Log */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Config Change Audit Log</CardTitle>
            <div className="flex gap-2">
              <Input placeholder="Filter by key..." value={keyFilter} onChange={e => setKeyFilter(e.target.value)} className="w-48" />
              <Select
                value={byFilter}
                onChange={e => setByFilter(e.target.value)}
                options={[{ value: '', label: 'All Sources' }, { value: 'auto_apply', label: 'Auto Apply' }, { value: 'dashboard_manual', label: 'Dashboard' }, { value: 'manual', label: 'Manual/CLI' }, { value: 'rollback', label: 'Rollback' }]}
                className="w-40"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <DataGuard data={filteredChanges} minRows={1} name="config changes">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border-custom text-text-muted">
                    <th className="text-left py-2 px-2">When</th>
                    <th className="text-left py-2 px-2">Key</th>
                    <th className="text-left py-2 px-2">Old</th>
                    <th className="text-left py-2 px-2">New</th>
                    <th className="text-left py-2 px-2">By</th>
                    <th className="text-left py-2 px-2">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredChanges.map(change => (
                    <tr key={change.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                      <td className="py-2 px-2 text-text-muted whitespace-nowrap">{formatDateTime(change.changed_at)}</td>
                      <td className="py-2 px-2 font-mono text-xs max-w-xs truncate" title={change.key}>{change.key}</td>
                      <td className="py-2 px-2 text-text-muted max-w-[100px] truncate">{change.old_value}</td>
                      <td className="py-2 px-2 text-trade-blue max-w-[100px] truncate">{change.new_value}</td>
                      <td className="py-2 px-2">
                        <Badge variant={byColors[change.changed_by] || 'grey'}>
                          {change.changed_by}
                        </Badge>
                      </td>
                      <td className="py-2 px-2">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => { setSelectedChange(change); setRollbackModalOpen(true) }}
                          disabled={change.changed_by === 'rollback'}
                        >
                          <RotateCcw size={14} className="mr-1" /> Rollback
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Before/After Impact */}
      <Card>
        <CardHeader><CardTitle>Before/After Impact</CardTitle></CardHeader>
        <CardContent>
          {hasSufficientHistory ? (
            <DataGuard data={appliedProposals} minRows={2} name="applied proposals">
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={appliedProposals}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                    <XAxis dataKey="target" stroke="#6b7280" fontSize={10} interval={0} angle={-20} textAnchor="end" height={60} />
                    <YAxis stroke="#6b7280" fontSize={12} tickFormatter={v => `${v}%`} />
                    <Tooltip contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }} />
                    <Legend />
                    <Bar dataKey="before_wr" name="Before" fill="#6b7280" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="after_wr" name="After" fill="#3b82f6" radius={[4, 4, 0, 0]}>
                      {appliedProposals.map((entry, index) => (
                        <Cell key={index} fill={entry.after_wr > entry.before_wr ? '#10b981' : '#ef4444'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </DataGuard>
          ) : (
            <div className="flex flex-col items-center justify-center h-40 text-text-muted gap-2">
              <span className="text-2xl">📊</span>
              <p className="text-sm">This chart needs 4+ weeks of applied proposals to populate.</p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Diff Modal */}
      <Modal open={diffModalOpen} onClose={() => setDiffModalOpen(false)} title="Script Patch Diff" className="max-w-2xl">
        <div className="bg-bg-base rounded border border-border-custom p-4 font-mono text-xs overflow-x-auto">
          {selectedDiff.split('\\n').map((line, i) => (
            <div key={i} className={cn(
              'px-2 py-0.5',
              line.startsWith('+') ? 'bg-trade-green/10 text-trade-green' :
              line.startsWith('-') ? 'bg-trade-red/10 text-trade-red' :
              line.startsWith('@@') ? 'text-trade-blue' :
              'text-text-muted'
            )}>
              {line || ' '}
            </div>
          ))}
        </div>
      </Modal>

      {/* Approve Modal */}
      <Modal open={approveModalOpen} onClose={() => setApproveModalOpen(false)} title="Approve Proposal?">
        {selectedProposal && (
          <div className="space-y-4">
            <div className="bg-bg-elevated/50 rounded p-3 text-sm space-y-1">
              <div><span className="text-text-muted">Type:</span> <Badge variant={typeColors[selectedProposal.type]}>{selectedProposal.type}</Badge></div>
              <div><span className="text-text-muted">Target:</span> {selectedProposal.target}</div>
              <div><span className="text-text-muted">Change:</span> {selectedProposal.current_value} → {selectedProposal.new_value}</div>
              <div><span className="text-text-muted">Confidence:</span> {selectedProposal.confidence}%</div>
            </div>
            <div>
              <span className="text-text-muted text-sm">Rationale:</span>
              <p className="text-sm text-text-primary mt-1">{selectedProposal.rationale}</p>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setApproveModalOpen(false)}>Cancel</Button>
              <Button variant="success" onClick={() => handleApprove(selectedProposal.id)}>Confirm Approve</Button>
            </div>
          </div>
        )}
      </Modal>

      {/* Rollback Modal */}
      <Modal open={rollbackModalOpen} onClose={() => setRollbackModalOpen(false)} title="Rollback Configuration?">
        {selectedChange && (
          <div className="space-y-4">
            <div className="bg-trade-red/10 border border-trade-red/20 rounded p-3 text-sm text-trade-red flex items-start gap-2">
              <AlertTriangle size={16} className="mt-0.5 shrink-0" />
              <div>
                <p className="font-medium">This cannot be undone automatically.</p>
                <p className="mt-1">Reverting <span className="font-mono">{selectedChange.key}</span> from <span className="font-mono">{selectedChange.new_value}</span> back to <span className="font-mono">{selectedChange.old_value}</span>.</p>
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setRollbackModalOpen(false)}>Cancel</Button>
              <Button variant="danger" onClick={() => handleRollback(selectedChange)}>Confirm Rollback</Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}
