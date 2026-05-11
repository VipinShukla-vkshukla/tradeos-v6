import React, { useState, useEffect } from 'react'
import { Activity, Clock } from 'lucide-react'
import { Badge } from './ui/Badge'

export function Header({ regime = 'RISK ON', pipelineLastRun = '2026-05-09 14:02 IST' }) {
  const [time, setTime] = useState(new Date())

  useEffect(() => {
    const timer = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(timer)
  }, [])

  const regimeColors = {
    'RISK ON': 'green',
    'NEUTRAL': 'yellow',
    'RECOVERING': 'blue',
    'RISK OFF': 'red',
  }

  return (
    <header className="border-b border-border-custom bg-bg-surface/50 backdrop-blur">
      <div className="flex items-center justify-between px-6 py-3">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-trade-green animate-pulse" />
            <h1 className="text-lg font-bold tracking-tight text-text-primary">
              TRADEOS INTELLIGENCE CONSOLE
            </h1>
          </div>
        </div>
        <div className="flex items-center gap-4">
          <Badge variant={regimeColors[regime] || 'grey'}>{regime}</Badge>
          <div className="flex items-center gap-1.5 text-xs text-text-muted">
            <Activity size={14} className="text-trade-green" />
            <span>Pipeline: {pipelineLastRun}</span>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-text-muted font-mono">
            <Clock size={14} />
            <span>{time.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false })} IST</span>
          </div>
        </div>
      </div>
    </header>
  )
}
