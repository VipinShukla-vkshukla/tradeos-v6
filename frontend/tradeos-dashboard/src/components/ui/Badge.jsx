import React from 'react'
import { cn } from '../../lib/utils'

export function Badge({ className, variant = 'default', children }) {
  const variants = {
    default: 'bg-trade-blue/10 text-trade-blue border-trade-blue/20',
    green: 'bg-trade-green/10 text-trade-green border-trade-green/20',
    yellow: 'bg-trade-yellow/10 text-trade-yellow border-trade-yellow/20',
    red: 'bg-trade-red/10 text-trade-red border-trade-red/20',
    orange: 'bg-trade-orange/10 text-trade-orange border-trade-orange/20',
    purple: 'bg-trade-purple/10 text-trade-purple border-trade-purple/20',
    grey: 'bg-text-dim/20 text-text-muted border-text-dim/30',
  }
  return (
    <span className={cn('inline-flex items-center border px-2 py-0.5 rounded text-xs font-medium', variants[variant], className)}>
      {children}
    </span>
  )
}
