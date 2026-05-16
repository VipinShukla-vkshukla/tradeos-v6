'use client';

import { useState, useEffect, useMemo } from 'react';
import { Settings, Search, Save, X, Edit2, RefreshCw, Info } from 'lucide-react';
import { Panel, KPICard } from '@/components/core/Panel';
import { DataGuard, SkeletonTable } from '@/components/core/DataGuard';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';
import { formatDate } from '@/lib/formatters';
import { queries } from '@/lib/supabase';
import { configApi } from '@/lib/api';
import type { ConfigEntry } from '@/types/database';

// Semantic category map based on key prefixes
const CATEGORY_ORDER = ['AI', 'BRAIN', 'REGIME', 'SCREENER', 'TRADING', 'PIPELINE', 'ALERT', 'GENERAL'];

function categorize(key: string): string {
  const k = key.toUpperCase();
  if (k.startsWith('AI_')) return 'AI';
  if (k.startsWith('BRAIN_')) return 'BRAIN';
  if (k.startsWith('REGIME_') || k.includes('REGIME')) return 'REGIME';
  if (k.startsWith('SCREENER_') || k.includes('SCREEN')) return 'SCREENER';
  if (k.startsWith('TRADE_') || k.startsWith('POSITION_') || k.startsWith('RISK_') || k.includes('CAPITAL') || k.includes('CAPITAL')) return 'TRADING';
  if (k.startsWith('PIPELINE_') || k.startsWith('RUN_') || k.includes('PIPELINE')) return 'PIPELINE';
  if (k.startsWith('ALERT_') || k.startsWith('TELEGRAM_') || k.includes('ALERT')) return 'ALERT';
  return 'GENERAL';
}

// Value type inference for display
function inferType(value: string): 'boolean' | 'number' | 'list' | 'text' {
  if (value === 'true' || value === 'false') return 'boolean';
  if (!isNaN(Number(value)) && value !== '') return 'number';
  if (value.includes(',')) return 'list';
  return 'text';
}

function ValueDisplay({ value }: { value: string }) {
  const type = inferType(value);
  if (type === 'boolean') {
    return (
      <span className={`text-xs font-medium font-mono ${value === 'true' ? 'text-profit' : 'text-muted-foreground'}`}>
        {value}
      </span>
    );
  }
  if (type === 'number') {
    return <span className="text-xs font-mono text-blue-400">{value}</span>;
  }
  if (type === 'list') {
    return (
      <div className="flex flex-wrap gap-1">
        {value.split(',').map((v, i) => (
          <span key={i} className="text-xs px-1 py-0.5 rounded bg-muted text-muted-foreground font-mono">{v.trim()}</span>
        ))}
      </div>
    );
  }
  return <span className="text-xs font-mono">{value}</span>;
}

function ConfigRow({ entry, onSave }: { entry: ConfigEntry; onSave: (key: string, value: string) => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(entry.value);
  const [saving, setSaving] = useState(false);

  async function save() {
    setSaving(true);
    try {
      await onSave(entry.key, draft);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <tr className="border-b border-border/40 hover:bg-panel-hover/60 transition-colors group">
      <td className="py-2.5 px-3 w-48">
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-xs text-blue-400 break-all">{entry.key}</span>
          {entry.description && (
            <TooltipProvider delayDuration={200}>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Info className="h-3 w-3 text-muted-foreground/50 shrink-0 cursor-help" />
                </TooltipTrigger>
                <TooltipContent className="max-w-xs text-xs">{entry.description}</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
        </div>
      </td>
      <td className="py-2.5 px-3">
        {editing ? (
          <Input value={draft} onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') save(); if (e.key === 'Escape') { setDraft(entry.value); setEditing(false); } }}
            className="h-6 text-xs font-mono w-48" autoFocus />
        ) : (
          <ValueDisplay value={entry.value} />
        )}
      </td>
      <td className="py-2.5 px-3 text-xs text-muted-foreground whitespace-nowrap">
        {formatDate(entry.updated_at, 'dd MMM HH:mm')}
      </td>
      <td className="py-2.5 px-3">
        {editing ? (
          <div className="flex items-center gap-1">
            <Button size="sm" variant="outline" className="h-6 text-xs" onClick={save} disabled={saving}>
              <Save className="h-3 w-3 mr-1" />{saving ? '...' : 'Save'}
            </Button>
            <Button size="sm" variant="ghost" className="h-6 w-6 p-0" onClick={() => { setDraft(entry.value); setEditing(false); }}>
              <X className="h-3 w-3" />
            </Button>
          </div>
        ) : (
          <Button size="sm" variant="ghost"
            className="h-6 text-xs opacity-0 group-hover:opacity-100 transition-opacity"
            onClick={() => setEditing(true)}>
            <Edit2 className="h-3 w-3 mr-1" />Edit
          </Button>
        )}
      </td>
    </tr>
  );
}

// ─── Main Tab ─────────────────────────────────────────────────────────────
export function DataManagementTab() {
  const [config, setConfig] = useState<ConfigEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [activeCategory, setActiveCategory] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const { data, error: e } = await queries.getSystemConfig();
      if (e) throw e;
      setConfig(data ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleSave(key: string, value: string) {
    await configApi.update(key, value, 'Dashboard edit');
    setConfig((prev) => prev.map((c) => c.key === key ? { ...c, value, updated_at: new Date().toISOString() } : c));
    setSaveMsg(`✓ Saved ${key}`);
    setTimeout(() => setSaveMsg(null), 3000);
  }

  // Group and filter
  const grouped = useMemo(() => {
    const filtered = config.filter((c) =>
      !search || c.key.toLowerCase().includes(search.toLowerCase()) ||
      (c.description ?? '').toLowerCase().includes(search.toLowerCase()) ||
      c.value.toLowerCase().includes(search.toLowerCase())
    );

    const map: Record<string, ConfigEntry[]> = {};
    for (const entry of filtered) {
      const cat = categorize(entry.key);
      if (!map[cat]) map[cat] = [];
      map[cat].push(entry);
    }
    return map;
  }, [config, search]);

  const categories = CATEGORY_ORDER.filter((c) => grouped[c]?.length > 0);
  const displayCategories = activeCategory ? [activeCategory] : categories;
  const errObj = error ? new Error(error) : null;

  return (
    <div className="space-y-4">
      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {loading ? null : (
          <>
            <KPICard title="Config Keys" value={config.length.toString()}
              description="Total keys in system_config" icon={<Settings className="h-4 w-4" />} />
            {['AI', 'BRAIN', 'TRADING'].map((cat) => (
              <KPICard key={cat} title={`${cat} Keys`}
                value={(grouped[cat]?.length ?? 0).toString()}
                description={`Click to filter`} icon={<Settings className="h-4 w-4" />} />
            ))}
          </>
        )}
      </div>

      {saveMsg && (
        <div className="bg-green-500/10 border border-green-500/20 rounded px-4 py-2 text-sm text-green-400">
          {saveMsg}
        </div>
      )}

      <Panel title={`System Configuration — ${config.length} keys`}
        description="Edit any value inline. Changes write to system_config and are logged to config_change_log."
        dataSource="supabase" tableName="system_config" isLoading={loading}
        toolbar={
          <div className="flex items-center gap-2">
            {categories.map((cat) => (
              <Button key={cat} size="sm" variant={activeCategory === cat ? 'default' : 'outline'}
                className="h-6 text-xs" onClick={() => setActiveCategory(activeCategory === cat ? null : cat)}>
                {cat}
              </Button>
            ))}
            <div className="relative w-36">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-muted-foreground" />
              <Input value={search} onChange={(e) => setSearch(e.target.value)}
                placeholder="Search keys..." className="pl-6 h-6 text-xs" />
            </div>
            <Button size="sm" variant="ghost" className="h-6 w-6 p-0" onClick={load}>
              <RefreshCw className="h-3 w-3" />
            </Button>
          </div>
        }>
        <DataGuard data={config} isLoading={loading} error={errObj}
          loadingContent={<SkeletonTable rows={10} cols={4} />}
          emptyTitle="No config keys" emptyDescription="system_config table is empty">
          {() => (
            <div className="space-y-6">
              {displayCategories.map((cat) => (
                <div key={cat}>
                  <div className="flex items-center gap-2 mb-2 px-3">
                    <div className="text-xs font-semibold text-muted-foreground uppercase tracking-widest">{cat}</div>
                    <div className="flex-1 h-px bg-border/50" />
                    <div className="text-xs text-muted-foreground">{grouped[cat]?.length} keys</div>
                  </div>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/30">
                        {['Key', 'Value', 'Last Updated', 'Action'].map((h) => (
                          <th key={h} className="text-left py-2 px-3 text-xs font-medium text-muted-foreground/70">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {grouped[cat]?.map((entry) => (
                        <ConfigRow key={entry.key} entry={entry} onSave={handleSave} />
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </div>
          )}
        </DataGuard>
      </Panel>
    </div>
  );
}

export default DataManagementTab;
