import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { CORE_TABS, type TabConfig } from '@/types/registry';

// Re-export TabConfig as Tab for backward compat with TabBar imports
export type Tab = TabConfig;

interface LayoutState {
  tabs: TabConfig[];
  activeTabId: string;
  chartBuilderOpen: boolean;

  setActiveTab: (id: string) => void;
  addTab: (tab: TabConfig) => void;
  removeTab: (id: string) => void;
  renameTab: (id: string, name: string) => void;
  reorderTabs: (ids: string[]) => void;
  addViewToTab: (tabId: string, viewId: string) => void;
  removeViewFromTab: (tabId: string, viewId: string) => void;
  getTabViews: (tabId: string) => string[];
  setChartBuilderOpen: (open: boolean) => void;
}

export const useLayoutStore = create<LayoutState>()(
  persist(
    (set, get) => ({
      tabs: CORE_TABS,
      activeTabId: 'performance',
      chartBuilderOpen: false,

      setActiveTab: (id) => set({ activeTabId: id }),

      addTab: (tab) =>
        set((state) => ({ tabs: [...state.tabs, { ...tab, isCore: false, isVisible: true }] })),

      removeTab: (id) =>
        set((state) => ({
          tabs: state.tabs.filter((t) => t.id !== id),
          activeTabId: state.activeTabId === id ? 'performance' : state.activeTabId,
        })),

      renameTab: (id, name) =>
        set((state) => ({
          tabs: state.tabs.map((t) => (t.id === id ? { ...t, name } : t)),
        })),

      reorderTabs: (ids) =>
        set((state) => {
          const map = new Map(state.tabs.map((t) => [t.id, t]));
          return { tabs: ids.map((id) => map.get(id)).filter(Boolean) as TabConfig[] };
        }),

      addViewToTab: (tabId, viewId) =>
        set((state) => ({
          tabs: state.tabs.map((t) =>
            t.id === tabId ? { ...t, views: [...(t.views ?? []), viewId] } : t
          ),
        })),

      removeViewFromTab: (tabId, viewId) =>
        set((state) => ({
          tabs: state.tabs.map((t) =>
            t.id === tabId ? { ...t, views: (t.views ?? []).filter((v) => v !== viewId) } : t
          ),
        })),

      getTabViews: (tabId) => get().tabs.find((t) => t.id === tabId)?.views ?? [],
      setChartBuilderOpen: (open) => set({ chartBuilderOpen: open }),
    }),
    {
      name: 'tradeos-layout',
      // Reconcile the persisted layout against CORE_TABS on every load.
      //
      // BUG FIX: this previously mapped over the PERSISTED list only. It
      // restored canonical names for tabs it recognised, but a core tab added
      // to CORE_TABS after a user's localStorage was written simply never
      // appeared — no error, nothing to click, and clearing site data was the
      // only cure. Anyone who had opened the console once could never see a
      // newly shipped tab.
      //
      // Now: keep the user's order and their custom tabs, refresh the metadata
      // of core tabs, and APPEND any core tab that is missing.
      merge: (persisted: unknown, current) => {
        const p = persisted as Partial<LayoutState>;
        const coreMap = new Map(CORE_TABS.map((t) => [t.id, t]));
        const persistedTabs = p.tabs ?? current.tabs;

        const reconciled = persistedTabs.map((t: TabConfig) => {
          const core = coreMap.get(t.id);
          return core ? { ...t, name: core.name, icon: core.icon, isCore: true } : t;
        });

        const seen = new Set(reconciled.map((t: TabConfig) => t.id));
        const added = CORE_TABS.filter((t) => !seen.has(t.id));

        // Guard against a persisted activeTabId pointing at a tab the user has
        // since deleted, which would render an empty console.
        const tabs = [...reconciled, ...added];
        const activeTabId =
          tabs.some((t) => t.id === (p.activeTabId ?? current.activeTabId))
            ? (p.activeTabId ?? current.activeTabId)
            : (tabs[0]?.id ?? 'performance');

        return { ...current, ...p, tabs, activeTabId };
      },
    }
  )
);
