'use client';

import { useState, useEffect, useCallback } from 'react';
import { Header } from '@/components/core/Header';
import { TabBar, MobileTabBar } from '@/components/core/TabBar';
import { ThemeEditor } from '@/components/core/ThemeEditor';
import { DevelopmentBanner } from '@/components/core/DataSourceBadge';
import { CommandPalette } from '@/components/core/CommandPalette';
import { AlertRail } from '@/components/core/AlertRail';
import { ViewWizard } from '@/components/views/ViewWizard';
import { ChartBuilder } from '@/components/charts/ChartBuilder';
import { CustomTabView } from '@/components/views/CustomTabView';
import { SmartTabGenerator } from '@/components/views/SmartTabGenerator';
import { useLayoutStore } from '@/store/layoutStore';
import { useViewStore } from '@/store/viewStore';
import { initializeTheme } from '@/store/themeStore';
import { isSupabaseConfigured, getSupabaseWarning, queries } from '@/lib/supabase';
import { viewsApi } from '@/lib/api';
import { KeyboardShortcutsProvider } from '@/hooks/useKeyboardShortcuts';
import type { RegimeState } from '@/types/database';

// Core tab components
import { PerformanceTab } from '@/components/tabs/PerformanceTab';
import { PositionsTab } from '@/components/tabs/PositionsTab';
import { AIIntelTab } from '@/components/tabs/AIIntelTab';
import { IntradayTab } from '@/components/tabs/IntradayTab';
import { StrategyModeSwitch } from '@/components/core/StrategyModeSwitch';
import { BrainEngineTab } from '@/components/tabs/BrainEngineTab';
import { AllocatorTab } from '@/components/tabs/AllocatorTab';
import { DataManagementTab } from '@/components/tabs/DataManagementTab';

const CORE_TAB_IDS = new Set(['performance', 'positions', 'ai-intelligence', 'brain-engine', 'data-management', 'allocator']);

export default function Dashboard() {
  const [themeEditorOpen, setThemeEditorOpen] = useState(false);
  const [regime, setRegime] = useState<RegimeState>('NEUTRAL');
  const [aiStatus, setAiStatus] = useState<'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED'>('AVAILABLE');
  const [pipelineLastRun, setPipelineLastRun] = useState<string>('Unknown');

  const { activeTabId, tabs, chartBuilderOpen, setChartBuilderOpen } = useLayoutStore();
  const strategyMode = useLayoutStore((s) => s.strategyMode);

  // Drives the live dot on the Intraday switch. Read here rather than inside
  // the switch so the indicator is truthful even while sitting in swing mode —
  // knowing the monitor died is most useful when you are not looking at it.
  const [intradayLive, setIntradayLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const { data } = await queries.getIntradayHeartbeat();
        const ts = data?.[0]?.ts;
        if (!cancelled) {
          setIntradayLive(!!ts && Date.now() - new Date(ts).getTime() < 20 * 60_000);
        }
      } catch { if (!cancelled) setIntradayLive(false); }
    };
    check();
    const t = setInterval(check, 60_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);
  const { setViews } = useViewStore();

  // ── Track which custom tabs have been mounted at least once ────────────────
  // Once a custom tab is visited, we keep it in the DOM (display:none when
  // inactive). This means AutoDashboard never re-fetches on navigation —
  // data stays live from the initial load + realtime subscription.
  const [visitedCustomTabIds, setVisitedCustomTabIds] = useState<Set<string>>(() => {
    // Seed with active tab if it's a custom tab (handles page reload on custom tab)
    return CORE_TAB_IDS.has(activeTabId) ? new Set() : new Set([activeTabId]);
  });

  useEffect(() => {
    if (!CORE_TAB_IDS.has(activeTabId)) {
      setVisitedCustomTabIds((prev) => {
        if (prev.has(activeTabId)) return prev; // already mounted, no re-render
        return new Set([...prev, activeTabId]);
      });
    }
  }, [activeTabId]);

  useEffect(() => { initializeTheme(); }, []);

  // Live regime from market_regime
  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getMarketRegime().then(({ data }) => {
      if (data?.[0]) {
        const r = data[0];
        setRegime((r.computed_regime ?? r.regime) as RegimeState);
      }
    });
  }, []);

  // AI provider status from system_config
  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getSystemConfig().then(({ data }) => {
      if (!data) return;
      const cfg = Object.fromEntries(data.map((r) => [r.key, r.value]));
      const provider = cfg['ai_active_provider'] || cfg['ai_provider'];
      if (!provider || provider === 'disabled') setAiStatus('DISABLED');
      else if (cfg['ai_fallback_mode'] === 'true') setAiStatus('DEGRADED');
      else setAiStatus('AVAILABLE');
    });
  }, []);

  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    const { createClient } = require('@supabase/supabase-js');
    const supabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
    );
    supabase
      .from('ai_context')
      .select('date')
      .eq('symbol', '__FINAL_PICKS__')
      .order('date', { ascending: false })
      .limit(1)
      .then(({ data }: { data: { date: string }[] | null }) => {
        if (data?.[0]?.date) {
          setPipelineLastRun(
            new Date(data[0].date).toLocaleString('en-IN', {
              dateStyle: 'short',
              timeStyle: 'short',
            })
          );
        }
      })
      .catch(() => null);
  }, []);

  // ── Load Supabase-persisted custom views (merge, not replace) ─────────────
  // viewStore.setViews now MERGES incoming views with local state, so
  // viewType / primaryTable are always preserved from localStorage even if
  // Supabase doesn't store those fields fully.
  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    viewsApi
      .list()
      .then((views) => {
        if (views.length > 0) setViews(views as never);
      })
      .catch(() => {
        // custom_views table may not exist yet — local views remain intact
      });
  }, [setViews]);

  // ── Core tab renderer ──────────────────────────────────────────────────────
  const renderCoreTab = useCallback(() => {
    switch (activeTabId) {
      case 'performance':     return <PerformanceTab />;
      case 'positions':       return <PositionsTab />;
      case 'ai-intelligence': return <AIIntelTab />;
      case 'brain-engine':    return <BrainEngineTab />;
      case 'data-management': return <DataManagementWithGenerator />;
      case 'allocator':       return <AllocatorTab />;
      default:                return null;
    }
  }, [activeTabId]);

  const supabaseWarning = getSupabaseWarning();
  const isCustomTabActive = !CORE_TAB_IDS.has(activeTabId);

  return (
    <KeyboardShortcutsProvider>
      <div className="min-h-screen bg-dashboard flex flex-col">
        <Header
          marketRegime={regime}
          aiProviderStatus={aiStatus}
          pipelineLastRun={pipelineLastRun}
          onSettingsClick={() => setThemeEditorOpen(true)}
          notificationCount={0}
        />

        {/* Mode switch + tab bar.
            The swing tab strip is hidden in intraday mode rather than disabled:
            intraday is a single coherent view, and leaving five inert swing tabs
            on screen would imply they still apply to what is being shown. */}
        <div className="flex items-stretch bg-header border-b border-border">
          <div className="flex items-center px-3 shrink-0">
            <StrategyModeSwitch liveDot={intradayLive} />
          </div>
          {strategyMode === 'swing' && (
            <div className="flex-1 min-w-0 overflow-hidden">
              <TabBar />
            </div>
          )}
        </div>

        <main className="flex-1 overflow-auto p-4 pb-20 md:pb-4">
          {supabaseWarning && <DevelopmentBanner message={supabaseWarning} />}

          {/* ── Intraday mode: one view, no tabs ─────────────────────────── */}
          {strategyMode === 'intraday' && <IntradayTab />}

          {/* ── Core tabs: normal switch (fast, direct Supabase queries) ─── */}
          {strategyMode === 'swing' && !isCustomTabActive && renderCoreTab()}

          {/* ── Custom tabs: keep-alive mount strategy ───────────────────────
            Once a custom tab is visited it stays in the DOM, hidden with
            CSS when inactive. This means:
            - AutoDashboard never re-fetches on navigation (Problem 1 fix)
            - Realtime subscriptions stay alive across tab switches
            - Only the first visit pays the loading cost; all subsequent
              navigations are instant

            We use `display` style (not conditional rendering) so React
            doesn't unmount the component or fire cleanup effects.
          ─────────────────────────────────────────────────────────────── */}
          {strategyMode === 'swing' && [...visitedCustomTabIds].map((tabId) => (
            <div
              key={tabId}
              style={{
                display: isCustomTabActive && activeTabId === tabId ? 'block' : 'none',
              }}
            >
              <CustomTabView tabId={tabId} />
            </div>
          ))}
        </main>

        <MobileTabBar />
        <CommandPalette />
        {/* Live position alerts, mounted globally so an EXIT_STOP does not depend
            on which tab happens to be open. Complements Telegram/Discord. */}
        <AlertRail />
        <ViewWizard />
        <ChartBuilder open={chartBuilderOpen} onClose={() => setChartBuilderOpen(false)} />
        <ThemeEditor open={themeEditorOpen} onOpenChange={setThemeEditorOpen} />
      </div>
    </KeyboardShortcutsProvider>
  );
}

// ─── DataManagement tab with Smart Generator prominently placed ───────────
function DataManagementWithGenerator() {
  const { DataManagementTab: DMTab } = require('@/components/tabs/DataManagementTab');

  return (
    <div className="space-y-4">
      {/* Smart Tab Generator — top of data management */}
      <div className="p-4 rounded-xl border border-indigo-500/30 bg-indigo-500/5 flex items-start justify-between gap-4">
        <div>
          <div className="font-semibold text-indigo-400 flex items-center gap-2">
            <span>✦</span> Smart Tab Generator
          </div>
          <p className="text-sm text-muted-foreground mt-1">
            Scans all your Supabase tables, detects which have live data, and automatically
            creates tabs with the right charts and AI insights — no manual setup.
          </p>
        </div>
        <SmartTabGenerator />
      </div>

      <DataManagementTab />
    </div>
  );
}
