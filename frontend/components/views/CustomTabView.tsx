// ─── components/views/CustomTabView.tsx ──────────────────────────────────
// Replace the existing CustomTabView with this version.
// Views with viewType='auto' render AutoDashboard.
// Views with viewType='table' render LiveDataView (manual wizard views).

'use client';

import { useState } from 'react';
import { Plus, LayoutGrid } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useViewStore } from '@/store/viewStore';
import { useLayoutStore } from '@/store/layoutStore';
import { AutoDashboard } from '@/components/views/AutoDashboard';
import { LiveDataView } from '@/components/views/LiveDataView';

interface CustomTabViewProps {
  tabId: string;
}

export function CustomTabView({ tabId }: CustomTabViewProps) {
  const { views, openWizard } = useViewStore();
  const { getTabViews } = useLayoutStore();

  const tabViewIds = getTabViews(tabId);
  const tabViews = views.filter((v) => tabViewIds.includes(v.id));

  if (tabViews.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4 text-center">
        <LayoutGrid className="h-12 w-12 text-muted-foreground/30" />
        <div>
          <div className="text-lg font-medium">Empty Tab</div>
          <div className="text-sm text-muted-foreground mt-1">
            Add a view using the View Wizard (⊞ icon in the tab bar),
            or use Smart Tab Generator to auto-populate from your data.
          </div>
        </div>
        <Button variant="outline" onClick={() => openWizard()}>
          <Plus className="h-4 w-4 mr-2" />Create View
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {tabViews.map((view) => {
        const viewType = (view as { viewType?: string }).viewType;
        const table = (view as { primaryTable?: string; baseTable?: string }).primaryTable
          || (view as { primaryTable?: string; baseTable?: string }).baseTable;

        // Auto-generated views use AutoDashboard
        if (viewType === 'auto' && table) {
          return (
            <div key={view.id}>
              <AutoDashboard table={table} title={view.name} showAIInsights={true} />
            </div>
          );
        }

        // Manual wizard views use LiveDataView
        return <LiveDataView key={view.id} view={view as never} />;
      })}

      <div className="flex justify-center pt-2">
        <Button variant="outline" size="sm" onClick={() => openWizard()}>
          <Plus className="h-4 w-4 mr-2" />Add Another View
        </Button>
      </div>
    </div>
  );
}

// ─── ALSO: Update app/page.tsx ────────────────────────────────────────────
// Add SmartTabGenerator to the header or a visible button.
// Add this import to page.tsx:
//
//   import { SmartTabGenerator } from '@/components/views/SmartTabGenerator';
//
// Add this somewhere visible in the layout — e.g. next to the TabBar:
//
//   <div className="flex items-center justify-between px-4 py-2 bg-header border-b border-border">
//     <TabBar />
//     <SmartTabGenerator />
//   </div>
//
// Or add it as a button in the DataManagement tab header.
//
// The SmartTabGenerator button reads all your Supabase tables, finds which have data,
// and auto-creates tabs with AutoDashboard views — no manual configuration.
