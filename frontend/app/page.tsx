'use client';

import { useState, useEffect, useCallback } from 'react';
import { Header } from '@/components/core/Header';
import { TabBar, MobileTabBar } from '@/components/core/TabBar';
import { ThemeEditor } from '@/components/core/ThemeEditor';
import { DevelopmentBanner } from '@/components/core/DataSourceBadge';
import { CommandPalette } from '@/components/core/CommandPalette';
import { ViewWizard } from '@/components/views/ViewWizard';
import { ChartBuilder } from '@/components/charts/ChartBuilder';
import { CustomTabView } from '@/components/views/CustomTabView';
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
import { BrainEngineTab } from '@/components/tabs/BrainEngineTab';
import { DataManagementTab } from '@/components/tabs/DataManagementTab';

export default function Dashboard() {
  const [themeEditorOpen, setThemeEditorOpen] = useState(false);
  const [regime, setRegime] = useState<RegimeState>('NEUTRAL');
  const [aiStatus, setAiStatus] = useState<'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED'>('AVAILABLE');

  const { activeTabId, chartBuilderOpen, setChartBuilderOpen } = useLayoutStore();
  const { setViews } = useViewStore();

  // ── Initialize theme ────────────────────────────────────────────────────
  useEffect(() => { initializeTheme(); }, []);

  // ── Live regime from market_regime table ────────────────────────────────
  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getMarketRegime().then(({ data }) => {
      if (data?.[0]) {
        const r = data[0];
        setRegime((r.computed_regime ?? r.regime) as RegimeState);
      }
    });
  }, []);

  // ── Live AI provider status from system_config ──────────────────────────
  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getSystemConfig().then(({ data }) => {
      if (!data) return;
      const cfg = Object.fromEntries((data).map((r) => [r.key, r.value]));
      const provider = cfg['ai_active_provider'] || cfg['ai_provider'];
      if (!provider || provider === 'disabled') {
        setAiStatus('DISABLED');
      } else if (cfg['ai_fallback_mode'] === 'true') {
        setAiStatus('DEGRADED');
      } else {
        setAiStatus('AVAILABLE');
      }
    });
  }, []);

  // ── Load Supabase-persisted custom views into viewStore ─────────────────
  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    viewsApi.list()
      .then((serverViews) => {
        if (serverViews.length > 0) setViews(serverViews as never);
      })
      .catch(() => {
        // custom_views table may not exist yet — gracefully ignore
        // Run supabase_migration.sql to enable this feature
      });
  }, [setViews]);

  // ── Tab content renderer ────────────────────────────────────────────────
  const renderTabContent = useCallback(() => {
    switch (activeTabId) {
      case 'performance':     return <PerformanceTab />;
      case 'positions':       return <PositionsTab />;
      case 'ai-intelligence': return <AIIntelTab />;
      case 'brain-engine':    return <BrainEngineTab />;
      case 'data-management': return <DataManagementTab />;
      default:
        // Custom tab — render views assigned to this tab
        return <CustomTabView tabId={activeTabId} />;
    }
  }, [activeTabId]);

  const supabaseWarning = getSupabaseWarning();

  return (
    <KeyboardShortcutsProvider>
      <div className="min-h-screen bg-dashboard flex flex-col">
        <Header
          marketRegime={regime}
          aiProviderStatus={aiStatus}
          onSettingsClick={() => setThemeEditorOpen(true)}
          notificationCount={0}
        />

        <TabBar onManageClick={() => {}} />

        <main className="flex-1 overflow-auto p-4 pb-20 md:pb-4">
          {supabaseWarning && <DevelopmentBanner message={supabaseWarning} />}
          {renderTabContent()}
        </main>

        <MobileTabBar />
        <CommandPalette />
        <ViewWizard />
        <ChartBuilder open={chartBuilderOpen} onClose={() => setChartBuilderOpen(false)} />
        <ThemeEditor open={themeEditorOpen} onOpenChange={setThemeEditorOpen} />
      </div>
    </KeyboardShortcutsProvider>
  );
}
