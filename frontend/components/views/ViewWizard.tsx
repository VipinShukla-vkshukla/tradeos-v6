'use client';

import { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { useViewStore } from '@/store/viewStore';
import { useLayoutStore } from '@/store/layoutStore';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { Checkbox } from '@/components/ui/checkbox';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Badge } from '@/components/ui/badge';
import {
  Database, Table2, Columns3, Link2, Filter, Check,
  ChevronRight, ChevronLeft, Sparkles, AlertCircle, Loader2,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { schemaApi, aiApi } from '@/lib/api';
import type { TableMeta, ColumnMeta } from '@/lib/api';

const WIZARD_STEPS = [
  { id: 0, label: 'Name',       icon: Database,  description: 'Name your view' },
  { id: 1, label: 'Base Table', icon: Table2,    description: 'Select primary table' },
  { id: 2, label: 'Columns',    icon: Columns3,  description: 'Choose columns' },
  { id: 3, label: 'Joins',      icon: Link2,     description: 'Add related tables' },
  { id: 4, label: 'Filters',    icon: Filter,    description: 'Set default filters' },
];

export function ViewWizard() {
  const {
    wizardOpen, wizardStep, wizardDraft,
    closeWizard, setWizardStep, updateWizardDraft, addView, updateView,
  } = useViewStore();

  const { tabs, activeTabId, addViewToTab } = useLayoutStore();

  // ── Schema loaded from /api/schema (real Supabase tables) ──────────────
  const [schema, setSchema] = useState<TableMeta[]>([]);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [selectedColumns, setSelectedColumns] = useState<Set<string>>(new Set());

  // ── AI prompt state ─────────────────────────────────────────────────────
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiSuggesting, setAiSuggesting] = useState(false);
  const [aiMessage, setAiMessage] = useState<string | null>(null);

  // ── Tab assignment ──────────────────────────────────────────────────────
  const [targetTabId, setTargetTabId] = useState<string>(activeTabId);

  useEffect(() => {
    if (!wizardOpen) return;
    setSchemaLoading(true);
    schemaApi.getTables()
      .then(setSchema)
      .catch(() => setSchema([]))
      .finally(() => setSchemaLoading(false));
  }, [wizardOpen]);

  useEffect(() => {
    if (wizardDraft?.columns) {
      setSelectedColumns(
        new Set(wizardDraft.columns.map((c: { sourceTable: string; sourceColumn: string }) =>
          `${c.sourceTable}.${c.sourceColumn}`
        ))
      );
    }
  }, [wizardDraft?.columns]);

  useEffect(() => {
    setTargetTabId(activeTabId);
  }, [activeTabId, wizardOpen]);

  const baseTable = wizardDraft?.baseTable || (wizardDraft as { primaryTable?: string })?.primaryTable || '';
  const currentTable = schema.find((t) => t.name === baseTable);

  // Foreign key heuristic: columns ending in _id whose name minus _id matches a table
  const foreignKeyTables = currentTable?.columns
    .filter((c) => c.name.endsWith('_id'))
    .map((c) => ({
      column: c,
      targetTable: schema.find((t) => t.name === c.name.replace(/_id$/, '')),
    }))
    .filter((fk): fk is { column: ColumnMeta; targetTable: TableMeta } => !!fk.targetTable) ?? [];

  // ── Column toggle ────────────────────────────────────────────────────────
  const handleColumnToggle = (tableName: string, col: ColumnMeta) => {
    const key = `${tableName}.${col.name}`;
    const next = new Set(selectedColumns);
    if (next.has(key)) next.delete(key); else next.add(key);
    setSelectedColumns(next);

    const columns = Array.from(next).map((k) => {
      const [tbl, colName] = k.split('.');
      const tSchema = schema.find((t) => t.name === tbl);
      const cSchema = tSchema?.columns.find((c) => c.name === colName);
      return {
        id: crypto.randomUUID(),
        sourceTable: tbl,
        sourceColumn: colName,
        alias: undefined,
        type: cSchema?.type || 'text',
        label: colName.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase()),
        visible: true,
        sortable: true,
        filterable: cSchema?.type !== 'jsonb',
      };
    });
    updateWizardDraft({ columns });
  };

  // ── Join toggle ──────────────────────────────────────────────────────────
  const handleAddJoin = (fkColumn: ColumnMeta, targetTable: TableMeta) => {
    if (!wizardDraft) return;
    const existing = wizardDraft.joins ?? [];
    const alreadyJoined = existing.some((j: { targetTable?: string }) => j.targetTable === targetTable.name);
    if (alreadyJoined) {
      updateWizardDraft({ joins: existing.filter((j: { targetTable?: string }) => j.targetTable !== targetTable.name) });
    } else {
      updateWizardDraft({
        joins: [...existing, {
          id: crypto.randomUUID(),
          type: 'LEFT',
          sourceTable: baseTable,
          sourceColumn: fkColumn.name,
          targetTable: targetTable.name,
          targetColumn: 'id',
        }],
      });
    }
  };

  // ── AI Suggest — calls /api/ai/suggest which reads system_config for provider ──
  const handleAISuggest = async () => {
    if (!baseTable || !aiPrompt.trim()) {
      setAiMessage('Enter a prompt first, e.g. "show me high conviction BUY signals with sector and score"');
      return;
    }
    setAiSuggesting(true);
    setAiMessage(null);
    try {
      const result = await aiApi.suggest({
        context: aiPrompt,
        baseTable,
        availableTables: schema.map((t) => t.name),
        currentColumns: Array.from(selectedColumns),
      });

      // Apply suggested columns
      if (result.suggestedColumns?.length > 0) {
        const next = new Set<string>();
        for (const col of result.suggestedColumns) {
          next.add(`${baseTable}.${col}`);
        }
        setSelectedColumns(next);
        const columns = Array.from(next).map((k) => {
          const [tbl, colName] = k.split('.');
          const tSchema = schema.find((t) => t.name === tbl);
          const cSchema = tSchema?.columns.find((c) => c.name === colName);
          return {
            id: crypto.randomUUID(),
            sourceTable: tbl,
            sourceColumn: colName,
            alias: undefined,
            type: cSchema?.type || 'text',
            label: colName.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase()),
            visible: true,
            sortable: true,
            filterable: cSchema?.type !== 'jsonb',
          };
        });
        updateWizardDraft({ columns });
      }

      // Apply suggested joins
      if (result.suggestedJoins?.length > 0) {
        const joins = result.suggestedJoins.map((j) => ({
          id: crypto.randomUUID(),
          type: j.type || 'LEFT',
          sourceTable: baseTable,
          sourceColumn: j.on.split('=')[0].trim().replace(`${baseTable}.`, ''),
          targetTable: j.table,
          targetColumn: j.on.split('=')[1].trim().replace(`${j.table}.`, ''),
        }));
        updateWizardDraft({ joins });
      }

      setAiMessage(result.explanation || 'Suggestions applied!');
    } catch (e) {
      setAiMessage(e instanceof Error ? e.message : 'AI suggestion failed');
    } finally {
      setAiSuggesting(false);
    }
  };

  // ── Save view + assign to tab ────────────────────────────────────────────
  const handleSave = async () => {
    if (!wizardDraft?.name || !baseTable) return;

    const viewId = wizardDraft.id || crypto.randomUUID();
    const view = {
      id: viewId,
      name: wizardDraft.name,
      description: wizardDraft.description,
      primaryTable: baseTable,
      baseTable,
      columns: wizardDraft.columns ?? [],
      filters: wizardDraft.filters ?? [],
      joins: wizardDraft.joins ?? [],
      sorts: wizardDraft.sorts ?? [],
      viewType: 'table' as const,
      sourceMode: 'single_table' as const,
      createdAt: wizardDraft.createdAt ?? new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };

    const existing = useViewStore.getState().views.find((v) => v.id === viewId);
    if (existing) {
      await updateView(viewId, view as never);
    } else {
      await addView(view as never);
    }

    // Assign view to selected tab
    addViewToTab(targetTabId, viewId);

    closeWizard();
  };

  const canProceed = () => {
    switch (wizardStep) {
      case 0: return !!wizardDraft?.name?.trim();
      case 1: return !!baseTable;
      case 2: return (wizardDraft?.columns?.length ?? 0) > 0;
      default: return true;
    }
  };

  const renderStepContent = () => {
    switch (wizardStep) {
      // ── Step 0: Name ────────────────────────────────────────────────────
      case 0:
        return (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="view-name">View Name</Label>
              <Input id="view-name" placeholder="e.g., High Conviction BUY Signals"
                value={wizardDraft?.name || ''}
                onChange={(e) => updateWizardDraft({ name: e.target.value })}
                className="bg-panel-hover" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="view-description">Description (optional)</Label>
              <Input id="view-description" placeholder="Brief description of this view's purpose"
                value={wizardDraft?.description || ''}
                onChange={(e) => updateWizardDraft({ description: e.target.value })}
                className="bg-panel-hover" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="target-tab">Add to Tab</Label>
              <Select value={targetTabId} onValueChange={setTargetTabId}>
                <SelectTrigger className="bg-panel-hover">
                  <SelectValue placeholder="Select tab" />
                </SelectTrigger>
                <SelectContent>
                  {tabs.map((tab) => (
                    <SelectItem key={tab.id} value={tab.id}>{tab.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        );

      // ── Step 1: Base Table ───────────────────────────────────────────────
      case 1:
        return (
          <div className="space-y-4">
            <Label>Select Base Table</Label>
            {schemaLoading ? (
              <div className="flex items-center justify-center py-8">
                <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                <span className="ml-2 text-sm text-muted-foreground">Loading schema from Supabase...</span>
              </div>
            ) : schema.length === 0 ? (
              <div className="text-center py-8 text-muted-foreground">
                <Database className="h-8 w-8 mx-auto mb-2 opacity-50" />
                <p>No tables loaded</p>
                <p className="text-xs mt-1">Check SUPABASE_SERVICE_ROLE_KEY in .env.local</p>
              </div>
            ) : (
              <ScrollArea className="h-[300px]">
                <div className="grid grid-cols-2 gap-2 pr-2">
                  {schema.map((table) => (
                    <button key={table.name}
                      onClick={() => updateWizardDraft({ baseTable: table.name, primaryTable: table.name, columns: [], joins: [] })}
                      className={cn(
                        'flex items-center gap-3 p-3 rounded-lg border transition-all text-left',
                        baseTable === table.name
                          ? 'border-primary bg-primary/10'
                          : 'border-border hover:border-primary/50 hover:bg-panel-hover'
                      )}>
                      <Table2 className="h-5 w-5 text-muted-foreground shrink-0" />
                      <div className="min-w-0">
                        <div className="font-medium truncate">{table.name}</div>
                        <div className="text-xs text-muted-foreground">{table.columns.length} cols</div>
                      </div>
                      {baseTable === table.name && <Check className="h-4 w-4 text-primary ml-auto shrink-0" />}
                    </button>
                  ))}
                </div>
              </ScrollArea>
            )}
          </div>
        );

      // ── Step 2: Columns ──────────────────────────────────────────────────
      case 2:
        return (
          <div className="space-y-4">
            {/* AI prompt input */}
            <div className="space-y-2">
              <Label>AI Assist (powered by your system_config ai_provider)</Label>
              <div className="flex gap-2">
                <Textarea
                  placeholder='e.g., "show me high conviction BUY signals grouped by sector with score and regime"'
                  value={aiPrompt}
                  onChange={(e) => setAiPrompt(e.target.value)}
                  className="bg-panel-hover text-sm min-h-[60px] resize-none"
                  rows={2}
                />
                <Button variant="outline" size="sm" onClick={handleAISuggest}
                  disabled={aiSuggesting || !baseTable}
                  className="shrink-0 self-start">
                  {aiSuggesting
                    ? <Loader2 className="h-4 w-4 animate-spin" />
                    : <Sparkles className="h-4 w-4" />}
                </Button>
              </div>
              {aiMessage && (
                <p className="text-xs text-muted-foreground">{aiMessage}</p>
              )}
            </div>

            <div className="flex items-center justify-between">
              <Label>Or select columns manually</Label>
              <span className="text-xs text-muted-foreground">{selectedColumns.size} selected</span>
            </div>

            {currentTable ? (
              <ScrollArea className="h-[220px] pr-4">
                <div className="space-y-1">
                  {currentTable.columns.map((col) => {
                    const key = `${currentTable.name}.${col.name}`;
                    const isSelected = selectedColumns.has(key);
                    return (
                      <label key={col.name}
                        className={cn(
                          'flex items-center gap-3 p-2 rounded-lg border cursor-pointer transition-all',
                          isSelected ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50'
                        )}>
                        <Checkbox checked={isSelected}
                          onCheckedChange={() => handleColumnToggle(currentTable.name, col)} />
                        <div className="flex-1 min-w-0 flex items-center gap-2">
                          <span className="font-mono text-sm truncate">{col.name}</span>
                          <Badge variant="outline" className="text-xs shrink-0">{col.type}</Badge>
                        </div>
                      </label>
                    );
                  })}
                </div>
              </ScrollArea>
            ) : (
              <div className="flex items-center gap-2 p-4 rounded-lg bg-warning/10 text-warning">
                <AlertCircle className="h-5 w-5" />
                <span>Please select a base table first</span>
              </div>
            )}
          </div>
        );

      // ── Step 3: Joins ────────────────────────────────────────────────────
      case 3:
        return (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <Label>Join Related Tables</Label>
              <Badge variant="outline">{foreignKeyTables.length} available</Badge>
            </div>

            {foreignKeyTables.length > 0 ? (
              <div className="space-y-2">
                {foreignKeyTables.map(({ column, targetTable }) => {
                  const isJoined = (wizardDraft?.joins ?? []).some(
                    (j: { targetTable?: string }) => j.targetTable === targetTable.name
                  );
                  return (
                    <button key={column.name}
                      onClick={() => handleAddJoin(column, targetTable)}
                      className={cn(
                        'w-full flex items-center gap-3 p-3 rounded-lg border transition-all text-left',
                        isJoined ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/50'
                      )}>
                      <Link2 className={cn('h-5 w-5', isJoined ? 'text-primary' : 'text-muted-foreground')} />
                      <div className="flex-1">
                        <div className="font-medium">{targetTable.name}</div>
                        <div className="text-xs text-muted-foreground">
                          {baseTable}.{column.name} → {targetTable.name}.id
                        </div>
                      </div>
                      {isJoined && <Check className="h-4 w-4 text-primary" />}
                    </button>
                  );
                })}
              </div>
            ) : (
              <div className="text-center py-8 text-muted-foreground">
                <Link2 className="h-8 w-8 mx-auto mb-2 opacity-30" />
                <p className="text-sm">No foreign key columns detected</p>
                <p className="text-xs mt-1">Columns ending in _id are detected automatically</p>
              </div>
            )}

            {(wizardDraft?.joins?.length ?? 0) > 0 && (
              <div className="p-3 rounded-lg bg-panel-hover">
                <Label className="text-xs text-muted-foreground mb-2 block">
                  Joined tables — columns from these are available in step 2
                </Label>
                <div className="flex flex-wrap gap-2">
                  {wizardDraft?.joins?.map((j: { id: string; targetTable?: string }) => (
                    <Badge key={j.id} variant="secondary">{j.targetTable}</Badge>
                  ))}
                </div>
              </div>
            )}
          </div>
        );

      // ── Step 4: Filters + Summary ────────────────────────────────────────
      case 4:
        return (
          <div className="space-y-4">
            <Label>Default Filters (optional)</Label>
            <div className="p-4 rounded-lg border border-dashed border-border text-center text-muted-foreground">
              <Filter className="h-8 w-8 mx-auto mb-2 opacity-50" />
              <p className="text-sm">Filters can be added after creating the view</p>
              <p className="text-xs mt-1">Use the Enhance menu on the view panel</p>
            </div>

            <div className="p-4 rounded-lg bg-primary/10 border border-primary/20">
              <h4 className="font-medium mb-3">View Summary</h4>
              <div className="space-y-1.5 text-sm">
                {[
                  ['Name', wizardDraft?.name],
                  ['Base Table', baseTable],
                  ['Columns', `${wizardDraft?.columns?.length ?? 0} selected`],
                  ['Joins', `${wizardDraft?.joins?.length ?? 0} tables`],
                  ['Add to Tab', tabs.find((t) => t.id === targetTabId)?.label ?? targetTabId],
                ].map(([label, val]) => (
                  <div key={label} className="flex justify-between">
                    <span className="text-muted-foreground">{label}:</span>
                    <span className="font-medium">{val}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        );

      default:
        return null;
    }
  };

  return (
    <Dialog open={wizardOpen} onOpenChange={closeWizard}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Create Dynamic View</DialogTitle>
          <DialogDescription>
            Build a custom view from your Supabase tables — with AI assist powered by your system_config ai_provider
          </DialogDescription>
        </DialogHeader>

        {/* Step Indicator */}
        <div className="flex items-center justify-between px-2 py-4 border-b border-border">
          {WIZARD_STEPS.map((step, index) => {
            const Icon = step.icon;
            const isActive = wizardStep === index;
            const isComplete = wizardStep > index;
            return (
              <button key={step.id}
                onClick={() => index < wizardStep && setWizardStep(index)}
                disabled={index > wizardStep}
                className={cn(
                  'flex flex-col items-center gap-1 transition-all',
                  isActive && 'text-primary',
                  isComplete && 'text-success cursor-pointer',
                  !isActive && !isComplete && 'text-muted-foreground'
                )}>
                <div className={cn(
                  'w-10 h-10 rounded-full flex items-center justify-center border-2 transition-all',
                  isActive && 'border-primary bg-primary/10',
                  isComplete && 'border-success bg-success/10',
                  !isActive && !isComplete && 'border-border'
                )}>
                  {isComplete ? <Check className="h-5 w-5" /> : <Icon className="h-5 w-5" />}
                </div>
                <span className="text-xs font-medium">{step.label}</span>
              </button>
            );
          })}
        </div>

        {/* Step Content */}
        <div className="min-h-[350px] py-4">
          <AnimatePresence mode="wait">
            <motion.div key={wizardStep}
              initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -20 }} transition={{ duration: 0.2 }}>
              {renderStepContent()}
            </motion.div>
          </AnimatePresence>
        </div>

        {/* Navigation */}
        <div className="flex items-center justify-between pt-4 border-t border-border">
          <Button variant="outline"
            onClick={() => wizardStep > 0 ? setWizardStep(wizardStep - 1) : closeWizard()}>
            <ChevronLeft className="h-4 w-4 mr-2" />
            {wizardStep > 0 ? 'Back' : 'Cancel'}
          </Button>
          {wizardStep < WIZARD_STEPS.length - 1 ? (
            <Button onClick={() => setWizardStep(wizardStep + 1)} disabled={!canProceed()}>
              Next <ChevronRight className="h-4 w-4 ml-2" />
            </Button>
          ) : (
            <Button onClick={handleSave} disabled={!canProceed()}>
              <Check className="h-4 w-4 mr-2" /> Create View
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
