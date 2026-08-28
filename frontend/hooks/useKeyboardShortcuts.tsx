'use client'

import { useEffect, useCallback, type ReactNode } from 'react'
import { useLayoutStore } from '@/store/layoutStore'

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

const SHORTCUTS: Shortcut[] = [
  { key: '1', meta: true, handler: () => useLayoutStore.getState().setActiveTab('overview'), description: 'Go to Overview' },
  { key: '2', meta: true, handler: () => useLayoutStore.getState().setActiveTab('positions'), description: 'Go to Positions & P&L' },
  { key: '3', meta: true, handler: () => useLayoutStore.getState().setActiveTab('intraday'), description: 'Go to Intraday' },
  { key: '4', meta: true, handler: () => useLayoutStore.getState().setActiveTab('performance'), description: 'Go to Engines' },
  { key: '5', meta: true, handler: () => useLayoutStore.getState().setActiveTab('allocator'), description: 'Go to Allocator' },
  { key: '6', meta: true, handler: () => useLayoutStore.getState().setActiveTab('brain-engine'), description: 'Go to Brain Engine' },
  { key: '7', meta: true, handler: () => useLayoutStore.getState().setActiveTab('ai-intelligence'), description: 'Go to AI Intelligence' },
  { key: '8', meta: true, handler: () => useLayoutStore.getState().setActiveTab('data-management'), description: 'Go to Data Management' },
  { key: 'r', meta: true, shift: true, handler: () => window.location.reload(), description: 'Refresh all data' },
]

export function useKeyboardShortcuts() {
  const handleKeyDown = useCallback((event: KeyboardEvent) => {
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
