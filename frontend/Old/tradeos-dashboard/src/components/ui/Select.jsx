import React from 'react'
import { cn } from '../../lib/utils'

export function Select({ className, options, value, onChange, placeholder }) {
  return (
    <select
      value={value}
      onChange={onChange}
      className={cn(
        'flex h-9 w-full rounded-md border border-border-custom bg-bg-base px-3 py-1 text-sm text-text-primary shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-trade-blue',
        className
      )}
    >
      {placeholder && <option value="" disabled>{placeholder}</option>}
      {options.map(opt => (
        <option key={opt.value} value={opt.value}>{opt.label}</option>
      ))}
    </select>
  )
}
