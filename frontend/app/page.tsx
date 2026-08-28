'use client';

import { useState, useEffect } from 'react';
import { Header } from '@/components/core/Header';
import { Sidebar } from '@/components/core/Sidebar';
import { DailyBookSummary } from '@/components/core/DailyBookSummary';
import { useLayoutStore } from '@/store/layoutStore';
import { isSupabaseConfigured, getSupabaseWarning, queries } from '@/lib/supabase';
import { KeyboardShortcutsProvider } from '@/hooks/useKeyboardShortcuts';
import type { RegimeState } from '@/types/database';

import { OverviewTab } from '@/components/tabs/OverviewTab';
import { PerformanceTab } from '@/components/tabs/PerformanceTab';
import { PositionsTab } from '@/components/tabs/PositionsTab';
import { IntradayTab } from '@/components/tabs/IntradayTab';
import { AllocatorTab } from '@/components/tabs/AllocatorTab';
import { BrainEngineTab } from '@/components/tabs/BrainEngineTab';
import { AIIntelTab } from '@/components/tabs/AIIntelTab';
import { DataManagementTab } from '@/components/tabs/DataManagementTab';

function DevelopmentBanner({ message }: { message: string }) {
  return (
    <div className="mb-4 rounded-lg border border-warning/30 bg-warning/10 px-4 py-2.5 text-xs text-warning">
      {message}
    </div>
  );
}

export default function Dashboard() {
  const [regime, setRegime] = useState<RegimeState>('NEUTRAL');
  const [aiStatus, setAiStatus] = useState<'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED'>('AVAILABLE');
  const activeTabId = useLayoutStore((s) => s.activeTabId);

  // Drives the live dot next to Intraday in the sidebar — read here so the
  // indicator is truthful even while looking at a different screen; knowing
  // the monitor died is most useful when you are not looking at it.
  const [intradayLive, setIntradayLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      try {
        const { data } = await queries.getIntradayHeartbeat();
        const ts = data?.[0]?.ts;
        if (!cancelled) setIntradayLive(!!ts && Date.now() - new Date(ts).getTime() < 20 * 60_000);
      } catch { if (!cancelled) setIntradayLive(false); }
    };
    check();
    const t = setInterval(check, 60_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getMarketRegime().then(({ data }) => {
      if (data?.[0]) setRegime((data[0].computed_regime ?? data[0].regime) as RegimeState);
    });
  }, []);

  useEffect(() => {
    if (!isSupabaseConfigured()) return;
    queries.getSystemConfig(['ai_active_provider', 'ai_provider', 'ai_fallback_mode']).then(({ data }) => {
      if (!data) return;
      const cfg = Object.fromEntries(data.map((r) => [r.key, r.value]));
      const provider = cfg['ai_active_provider'] || cfg['ai_provider'];
      if (!provider || provider === 'disabled') setAiStatus('DISABLED');
      else if (cfg['ai_fallback_mode'] === 'true') setAiStatus('DEGRADED');
      else setAiStatus('AVAILABLE');
    });
  }, []);

  function renderTab() {
    switch (activeTabId) {
      case 'overview':        return <OverviewTab />;
      case 'positions':       return <PositionsTab />;
      case 'intraday':        return <IntradayTab />;
      case 'performance':     return <PerformanceTab />;
      case 'allocator':       return <AllocatorTab />;
      case 'brain-engine':    return <BrainEngineTab />;
      case 'ai-intelligence': return <AIIntelTab />;
      case 'data-management': return <DataManagementTab />;
      default:                return <OverviewTab />;
    }
  }

  const supabaseWarning = getSupabaseWarning();

  return (
    <KeyboardShortcutsProvider>
      <div className="h-screen bg-dashboard flex flex-col overflow-hidden">
        <Header marketRegime={regime} aiProviderStatus={aiStatus} />

        <div className="flex flex-1 min-h-0">
          <Sidebar intradayLive={intradayLive} />

          <main className="flex-1 min-w-0 overflow-auto p-4">
            {supabaseWarning && <DevelopmentBanner message={supabaseWarning} />}

            {/* Only on Overview — matches Main.dc.html exactly. Positions &
                P&L has its own distinct KPI row (Open Positions/Unrealized/
                Open Risk/Avg R) in PositionsPnL.dc.html, not this block —
                showing both would duplicate the same book-value numbers. */}
            {activeTabId === 'overview' && (
              <div className="mb-4">
                <DailyBookSummary />
              </div>
            )}

            {renderTab()}
          </main>
        </div>
      </div>
    </KeyboardShortcutsProvider>
  );
}
