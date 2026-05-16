'use client';

import { useState } from 'react';
import {
  TrendingUp, Briefcase, Brain, Cpu, Database,
  Plus, X, Edit2, Check, MoreHorizontal, LayoutGrid,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
  DialogDescription, DialogFooter,
} from '@/components/ui/dialog';
import { useLayoutStore } from '@/store/layoutStore';
import { useViewStore } from '@/store/viewStore';
import { type TabConfig } from '@/types/registry';
import { cn } from '@/lib/utils';

const ICON_MAP: Record<string, React.ElementType> = {
  'performance':     TrendingUp,
  'positions':       Briefcase,
  'ai-intelligence': Brain,
  'brain-engine':    Cpu,
  'data-management': Database,
};

function TabButton({
  tab, isActive, onActivate, onRename, onRemove,
}: {
  tab: TabConfig;
  isActive: boolean;
  onActivate: () => void;
  onRename: (name: string) => void;
  onRemove: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(tab.name);
  const Icon = ICON_MAP[tab.id] ?? LayoutGrid;

  function commit() {
    if (draft.trim()) onRename(draft.trim());
    setEditing(false);
  }

  return (
    <div
      onClick={onActivate}
      className={cn(
        'group relative flex items-center gap-2 px-4 py-3 cursor-pointer select-none shrink-0',
        'border-b-2 transition-all',
        isActive
          ? 'border-primary text-foreground bg-primary/5'
          : 'border-transparent text-muted-foreground hover:text-foreground hover:bg-white/5',
      )}
    >
      <Icon className="h-4 w-4 shrink-0" />

      {editing ? (
        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') setEditing(false); }}
            className="h-5 w-28 text-xs px-1"
            autoFocus
          />
          <Button size="sm" variant="ghost" className="h-5 w-5 p-0" onClick={commit}>
            <Check className="h-3 w-3" />
          </Button>
        </div>
      ) : (
        <span className="text-sm font-medium whitespace-nowrap">{tab.name}</span>
      )}

      {!tab.isCore && !editing && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="ghost"
              className="h-4 w-4 p-0 opacity-0 group-hover:opacity-100 ml-0.5"
              onClick={(e) => e.stopPropagation()}>
              <MoreHorizontal className="h-3 w-3" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem onClick={(e) => { e.stopPropagation(); setEditing(true); }}>
              <Edit2 className="h-3.5 w-3.5 mr-2" />Rename
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="text-destructive" onClick={(e) => { e.stopPropagation(); onRemove(); }}>
              <X className="h-3.5 w-3.5 mr-2" />Remove
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

export function TabBar() {
  const { tabs, activeTabId, setActiveTab, addTab, removeTab, renameTab } = useLayoutStore();
  const { openWizard } = useViewStore();
  const [addOpen, setAddOpen] = useState(false);
  const [removeId, setRemoveId] = useState<string | null>(null);
  const [newName, setNewName] = useState('');

  function handleAdd() {
    if (!newName.trim()) return;
    addTab({
      id: `custom-${Date.now()}`,
      name: newName.trim(),
      icon: 'LayoutGrid',
      isCore: false,
      isVisible: true,
      order: tabs.length,
      views: [],
    });
    setNewName('');
    setAddOpen(false);
  }

  return (
    <>
      <div className="bg-header border-b border-border flex items-stretch overflow-x-auto scrollbar-none">
        <div className="flex items-stretch flex-1 min-w-0">
          {tabs.filter((t) => t.isVisible !== false).map((tab) => (
            <TabButton
              key={tab.id}
              tab={tab}
              isActive={activeTabId === tab.id}
              onActivate={() => setActiveTab(tab.id)}
              onRename={(name) => renameTab(tab.id, name)}
              onRemove={() => setRemoveId(tab.id)}
            />
          ))}
        </div>
        <div className="flex items-center gap-1 px-3 border-l border-border/40 shrink-0">
          <Button variant="ghost" size="sm" className="h-8 w-8 p-0 text-muted-foreground hover:text-foreground"
            title="Add view to current tab" onClick={() => openWizard()}>
            <LayoutGrid className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="sm" className="h-8 w-8 p-0 text-muted-foreground hover:text-foreground"
            title="Add new tab" onClick={() => setAddOpen(true)}>
            <Plus className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Add tab dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>New Custom Tab</DialogTitle>
            <DialogDescription>Add a tab and populate it with views from the View Wizard.</DialogDescription>
          </DialogHeader>
          <Input placeholder="e.g., Sector Analysis" value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAdd()} autoFocus />
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddOpen(false)}>Cancel</Button>
            <Button onClick={handleAdd} disabled={!newName.trim()}>
              <Plus className="h-4 w-4 mr-2" />Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Remove tab dialog */}
      <Dialog open={!!removeId} onOpenChange={() => setRemoveId(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Remove Tab?</DialogTitle>
            <DialogDescription>Views inside remain in your store and can be re-added.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRemoveId(null)}>Cancel</Button>
            <Button variant="destructive" onClick={() => { if (removeId) removeTab(removeId); setRemoveId(null); }}>
              Remove
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function MobileTabBar() {
  const { tabs, activeTabId, setActiveTab } = useLayoutStore();
  return (
    <div className="md:hidden fixed bottom-0 left-0 right-0 bg-header border-t border-border flex z-50">
      {tabs.filter((t) => t.isVisible !== false).slice(0, 5).map((tab) => {
        const Icon = ICON_MAP[tab.id] ?? LayoutGrid;
        return (
          <button key={tab.id} onClick={() => setActiveTab(tab.id)}
            className={cn('flex-1 flex flex-col items-center gap-0.5 py-2 text-xs transition-colors',
              activeTabId === tab.id ? 'text-primary' : 'text-muted-foreground')}>
            <Icon className="h-5 w-5" />
            <span className="truncate max-w-[56px]">{(tab.name ?? 'Tab').split(' ')[0]}</span>
          </button>
        );
      })}
    </div>
  );
}
