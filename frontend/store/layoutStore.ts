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
      // Merge: always restore core tab names from CORE_TABS even if localStorage is stale
      merge: (persisted: unknown, current) => {
        const p = persisted as Partial<LayoutState>;
        const coreMap = new Map(CORE_TABS.map((t) => [t.id, t]));
        const mergedTabs = (p.tabs ?? current.tabs).map((t: TabConfig) => {
          const core = coreMap.get(t.id);
          // For core tabs: always use the canonical name from CORE_TABS
          if (core) return { ...t, name: core.name, icon: core.icon, isCore: true };
          return t;
        });
        return { ...current, ...p, tabs: mergedTabs };
      },
    }
  )
);
