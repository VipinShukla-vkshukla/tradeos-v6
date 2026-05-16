'use client'

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Link2,
  Plus,
  Trash2,
  ArrowRight,
  Sparkles,
  Info,
  Loader2
} from 'lucide-react'
import { cn } from '@/lib/utils'
import type { TableSchema, ViewJoin } from '@/types/registry'

interface JoinBuilderProps {
  baseTable: string
  joins: ViewJoin[]
  schema: TableSchema[]
  onAddJoin: (join: ViewJoin) => void
  onRemoveJoin: (joinId: string) => void
  onUpdateJoin: (joinId: string, updates: Partial<ViewJoin>) => void
  onAISuggest?: () => Promise<ViewJoin[]>
}

const JOIN_TYPES = [
  { value: 'inner', label: 'INNER JOIN', description: 'Only matching rows' },
  { value: 'left', label: 'LEFT JOIN', description: 'All from left, matching from right' },
  { value: 'right', label: 'RIGHT JOIN', description: 'All from right, matching from left' },
  { value: 'full', label: 'FULL JOIN', description: 'All rows from both tables' },
]

export function JoinBuilder({
  baseTable,
  joins,
  schema,
  onAddJoin,
  onRemoveJoin,
  onUpdateJoin,
  onAISuggest
}: JoinBuilderProps) {
  const [newJoin, setNewJoin] = useState<Partial<ViewJoin>>({
    type: 'left',
    sourceTable: baseTable
  })
  const [aiLoading, setAiLoading] = useState(false)

  const baseTableSchema = schema.find(t => t.name === baseTable)
  const availableTables = schema.filter(t => 
    t.name !== baseTable && 
    !joins.some(j => j.targetTable === t.name)
  )

  const getTableColumns = (tableName: string) => {
    return schema.find(t => t.name === tableName)?.columns || []
  }

  const handleAddJoin = () => {
    if (!newJoin.targetTable || !newJoin.sourceColumn || !newJoin.targetColumn) return

    const join: ViewJoin = {
      id: crypto.randomUUID(),
      type: newJoin.type || 'left',
      sourceTable: baseTable,
      sourceColumn: newJoin.sourceColumn,
      targetTable: newJoin.targetTable,
      targetColumn: newJoin.targetColumn
    }

    onAddJoin(join)
    setNewJoin({ type: 'left', sourceTable: baseTable })
  }

  const handleAISuggest = async () => {
    if (!onAISuggest) return
    setAiLoading(true)
    try {
      const suggestions = await onAISuggest()
      suggestions.forEach(join => {
        if (!joins.some(j => j.targetTable === join.targetTable)) {
          onAddJoin(join)
        }
      })
    } finally {
      setAiLoading(false)
    }
  }

  // Auto-detect foreign key relationships
  const detectForeignKeys = (targetTable: string) => {
    const sourceColumns = baseTableSchema?.columns || []
    const targetTableSchema = schema.find(t => t.name === targetTable)
    const targetColumns = targetTableSchema?.columns || []

    // Check for FK from source to target
    const sourceFk = sourceColumns.find(c => c.foreignKey?.table === targetTable)
    if (sourceFk) {
      setNewJoin(prev => ({
        ...prev,
        sourceColumn: sourceFk.name,
        targetColumn: sourceFk.foreignKey!.column
      }))
      return
    }

    // Check for FK from target to source
    const targetFk = targetColumns.find(c => c.foreignKey?.table === baseTable)
    if (targetFk) {
      setNewJoin(prev => ({
        ...prev,
        sourceColumn: targetFk.foreignKey!.column,
        targetColumn: targetFk.name
      }))
      return
    }

    // Clear if no FK detected
    setNewJoin(prev => ({
      ...prev,
      sourceColumn: undefined,
      targetColumn: undefined
    }))
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h4 className="text-sm font-medium flex items-center gap-2">
          <Link2 className="h-4 w-4" />
          Table Joins
          <Badge variant="outline" className="ml-2">
            {joins.length}
          </Badge>
        </h4>
        {onAISuggest && (
          <Button
            variant="outline"
            size="sm"
            onClick={handleAISuggest}
            disabled={aiLoading}
            className="gap-2"
          >
            {aiLoading ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Sparkles className="h-4 w-4" />
            )}
            Auto-detect
          </Button>
        )}
      </div>

      {/* Existing Joins */}
      {joins.length > 0 && (
        <ScrollArea className="max-h-[200px]">
          <div className="space-y-2">
            {joins.map((join) => (
              <div
                key={join.id}
                className="flex items-center gap-2 p-3 rounded-lg bg-surface-elevated border border-border"
              >
                <div className="flex-1 flex items-center gap-2 text-sm">
                  <Badge variant="outline">{join.type.toUpperCase()}</Badge>
                  <span className="font-medium">{join.sourceTable}</span>
                  <span className="text-muted-foreground">.{join.sourceColumn}</span>
                  <ArrowRight className="h-4 w-4 text-muted-foreground" />
                  <span className="font-medium">{join.targetTable}</span>
                  <span className="text-muted-foreground">.{join.targetColumn}</span>
                </div>
                <Select
                  value={join.type}
                  onValueChange={(type) => onUpdateJoin(join.id, { type: type as ViewJoin['type'] })}
                >
                  <SelectTrigger className="w-32 h-8">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {JOIN_TYPES.map((jt) => (
                      <SelectItem key={jt.value} value={jt.value}>
                        {jt.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-destructive hover:text-destructive"
                  onClick={() => onRemoveJoin(join.id)}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            ))}
          </div>
        </ScrollArea>
      )}

      {/* Add New Join */}
      {availableTables.length > 0 && (
        <div className="p-4 rounded-lg border border-dashed border-border space-y-3">
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Plus className="h-4 w-4" />
            Add new join
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <label className="text-xs text-muted-foreground">Join Type</label>
              <Select
                value={newJoin.type || 'left'}
                onValueChange={(type) => setNewJoin(prev => ({ ...prev, type: type as ViewJoin['type'] }))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {JOIN_TYPES.map((jt) => (
                    <SelectItem key={jt.value} value={jt.value}>
                      <div className="flex items-center gap-2">
                        {jt.label}
                        <TooltipProvider>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Info className="h-3 w-3 text-muted-foreground" />
                            </TooltipTrigger>
                            <TooltipContent>{jt.description}</TooltipContent>
                          </Tooltip>
                        </TooltipProvider>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <label className="text-xs text-muted-foreground">Target Table</label>
              <Select
                value={newJoin.targetTable}
                onValueChange={(table) => {
                  setNewJoin(prev => ({ ...prev, targetTable: table }))
                  detectForeignKeys(table)
                }}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select table" />
                </SelectTrigger>
                <SelectContent>
                  {availableTables.map((table) => (
                    <SelectItem key={table.name} value={table.name}>
                      {table.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {newJoin.targetTable && (
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-2">
                <label className="text-xs text-muted-foreground">
                  {baseTable} column
                </label>
                <Select
                  value={newJoin.sourceColumn}
                  onValueChange={(col) => setNewJoin(prev => ({ ...prev, sourceColumn: col }))}
                >
                  <SelectTrigger className={cn(newJoin.sourceColumn && 'border-success')}>
                    <SelectValue placeholder="Select column" />
                  </SelectTrigger>
                  <SelectContent>
                    {getTableColumns(baseTable).map((col) => (
                      <SelectItem key={col.name} value={col.name}>
                        <div className="flex items-center gap-2">
                          {col.name}
                          {col.foreignKey && (
                            <Badge variant="outline" className="text-xs">
                              FK → {col.foreignKey.table}
                            </Badge>
                          )}
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-2">
                <label className="text-xs text-muted-foreground">
                  {newJoin.targetTable} column
                </label>
                <Select
                  value={newJoin.targetColumn}
                  onValueChange={(col) => setNewJoin(prev => ({ ...prev, targetColumn: col }))}
                >
                  <SelectTrigger className={cn(newJoin.targetColumn && 'border-success')}>
                    <SelectValue placeholder="Select column" />
                  </SelectTrigger>
                  <SelectContent>
                    {getTableColumns(newJoin.targetTable).map((col) => (
                      <SelectItem key={col.name} value={col.name}>
                        <div className="flex items-center gap-2">
                          {col.name}
                          {col.foreignKey && (
                            <Badge variant="outline" className="text-xs">
                              FK → {col.foreignKey.table}
                            </Badge>
                          )}
                        </div>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          )}

          <Button
            onClick={handleAddJoin}
            disabled={!newJoin.targetTable || !newJoin.sourceColumn || !newJoin.targetColumn}
            className="w-full"
          >
            <Plus className="h-4 w-4 mr-2" />
            Add Join
          </Button>
        </div>
      )}

      {availableTables.length === 0 && joins.length > 0 && (
        <div className="text-center py-4 text-sm text-muted-foreground">
          All available tables have been joined
        </div>
      )}
    </div>
  )
}
