'use client';

import { useState, useEffect } from 'react';
import { RefreshCw, Trash2 } from 'lucide-react';
import { Panel } from '@/components/core/Panel';
import { DataGuard, SkeletonTable } from '@/components/core/DataGuard';
import { Button } from '@/components/ui/button';
import { queryTable } from '@/lib/supabase';
import { viewsApi } from '@/lib/api';
import { useViewStore } from '@/store/viewStore';
import { useLayoutStore } from '@/store/layoutStore';
import type { DynamicView } from '@/types/registry';

interface LiveDataViewProps {
  view: DynamicView;
}

export function LiveDataView({ view }: LiveDataViewProps) {
  const [data, setData] = useState<Record<string, unknown>[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const { deleteView } = useViewStore();
  const { activeTabId, removeViewFromTab } = useLayoutStore();

  const baseTable = view.primaryTable || (view as { baseTable?: string }).baseTable || '';

  // ✅ Guard: ensure cols is always an array regardless of what view.columns is
  const cols = Array.isArray(view.columns) ? view.columns : [];

  // Build column select string — if no columns defined, fetch everything
  const selectColumns = cols.length > 0
    ? cols
        .filter((c) => c.visible !== false)
        .map((c) => c.alias ? `${c.sourceColumn}:${c.alias}` : c.sourceColumn)
        .join(', ')
    : '*';

  // Build filters from view.filters
  const filterObj: Record<string, unknown> = {};
  for (const f of view.filters || []) {
    if (f.value !== undefined && f.value !== '') {
      filterObj[f.column] = f.operator === 'eq' ? f.value : { [f.operator]: f.value };
    }
  }

  async function load() {
    if (!baseTable) return;
    setIsLoading(true);
    setError(null);
    try {
      const result = await queryTable<Record<string, unknown>>(baseTable, {
        select: selectColumns,
        filter: Object.keys(filterObj).length > 0 ? filterObj : undefined,
        order: view.sorts?.[0]
          ? { column: view.sorts[0].column, ascending: view.sorts[0].direction === 'asc' }
          : undefined,
        limit: 100,
      });
      if (result.error) throw result.error;
      setData(result.data ?? []);
    } catch (e) {
      setError(e instanceof Error ? e : new Error('Query failed'));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => { load(); }, [view.id]);

  async function handleDelete() {
    if (!confirm(`Delete view "${view.name}"?`)) return;
    try {
      await viewsApi.delete(view.id);
    } catch { /* ignore — might not be in Supabase yet */ }
    deleteView(view.id);
    removeViewFromTab(activeTabId, view.id);
  }

  // ✅ Derive visible columns:
  //   1. Use view.columns if defined and non-empty
  //   2. Fall back to keys from first data row (works when columns is undefined/empty)
  const visibleColumns = cols.length > 0
    ? cols.filter((c) => c.visible !== false)
    : data.length > 0
      ? Object.keys(data[0]).map((k) => ({
          id: k,
          sourceColumn: k,
          sourceTable: baseTable,
          alias: undefined,
          visible: true,
          type: 'text' as const,
        }))
      : [];

  return (
    <Panel
      title={view.name}
      description={`${baseTable}${view.joins?.length ? ` + ${view.joins.length} join(s)` : ''} — ${data.length} rows`}
      dataSource="supabase"
      tableName={baseTable}
      isLoading={isLoading}
      toolbar={
        <div className="flex items-center gap-1">
          <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={load}>
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
          <Button size="sm" variant="ghost" className="h-7 text-xs text-destructive hover:text-destructive" onClick={handleDelete}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      }
    >
      <DataGuard
        data={data}
        isLoading={isLoading}
        error={error}
        loadingContent={<SkeletonTable rows={5} cols={visibleColumns.length || 5} />}
        emptyTitle="No data"
        emptyDescription={`No rows returned from ${baseTable}`}
      >
        {(rows) => (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  {visibleColumns.map((col) => (
                    <th
                      key={col.id || col.sourceColumn}
                      className="text-left py-2.5 px-3 font-medium text-muted-foreground text-xs"
                    >
                      {col.alias || col.sourceColumn}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i} className="border-b border-border/50 hover:bg-panel-hover transition-colors">
                    {visibleColumns.map((col) => {
                      const key = col.alias || col.sourceColumn;
                      const val = row[key] ?? row[col.sourceColumn];
                      return (
                        <td key={col.id || col.sourceColumn} className="py-2.5 px-3 text-xs">
                          {val == null
                            ? <span className="text-muted-foreground">—</span>
                            : typeof val === 'boolean'
                              ? (val ? '✓' : '✗')
                              : typeof val === 'object'
                                ? JSON.stringify(val).slice(0, 60)
                                : String(val)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </DataGuard>
    </Panel>
  );
}