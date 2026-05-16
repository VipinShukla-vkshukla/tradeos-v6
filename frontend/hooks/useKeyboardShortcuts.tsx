'use client'

import { useEffect, useCallback, type ReactNode } from 'react'
import { useLayoutStore } from '@/store/layoutStore'
import { useThemeStore } from '@/store/themeStore'
import { useViewStore } from '@/store/viewStore'
import { THEME_PRESETS } from '@/types/theme'

type ShortcutHandler = () => void

interface Shortcut {
  key: string
  ctrl?: boolean
  meta?: boolean
  shift?: boolean
  alt?: boolean
  handler: ShortcutHandler
  description: string
}

// Get preset IDs for cycling
const PRESET_IDS = THEME_PRESETS.map(p => p.id)

const SHORTCUTS: Shortcut[] = [
  // Navigation shortcuts
  { key: '1', meta: true, handler: () => useLayoutStore.getState().setActiveTab('performance'), description: 'Go to Performance' },
  { key: '2', meta: true, handler: () => useLayoutStore.getState().setActiveTab('positions'), description: 'Go to Positions' },
  { key: '3', meta: true, handler: () => useLayoutStore.getState().setActiveTab('ai-intelligence'), description: 'Go to AI Intelligence' },
  { key: '4', meta: true, handler: () => useLayoutStore.getState().setActiveTab('brain-engine'), description: 'Go to Brain Engine' },
  { key: '5', meta: true, handler: () => useLayoutStore.getState().setActiveTab('data-management'), description: 'Go to Data Management' },
  
  // Action shortcuts
  { key: 'n', meta: true, shift: true, handler: () => useViewStore.getState().openWizard(), description: 'Create new view' },
  { key: 'b', meta: true, handler: () => useLayoutStore.getState().setChartBuilderOpen(true), description: 'Open Chart Builder' },
  { key: 'r', meta: true, shift: true, handler: () => window.location.reload(), description: 'Refresh all data' },
  
  // Theme presets - cycle through available themes
  { key: '`', meta: true, handler: () => {
    const state = useThemeStore.getState()
    const currentIndex = PRESET_IDS.indexOf(state.currentPresetId)
    const nextIndex = (currentIndex + 1) % PRESET_IDS.length
    state.setPreset(PRESET_IDS[nextIndex])
  }, description: 'Cycle themes' },
]

export function useKeyboardShortcuts() {
  const handleKeyDown = useCallback((event: KeyboardEvent) => {
    // Don't trigger shortcuts when typing in inputs
    if (
      event.target instanceof HTMLInputElement ||
      event.target instanceof HTMLTextAreaElement ||
      (event.target instanceof HTMLElement && event.target.isContentEditable)
    ) {
      return
    }

    for (const shortcut of SHORTCUTS) {
      const metaMatch = shortcut.meta ? (event.metaKey || event.ctrlKey) : true
      const ctrlMatch = shortcut.ctrl ? event.ctrlKey : true
      const shiftMatch = shortcut.shift ? event.shiftKey : !event.shiftKey
      const altMatch = shortcut.alt ? event.altKey : !event.altKey
      const keyMatch = event.key.toLowerCase() === shortcut.key.toLowerCase()

      if (keyMatch && metaMatch && ctrlMatch && shiftMatch && altMatch) {
        event.preventDefault()
        shortcut.handler()
        return
      }
    }
  }, [])

  useEffect(() => {
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])

  return { shortcuts: SHORTCUTS }
}

export function KeyboardShortcutsProvider({ children }: { children: ReactNode }) {
  useKeyboardShortcuts()
  return <>{children}</>
}
