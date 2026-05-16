'use client'

import { useEffect, useCallback, useRef, useState } from 'react'
import { supabase, isSupabaseConfigured } from '@/lib/supabase'
import type { RealtimeChannel, RealtimePostgresChangesPayload } from '@supabase/supabase-js'
import { mutate } from 'swr'

type ChangeHandler<T> = (payload: RealtimePostgresChangesPayload<T>) => void

interface UseRealtimeOptions<T> {
  table: string
  schema?: string
  event?: 'INSERT' | 'UPDATE' | 'DELETE' | '*'
  filter?: string
  onInsert?: (record: T) => void
  onUpdate?: (record: T, old: T) => void
  onDelete?: (old: T) => void
  swrKey?: string // Automatically invalidate SWR cache on changes
}

export function useRealtime<T extends Record<string, unknown>>({
  table,
  schema = 'public',
  event = '*',
  filter,
  onInsert,
  onUpdate,
  onDelete,
  swrKey
}: UseRealtimeOptions<T>) {
  const channelRef = useRef<RealtimeChannel | null>(null)
  const [isConnected, setIsConnected] = useState(false)
  const [lastEvent, setLastEvent] = useState<RealtimePostgresChangesPayload<T> | null>(null)

  const handleChange: ChangeHandler<T> = useCallback((payload) => {
    setLastEvent(payload)
    
    // Invalidate SWR cache if key provided
    if (swrKey) {
      mutate(swrKey)
    }

    switch (payload.eventType) {
      case 'INSERT':
        onInsert?.(payload.new as T)
        break
      case 'UPDATE':
        onUpdate?.(payload.new as T, payload.old as T)
        break
      case 'DELETE':
        onDelete?.(payload.old as T)
        break
    }
  }, [onInsert, onUpdate, onDelete, swrKey])

  useEffect(() => {
    if (!isSupabaseConfigured()) {
      return
    }

    const channelName = `${schema}:${table}:${event}${filter ? `:${filter}` : ''}`
    
    const channel = supabase
      .channel(channelName)
      .on(
        'postgres_changes',
        {
          event,
          schema,
          table,
          filter
        },
        handleChange
      )
      .subscribe((status) => {
        setIsConnected(status === 'SUBSCRIBED')
      })

    channelRef.current = channel

    return () => {
      if (channelRef.current) {
        supabase.removeChannel(channelRef.current)
        channelRef.current = null
      }
    }
  }, [table, schema, event, filter, handleChange])

  return { isConnected, lastEvent }
}

// Hook for subscribing to multiple tables at once
interface MultiTableSubscription<T> {
  table: string
  event?: 'INSERT' | 'UPDATE' | 'DELETE' | '*'
  filter?: string
  handler: ChangeHandler<T>
}

export function useRealtimeMulti<T extends Record<string, unknown>>(
  subscriptions: MultiTableSubscription<T>[]
) {
  const channelsRef = useRef<RealtimeChannel[]>([])
  const [connectedTables, setConnectedTables] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (!isSupabaseConfigured()) {
      return
    }

    const channels: RealtimeChannel[] = []

    subscriptions.forEach(({ table, event = '*', filter, handler }) => {
      const channelName = `multi:${table}:${event}${filter ? `:${filter}` : ''}`
      
      const channel = supabase
        .channel(channelName)
        .on(
          'postgres_changes',
          {
            event,
            schema: 'public',
            table,
            filter
          },
          handler
        )
        .subscribe((status) => {
          setConnectedTables(prev => {
            const next = new Set(prev)
            if (status === 'SUBSCRIBED') {
              next.add(table)
            } else {
              next.delete(table)
            }
            return next
          })
        })

      channels.push(channel)
    })

    channelsRef.current = channels

    return () => {
      channels.forEach(channel => {
        supabase.removeChannel(channel)
      })
      channelsRef.current = []
    }
  }, [subscriptions])

  return { connectedTables }
}

// Specialized hooks for common use cases

export function useSignalsRealtime(onNewSignal?: (signal: Record<string, unknown>) => void) {
  return useRealtime({
    table: 'signals',
    event: 'INSERT',
    onInsert: onNewSignal,
    swrKey: 'signals'
  })
}

export function usePositionsRealtime(
  onUpdate?: (position: Record<string, unknown>) => void
) {
  return useRealtime({
    table: 'positions',
    event: '*',
    onInsert: onUpdate,
    onUpdate: (newPos) => onUpdate?.(newPos),
    swrKey: 'positions'
  })
}

export function useProposalsRealtime(
  onNewProposal?: (proposal: Record<string, unknown>) => void
) {
  return useRealtime({
    table: 'proposals',
    event: 'INSERT',
    onInsert: onNewProposal,
    swrKey: 'proposals'
  })
}

// Presence tracking for collaborative features
export function usePresence(roomId: string) {
  const [users, setUsers] = useState<Array<{ user_id: string; online_at: string }>>([])
  const channelRef = useRef<RealtimeChannel | null>(null)

  useEffect(() => {
    if (!isSupabaseConfigured()) {
      return
    }

    const channel = supabase.channel(`presence:${roomId}`)
    
    channel
      .on('presence', { event: 'sync' }, () => {
        const state = channel.presenceState()
        const userList = Object.values(state).flat() as Array<{ user_id: string; online_at: string }>
        setUsers(userList)
      })
      .on('presence', { event: 'join' }, ({ newPresences }) => {
        setUsers(prev => [...prev, ...newPresences as Array<{ user_id: string; online_at: string }>])
      })
      .on('presence', { event: 'leave' }, ({ leftPresences }) => {
        const leftIds = new Set((leftPresences as Array<{ user_id: string }>).map(p => p.user_id))
        setUsers(prev => prev.filter(u => !leftIds.has(u.user_id)))
      })
      .subscribe(async (status) => {
        if (status === 'SUBSCRIBED') {
          await channel.track({
            user_id: crypto.randomUUID(),
            online_at: new Date().toISOString()
          })
        }
      })

    channelRef.current = channel

    return () => {
      if (channelRef.current) {
        supabase.removeChannel(channelRef.current)
        channelRef.current = null
      }
    }
  }, [roomId])

  return { users }
}
