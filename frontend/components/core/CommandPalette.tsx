'use client'

import { useEffect, useState, useCallback } from 'react'
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
  CommandShortcut,
} from '@/components/ui/command'
import { Badge } from '@/components/ui/badge'
import {
  BarChart3,
  Brain,
  Database,
  Plus,
  Settings,
  Palette,
  RefreshCw,
  Download,
  Sparkles,
  TrendingUp,
  Briefcase,
  Table2,
} from 'lucide-react'
import { useLayoutStore } from '@/store/layoutStore'
import { useViewStore } from '@/store/viewStore'
import { useThemeStore } from '@/store/themeStore'
import { THEME_PRESETS } from '@/types/theme'

interface CommandAction {
  id: string
  label: string
  description?: string
  icon: React.ComponentType<{ className?: string }>
  shortcut?: string
  action: () => void
  group: 'navigation' | 'views' | 'actions' | 'settings'
  keywords?: string[]
}

export function CommandPalette() {
  const [open, setOpen] = useState(false)
  const { setActiveTab } = useLayoutStore()
  const { openWizard, views } = useViewStore()
  const { setPreset, currentPresetId } = useThemeStore()

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.key === 'k' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        setOpen((open) => !open)
      }
    }

    document.addEventListener('keydown', down)
    return () => document.removeEventListener('keydown', down)
  }, [])

  const runAction = useCallback((action: () => void) => {
    action()
    setOpen(false)
  }, [])

  const actions: CommandAction[] = [
    // Navigation
    {
      id: 'nav-performance',
      label: 'Performance',
      description: 'View performance metrics and analytics',
      icon: TrendingUp,
      shortcut: '1',
      action: () => setActiveTab('performance'),
      group: 'navigation',
      keywords: ['kpi', 'metrics', 'charts', 'analytics']
    },
    {
      id: 'nav-positions',
      label: 'Positions',
      description: 'View and manage trading positions',
      icon: Briefcase,
      shortcut: '2',
      action: () => setActiveTab('positions'),
      group: 'navigation',
      keywords: ['trades', 'portfolio', 'holdings']
    },
    {
      id: 'nav-ai',
      label: 'AI Intelligence',
      description: 'AI-generated signals and insights',
      icon: Sparkles,
      shortcut: '3',
      action: () => setActiveTab('ai-intelligence'),
      group: 'navigation',
      keywords: ['signals', 'intelligence', 'recommendations']
    },
    {
      id: 'nav-brain',
      label: 'Brain Engine',
      description: 'Engine proposals and audit trails',
      icon: Brain,
      shortcut: '4',
      action: () => setActiveTab('brain-engine'),
      group: 'navigation',
      keywords: ['engine', 'proposals', 'audit', 'logs']
    },
    {
      id: 'nav-data',
      label: 'Data Management',
      description: 'Manage data sources and schemas',
      icon: Database,
      shortcut: '5',
      action: () => setActiveTab('data-management'),
      group: 'navigation',
      keywords: ['tables', 'schema', 'sources']
    },

    // Views
    {
      id: 'view-create',
      label: 'Create New View',
      description: 'Build a custom data view',
      icon: Plus,
      action: () => openWizard(),
      group: 'views',
      keywords: ['new', 'custom', 'build']
    },
    ...views.map((view) => ({
      id: `view-${view.id}`,
      label: view.name,
      description: `Custom view on ${view.baseTable}`,
      icon: Table2,
      action: () => {
        useViewStore.getState().setActiveView(view.id)
        setActiveTab('data-management')
      },
      group: 'views' as const,
      keywords: [view.baseTable, 'custom', 'view']
    })),

    // Actions
    {
      id: 'action-refresh',
      label: 'Refresh All Data',
      description: 'Reload data from all sources',
      icon: RefreshCw,
      shortcut: 'R',
      action: () => window.location.reload(),
      group: 'actions',
      keywords: ['reload', 'update', 'sync']
    },
    {
      id: 'action-chart',
      label: 'Open Chart Builder',
      description: 'Create custom visualizations',
      icon: BarChart3,
      action: () => useLayoutStore.getState().setChartBuilderOpen(true),
      group: 'actions',
      keywords: ['visualization', 'graph', 'build']
    },
    {
      id: 'action-export',
      label: 'Export Dashboard',
      description: 'Download dashboard configuration',
      icon: Download,
      action: () => {
        const config = {
          layout: useLayoutStore.getState(),
          views: useViewStore.getState().views,
          theme: currentPresetId
        }
        const blob = new Blob([JSON.stringify(config, null, 2)], { type: 'application/json' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = 'tradeos-dashboard.json'
        a.click()
        URL.revokeObjectURL(url)
      },
      group: 'actions',
      keywords: ['download', 'save', 'backup']
    },

    // Settings - Theme presets from THEME_PRESETS
    ...THEME_PRESETS.map((preset) => ({
      id: `settings-preset-${preset.id}`,
      label: `${preset.name} Theme`,
      icon: Palette,
      action: () => setPreset(preset.id),
      group: 'settings' as const,
      keywords: ['theme', preset.name.toLowerCase()]
    })),
  ]

  const groupedActions = {
    navigation: actions.filter(a => a.group === 'navigation'),
    views: actions.filter(a => a.group === 'views'),
    actions: actions.filter(a => a.group === 'actions'),
    settings: actions.filter(a => a.group === 'settings'),
  }

  return (
    <CommandDialog open={open} onOpenChange={setOpen}>
      <CommandInput placeholder="Type a command or search..." />
      <CommandList>
        <CommandEmpty>No results found.</CommandEmpty>

        <CommandGroup heading="Navigation">
          {groupedActions.navigation.map((action) => {
            const Icon = action.icon
            return (
              <CommandItem
                key={action.id}
                onSelect={() => runAction(action.action)}
                className="flex items-center gap-3"
              >
                <Icon className="h-4 w-4" />
                <div className="flex flex-col flex-1">
                  <span>{action.label}</span>
                  {action.description && (
                    <span className="text-xs text-muted-foreground">{action.description}</span>
                  )}
                </div>
                {action.shortcut && (
                  <CommandShortcut>⌘{action.shortcut}</CommandShortcut>
                )}
              </CommandItem>
            )
          })}
        </CommandGroup>

        {groupedActions.views.length > 0 && (
          <>
            <CommandSeparator />
            <CommandGroup heading="Views">
              {groupedActions.views.map((action) => {
                const Icon = action.icon
                return (
                  <CommandItem
                    key={action.id}
                    onSelect={() => runAction(action.action)}
                    className="flex items-center gap-3"
                  >
                    <Icon className="h-4 w-4" />
                    <div className="flex flex-col flex-1">
                      <span>{action.label}</span>
                      {action.description && (
                        <span className="text-xs text-muted-foreground">{action.description}</span>
                      )}
                    </div>
                  </CommandItem>
                )
              })}
            </CommandGroup>
          </>
        )}

        <CommandSeparator />
        <CommandGroup heading="Actions">
          {groupedActions.actions.map((action) => {
            const Icon = action.icon
            return (
              <CommandItem
                key={action.id}
                onSelect={() => runAction(action.action)}
                className="flex items-center gap-3"
              >
                <Icon className="h-4 w-4" />
                <div className="flex flex-col flex-1">
                  <span>{action.label}</span>
                  {action.description && (
                    <span className="text-xs text-muted-foreground">{action.description}</span>
                  )}
                </div>
                {action.shortcut && (
                  <CommandShortcut>⌘{action.shortcut}</CommandShortcut>
                )}
              </CommandItem>
            )
          })}
        </CommandGroup>

        <CommandSeparator />
        <CommandGroup heading="Themes">
          {groupedActions.settings.map((action) => {
            const Icon = action.icon
            return (
              <CommandItem
                key={action.id}
                onSelect={() => runAction(action.action)}
                className="flex items-center gap-3"
              >
                <Icon className="h-4 w-4" />
                <span>{action.label}</span>
              </CommandItem>
            )
          })}
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}
