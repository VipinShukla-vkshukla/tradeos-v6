import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { DynamicView, ViewColumn, ViewFilter, ViewJoin } from '@/types/registry';
import { viewsApi } from '@/lib/api';

interface ViewState {
  views: DynamicView[];
  activeViewId: string | null;

  wizardOpen: boolean;
  wizardStep: number;
  wizardDraft: Partial<DynamicView> | null;

  // CRUD
  setViews: (views: DynamicView[]) => void;
  addView: (view: DynamicView) => Promise<void>;
  updateView: (id: string, updates: Partial<DynamicView>) => Promise<void>;
  deleteView: (id: string) => void;
  setActiveView: (id: string | null) => void;

  // Wizard
  openWizard: (existing?: DynamicView) => void;
  closeWizard: () => void;
  setWizardStep: (step: number) => void;
  updateWizardDraft: (updates: Partial<DynamicView>) => void;

  // Column management
  addColumn: (viewId: string, column: ViewColumn) => void;
  removeColumn: (viewId: string, columnId: string) => void;
  reorderColumns: (viewId: string, columnIds: string[]) => void;

  // Filter management
  addFilter: (viewId: string, filter: ViewFilter) => void;
  removeFilter: (viewId: string, filterId: string) => void;
  updateFilter: (viewId: string, filterId: string, updates: Partial<ViewFilter>) => void;

  // Join management
  addJoin: (viewId: string, join: ViewJoin) => void;
  removeJoin: (viewId: string, joinId: string) => void;
}

export const useViewStore = create<ViewState>()(
  persist(
    (set, get) => ({
      views: [],
      activeViewId: null,
      wizardOpen: false,
      wizardStep: 0,
      wizardDraft: null,

      setViews: (views) => set({ views }),

      // addView: saves to Supabase first, then local store
      addView: async (view) => {
        try {
          await viewsApi.create({
            id: view.id,
            name: view.name,
            description: view.description,
            baseTable: view.primaryTable || (view as { baseTable?: string }).baseTable || '',
            columns: view.columns,
            joins: view.joins,
            filters: view.filters,
            sorts: view.sorts,
            viewType: view.viewType,
            chartConfig: view.chartConfig,
          });
        } catch {
          // Supabase save failed (table may not exist yet) — still keep in local store
          console.warn('Could not persist view to Supabase — stored locally only. Run supabase_migration.sql to enable persistence.');
        }
        set((state) => ({ views: [...state.views, view] }));
      },

      // updateView: updates Supabase + local
      updateView: async (id, updates) => {
        try {
          await viewsApi.update(id, updates as Parameters<typeof viewsApi.update>[1]);
        } catch {
          console.warn('Could not update view in Supabase');
        }
        set((state) => ({
          views: state.views.map((v) =>
            v.id === id ? { ...v, ...updates, updatedAt: new Date().toISOString() } : v
          ),
        }));
      },

      deleteView: (id) =>
        set((state) => ({
          views: state.views.filter((v) => v.id !== id),
          activeViewId: state.activeViewId === id ? null : state.activeViewId,
        })),

      setActiveView: (id) => set({ activeViewId: id }),

      openWizard: (existing) =>
        set({
          wizardOpen: true,
          wizardStep: 0,
          wizardDraft: existing || {
            id: crypto.randomUUID(),
            name: '',
            baseTable: '',
            primaryTable: '',
            columns: [],
            filters: [],
            joins: [],
            sorts: [],
            viewType: 'table',
            sourceMode: 'single_table',
            createdAt: new Date().toISOString(),
            updatedAt: new Date().toISOString(),
          },
        }),

      closeWizard: () =>
        set({ wizardOpen: false, wizardStep: 0, wizardDraft: null }),

      setWizardStep: (step) => set({ wizardStep: step }),

      updateWizardDraft: (updates) =>
        set((state) => ({
          wizardDraft: state.wizardDraft ? { ...state.wizardDraft, ...updates } : null,
        })),

      addColumn: (viewId, column) =>
        set((state) => ({
          views: state.views.map((v) =>
            v.id === viewId
              ? { ...v, columns: [...v.columns, column], updatedAt: new Date().toISOString() }
              : v
          ),
        })),

      removeColumn: (viewId, columnId) =>
        set((state) => ({
          views: state.views.map((v) =>
            v.id === viewId
              ? { ...v, columns: v.columns.filter((c) => c.id !== columnId), updatedAt: new Date().toISOString() }
              : v
          ),
        })),

      reorderColumns: (viewId, columnIds) =>
        set((state) => ({
          views: state.views.map((v) => {
            if (v.id !== viewId) return v;
            const map = new Map(v.columns.map((c) => [c.id, c]));
            const reordered = columnIds.map((id) => map.get(id)).filter(Boolean) as ViewColumn[];
            return { ...v, columns: reordered, updatedAt: new Date().toISOString() };
          }),
        })),

      addFilter: (viewId, filter) =>
        set((state) => ({
          views: state.views.map((v) =>
            v.id === viewId
              ? { ...v, filters: [...v.filters, filter], updatedAt: new Date().toISOString() }
              : v
          ),
        })),

      removeFilter: (viewId, filterId) =>
        set((state) => ({
          views: state.views.map((v) =>
            v.id === viewId
              ? { ...v, filters: v.filters.filter((f) => f.id !== filterId), updatedAt: new Date().toISOString() }
              : v
          ),
        })),

      updateFilter: (viewId, filterId, updates) =>
        set((state) => ({
          views: state.views.map((v) =>
            v.id === viewId
              ? {
                  ...v,
                  filters: v.filters.map((f) => (f.id === filterId ? { ...f, ...updates } : f)),
                  updatedAt: new Date().toISOString(),
                }
              : v
          ),
        })),

      addJoin: (viewId, join) =>
        set((state) => ({
          views: state.views.map((v) =>
            v.id === viewId
              ? { ...v, joins: [...(v.joins ?? []), join], updatedAt: new Date().toISOString() }
              : v
          ),
        })),

      removeJoin: (viewId, joinId) =>
        set((state) => ({
          views: state.views.map((v) =>
            v.id === viewId
              ? { ...v, joins: (v.joins ?? []).filter((j) => j.id !== joinId), updatedAt: new Date().toISOString() }
              : v
          ),
        })),
    }),
    {
      name: 'tradeos-views',
      // Persist locally as fallback when Supabase custom_views doesn't exist yet
      partialize: (state) => ({ views: state.views }),
    }
  )
);
