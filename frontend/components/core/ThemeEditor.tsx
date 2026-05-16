'use client';

import { useState, useCallback } from 'react';
import { 
  X, 
  Palette, 
  Download, 
  Upload, 
  Share2, 
  RotateCcw,
  Save,
  Check,
  Copy,
  Trash2
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion';
import { useThemeStore } from '@/store/themeStore';
import { THEME_PRESETS, type ThemeTokens } from '@/types/theme';

interface ThemeEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// Token groups for organization
const tokenGroups: Record<string, { label: string; tokens: (keyof ThemeTokens)[] }> = {
  backgrounds: {
    label: 'Backgrounds',
    tokens: ['bgDashboard', 'bgPanel', 'bgPanelHover', 'bgHeader', 'bgInput', 'bgModal', 'bgTooltip'],
  },
  text: {
    label: 'Text Colors',
    tokens: ['textPrimary', 'textSecondary', 'textMuted', 'textInverse'],
  },
  borders: {
    label: 'Borders',
    tokens: ['borderDefault', 'borderFocus', 'borderError'],
  },
  semantic: {
    label: 'Semantic Colors',
    tokens: ['accentPrimary', 'accentPrimaryHover', 'success', 'warning', 'danger', 'info'],
  },
  trading: {
    label: 'Trading Colors',
    tokens: ['profitGreen', 'lossRed', 'bullish', 'bearish', 'neutral'],
  },
  charts: {
    label: 'Chart Palette',
    tokens: ['chart1', 'chart2', 'chart3', 'chart4', 'chart5', 'chart6', 'chart7', 'chart8'],
  },
};

// Friendly names for tokens
const tokenLabels: Partial<Record<keyof ThemeTokens, string>> = {
  bgDashboard: 'Dashboard',
  bgPanel: 'Panel',
  bgPanelHover: 'Panel Hover',
  bgHeader: 'Header',
  bgInput: 'Input',
  bgModal: 'Modal',
  bgTooltip: 'Tooltip',
  textPrimary: 'Primary',
  textSecondary: 'Secondary',
  textMuted: 'Muted',
  textInverse: 'Inverse',
  borderDefault: 'Default',
  borderFocus: 'Focus',
  borderError: 'Error',
  accentPrimary: 'Primary Accent',
  accentPrimaryHover: 'Primary Hover',
  profitGreen: 'Profit',
  lossRed: 'Loss',
};

function ColorPicker({
  tokenKey,
  value,
  label,
  onChange,
}: {
  tokenKey: string;
  value: string;
  label: string;
  onChange: (key: keyof ThemeTokens, value: string) => void;
}) {
  const [localValue, setLocalValue] = useState(value);

  const handleChange = useCallback(
    (newValue: string) => {
      setLocalValue(newValue);
      // Debounce the actual change
      const timeout = setTimeout(() => {
        onChange(tokenKey as keyof ThemeTokens, newValue);
      }, 100);
      return () => clearTimeout(timeout);
    },
    [tokenKey, onChange]
  );

  return (
    <div className="flex items-center gap-2">
      <div className="relative">
        <input
          type="color"
          value={localValue}
          onChange={(e) => handleChange(e.target.value)}
          className="sr-only"
          id={`color-${tokenKey}`}
        />
        <label
          htmlFor={`color-${tokenKey}`}
          className="block h-8 w-8 rounded-md border border-border cursor-pointer hover:ring-2 hover:ring-ring transition-shadow"
          style={{ backgroundColor: localValue }}
        />
      </div>
      <div className="flex-1 min-w-0">
        <Label htmlFor={`input-${tokenKey}`} className="text-xs text-muted-foreground">
          {label}
        </Label>
        <Input
          id={`input-${tokenKey}`}
          value={localValue}
          onChange={(e) => handleChange(e.target.value)}
          className="h-7 text-xs font-mono"
          placeholder="#000000"
        />
      </div>
    </div>
  );
}

export function ThemeEditor({ open, onOpenChange }: ThemeEditorProps) {
  const {
    currentPresetId,
    customPresets,
    getAllPresets,
    setPreset,
    setToken,
    resetToPreset,
    saveAsCustomPreset,
    deleteCustomPreset,
    importTheme,
    exportTheme,
    getShareUrl,
    getCurrentTheme,
  } = useThemeStore();

  const [saveDialogOpen, setSaveDialogOpen] = useState(false);
  const [newPresetName, setNewPresetName] = useState('');
  const [copied, setCopied] = useState(false);

  const currentTheme = getCurrentTheme();
  const allPresets = getAllPresets();

  const handleSavePreset = () => {
    if (newPresetName.trim()) {
      saveAsCustomPreset(newPresetName.trim());
      setNewPresetName('');
      setSaveDialogOpen(false);
    }
  };

  const handleExport = () => {
    const json = exportTheme();
    const blob = new Blob([json], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'tradeos-theme.json';
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleImport = () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (file) {
        const text = await file.text();
        if (!importTheme(text)) {
          alert('Invalid theme file');
        }
      }
    };
    input.click();
  };

  const handleShare = () => {
    const url = getShareUrl();
    navigator.clipboard.writeText(url);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-[400px] sm:w-[540px] overflow-y-auto bg-panel border-border">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <Palette className="h-5 w-5" />
            Theme Editor
          </SheetTitle>
          <SheetDescription>
            Customize the dashboard appearance with theme presets or create your own.
          </SheetDescription>
        </SheetHeader>

        <div className="mt-6 space-y-6">
          {/* Preset selector */}
          <div className="space-y-3">
            <Label className="text-sm font-medium">Theme Presets</Label>
            <div className="grid grid-cols-2 gap-2">
              {allPresets.map((preset) => (
                <button
                  key={preset.id}
                  onClick={() => setPreset(preset.id)}
                  className={`
                    relative p-3 rounded-lg border text-left transition-all
                    ${currentPresetId === preset.id
                      ? 'border-accent-primary bg-accent-primary/10'
                      : 'border-border hover:border-muted-foreground'
                    }
                  `}
                >
                  <div className="flex items-center gap-2 mb-2">
                    <div
                      className="h-4 w-4 rounded-full border border-border"
                      style={{ backgroundColor: preset.tokens.accentPrimary }}
                    />
                    <span className="text-sm font-medium truncate">{preset.name}</span>
                  </div>
                  <div className="flex gap-1">
                    {[preset.tokens.bgPanel, preset.tokens.textPrimary, preset.tokens.accentPrimary].map(
                      (color, i) => (
                        <div
                          key={i}
                          className="h-3 w-3 rounded-sm border border-border"
                          style={{ backgroundColor: color }}
                        />
                      )
                    )}
                  </div>
                  {currentPresetId === preset.id && (
                    <div className="absolute top-2 right-2">
                      <Check className="h-4 w-4 text-accent-primary" />
                    </div>
                  )}
                  {!preset.id.startsWith('custom-') && !THEME_PRESETS.find(p => p.id === preset.id) && (
                    <Button
                      variant="ghost"
                      size="icon"
                      className="absolute top-1 right-1 h-6 w-6 opacity-0 group-hover:opacity-100"
                      onClick={(e) => {
                        e.stopPropagation();
                        deleteCustomPreset(preset.id);
                      }}
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  )}
                </button>
              ))}
            </div>
          </div>

          {/* Action buttons */}
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={handleExport} className="gap-1.5">
              <Download className="h-3.5 w-3.5" />
              Export
            </Button>
            <Button variant="outline" size="sm" onClick={handleImport} className="gap-1.5">
              <Upload className="h-3.5 w-3.5" />
              Import
            </Button>
            <Button variant="outline" size="sm" onClick={handleShare} className="gap-1.5">
              {copied ? <Check className="h-3.5 w-3.5" /> : <Share2 className="h-3.5 w-3.5" />}
              {copied ? 'Copied!' : 'Share'}
            </Button>
            <Button variant="outline" size="sm" onClick={resetToPreset} className="gap-1.5">
              <RotateCcw className="h-3.5 w-3.5" />
              Reset
            </Button>
          </div>

          {/* Save as preset */}
          {saveDialogOpen ? (
            <div className="flex gap-2">
              <Input
                value={newPresetName}
                onChange={(e) => setNewPresetName(e.target.value)}
                placeholder="Preset name..."
                className="h-9"
                autoFocus
              />
              <Button size="sm" onClick={handleSavePreset}>Save</Button>
              <Button size="sm" variant="ghost" onClick={() => setSaveDialogOpen(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
          ) : (
            <Button 
              variant="secondary" 
              size="sm" 
              onClick={() => setSaveDialogOpen(true)}
              className="w-full gap-1.5"
            >
              <Save className="h-3.5 w-3.5" />
              Save as Custom Preset
            </Button>
          )}

          {/* Color pickers by group */}
          <Accordion type="multiple" className="w-full" defaultValue={['backgrounds']}>
            {Object.entries(tokenGroups).map(([groupKey, group]) => (
              <AccordionItem key={groupKey} value={groupKey}>
                <AccordionTrigger className="text-sm font-medium">
                  {group.label}
                </AccordionTrigger>
                <AccordionContent>
                  <div className="grid grid-cols-2 gap-3 pt-2">
                    {group.tokens.map((tokenKey) => (
                      <ColorPicker
                        key={tokenKey}
                        tokenKey={tokenKey}
                        value={currentTheme[tokenKey]}
                        label={tokenLabels[tokenKey] || tokenKey}
                        onChange={setToken}
                      />
                    ))}
                  </div>
                </AccordionContent>
              </AccordionItem>
            ))}
          </Accordion>
        </div>
      </SheetContent>
    </Sheet>
  );
}

export default ThemeEditor;
