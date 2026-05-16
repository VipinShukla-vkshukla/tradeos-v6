'use client'

import { useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  BarChart3,
  LineChart,
  PieChart,
  AreaChart,
  ScatterChart,
  Sparkles,
  X,
  Maximize2,
  Minimize2,
  Send,
  Palette,
  Settings2,
  Database,
  Loader2,
  Download,
  Copy,
  RefreshCw
} from 'lucide-react'
import {
  BarChart,
  Bar,
  LineChart as RechartsLineChart,
  Line,
  PieChart as RechartsPieChart,
  Pie,
  AreaChart as RechartsAreaChart,
  Area,
  ScatterChart as RechartsScatterChart,
  Scatter,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  Cell
} from 'recharts'
import { cn } from '@/lib/utils'
import { extendedApi } from '@/lib/api'
import type { TableSchema } from '@/types/registry'
import useSWR from 'swr'

interface ChartBuilderProps {
  open: boolean
  onClose: () => void
  initialData?: ChartConfig
}

interface ChartConfig {
  id: string
  name: string
  type: ChartType
  dataSource: string
  xAxis: string
  yAxis: string[]
  groupBy?: string
  aggregation?: 'sum' | 'avg' | 'count' | 'min' | 'max'
  colors: string[]
  filters?: Array<{ column: string; operator: string; value: string }>
}

type ChartType = 'bar' | 'line' | 'pie' | 'area' | 'scatter'

const CHART_TYPES = [
  { type: 'bar' as const, icon: BarChart3, label: 'Bar Chart' },
  { type: 'line' as const, icon: LineChart, label: 'Line Chart' },
  { type: 'area' as const, icon: AreaChart, label: 'Area Chart' },
  { type: 'pie' as const, icon: PieChart, label: 'Pie Chart' },
  { type: 'scatter' as const, icon: ScatterChart, label: 'Scatter Plot' },
]

const DEFAULT_COLORS = [
  'hsl(var(--accent))',
  'hsl(var(--success))',
  'hsl(var(--warning))',
  'hsl(var(--info))',
  'hsl(var(--destructive))',
]

const AGGREGATIONS = [
  { value: 'sum', label: 'Sum' },
  { value: 'avg', label: 'Average' },
  { value: 'count', label: 'Count' },
  { value: 'min', label: 'Minimum' },
  { value: 'max', label: 'Maximum' },
]

export function ChartBuilder({ open, onClose, initialData }: ChartBuilderProps) {
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [activeTab, setActiveTab] = useState<'data' | 'style' | 'ai'>('data')
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [previewData, setPreviewData] = useState<Record<string, unknown>[]>([])
  const [previewLoading, setPreviewLoading] = useState(false)

  const [config, setConfig] = useState<ChartConfig>(initialData || {
    id: crypto.randomUUID(),
    name: 'New Chart',
    type: 'bar',
    dataSource: '',
    xAxis: '',
    yAxis: [],
    colors: DEFAULT_COLORS
  })

  const { data: schema } = useSWR<TableSchema[]>('schema', () => extendedApi.getSchema())

  const selectedTable = schema?.find(t => t.name === config.dataSource)
  const numericColumns = selectedTable?.columns.filter(c => 
    ['integer', 'bigint', 'decimal', 'numeric', 'real', 'double precision'].includes(c.type)
  ) || []
  const allColumns = selectedTable?.columns || []

  const updateConfig = useCallback((updates: Partial<ChartConfig>) => {
    setConfig(prev => ({ ...prev, ...updates }))
  }, [])

  const handleFetchPreview = async () => {
    if (!config.dataSource || !config.xAxis || config.yAxis.length === 0) return
    
    setPreviewLoading(true)
    try {
      const data = await extendedApi.queryTable(config.dataSource, {
        columns: [config.xAxis, ...config.yAxis],
        limit: 100,
        aggregation: config.aggregation,
        groupBy: config.groupBy
      })
      setPreviewData(data || [])
    } catch {
      setPreviewData([])
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleAIGenerate = async () => {
    if (!aiPrompt.trim()) return
    
    setAiLoading(true)
    try {
      const response = await extendedApi.generateChart(aiPrompt)
      if (response?.config) {
        setConfig(prev => ({ ...prev, ...response.config }))
        if (response.data) {
          setPreviewData(response.data)
        }
      }
    } finally {
      setAiLoading(false)
      setAiPrompt('')
    }
  }

  const handleExport = (format: 'png' | 'svg' | 'json') => {
    if (format === 'json') {
      const blob = new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${config.name || 'chart'}.json`
      a.click()
      URL.revokeObjectURL(url)
    }
  }

  const renderChart = () => {
    if (previewData.length === 0) {
      return (
        <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
          <BarChart3 className="h-16 w-16 mb-4 opacity-20" />
          <p>Configure data source to preview chart</p>
        </div>
      )
    }

    const chartProps = {
      data: previewData,
      margin: { top: 20, right: 30, left: 20, bottom: 20 }
    }

    switch (config.type) {
      case 'bar':
        return (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart {...chartProps}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey={config.xAxis} stroke="hsl(var(--muted-foreground))" />
              <YAxis stroke="hsl(var(--muted-foreground))" />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--surface-elevated))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px'
                }}
              />
              <Legend />
              {config.yAxis.map((key, i) => (
                <Bar key={key} dataKey={key} fill={config.colors[i % config.colors.length]} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        )

      case 'line':
        return (
          <ResponsiveContainer width="100%" height="100%">
            <RechartsLineChart {...chartProps}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey={config.xAxis} stroke="hsl(var(--muted-foreground))" />
              <YAxis stroke="hsl(var(--muted-foreground))" />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--surface-elevated))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px'
                }}
              />
              <Legend />
              {config.yAxis.map((key, i) => (
                <Line
                  key={key}
                  type="monotone"
                  dataKey={key}
                  stroke={config.colors[i % config.colors.length]}
                  strokeWidth={2}
                  dot={{ fill: config.colors[i % config.colors.length] }}
                />
              ))}
            </RechartsLineChart>
          </ResponsiveContainer>
        )

      case 'area':
        return (
          <ResponsiveContainer width="100%" height="100%">
            <RechartsAreaChart {...chartProps}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey={config.xAxis} stroke="hsl(var(--muted-foreground))" />
              <YAxis stroke="hsl(var(--muted-foreground))" />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--surface-elevated))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px'
                }}
              />
              <Legend />
              {config.yAxis.map((key, i) => (
                <Area
                  key={key}
                  type="monotone"
                  dataKey={key}
                  stroke={config.colors[i % config.colors.length]}
                  fill={config.colors[i % config.colors.length]}
                  fillOpacity={0.3}
                />
              ))}
            </RechartsAreaChart>
          </ResponsiveContainer>
        )

      case 'pie':
        return (
          <ResponsiveContainer width="100%" height="100%">
            <RechartsPieChart>
              <Pie
                data={previewData}
                dataKey={config.yAxis[0] || 'value'}
                nameKey={config.xAxis}
                cx="50%"
                cy="50%"
                outerRadius={120}
                label
              >
                {previewData.map((_, i) => (
                  <Cell key={i} fill={config.colors[i % config.colors.length]} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--surface-elevated))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px'
                }}
              />
              <Legend />
            </RechartsPieChart>
          </ResponsiveContainer>
        )

      case 'scatter':
        return (
          <ResponsiveContainer width="100%" height="100%">
            <RechartsScatterChart {...chartProps}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
              <XAxis dataKey={config.xAxis} stroke="hsl(var(--muted-foreground))" />
              <YAxis dataKey={config.yAxis[0]} stroke="hsl(var(--muted-foreground))" />
              <Tooltip
                contentStyle={{
                  backgroundColor: 'hsl(var(--surface-elevated))',
                  border: '1px solid hsl(var(--border))',
                  borderRadius: '8px'
                }}
              />
              <Scatter data={previewData} fill={config.colors[0]} />
            </RechartsScatterChart>
          </ResponsiveContainer>
        )

      default:
        return null
    }
  }

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className={cn(
        'p-0 gap-0',
        isFullscreen ? 'max-w-[100vw] h-[100vh] rounded-none' : 'max-w-5xl h-[80vh]'
      )}>
        <DialogHeader className="px-6 py-4 border-b border-border flex-row items-center justify-between">
          <div className="flex items-center gap-3">
            <BarChart3 className="h-5 w-5 text-accent" />
            <DialogTitle>Chart Builder</DialogTitle>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="icon" onClick={() => setIsFullscreen(!isFullscreen)}>
              {isFullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </Button>
            <Button variant="ghost" size="icon" onClick={onClose}>
              <X className="h-4 w-4" />
            </Button>
          </div>
        </DialogHeader>

        <div className="flex flex-1 overflow-hidden">
          {/* Left Panel - Configuration */}
          <div className="w-80 border-r border-border flex flex-col">
            <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as typeof activeTab)} className="flex-1 flex flex-col">
              <TabsList className="w-full justify-start rounded-none border-b border-border h-12 px-2">
                <TabsTrigger value="data" className="gap-2">
                  <Database className="h-4 w-4" />
                  Data
                </TabsTrigger>
                <TabsTrigger value="style" className="gap-2">
                  <Palette className="h-4 w-4" />
                  Style
                </TabsTrigger>
                <TabsTrigger value="ai" className="gap-2">
                  <Sparkles className="h-4 w-4" />
                  AI
                </TabsTrigger>
              </TabsList>

              <ScrollArea className="flex-1">
                <div className="p-4 space-y-6">
                  <TabsContent value="data" className="mt-0 space-y-4">
                    <div className="space-y-2">
                      <Label>Chart Name</Label>
                      <Input
                        value={config.name}
                        onChange={(e) => updateConfig({ name: e.target.value })}
                        placeholder="My Chart"
                      />
                    </div>

                    <div className="space-y-2">
                      <Label>Chart Type</Label>
                      <div className="grid grid-cols-5 gap-1">
                        {CHART_TYPES.map(({ type, icon: Icon, label }) => (
                          <button
                            key={type}
                            onClick={() => updateConfig({ type })}
                            className={cn(
                              'p-2 rounded-lg border transition-all flex flex-col items-center gap-1',
                              config.type === type
                                ? 'border-accent bg-accent/10'
                                : 'border-border hover:border-accent/50'
                            )}
                            title={label}
                          >
                            <Icon className="h-5 w-5" />
                          </button>
                        ))}
                      </div>
                    </div>

                    <div className="space-y-2">
                      <Label>Data Source</Label>
                      <Select
                        value={config.dataSource}
                        onValueChange={(v) => updateConfig({ dataSource: v, xAxis: '', yAxis: [] })}
                      >
                        <SelectTrigger>
                          <SelectValue placeholder="Select table" />
                        </SelectTrigger>
                        <SelectContent>
                          {schema?.map((table) => (
                            <SelectItem key={table.name} value={table.name}>
                              {table.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>

                    {config.dataSource && (
                      <>
                        <div className="space-y-2">
                          <Label>X-Axis (Category)</Label>
                          <Select
                            value={config.xAxis}
                            onValueChange={(v) => updateConfig({ xAxis: v })}
                          >
                            <SelectTrigger>
                              <SelectValue placeholder="Select column" />
                            </SelectTrigger>
                            <SelectContent>
                              {allColumns.map((col) => (
                                <SelectItem key={col.name} value={col.name}>
                                  {col.name}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>

                        <div className="space-y-2">
                          <Label>Y-Axis (Values)</Label>
                          <div className="space-y-2">
                            {numericColumns.map((col) => (
                              <label
                                key={col.name}
                                className={cn(
                                  'flex items-center gap-2 p-2 rounded border cursor-pointer transition-all',
                                  config.yAxis.includes(col.name)
                                    ? 'border-accent bg-accent/10'
                                    : 'border-border hover:border-accent/50'
                                )}
                              >
                                <input
                                  type="checkbox"
                                  checked={config.yAxis.includes(col.name)}
                                  onChange={(e) => {
                                    if (e.target.checked) {
                                      updateConfig({ yAxis: [...config.yAxis, col.name] })
                                    } else {
                                      updateConfig({ yAxis: config.yAxis.filter(y => y !== col.name) })
                                    }
                                  }}
                                  className="sr-only"
                                />
                                <span className="text-sm">{col.name}</span>
                                <Badge variant="outline" className="ml-auto text-xs">
                                  {col.type}
                                </Badge>
                              </label>
                            ))}
                          </div>
                        </div>

                        <div className="space-y-2">
                          <Label>Aggregation</Label>
                          <Select
                            value={config.aggregation}
                            onValueChange={(v) => updateConfig({ aggregation: v as ChartConfig['aggregation'] })}
                          >
                            <SelectTrigger>
                              <SelectValue placeholder="None" />
                            </SelectTrigger>
                            <SelectContent>
                              <SelectItem value="">None</SelectItem>
                              {AGGREGATIONS.map((agg) => (
                                <SelectItem key={agg.value} value={agg.value}>
                                  {agg.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>

                        <Button onClick={handleFetchPreview} disabled={previewLoading} className="w-full">
                          {previewLoading ? (
                            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                          ) : (
                            <RefreshCw className="h-4 w-4 mr-2" />
                          )}
                          Fetch Preview
                        </Button>
                      </>
                    )}
                  </TabsContent>

                  <TabsContent value="style" className="mt-0 space-y-4">
                    <div className="space-y-2">
                      <Label>Colors</Label>
                      <div className="flex flex-wrap gap-2">
                        {config.colors.map((color, i) => (
                          <div
                            key={i}
                            className="w-8 h-8 rounded-lg border border-border cursor-pointer"
                            style={{ backgroundColor: color }}
                            title={`Color ${i + 1}`}
                          />
                        ))}
                      </div>
                    </div>
                  </TabsContent>

                  <TabsContent value="ai" className="mt-0 space-y-4">
                    <div className="space-y-2">
                      <Label>Describe your chart</Label>
                      <Textarea
                        value={aiPrompt}
                        onChange={(e) => setAiPrompt(e.target.value)}
                        placeholder="e.g., Show me a bar chart of win rate by signal type for the last 30 days"
                        rows={4}
                      />
                    </div>

                    <div className="flex flex-wrap gap-2">
                      {[
                        'Win rate trend',
                        'P&L by engine',
                        'Signal distribution',
                        'Volume by hour'
                      ].map((suggestion) => (
                        <Badge
                          key={suggestion}
                          variant="outline"
                          className="cursor-pointer hover:bg-accent/20"
                          onClick={() => setAiPrompt(suggestion)}
                        >
                          {suggestion}
                        </Badge>
                      ))}
                    </div>

                    <Button
                      onClick={handleAIGenerate}
                      disabled={!aiPrompt.trim() || aiLoading}
                      className="w-full bg-accent hover:bg-accent/90"
                    >
                      {aiLoading ? (
                        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      ) : (
                        <Sparkles className="h-4 w-4 mr-2" />
                      )}
                      Generate Chart
                    </Button>
                  </TabsContent>
                </div>
              </ScrollArea>
            </Tabs>
          </div>

          {/* Right Panel - Preview */}
          <div className="flex-1 flex flex-col">
            <div className="px-4 py-2 border-b border-border flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{config.name}</span>
                <Badge variant="outline">{config.type}</Badge>
              </div>
              <div className="flex items-center gap-2">
                <Button variant="ghost" size="sm" onClick={() => handleExport('json')}>
                  <Download className="h-4 w-4 mr-2" />
                  Export
                </Button>
                <Button variant="ghost" size="sm" onClick={() => navigator.clipboard.writeText(JSON.stringify(config))}>
                  <Copy className="h-4 w-4 mr-2" />
                  Copy Config
                </Button>
              </div>
            </div>
            
            <div className="flex-1 p-6">
              <AnimatePresence mode="wait">
                <motion.div
                  key={config.type + config.dataSource}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  className="h-full"
                >
                  {previewLoading ? (
                    <div className="flex items-center justify-center h-full">
                      <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
                    </div>
                  ) : (
                    renderChart()
                  )}
                </motion.div>
              </AnimatePresence>
            </div>
          </div>
        </div>

        {/* Bottom Actions */}
        <div className="px-6 py-4 border-t border-border flex items-center justify-end gap-2">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button className="bg-accent hover:bg-accent/90">
            Save Chart
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
