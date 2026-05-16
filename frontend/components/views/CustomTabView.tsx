'use client';

import { useState, useEffect } from 'react';
import { Plus, LayoutGrid } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useViewStore } from '@/store/viewStore';
import { useLayoutStore } from '@/store/layoutStore';
import { LiveDataView } from '@/components/views/LiveDataView';

interface CustomTabViewProps {
  tabId: string;
}

export function CustomTabView({ tabId }: CustomTabViewProps) {
  const { views, openWizard } = useViewStore();
  const { getTabViews } = useLayoutStore();

  // Views assigned to this tab
  const tabViewIds = getTabViews(tabId);
  const tabViews = views.filter((v) => tabViewIds.includes(v.id));

  if (tabViews.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4 text-center">
        <LayoutGrid className="h-12 w-12 text-muted-foreground/30" />
        <div>
          <div className="text-lg font-medium">Empty Tab</div>
          <div className="text-sm text-muted-foreground mt-1">
            Add a view by clicking the + button, or use the View Wizard to create one.
          </div>
        </div>
        <Button variant="outline" onClick={() => openWizard()}>
          <Plus className="h-4 w-4 mr-2" />
          Create View
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {tabViews.map((view) => (
        <LiveDataView key={view.id} view={view as never} />
      ))}
      <div className="flex justify-center pt-2">
        <Button variant="outline" size="sm" onClick={() => openWizard()}>
          <Plus className="h-4 w-4 mr-2" />
          Add Another View
        </Button>
      </div>
    </div>
  );
}
