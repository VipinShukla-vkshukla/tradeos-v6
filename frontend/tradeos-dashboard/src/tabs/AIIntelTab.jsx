import React, { useState } from 'react'
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  BarChart, Bar, Cell
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/Card'
import { DataGuard } from '../components/DataGuard'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Input } from '../components/ui/Input'
import { Modal } from '../components/ui/Modal'
import {
  mockPerformanceMetrics, mockMarketIntel, mockAIProviders,
  mockSelfImprovement, mockLessons
} from '../data/mockData'
import { formatPercent, formatDate, cn } from '../lib/utils'
import { AlertTriangle, Archive, TrendingUp, BookOpen } from 'lucide-react'

export function AIIntelTab() {
  const [searchTerm, setSearchTerm] = useState('')
  const [editingLesson, setEditingLesson] = useState(null)
  const [editConfidence, setEditConfidence] = useState('')
  const [lessons, setLessons] = useState(mockLessons)
  const [retireModalOpen, setRetireModalOpen] = useState(false)
  const [retireLessonId, setRetireLessonId] = useState(null)

  const weeklyData = mockPerformanceMetrics.weekly.slice(-12)
  const chartData = weeklyData.map(d => ({
    date: d.date.slice(5),
    high: d.ai_high_conv_wr,
    medium: d.ai_medium_conv_wr,
    low: d.ai_low_conv_wr,
  }))

  const highLowDiff = weeklyData.length > 0 ? Math.abs(weeklyData[weeklyData.length - 1].ai_high_conv_wr - weeklyData[weeklyData.length - 1].ai_low_conv_wr) : 0

  const filteredLessons = lessons.filter(l =>
    l.scenario.toLowerCase().includes(searchTerm.toLowerCase()) ||
    l.rule.toLowerCase().includes(searchTerm.toLowerCase())
  )

  const handleRetire = (id) => {
    setLessons(prev => prev.map(l => l.id === id ? { ...l, confidence: 0, applied: false } : l))
    setRetireModalOpen(false)
  }

  const handleConfidenceUpdate = (id) => {
    setLessons(prev => prev.map(l => l.id === id ? { ...l, confidence: parseFloat(editConfidence) } : l))
    setEditingLesson(null)
  }

  return (
    <div className="space-y-6">
      {/* Conviction Accuracy */}
      <Card>
        <CardHeader>
          <CardTitle>Conviction Accuracy</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={weeklyData} minRows={4} name="conviction data">
            {highLowDiff < 5 && (
              <div className="mb-4 bg-trade-orange/10 border border-trade-orange/20 rounded p-3 text-trade-orange text-sm flex items-center gap-2">
                <AlertTriangle size={16} />
                AI conviction not differentiating — HIGH and LOW within 5pp
              </div>
            )}
            <div className="h-72">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis dataKey="date" stroke="#6b7280" fontSize={12} />
                  <YAxis domain={[0, 100]} stroke="#6b7280" fontSize={12} tickFormatter={v => `${v}%`} />
                  <Tooltip contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }} />
                  <Line type="monotone" dataKey="high" stroke="#10b981" strokeWidth={2} name="HIGH Conviction" />
                  <Line type="monotone" dataKey="medium" stroke="#f59e0b" strokeWidth={2} name="MEDIUM Conviction" />
                  <Line type="monotone" dataKey="low" stroke="#ef4444" strokeWidth={2} name="LOW Conviction" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Market Intel Timeline */}
      <Card>
        <CardHeader>
          <CardTitle>Market Intel Timeline (30 days)</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={mockMarketIntel} minRows={1} name="market intel">
            <div className="space-y-3">
              {mockMarketIntel.map(intel => (
                <div key={intel.id} className="flex items-start gap-3 p-3 rounded-lg bg-bg-elevated/30 border border-border-custom/50">
                  <div className={cn(
                    'w-3 h-3 rounded-full mt-1.5 shrink-0',
                    intel.tone === 'FULL' ? 'bg-trade-green' :
                    intel.tone === 'HALF' ? 'bg-trade-yellow' :
                    intel.tone === 'QUARTER' ? 'bg-trade-orange' : 'bg-trade-red'
                  )} />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <Badge variant={
                        intel.tone === 'FULL' ? 'green' :
                        intel.tone === 'HALF' ? 'yellow' :
                        intel.tone === 'QUARTER' ? 'orange' : 'red'
                      }>{intel.tone}</Badge>
                      <span className="text-xs text-text-muted">{formatDateTime(intel.created_at)}</span>
                    </div>
                    <p className="text-sm text-text-primary">{intel.summary}</p>
                  </div>
                </div>
              ))}
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* AI Provider Comparison */}
      <Card>
        <CardHeader>
          <CardTitle>AI Provider Comparison</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={mockAIProviders.filter(p => p.count >= 10)} minRows={1} name="provider data">
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={mockAIProviders.filter(p => p.count >= 10)} layout="vertical">
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis type="number" domain={[0, 100]} stroke="#6b7280" fontSize={12} tickFormatter={v => `${v}%`} />
                  <YAxis type="category" dataKey="provider" stroke="#6b7280" fontSize={12} width={80} />
                  <Tooltip contentStyle={{ backgroundColor: '#111827', border: '1px solid #1f2937', borderRadius: '6px' }} />
                  <Bar dataKey="win_rate" radius={[0, 4, 4, 0]}>
                    {mockAIProviders.map((entry, index) => (
                      <Cell key={index} fill={
                        entry.win_rate > 60 ? '#10b981' :
                        entry.win_rate >= 40 ? '#f59e0b' : '#ef4444'
                      } />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-2 text-xs text-text-muted text-center">
              Only providers with ≥10 signals shown
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Self-Improvement Feed */}
      <Card>
        <CardHeader>
          <CardTitle>Self-Improvement Feed</CardTitle>
        </CardHeader>
        <CardContent>
          <DataGuard data={mockSelfImprovement} minRows={1} name="improvement notes">
            <div className="space-y-3">
              {mockSelfImprovement.sort((a, b) => b.recurrence_count - a.recurrence_count).map(note => (
                <div key={note.id} className="p-4 rounded-lg bg-bg-elevated/30 border border-border-custom/50">
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <span className="text-lg">🔁</span>
                      <span className="text-sm font-medium">Seen {note.recurrence_count}×</span>
                      <Badge variant={note.confidence === 'HIGH' ? 'green' : note.confidence === 'MEDIUM' ? 'yellow' : 'red'}>{note.confidence}</Badge>
                    </div>
                    <Badge variant="purple">{note.sectors}</Badge>
                  </div>
                  <p className="text-sm text-text-primary italic mb-2">"{note.rule}"</p>
                  <div className="flex items-center justify-between text-xs text-text-muted">
                    <span>First: {formatDate(note.first_seen)} | Last: {formatDate(note.last_seen)}</span>
                    <Button size="sm" variant="ghost"><Archive size={14} className="mr-1" /> Archive</Button>
                  </div>
                </div>
              ))}
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      {/* Lesson Library */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Lesson Library</CardTitle>
            <div className="relative">
              <BookOpen size={16} className="absolute left-2.5 top-2.5 text-text-muted" />
              <Input
                placeholder="Search lessons..."
                value={searchTerm}
                onChange={e => setSearchTerm(e.target.value)}
                className="pl-9 w-64"
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <DataGuard data={filteredLessons} minRows={1} name="lessons">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border-custom text-text-muted">
                    <th className="text-left py-2 px-3">Scenario</th>
                    <th className="text-left py-2 px-3">Rule</th>
                    <th className="text-left py-2 px-3">Confidence</th>
                    <th className="text-left py-2 px-3">Applied</th>
                    <th className="text-left py-2 px-3">Strategy</th>
                    <th className="text-left py-2 px-3">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredLessons.map(lesson => (
                    <tr key={lesson.id} className="border-b border-border-custom/50 hover:bg-bg-elevated/50">
                      <td className="py-2 px-3 font-medium">{lesson.scenario}</td>
                      <td className="py-2 px-3 text-text-muted max-w-xs truncate" title={lesson.rule}>{lesson.rule}</td>
                      <td className="py-2 px-3">
                        {editingLesson === lesson.id ? (
                          <div className="flex items-center gap-2">
                            <Input
                              type="number"
                              value={editConfidence}
                              onChange={e => setEditConfidence(e.target.value)}
                              className="w-20"
                            />
                            <Button size="sm" onClick={() => handleConfidenceUpdate(lesson.id)}>Save</Button>
                          </div>
                        ) : (
                          <div className="flex items-center gap-2 cursor-pointer" onClick={() => { setEditingLesson(lesson.id); setEditConfidence(lesson.confidence) }}>
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
                        )}
                      </td>
                      <td className="py-2 px-3">
                        {lesson.applied ? (
                          <span className="text-trade-green">✓</span>
                        ) : (
                          <span className="text-text-dim">—</span>
                        )}
                      </td>
                      <td className="py-2 px-3"><Badge variant="grey">{lesson.strategy}</Badge></td>
                      <td className="py-2 px-3">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => { setRetireLessonId(lesson.id); setRetireModalOpen(true) }}
                        >Retire</Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataGuard>
        </CardContent>
      </Card>

      <Modal open={retireModalOpen} onClose={() => setRetireModalOpen(false)} title="Retire Lesson?">
        <div className="space-y-4">
          <p className="text-sm text-text-muted">This will set confidence to 0% and mark as not applied. This action cannot be undone.</p>
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setRetireModalOpen(false)}>Cancel</Button>
            <Button variant="danger" onClick={() => handleRetire(retireLessonId)}>Confirm Retire</Button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
