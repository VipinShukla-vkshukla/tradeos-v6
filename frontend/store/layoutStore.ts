import { create } from 'zustand';
import { persist } from 'zustand/middleware';

const VALID_TABS = new Set([
  'overview', 'positions', 'intraday', 'performance', 'allocator',
  'brain-engine', 'ai-intelligence', 'data-management',
]);

interface LayoutState {
  activeTabId: string;
  setActiveTab: (id: string) => void;
}

export const useLayoutStore = create<LayoutState>()(
  persist(
    (set) => ({
      activeTabId: 'overview',
      setActiveTab: (id) => set({ activeTabId: id }),
    }),
    {
      name: 'tradeos-layout',
      // A persisted activeTabId from the old custom-tab system (or any id no
      // longer in the fixed nav list) would render nothing active — fall back
      // to 'overview' rather than a blank screen.
      merge: (persisted, current) => {
        const p = persisted as Partial<LayoutState>;
        const activeTabId = p.activeTabId && VALID_TABS.has(p.activeTabId) ? p.activeTabId : 'overview';
        return { ...current, activeTabId };
      },
    }
  )
);
