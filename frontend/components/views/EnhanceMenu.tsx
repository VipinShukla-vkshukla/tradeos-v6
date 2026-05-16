'use client'

import { useState } from 'react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  MoreVertical,
  BarChart3,
  Table2,
  Filter,
  SortAsc,
  Download,
  Sparkles,
  RefreshCw,
  Eye,
  EyeOff,
  Copy,
  Columns3,
  ArrowUpDown,
  Loader2
} from 'lucide-react'
import { cn } from '@/lib/utils'

interface EnhanceMenuProps {
  panelId: string
  panelType: 'table' | 'chart' | 'kpi' | 'custom'
  onAddFilter?: (filter: EnhanceFilter) => void
  onAddSort?: (sort: EnhanceSort) => void
  onToggleColumn?: (columnId: string, visible: boolean) => void
  onExport?: (format: 'csv' | 'json') => void
  onRefresh?: () => void
  onConvertToChart?: () => void
  onAIEnhance?: (prompt: string) => Promise<void>
  columns?: Array<{ id: string; name: string; visible: boolean }>
  className?: string
}

export interface EnhanceFilter {
  id: string
  column: string
  operator: 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'startsWith' | 'endsWith'
  value: string
}

export interface EnhanceSort {
  column: string
  direction: 'asc' | 'desc'
}

const FILTER_OPERATORS = [
  { value: 'eq', label: 'Equals' },
  { value: 'neq', label: 'Not equals' },
  { value: 'gt', label: 'Greater than' },
  { value: 'gte', label: 'Greater or equal' },
  { value: 'lt', label: 'Less than' },
  { value: 'lte', label: 'Less or equal' },
  { value: 'contains', label: 'Contains' },
  { value: 'startsWith', label: 'Starts with' },
  { value: 'endsWith', label: 'Ends with' },
]

export function EnhanceMenu({
  panelId,
  panelType,
  onAddFilter,
  onAddSort,
  onToggleColumn,
  onExport,
  onRefresh,
  onConvertToChart,
  onAIEnhance,
  columns = [],
  className
}: EnhanceMenuProps) {
  const [filterDialogOpen, setFilterDialogOpen] = useState(false)
  const [aiDialogOpen, setAiDialogOpen] = useState(false)
  const [filterColumn, setFilterColumn] = useState('')
  const [filterOperator, setFilterOperator] = useState<EnhanceFilter['operator']>('eq')
  const [filterValue, setFilterValue] = useState('')
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiLoading, setAiLoading] = useState(false)

  const handleAddFilter = () => {
    if (!filterColumn || !filterValue) return
    
    onAddFilter?.({
      id: crypto.randomUUID(),
      column: filterColumn,
      operator: filterOperator,
      value: filterValue
    })
    
    setFilterDialogOpen(false)
    setFilterColumn('')
    setFilterOperator('eq')
    setFilterValue('')
  }

  const handleAIEnhance = async () => {
    if (!aiPrompt.trim() || !onAIEnhance) return
    
    setAiLoading(true)
    try {
      await onAIEnhance(aiPrompt)
      setAiDialogOpen(false)
      setAiPrompt('')
    } finally {
      setAiLoading(false)
    }
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className={cn('h-7 w-7', className)}
          >
            <MoreVertical className="h-4 w-4" />
            <span className="sr-only">Panel options</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          {onAIEnhance && (
            <>
              <DropdownMenuItem onClick={() => setAiDialogOpen(true)}>
                <Sparkles className="h-4 w-4 mr-2 text-accent" />
                AI Enhance
              </DropdownMenuItem>
              <DropdownMenuSeparator />
            </>
          )}
          
          {onRefresh && (
            <DropdownMenuItem onClick={onRefresh}>
              <RefreshCw className="h-4 w-4 mr-2" />
              Refresh Data
            </DropdownMenuItem>
          )}
          
          {onAddFilter && columns.length > 0 && (
            <DropdownMenuItem onClick={() => setFilterDialogOpen(true)}>
              <Filter className="h-4 w-4 mr-2" />
              Add Filter
            </DropdownMenuItem>
          )}
          
          {onAddSort && columns.length > 0 && (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <SortAsc className="h-4 w-4 mr-2" />
                Sort By
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                {columns.map((col) => (
                  <DropdownMenuSub key={col.id}>
                    <DropdownMenuSubTrigger>{col.name}</DropdownMenuSubTrigger>
                    <DropdownMenuSubContent>
                      <DropdownMenuItem onClick={() => onAddSort({ column: col.id, direction: 'asc' })}>
                        <ArrowUpDown className="h-4 w-4 mr-2" />
                        Ascending
                      </DropdownMenuItem>
                      <DropdownMenuItem onClick={() => onAddSort({ column: col.id, direction: 'desc' })}>
                        <ArrowUpDown className="h-4 w-4 mr-2 rotate-180" />
                        Descending
                      </DropdownMenuItem>
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          )}
          
          {onToggleColumn && columns.length > 0 && (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <Columns3 className="h-4 w-4 mr-2" />
                Toggle Columns
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                {columns.map((col) => (
                  <DropdownMenuItem
                    key={col.id}
                    onClick={() => onToggleColumn(col.id, !col.visible)}
                  >
                    {col.visible ? (
                      <Eye className="h-4 w-4 mr-2" />
                    ) : (
                      <EyeOff className="h-4 w-4 mr-2 text-muted-foreground" />
                    )}
                    {col.name}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          )}
          
          <DropdownMenuSeparator />
          
          {panelType === 'table' && onConvertToChart && (
            <DropdownMenuItem onClick={onConvertToChart}>
              <BarChart3 className="h-4 w-4 mr-2" />
              Convert to Chart
            </DropdownMenuItem>
          )}
          
          {panelType === 'chart' && (
            <DropdownMenuItem onClick={() => {}}>
              <Table2 className="h-4 w-4 mr-2" />
              View as Table
            </DropdownMenuItem>
          )}
          
          {onExport && (
            <DropdownMenuSub>
              <DropdownMenuSubTrigger>
                <Download className="h-4 w-4 mr-2" />
                Export
              </DropdownMenuSubTrigger>
              <DropdownMenuSubContent>
                <DropdownMenuItem onClick={() => onExport('csv')}>
                  Export as CSV
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => onExport('json')}>
                  Export as JSON
                </DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuSub>
          )}
          
          <DropdownMenuItem onClick={() => navigator.clipboard.writeText(panelId)}>
            <Copy className="h-4 w-4 mr-2" />
            Copy Panel ID
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* Filter Dialog */}
      <Dialog open={filterDialogOpen} onOpenChange={setFilterDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add Filter</DialogTitle>
            <DialogDescription>
              Create a filter condition for this panel
            </DialogDescription>
          </DialogHeader>
          
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>Column</Label>
              <Select value={filterColumn} onValueChange={setFilterColumn}>
                <SelectTrigger>
                  <SelectValue placeholder="Select column" />
                </SelectTrigger>
                <SelectContent>
                  {columns.map((col) => (
                    <SelectItem key={col.id} value={col.id}>
                      {col.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            
            <div className="space-y-2">
              <Label>Operator</Label>
              <Select value={filterOperator} onValueChange={(v) => setFilterOperator(v as EnhanceFilter['operator'])}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {FILTER_OPERATORS.map((op) => (
                    <SelectItem key={op.value} value={op.value}>
                      {op.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            
            <div className="space-y-2">
              <Label>Value</Label>
              <Input
                placeholder="Filter value"
                value={filterValue}
                onChange={(e) => setFilterValue(e.target.value)}
              />
            </div>
          </div>
          
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setFilterDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleAddFilter} disabled={!filterColumn || !filterValue}>
              Add Filter
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {/* AI Enhance Dialog */}
      <Dialog open={aiDialogOpen} onOpenChange={setAiDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Sparkles className="h-5 w-5 text-accent" />
              AI Enhance
            </DialogTitle>
            <DialogDescription>
              Describe what you want to change about this panel
            </DialogDescription>
          </DialogHeader>
          
          <div className="space-y-4 py-4">
            <Textarea
              placeholder="e.g., Show only profitable trades, add a trend line, group by signal type..."
              value={aiPrompt}
              onChange={(e) => setAiPrompt(e.target.value)}
              rows={4}
              className="resize-none"
            />
            
            <div className="flex flex-wrap gap-2">
              {['Show top performers', 'Add percentage changes', 'Group by category', 'Filter last 7 days'].map((suggestion) => (
                <button
                  key={suggestion}
                  onClick={() => setAiPrompt(suggestion)}
                  className="text-xs px-2 py-1 rounded-full bg-surface-elevated hover:bg-accent/20 transition-colors"
                >
                  {suggestion}
                </button>
              ))}
            </div>
          </div>
          
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setAiDialogOpen(false)}>
              Cancel
            </Button>
            <Button 
              onClick={handleAIEnhance} 
              disabled={!aiPrompt.trim() || aiLoading}
              className="bg-accent hover:bg-accent/90"
            >
              {aiLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Enhancing...
                </>
              ) : (
                <>
                  <Sparkles className="h-4 w-4 mr-2" />
                  Apply
                </>
              )}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
