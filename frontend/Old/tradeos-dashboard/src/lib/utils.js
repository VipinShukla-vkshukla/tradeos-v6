import { clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs) {
  return twMerge(clsx(inputs))
}

export function formatCurrency(value) {
  if (value === null || value === undefined) return '—'
  return '₹' + Number(value).toLocaleString('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 2 })
}

export function formatPercent(value, decimals = 1) {
  if (value === null || value === undefined) return '—'
  return Number(value).toFixed(decimals) + '%'
}

export function formatDelta(value) {
  if (value === null || value === undefined) return '—'
  const sign = value >= 0 ? '+' : ''
  return `${sign}${value.toFixed(1)}pp`
}

export function formatDate(dateStr) {
  if (!dateStr) return '—'
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' })
}

export function formatDateTime(dateStr) {
  if (!dateStr) return '—'
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }) + ' ' +
    d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

export function getRegimeBadge(regime) {
  const map = {
    'RISK ON': 'badge-green',
    'NEUTRAL': 'badge-yellow',
    'RECOVERING': 'badge-blue',
    'RISK OFF': 'badge-red',
  }
  return map[regime] || 'badge-grey'
}

export function getSignalTypeBadge(type) {
  const map = {
    'PRIME': 'badge-blue',
    'BREAKOUT': 'badge-green',
    'STAGED': 'badge-yellow',
    'REENTRY': 'badge-purple',
  }
  return map[type] || 'badge-grey'
}
