'use client';

import { useState } from 'react';
import {
  Sparkles, Loader2, Database, CheckCircle, Plus,
  TrendingUp, Brain, Cpu, BarChart2, Table2, RefreshCw, AlertCircle,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import { useLayoutStore } from '@/store/layoutStore';
import { useViewStore } from '@/store/viewStore';
import type { TableProfile } from '@/app/api/profile/route';

// ─── Table groups ──────────────────────────────────────────────────────────
const TABLE_GROUPS: Array<{
  tabName: string;
  icon: string;
  tables: string[];
  description: string;
}> = [
  {
    tabName: 'Trading Signals',
    icon: 'TrendingUp',
    tables: ['signal_log', 'master_shortlist'],
    description: 'Signal generation, conviction and filtering analysis',
  },
  {
    tabName: 'Market Regime',
    icon: 'BarChart2',
    tables: ['market_regime', 'regime_history', 'fii_dii_flow', 'sector_strength'],
    description: 'Regime classification, FII/DII flows, sector rotation',
  },
  {
    tabName: 'Portfolio Health',
    icon: 'Brain',
    tables: ['open_positions', 'closed_positions', 'order_history', 'shadow_trades'],
    description: 'Position tracking, execution quality, order history',
  },
  {
    tabName: 'AI Performance',
    icon: 'Cpu',
    tables: ['ai_model_performance', 'ai_context', 'lessons'],
    description: 'AI provider stats, conviction accuracy, system lessons',
  },
  {
    tabName: 'System Evolution',
    icon: 'Sparkles',
    tables: ['brain_proposals', 'brain_analysis_log', 'evolution_proposals'],
    description: 'Brain engine proposals, applied changes, system improvement',
  },
  {
    tabName: 'Data Quality',
    icon: 'Database',
    tables: ['data_anomalies', 'performance_metrics', 'screener_performance'],
    description: 'Data anomalies, pipeline performance, screener output quality',
  },
];

interface TabSuggestion {
  tabName: string;
  icon: string;
  description: string;
  tables: string[];
  rowCounts: Record<string, number>;
}

type SuggestionStatus = 'new' | 'exists';

export function SmartTabGenerator() {
  const [open,        setOpen]        = useState(false);
  const [scanning,    setScanning]    = useState(false);
  const [suggestions, setSuggestions] = useState<TabSuggestion[]>([]);
  const [selected,    setSelected]    = useState<Set<string>>(new Set());
  const [creating,    setCreating]    = useState(false);
  const [done,        setDone]        = useState<{ created: number; updated: number } | null>(null);
  const [scanError,   setScanError]   = useState<string | null>(null);
  // When true, running the generator on an existing tab will replace its
  // views with a fresh set instead of skipping it.
  const [updateMode,  setUpdateMode]  = useState(false);

  const { addTab, removeViewFromTab, tabs } = useLayoutStore();
  const { addView, views, deleteView }     = useViewStore();

  // ── Helpers ─────────────────────────────────────────────────────────────
  function getTabStatus(tabName: string): SuggestionStatus {
    return tabs.some((t) => t.name === tabName) ? 'exists' : 'new';
  }

  // ── Scan ─────────────────────────────────────────────────────────────────
  async function scanTables() {
    setScanning(true);
    setScanError(null);
    try {
      const res = await fetch('/api/profile');
      if (!res.ok) throw new Error(`Profile API returned ${res.status}`);
      const profiles: TableProfile[] = await res.json();
      const profileMap = new Map(profiles.map((p) => [p.table, p]));

      const newSuggestions: TabSuggestion[] = [];

      for (const group of TABLE_GROUPS) {
        const tablesWithData = group.tables.filter((t) => {
          const p = profileMap.get(t);
          return p && p.row_count > 0;
        });
        if (tablesWithData.length > 0) {
          const rowCounts: Record<string, number> = {};
          tablesWithData.forEach((t) => { rowCounts[t] = profileMap.get(t)?.row_count ?? 0; });
          newSuggestions.push({
            tabName: group.tabName,
            icon: group.icon,
            description: group.description,
            tables: tablesWithData,
            rowCounts,
          });
        }
      }

      // Uncovered tables with data → "Other Data" group
      const covered = new Set(TABLE_GROUPS.flatMap((g) => g.tables));
      const uncovered = profiles.filter((p) => !covered.has(p.table) && p.row_count > 0);
      if (uncovered.length > 0) {
        newSuggestions.push({
          tabName: 'Other Data',
          icon: 'Database',
          description: `${uncovered.length} additional tables with live data`,
          tables: uncovered.map((p) => p.table),
          rowCounts: Object.fromEntries(uncovered.map((p) => [p.table, p.row_count])),
        });
      }

      setSuggestions(newSuggestions);
      // Auto-select all (including existing ones — update mode toggle controls behaviour)
      setSelected(new Set(newSuggestions.map((s) => s.tabName)));
    } catch (e) {
      setScanError(e instanceof Error ? e.message : 'Scan failed');
    } finally {
      setScanning(false);
    }
  }

  function handleOpen() {
    setOpen(true);
    setDone(null);
    setSuggestions([]);
    setSelected(new Set());
    setScanError(null);
    scanTables();
  }

  function toggleSuggestion(tabName: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(tabName) ? next.delete(tabName) : next.add(tabName);
      return next;
    });
  }

  // ── Create / Update ───────────────────────────────────────────────────────
  async function handleCreate() {
    setCreating(true);
    let created = 0;
    let updated = 0;

    // Snapshot current store state once — avoids stale closure reads inside loop
    const currentTabs  = useLayoutStore.getState().tabs;
    const currentViews = useViewStore.getState().views;

    for (const suggestion of suggestions) {
      if (!selected.has(suggestion.tabName)) continue;

      const existingTab = currentTabs.find((t) => t.name === suggestion.tabName);

      if (existingTab && !updateMode) continue; // skip existing when not in update mode

      if (existingTab && updateMode) {
        // ── Update mode: remove all existing auto views for this tab,
        //    then add fresh ones with new IDs.
        const existingViewIds = useLayoutStore.getState().getTabViews(existingTab.id);
        for (const vid of existingViewIds) {
          removeViewFromTab(existingTab.id, vid);
          // Remove from viewStore too (only auto views — leave manual ones alone)
          const v = currentViews.find((view) => view.id === vid);
          if ((v as { viewType?: string } | undefined)?.viewType === 'auto') {
            deleteView(vid);
          }
        }

        // Add fresh views
        for (const table of suggestion.tables) {
          // ── FIX: crypto.randomUUID() instead of Date.now() ──────────────
          // Date.now() inside a fast loop produces the same millisecond
          // timestamp for multiple iterations → colliding IDs → broken views.
          const viewId = crypto.randomUUID();
          await addView({
            id: viewId,
            name: humanise(table),
            description: `Auto-generated · ${suggestion.rowCounts[table].toLocaleString()} rows`,
            primaryTable: table,
            baseTable: table,
            columns: [],
            filters: [],
            joins: [],
            sorts: [],
            viewType: 'auto',
            sourceMode: 'single_table',
            createdAt: new Date().toISOString(),
            updatedAt: new Date().toISOString(),
          } as never);
          useLayoutStore.getState().addViewToTab(existingTab.id, viewId);
        }
        updated++;
        continue;
      }

      // ── Create new tab ────────────────────────────────────────────────────
      const tabId = crypto.randomUUID(); // ← was `auto-${Date.now()}-…` before
      addTab({
        id: tabId,
        name: suggestion.tabName,
        icon: suggestion.icon,
        isCore: false,
        isVisible: true,
        order: 99,
        views: [],
      });

      for (const table of suggestion.tables) {
        const viewId = crypto.randomUUID(); // ← was `auto-view-${table}-${Date.now()}`
        await addView({
          id: viewId,
          name: humanise(table),
          description: `Auto-generated · ${suggestion.rowCounts[table].toLocaleString()} rows`,
          primaryTable: table,
          baseTable: table,
          columns: [],
          filters: [],
          joins: [],
          sorts: [],
          viewType: 'auto',
          sourceMode: 'single_table',
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
        } as never);
        useLayoutStore.getState().addViewToTab(tabId, viewId);
      }
      created++;
    }

    setCreating(false);
    setDone({ created, updated });
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function humanise(table: string) {
    return table.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  const iconMap: Record<string, React.ElementType> = {
    TrendingUp, Brain, Cpu, BarChart2, Sparkles, Database, Table2,
  };

  // Counts for footer
  const newCount      = suggestions.filter((s) => selected.has(s.tabName) && getTabStatus(s.tabName) === 'new').length;
  const existingCount = suggestions.filter((s) => selected.has(s.tabName) && getTabStatus(s.tabName) === 'exists').length;

  return (
    <>
      <Button onClick={handleOpen} className="flex items-center gap-2">
        <Sparkles className="h-4 w-4" />
        Auto-Generate Tabs
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Sparkles className="h-5 w-5 text-indigo-400" />
              Smart Tab Generator
            </DialogTitle>
            <DialogDescription>
              Scans all your Supabase tables, identifies which have data, and auto-creates
              tabs with the right visualizations — no configuration needed.
            </DialogDescription>
          </DialogHeader>

          {/* Scanning */}
          {scanning && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <Loader2 className="h-8 w-8 animate-spin text-indigo-400" />
              <p className="text-sm text-muted-foreground">Profiling your Supabase tables…</p>
              <p className="text-xs text-muted-foreground">Sampling data, detecting column types, building chart plans</p>
            </div>
          )}

          {/* Scan error */}
          {!scanning && scanError && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <AlertCircle className="h-8 w-8 text-red-400" />
              <p className="text-sm text-red-400">{scanError}</p>
              <Button variant="outline" onClick={scanTables}><RefreshCw className="h-4 w-4 mr-2" />Retry scan</Button>
            </div>
          )}

          {/* Done */}
          {!scanning && done && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <CheckCircle className="h-10 w-10 text-green-400" />
              <p className="font-semibold text-green-400">
                {done.created > 0 && `${done.created} tab${done.created !== 1 ? 's' : ''} created`}
                {done.created > 0 && done.updated > 0 && ' · '}
                {done.updated > 0 && `${done.updated} tab${done.updated !== 1 ? 's' : ''} updated`}
                {done.created === 0 && done.updated === 0 && 'Nothing to do — all tabs already up to date'}
              </p>
              <p className="text-sm text-muted-foreground">Switch to any tab to see auto-generated charts</p>
              <Button onClick={() => setOpen(false)}>Close</Button>
            </div>
          )}

          {/* Suggestions list */}
          {!scanning && !done && suggestions.length > 0 && (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <p className="text-sm text-muted-foreground">
                  {suggestions.length} tab group{suggestions.length !== 1 ? 's' : ''} found across live data.
                </p>
                <Button size="sm" variant="ghost" className="text-xs"
                  onClick={() => setSelected(new Set(suggestions.map((s) => s.tabName)))}>
                  Select all
                </Button>
              </div>

              {/* Update mode toggle */}
              <label className="flex items-center gap-2.5 cursor-pointer select-none text-sm p-2.5 rounded-lg border border-border/50 hover:border-border transition-colors">
                <div
                  onClick={() => setUpdateMode((v) => !v)}
                  className={`w-9 h-5 rounded-full transition-colors relative shrink-0 ${updateMode ? 'bg-indigo-500' : 'bg-muted'}`}
                >
                  <span className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform ${updateMode ? 'translate-x-4' : 'translate-x-0.5'}`} />
                </div>
                <div>
                  <span className="font-medium">Update existing tabs</span>
                  <span className="text-muted-foreground ml-1.5">
                    {updateMode
                      ? '— existing auto-generated views will be refreshed with current data'
                      : '— existing tabs will be skipped (default)'}
                  </span>
                </div>
              </label>

              <div className="space-y-2">
                {suggestions.map((s) => {
                  const Icon       = iconMap[s.icon] ?? Database;
                  const isSelected = selected.has(s.tabName);
                  const status     = getTabStatus(s.tabName);
                  const isExisting = status === 'exists';

                  return (
                    <button
                      key={s.tabName}
                      onClick={() => toggleSuggestion(s.tabName)}
                      className={`w-full text-left p-3 rounded-lg border transition-all ${
                        isSelected
                          ? isExisting && updateMode
                            ? 'border-amber-500/50 bg-amber-500/5'
                            : isExisting
                              ? 'border-border/40 bg-muted/30'
                              : 'border-indigo-500/50 bg-indigo-500/10'
                          : 'border-border/50 hover:border-border'
                      }`}
                    >
                      <div className="flex items-start gap-3">
                        {/* Checkbox */}
                        <div className={`w-5 h-5 rounded border-2 flex items-center justify-center shrink-0 mt-0.5 ${
                          isSelected
                            ? isExisting && updateMode
                              ? 'border-amber-400 bg-amber-400'
                              : isExisting
                                ? 'border-border bg-muted'
                                : 'border-indigo-400 bg-indigo-400'
                            : 'border-border'
                        }`}>
                          {isSelected && <CheckCircle className="h-3 w-3 text-white" />}
                        </div>
                        <Icon className="h-5 w-5 shrink-0 text-muted-foreground mt-0.5" />
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium">{s.tabName}</span>
                            {isExisting && (
                              <span className={`text-xs px-1.5 py-0.5 rounded ${
                                updateMode
                                  ? 'bg-amber-500/20 text-amber-400'
                                  : 'bg-muted text-muted-foreground'
                              }`}>
                                {updateMode ? '↻ Will update' : 'Already exists'}
                              </span>
                            )}
                            {!isExisting && isSelected && (
                              <span className="text-xs px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-400">
                                + New
                              </span>
                            )}
                          </div>
                          <p className="text-xs text-muted-foreground mt-0.5">{s.description}</p>
                          <div className="flex flex-wrap gap-1.5 mt-2">
                            {s.tables.map((t) => (
                              <span key={t} className="text-xs px-1.5 py-0.5 rounded bg-muted font-mono text-muted-foreground">
                                {t}
                                <span className="ml-1 text-foreground/60">({(s.rowCounts[t] ?? 0).toLocaleString()})</span>
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>

              {/* Footer */}
              <div className="flex justify-between items-center pt-2 border-t border-border">
                <div className="text-sm text-muted-foreground space-x-3">
                  {newCount > 0 && <span className="text-indigo-400">+{newCount} new</span>}
                  {existingCount > 0 && updateMode && <span className="text-amber-400">↻ {existingCount} update</span>}
                  {existingCount > 0 && !updateMode && <span>{existingCount} skipped</span>}
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
                  <Button
                    onClick={handleCreate}
                    disabled={selected.size === 0 || creating || (newCount === 0 && (!updateMode || existingCount === 0))}
                  >
                    {creating ? (
                      <><Loader2 className="h-4 w-4 mr-2 animate-spin" />Working…</>
                    ) : (
                      <>
                        <Plus className="h-4 w-4 mr-2" />
                        {newCount > 0 && updateMode && existingCount > 0
                          ? `Create ${newCount} · Update ${existingCount}`
                          : newCount > 0
                            ? `Create ${newCount} Tab${newCount !== 1 ? 's' : ''}`
                            : `Update ${existingCount} Tab${existingCount !== 1 ? 's' : ''}`
                        }
                      </>
                    )}
                  </Button>
                </div>
              </div>
            </div>
          )}

          {/* No data */}
          {!scanning && !done && !scanError && suggestions.length === 0 && (
            <div className="flex flex-col items-center justify-center py-12 gap-3">
              <Database className="h-10 w-10 text-muted-foreground/30" />
              <p className="text-sm text-muted-foreground">No tables with data found</p>
              <p className="text-xs text-muted-foreground">Run your pipeline to populate tables, then try again</p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}