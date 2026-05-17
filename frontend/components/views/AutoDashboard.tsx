'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import {
  Loader2, RefreshCw, Sparkles, AlertCircle,
  TrendingUp, BarChart2, PieChart, Maximize2, ChevronDown, ChevronUp, Check,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Panel, KPICard } from '@/components/core/Panel';
import { queryTable } from '@/lib/supabase';
import { generateVizPlan, buildQuerySpec } from '@/lib/autoViz';
import { useRealtime } from '@/hooks/useRealtime';
import type { TableProfile } from '@/app/api/profile/route';
import type { AutoVizPlan, ChartDecision, KPIDecision } from '@/lib/autoViz';
import type { AnalyzeResponse } from '@/app/api/ai/analyze/route';
import {
  LineChart, Line, AreaChart, Area, BarChart, Bar, ScatterChart, Scatter as ScatterDot,
  PieChart as RechartsPie, Pie, Cell, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts';

// ─── Types ─────────────────────────────────────────────────────────────────
interface EnhancedPlan extends AutoVizPlan {
  ai_improvements: string[];
  provider_used?: string;
  enhanced_at?: string;
}

// ─── Module-level runtime cache (survives tab navigation) ──────────────────
interface CacheEntry {
  profile: TableProfile;
  plan: AutoVizPlan;
  chartData: Record<string, Record<string, unknown>[]>;
  kpiData: Record<string, unknown>[];   // ← dedicated KPI dataset, new field
  ts: number;
}
const PROFILE_CACHE = new Map<string, CacheEntry>();
const CACHE_TTL_MS = 5 * 60 * 1000;

function getCached(table: string): CacheEntry | null {
  const entry = PROFILE_CACHE.get(table);
  if (!entry) return null;
  if (Date.now() - entry.ts > CACHE_TTL_MS) { PROFILE_CACHE.delete(table); return null; }
  return entry;
}

// ─── localStorage persistence for AI-enhanced plans ───────────────────────
// Enhanced plans survive page refreshes. User never needs to re-enhance
// unless they explicitly click "Reset to default".
const LS_KEY = (table: string) => `tradeos_enhanced_plan_v1_${table}`;

function loadEnhancedPlan(table: string): EnhancedPlan | null {
  try { const r = localStorage.getItem(LS_KEY(table)); return r ? JSON.parse(r) : null; }
  catch { return null; }
}
function saveEnhancedPlan(table: string, plan: EnhancedPlan) {
  try { localStorage.setItem(LS_KEY(table), JSON.stringify(plan)); } catch {}
}
function clearEnhancedPlan(table: string) {
  try { localStorage.removeItem(LS_KEY(table)); } catch {}
}

// ─── Fetch with hard client-side timeout ──────────────────────────────────
async function fetchWithTimeout(url: string, init: RequestInit & { timeoutMs?: number } = {}): Promise<Response> {
  const { timeoutMs = 15_000, signal: callerSignal, ...rest } = init;
  const tc = new AbortController();
  const timer = setTimeout(() => tc.abort(), timeoutMs);
  callerSignal?.addEventListener('abort', () => tc.abort());
  try {
    const res = await fetch(url, { ...rest, signal: tc.signal });
    clearTimeout(timer);
    return res;
  } catch (e) {
    clearTimeout(timer);
    if (e instanceof Error && e.name === 'AbortError' && !callerSignal?.aborted)
      throw new Error(`Timed out after ${timeoutMs / 1000}s — server warming up. Click Retry.`);
    throw e;
  }
}

// ─── Format helper ──────────────────────────────────────────────────────────
function fmt(value: unknown, format?: string): string {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  switch (format) {
    case 'currency_inr':
      if (isNaN(n)) return String(value);
      return `₹${Math.abs(n) >= 1e5 ? (n / 1e5).toFixed(1) + 'L' : Math.abs(n) >= 1000 ? (n / 1000).toFixed(1) + 'K' : n.toFixed(0)}`;
    case 'percent': return isNaN(n) ? String(value) : `${n.toFixed(1)}%`;
    case 'number':  return isNaN(n) ? String(value) : n % 1 === 0 ? n.toString() : n.toFixed(2);
    default:        return String(value);
  }
}

const CHART_COLORS = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#06b6d4', '#8b5cf6', '#f97316', '#84cc16'];

// ─── KPI Row ────────────────────────────────────────────────────────────────
// KEY FIX: receives `kpiData` — a dedicated fetch that selects only KPI columns.
// Previously this received chartData[chart0.id], which only contained the
// columns needed for that chart's axes (e.g. date + score), so delivery_pct
// and atr_pct were absent → NaN → '—'.
function AutoKPIRow({ kpis, kpiData }: { kpis: KPIDecision[]; kpiData: Record<string, unknown>[] }) {
  function computeKPI(kpi: KPIDecision): string {
    if (kpiData.length === 0) return '—';
    const values = kpiData.map((r) => Number(r[kpi.column])).filter((v) => !isNaN(v));
    if (values.length === 0) return '—';
    let result: number;
    switch (kpi.aggregate) {
      case 'sum':    result = values.reduce((a, b) => a + b, 0); break;
      case 'avg':    result = values.reduce((a, b) => a + b, 0) / values.length; break;
      case 'max':    result = Math.max(...values); break;
      case 'min':    result = Math.min(...values); break;
      case 'count':  result = values.length; break;
      case 'latest': result = values[0]; break;
      default:       result = values[0];
    }
    return fmt(result, kpi.format);
  }

  const icons = [TrendingUp, BarChart2, PieChart, Maximize2];
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
      {kpis.slice(0, 4).map((kpi, i) => {
        const Icon = icons[i % icons.length];
        return (
          <KPICard
            key={kpi.column}
            title={kpi.title}
            value={computeKPI(kpi)}
            description={kpi.reasoning.slice(0, 55)}
            icon={<Icon className="h-4 w-4" />}
          />
        );
      })}
    </div>
  );
}

// ─── Chart renderer ──────────────────────────────────────────────────────────
function AutoChart({ decision, data }: { decision: ChartDecision; data: Record<string, unknown>[] }) {
  if (data.length === 0) return (
    <div className="h-48 flex items-center justify-center text-sm text-muted-foreground">
      No data for this chart
    </div>
  );

  const xCol = decision.x_axis?.column;
  const yCol = decision.y_axes[0]?.column;

  function groupData(rows: Record<string, unknown>[]) {
    if (!decision.group_by) return rows;
    const grouped: Record<string, Record<string, unknown>> = {};
    rows.forEach((row) => {
      const key = String(row[decision.group_by!] ?? 'Unknown');
      if (!grouped[key]) grouped[key] = { [decision.group_by!]: key };
      const val = Number(row[yCol!]);
      if (!isNaN(val)) {
        grouped[key][yCol!] = (Number(grouped[key][yCol!] ?? 0)) + val;
        grouped[key]['_count'] = (Number(grouped[key]['_count'] ?? 0)) + 1;
      }
    });
    if (decision.aggregate === 'avg') {
      Object.values(grouped).forEach((g) => {
        if (Number(g['_count']) > 0) g[yCol!] = Number(g[yCol!]) / Number(g['_count']);
      });
    }
    return Object.values(grouped).sort((a, b) => Number(b[yCol!] ?? 0) - Number(a[yCol!] ?? 0)).slice(0, 12);
  }

  const chartData  = groupData(data.slice(0, 200));
  const yFormat    = decision.y_axes[0]?.format;
  const ttStyle    = { backgroundColor: 'var(--bg-tooltip)', border: '1px solid var(--border-default)', borderRadius: '6px', fontSize: '12px' };
  const axisProps  = { fontSize: 10, tickLine: false, axisLine: false, stroke: 'var(--text-muted)' };

  switch (decision.chart_type) {
    case 'line_chart':
    case 'area_chart': {
      const isArea = decision.chart_type === 'area_chart';
      const Comp = isArea ? AreaChart : LineChart;
      return (
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <Comp data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-default)" opacity={0.3} vertical={false} />
              <XAxis dataKey={xCol} {...axisProps} interval={Math.max(0, Math.floor(chartData.length / 6))} tickFormatter={(v) => typeof v === 'string' && v.length > 10 ? v.slice(5) : v} />
              <YAxis {...axisProps} tickFormatter={(v) => fmt(v, yFormat)} />
              <Tooltip contentStyle={ttStyle} formatter={(v) => [fmt(v, yFormat), decision.y_axes[0]?.label]} />
              {decision.y_axes.map((y, i) =>
                isArea
                  ? <Area key={y.column} type="monotone" dataKey={y.column} stroke={CHART_COLORS[i]} fill={CHART_COLORS[i]} fillOpacity={0.15} strokeWidth={2} dot={false} name={y.label} />
                  : <Line  key={y.column} type="monotone" dataKey={y.column} stroke={CHART_COLORS[i]} strokeWidth={2} dot={false} name={y.label} />
              )}
            </Comp>
          </ResponsiveContainer>
        </div>
      );
    }
    case 'bar_chart':
    case 'stacked_bar':
      return (
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-default)" opacity={0.3} vertical={false} />
              <XAxis dataKey={xCol ?? decision.group_by} {...axisProps} tickFormatter={(v) => String(v).slice(0, 10)} />
              <YAxis {...axisProps} tickFormatter={(v) => fmt(v, yFormat)} />
              <Tooltip contentStyle={ttStyle} formatter={(v) => [fmt(v, yFormat), '']} />
              <Bar dataKey={yCol ?? '_count'} radius={[3, 3, 0, 0]} maxBarSize={40}>
                {chartData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} fillOpacity={0.8} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      );
    case 'donut_chart': {
      const col = decision.group_by ?? xCol ?? yCol;
      const counts: Record<string, number> = {};
      data.forEach((r) => { const k = String(r[col!] ?? 'Unknown'); counts[k] = (counts[k] ?? 0) + 1; });
      const pieData = Object.entries(counts).sort(([, a], [, b]) => b - a).slice(0, 8).map(([name, value]) => ({ name, value }));
      return (
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <RechartsPie>
              <Pie data={pieData} cx="50%" cy="50%" innerRadius={50} outerRadius={80} dataKey="value" paddingAngle={3}>
                {pieData.map((_, i) => <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} fillOpacity={0.85} />)}
              </Pie>
              <Tooltip contentStyle={ttStyle} />
              <Legend iconType="circle" iconSize={8} formatter={(v) => <span className="text-xs text-muted-foreground">{v}</span>} />
            </RechartsPie>
          </ResponsiveContainer>
        </div>
      );
    }
    case 'scatter_plot': {
      const pts = data.slice(0, 200).map((r) => ({ x: Number(r[xCol!]), y: Number(r[yCol!]) })).filter((p) => !isNaN(p.x) && !isNaN(p.y));
      return (
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 5, right: 10, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border-default)" opacity={0.3} />
              <XAxis dataKey="x" {...axisProps} tickFormatter={(v) => fmt(v, decision.x_axis?.format)} />
              <YAxis dataKey="y" {...axisProps} tickFormatter={(v) => fmt(v, yFormat)} />
              <Tooltip contentStyle={ttStyle} />
              <ScatterDot data={pts} fill={CHART_COLORS[0]} fillOpacity={0.6} />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      );
    }
    case 'data_table': {
      const cols = decision.y_axes.slice(0, 8);
      return (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="border-b border-border">{cols.map((c) => <th key={c.column} className="text-left py-2 px-3 font-medium text-muted-foreground">{c.label}</th>)}</tr></thead>
            <tbody>
              {data.slice(0, 20).map((row, i) => (
                <tr key={i} className="border-b border-border/40 hover:bg-panel-hover">
                  {cols.map((c) => <td key={c.column} className="py-2 px-3 font-mono">{fmt(row[c.column], c.format)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    default:
      return <div className="h-32 flex items-center justify-center text-muted-foreground text-sm">Chart type not supported</div>;
  }
}

// ─── AI Insight panel ───────────────────────────────────────────────────────
function AIInsightPanel({ insight, loading }: { insight: AnalyzeResponse | null; loading: boolean }) {
  if (loading) return <div className="flex items-center gap-2 p-4 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />Analyzing data…</div>;
  if (!insight) return null;
  return (
    <div className="space-y-3 p-1">
      {insight.narrative && (
        <div className="p-3 rounded-lg bg-indigo-500/10 border border-indigo-500/20">
          <div className="flex items-center gap-1.5 mb-1.5">
            <Sparkles className="h-3.5 w-3.5 text-indigo-400" />
            <span className="text-xs font-semibold text-indigo-400 uppercase tracking-wide">AI Insight</span>
            <span className="text-xs text-muted-foreground">via {insight.provider_used}</span>
          </div>
          <p className="text-sm">{insight.narrative}</p>
        </div>
      )}
      {insight.anomalies?.length > 0 && (
        <div className="p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/20">
          <div className="flex items-center gap-1.5 mb-1.5">
            <AlertCircle className="h-3.5 w-3.5 text-yellow-400" />
            <span className="text-xs font-semibold text-yellow-400 uppercase tracking-wide">Anomalies</span>
          </div>
          <ul className="space-y-1">{insight.anomalies.map((a, i) => <li key={i} className="text-xs flex gap-2"><span>•</span>{a}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

// ─── AI Enhance summary panel ───────────────────────────────────────────────
function AIEnhancePanel({ enhancedPlan, onReset }: { enhancedPlan: EnhancedPlan; onReset: () => void }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="mb-4 p-3 rounded-lg border border-emerald-500/30 bg-emerald-500/5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Sparkles className="h-3.5 w-3.5 text-emerald-400" />
          <span className="text-xs font-semibold text-emerald-400 uppercase tracking-wide">AI Enhanced View</span>
          {enhancedPlan.provider_used && <span className="text-xs text-muted-foreground">via {enhancedPlan.provider_used}</span>}
          {enhancedPlan.enhanced_at && <span className="text-xs text-muted-foreground">· {new Date(enhancedPlan.enhanced_at).toLocaleDateString()}</span>}
        </div>
        <div className="flex items-center gap-3">
          <button onClick={() => setExpanded((v) => !v)} className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1">
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            {enhancedPlan.ai_improvements?.length ?? 0} improvements
          </button>
          <button onClick={onReset} className="text-xs text-muted-foreground hover:text-red-400 underline">Reset to default</button>
        </div>
      </div>
      {expanded && enhancedPlan.ai_improvements?.length > 0 && (
        <ul className="mt-2 space-y-1">
          {enhancedPlan.ai_improvements.map((imp, i) => (
            <li key={i} className="flex items-start gap-2 text-xs text-muted-foreground">
              <Check className="h-3 w-3 text-emerald-400 mt-0.5 shrink-0" />{imp}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ─── Main AutoDashboard ──────────────────────────────────────────────────────
interface AutoDashboardProps {
  table: string;
  title?: string;
  showAIInsights?: boolean;
  tabContext?: string; // passed to AI for better understanding of the view's purpose
}

export function AutoDashboard({ table, title, showAIInsights = true, tabContext }: AutoDashboardProps) {
  const [profile,      setProfile]      = useState<TableProfile | null>(null);
  const [plan,         setPlan]         = useState<AutoVizPlan | null>(null);
  const [chartData,    setChartData]    = useState<Record<string, Record<string, unknown>[]>>({});
  const [kpiData,      setKpiData]      = useState<Record<string, unknown>[]>([]);
  const [aiInsight,    setAiInsight]    = useState<AnalyzeResponse | null>(null);
  const [enhancedPlan, setEnhancedPlan] = useState<EnhancedPlan | null>(null);
  const [loading,      setLoading]      = useState(true);
  const [aiLoading,    setAiLoading]    = useState(false);
  const [enhancing,    setEnhancing]    = useState(false);
  const [error,        setError]        = useState<string | null>(null);
  const [enhanceErr,   setEnhanceErr]   = useState<string | null>(null);
  const [hasNewData,   setHasNewData]   = useState(false);

  const loadAbortRef     = useRef<AbortController | null>(null);
  const realtimeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load persisted enhanced plan from localStorage on mount
  useEffect(() => {
    const stored = loadEnhancedPlan(table);
    if (stored) setEnhancedPlan(stored);
  }, [table]);

  // Cleanup on unmount
  useEffect(() => () => {
    loadAbortRef.current?.abort();
    if (realtimeTimerRef.current) clearTimeout(realtimeTimerRef.current);
  }, []);

  // ── Main data loader ──────────────────────────────────────────────────────
  const load = useCallback(async (forceRefresh = false) => {
    loadAbortRef.current?.abort();
    const abort = new AbortController();
    loadAbortRef.current = abort;
    setHasNewData(false);

    // ── Cache hit: instant render ───────────────────────────────────────
    if (!forceRefresh) {
      const cached = getCached(table);
      if (cached) {
        setProfile(cached.profile);
        setPlan(cached.plan);
        setChartData(cached.chartData);
        setKpiData(cached.kpiData);
        setLoading(false);
        setError(null);
        return;
      }
    } else {
      PROFILE_CACHE.delete(table);
    }

    setLoading(true);
    setError(null);

    try {
      // Step 1: Profile the table (15s timeout prevents eternal loading)
      const profileRes = await fetchWithTimeout(
        `/api/profile?table=${encodeURIComponent(table)}`,
        { timeoutMs: 15_000, signal: abort.signal }
      );
      if (abort.signal.aborted) return;
      if (!profileRes.ok) throw new Error(`Profile failed (${profileRes.status}): ${await profileRes.text()}`);
      const tableProfile: TableProfile = await profileRes.json();
      if (abort.signal.aborted) return;

      // Step 2: Build viz plan — use AI-enhanced plan if one is saved,
      //         fall back to rule-based generateVizPlan
      const vizPlan = enhancedPlan ?? generateVizPlan(tableProfile);

      // Step 3a: Dedicated KPI fetch
      // ─────────────────────────────────────────────────────────────────
      // THE KPI FIX: We select ONLY the KPI columns in a separate query.
      //
      // Why this was broken:
      //   AutoKPIRow previously used chartData[chartsToShow[0].id], which is
      //   data fetched for the first chart (typically a time-series). That
      //   query's buildQuerySpec only selects the columns in its x_axis and
      //   y_axes (e.g. "date, score"). Columns like "delivery_pct" or "atr_pct"
      //   are not included → Number(row['delivery_pct']) === NaN → filtered
      //   out → values array is empty → returns '—'.
      //
      // The fix: fetch a separate query that explicitly selects all KPI columns.
      const kpiCols = [...new Set(vizPlan.kpis.map((k) => k.column))];
      let freshKpiData: Record<string, unknown>[] = [];
      if (kpiCols.length > 0 && !abort.signal.aborted) {
        const dateCol = tableProfile.columns.find((c) => c.is_primary_date)?.name;
        const { data: kd } = await queryTable<Record<string, unknown>>(table, {
          select: kpiCols.join(', '),
          order: dateCol ? { column: dateCol, ascending: false } : undefined,
          limit: 1000,
        });
        if (!abort.signal.aborted) freshKpiData = kd ?? [];
      }

      // Step 3b: Chart data in parallel
      const uniqueSpecs = [
        ...vizPlan.charts.filter((c) => c.chart_type !== 'data_table').slice(0, 4),
        ...vizPlan.charts.filter((c) => c.chart_type === 'data_table').slice(0, 1),
      ];
      const dataMap: Record<string, Record<string, unknown>[]> = {};
      await Promise.all(
        uniqueSpecs.map(async (decision) => {
          if (abort.signal.aborted) return;
          const spec = buildQuerySpec(table, decision);
          const { data } = await queryTable<Record<string, unknown>>(spec.table, {
            select: spec.select,
            order: spec.order ? { column: spec.order, ascending: false } : undefined,
            limit: spec.limit,
          });
          dataMap[decision.id] = data ?? [];
        })
      );
      if (abort.signal.aborted) return;

      // Store in module cache (includes kpiData)
      PROFILE_CACHE.set(table, {
        profile: tableProfile,
        plan: vizPlan,
        chartData: dataMap,
        kpiData: freshKpiData,
        ts: Date.now(),
      });

      setProfile(tableProfile);
      setPlan(vizPlan);
      setChartData(dataMap);
      setKpiData(freshKpiData);

      // Step 4: AI narrative (async, non-blocking)
      if (showAIInsights && tableProfile.row_count > 0) {
        setAiLoading(true);
        fetch('/api/ai/analyze', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ table, profile: tableProfile, sample_data: freshKpiData.slice(0, 30) }),
          signal: abort.signal,
        })
          .then((r) => r.json())
          .then((ins: AnalyzeResponse) => { if (!abort.signal.aborted) setAiInsight(ins); })
          .catch(() => null)
          .finally(() => { if (!abort.signal.aborted) setAiLoading(false); });
      }
    } catch (e) {
      if (abort.signal.aborted) return;
      setError(e instanceof Error ? e.message : 'Failed to load dashboard');
    } finally {
      if (!abort.signal.aborted) setLoading(false);
    }
  }, [table, showAIInsights, enhancedPlan]);

  useEffect(() => { load(); }, [load]);

  // ── AI View Enhancement ───────────────────────────────────────────────────
  // Calls /api/ai/enhance-view with the current profile + rule-based plan
  // + sample data. AI returns an improved AutoVizPlan that:
  //   - Chooses KPIs that actually have data
  //   - Picks better chart types for the actual data shape
  //   - Understands the financial context of each column
  //   - Persists to localStorage so it survives page refreshes
  const handleEnhance = useCallback(async () => {
    if (!profile || !plan) return;
    setEnhancing(true);
    setEnhanceErr(null);

    try {
      const res = await fetchWithTimeout('/api/ai/enhance-view', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          table,
          profile,
          currentPlan: plan,
          sampleData: kpiData.slice(0, 20),
          tabContext: tabContext ?? title ?? table,
        }),
        timeoutMs: 35_000, // AI calls need more time than profile fetches
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: res.statusText }));
        throw new Error(err.error ?? `Enhancement failed: ${res.status}`);
      }

      const improved: EnhancedPlan = await res.json();
      improved.enhanced_at = new Date().toISOString();

      // Persist + apply immediately
      saveEnhancedPlan(table, improved);
      setEnhancedPlan(improved);

      // Clear cache and reload with the enhanced plan active
      PROFILE_CACHE.delete(table);
      // Small delay so state updates propagate before load() reads enhancedPlan
      await new Promise((r) => setTimeout(r, 50));
      await load(true);
    } catch (e) {
      setEnhanceErr(e instanceof Error ? e.message : 'Enhancement failed');
    } finally {
      setEnhancing(false);
    }
  }, [profile, plan, kpiData, table, tabContext, title, load]);

  const handleResetEnhancement = useCallback(() => {
    clearEnhancedPlan(table);
    setEnhancedPlan(null);
    PROFILE_CACHE.delete(table);
    load(true);
  }, [table, load]);

  // ── Realtime: auto-reload on data changes (debounced 2s) ─────────────────
  const handleRealtimeChange = useCallback(() => {
    PROFILE_CACHE.delete(table);
    if (realtimeTimerRef.current) clearTimeout(realtimeTimerRef.current);
    realtimeTimerRef.current = setTimeout(() => { setHasNewData(false); load(true); }, 2_000);
    setHasNewData(true);
  }, [table, load]);

  useRealtime({
    table,
    event: '*',
    onInsert: handleRealtimeChange,
    onUpdate: handleRealtimeChange,
    onDelete: handleRealtimeChange,
  });

  // ── Render ────────────────────────────────────────────────────────────────
  if (loading) return (
    <div className="flex items-center justify-center h-64 gap-2 text-muted-foreground">
      <Loader2 className="h-5 w-5 animate-spin" /><span>Loading {table}…</span>
    </div>
  );

  if (error) return (
    <div className="p-4 border border-red-500/20 rounded-lg bg-red-500/10 space-y-2">
      <div className="flex items-center gap-2 text-red-400 text-sm font-medium">
        <AlertCircle className="h-4 w-4" />Failed to load: {table}
      </div>
      <div className="text-xs text-red-300 font-mono">{error}</div>
      <button onClick={() => load(true)} className="text-xs underline text-red-300 hover:text-red-100">Retry</button>
    </div>
  );

  if (!plan || !profile) return null;

  const activePlan   = enhancedPlan ?? plan;
  const displayTitle = title ?? activePlan.suggested_title;
  const chartsToShow = activePlan.charts.filter((c) => c.chart_type !== 'data_table').slice(0, 4);
  const tableChart   = activePlan.charts.find((c) => c.chart_type === 'data_table');

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="font-semibold text-lg">{displayTitle}</h3>
          <p className="text-sm text-muted-foreground">{activePlan.suggested_description}</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {hasNewData && (
            <span className="text-xs text-emerald-400 animate-pulse flex items-center gap-1">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 inline-block" />Updating…
            </span>
          )}
          <Button
            size="sm"
            variant={enhancedPlan ? 'outline' : 'default'}
            onClick={handleEnhance}
            disabled={enhancing}
            className={enhancedPlan ? '' : 'bg-indigo-600 hover:bg-indigo-700 text-white'}
          >
            {enhancing
              ? <><Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />Enhancing…</>
              : <><Sparkles className="h-3.5 w-3.5 mr-1.5" />{enhancedPlan ? 'Re-enhance' : 'Enhance with AI'}</>
            }
          </Button>
          <Button size="sm" variant="ghost" onClick={() => load(true)}>
            <RefreshCw className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Enhancement error */}
      {enhanceErr && (
        <div className="text-xs text-red-400 border border-red-500/20 rounded p-2 flex items-center gap-2">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />{enhanceErr}
        </div>
      )}

      {/* Enhancement summary (collapsible improvements list) */}
      {enhancedPlan && (
        <AIEnhancePanel enhancedPlan={enhancedPlan} onReset={handleResetEnhancement} />
      )}

      {/* KPIs — uses dedicated kpiData, never chart data */}
      {activePlan.kpis.length > 0 && (
        <AutoKPIRow kpis={activePlan.kpis} kpiData={kpiData} />
      )}

      {/* AI narrative insight */}
      {showAIInsights && (aiInsight || aiLoading) && (
        <Panel title="AI Analysis" description="Generated from actual data" dataSource="supabase" tableName={table}>
          <AIInsightPanel insight={aiInsight} loading={aiLoading} />
        </Panel>
      )}

      {/* Charts */}
      {chartsToShow.length > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {chartsToShow.map((decision) => (
            <Panel key={decision.id} title={decision.title} description={decision.description} dataSource="supabase" tableName={table}>
              <AutoChart decision={decision} data={chartData[decision.id] ?? []} />
            </Panel>
          ))}
        </div>
      )}

      {/* Raw data table */}
      {tableChart && chartData[tableChart.id] && (
        <Panel title="Raw Data" description={`${profile.row_count} total rows`} dataSource="supabase" tableName={table}>
          <AutoChart decision={tableChart} data={chartData[tableChart.id] ?? []} />
        </Panel>
      )}
    </div>
  );
}
