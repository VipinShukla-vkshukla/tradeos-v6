'use client'

import { useRef, useMemo, useState, useCallback } from 'react'
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  flexRender,
  ColumnDef,
  SortingState,
  ColumnFiltersState,
  VisibilityState,
  RowSelectionState,
} from '@tanstack/react-table'
import { useVirtualizer } from '@tanstack/react-virtual'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { DataGuard } from './DataGuard'
import { EnhanceMenu } from '@/components/views/EnhanceMenu'
import {
  ArrowUpDown,
  ArrowUp,
  ArrowDown,
  Search,
  Columns3,
  Download,
  RefreshCw,
  Loader2
} from 'lucide-react'

interface VirtualTableProps<T extends Record<string, unknown>> {
  data: T[]
  columns: ColumnDef<T>[]
  isLoading?: boolean
  error?: Error | null
  onRefresh?: () => void
  onExport?: (format: 'csv' | 'json') => void
  rowHeight?: number
  searchable?: boolean
  searchPlaceholder?: string
  emptyTitle?: string
  emptyDescription?: string
  className?: string
  panelId?: string
}

export function VirtualTable<T extends Record<string, unknown>>({
  data,
  columns,
  isLoading = false,
  error = null,
  onRefresh,
  onExport,
  rowHeight = 48,
  searchable = true,
  searchPlaceholder = 'Search...',
  emptyTitle = 'No data available',
  emptyDescription = 'Data will appear here once available',
  className,
  panelId = 'virtual-table'
}: VirtualTableProps<T>) {
  const parentRef = useRef<HTMLDivElement>(null)
  
  const [sorting, setSorting] = useState<SortingState>([])
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([])
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>({})
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})
  const [globalFilter, setGlobalFilter] = useState('')

  const table = useReactTable({
    data,
    columns,
    state: {
      sorting,
      columnFilters,
      columnVisibility,
      rowSelection,
      globalFilter,
    },
    enableRowSelection: true,
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onColumnVisibilityChange: setColumnVisibility,
    onRowSelectionChange: setRowSelection,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  })

  const { rows } = table.getRowModel()

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => rowHeight,
    overscan: 10,
  })

  const virtualRows = virtualizer.getVirtualItems()
  const totalSize = virtualizer.getTotalSize()

  const paddingTop = virtualRows.length > 0 ? virtualRows[0]?.start || 0 : 0
  const paddingBottom = virtualRows.length > 0
    ? totalSize - (virtualRows[virtualRows.length - 1]?.end || 0)
    : 0

  const handleExportCSV = useCallback(() => {
    const headers = table.getVisibleLeafColumns().map(col => col.id)
    const csvRows = [
      headers.join(','),
      ...rows.map(row => 
        headers.map(header => {
          const value = row.getValue(header)
          if (typeof value === 'string' && value.includes(',')) {
            return `"${value}"`
          }
          return String(value ?? '')
        }).join(',')
      )
    ]
    
    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${panelId}-export.csv`
    a.click()
    URL.revokeObjectURL(url)
  }, [rows, table, panelId])

  const handleExportJSON = useCallback(() => {
    const exportData = rows.map(row => row.original)
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${panelId}-export.json`
    a.click()
    URL.revokeObjectURL(url)
  }, [rows, panelId])

  const visibleColumns = useMemo(() => 
    table.getAllColumns()
      .filter(col => col.getCanHide())
      .map(col => ({
        id: col.id,
        name: col.id.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase()),
        visible: col.getIsVisible()
      })),
    [table, columnVisibility]
  )

  return (
    <DataGuard
      data={data}
      isLoading={isLoading}
      error={error}
      emptyTitle={emptyTitle}
      emptyDescription={emptyDescription}
    >
      <div className={cn('flex flex-col h-full', className)}>
        {/* Toolbar */}
        <div className="flex items-center gap-2 p-3 border-b border-border">
          {searchable && (
            <div className="relative flex-1 max-w-sm">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder={searchPlaceholder}
                value={globalFilter}
                onChange={(e) => setGlobalFilter(e.target.value)}
                className="pl-9 h-8"
              />
            </div>
          )}
          
          <div className="flex items-center gap-1 ml-auto">
            <Badge variant="outline" className="text-xs">
              {rows.length} rows
            </Badge>
            
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="h-8 gap-1">
                  <Columns3 className="h-4 w-4" />
                  Columns
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                {table.getAllColumns()
                  .filter((column) => column.getCanHide())
                  .map((column) => (
                    <DropdownMenuCheckboxItem
                      key={column.id}
                      checked={column.getIsVisible()}
                      onCheckedChange={(value) => column.toggleVisibility(!!value)}
                    >
                      {column.id.replace(/_/g, ' ')}
                    </DropdownMenuCheckboxItem>
                  ))}
              </DropdownMenuContent>
            </DropdownMenu>

            {onRefresh && (
              <Button variant="ghost" size="sm" className="h-8" onClick={onRefresh}>
                {isLoading ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )}
              </Button>
            )}

            <EnhanceMenu
              panelId={panelId}
              panelType="table"
              columns={visibleColumns}
              onToggleColumn={(id, visible) => {
                const col = table.getColumn(id)
                col?.toggleVisibility(visible)
              }}
              onExport={(format) => {
                if (format === 'csv') handleExportCSV()
                else handleExportJSON()
              }}
              onRefresh={onRefresh}
            />
          </div>
        </div>

        {/* Table Container */}
        <div ref={parentRef} className="flex-1 overflow-auto">
          <table className="w-full border-collapse">
            <thead className="sticky top-0 z-10 bg-surface">
              {table.getHeaderGroups().map((headerGroup) => (
                <tr key={headerGroup.id}>
                  {headerGroup.headers.map((header) => (
                    <th
                      key={header.id}
                      className="px-4 py-3 text-left text-xs font-medium text-muted-foreground uppercase tracking-wider border-b border-border bg-surface"
                      style={{ width: header.getSize() }}
                    >
                      {header.isPlaceholder ? null : (
                        <div
                          className={cn(
                            'flex items-center gap-2',
                            header.column.getCanSort() && 'cursor-pointer select-none hover:text-foreground'
                          )}
                          onClick={header.column.getToggleSortingHandler()}
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          {header.column.getCanSort() && (
                            <span className="w-4">
                              {header.column.getIsSorted() === 'asc' ? (
                                <ArrowUp className="h-3 w-3" />
                              ) : header.column.getIsSorted() === 'desc' ? (
                                <ArrowDown className="h-3 w-3" />
                              ) : (
                                <ArrowUpDown className="h-3 w-3 opacity-50" />
                              )}
                            </span>
                          )}
                        </div>
                      )}
                    </th>
                  ))}
                </tr>
              ))}
            </thead>
            <tbody>
              {paddingTop > 0 && (
                <tr>
                  <td style={{ height: `${paddingTop}px` }} colSpan={columns.length} />
                </tr>
              )}
              {virtualRows.map((virtualRow) => {
                const row = rows[virtualRow.index]
                return (
                  <tr
                    key={row.id}
                    data-state={row.getIsSelected() && 'selected'}
                    className="border-b border-border hover:bg-surface-elevated/50 transition-colors"
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td
                        key={cell.id}
                        className="px-4 py-3 text-sm"
                        style={{ width: cell.column.getSize() }}
                      >
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                )
              })}
              {paddingBottom > 0 && (
                <tr>
                  <td style={{ height: `${paddingBottom}px` }} colSpan={columns.length} />
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </DataGuard>
  )
}
