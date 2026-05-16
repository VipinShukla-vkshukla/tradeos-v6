import React from 'react'
import { cn } from '../../lib/utils'

export function Button({ className, variant = 'default', size = 'default', children, ...props }) {
  const variants = {
    default: 'bg-trade-blue text-white hover:bg-trade-blue/90',
    outline: 'border border-border-custom bg-transparent hover:bg-bg-elevated',
    ghost: 'hover:bg-bg-elevated',
    danger: 'bg-trade-red text-white hover:bg-trade-red/90',
    success: 'bg-trade-green text-white hover:bg-trade-green/90',
  }
  const sizes = {
    default: 'h-9 px-4 py-2',
    sm: 'h-7 px-3 text-xs',
    lg: 'h-11 px-6',
    icon: 'h-9 w-9',
  }
  return (
    <button
      className={cn(
        'inline-flex items-center justify-center rounded-md text-sm font-medium transition-colors focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50',
        variants[variant],
        sizes[size],
        className
      )}
      {...props}
    >
      {children}
    </button>
  )
}
