// TradeOS Formatting Utilities

import { format, parseISO, formatDistanceToNow } from 'date-fns';

// Currency formatting (Indian Rupee)
export function formatCurrency(
  value: number | null | undefined,
  options?: {
    showSign?: boolean;
    compact?: boolean;
    decimals?: number;
  }
): string {
  if (value === null || value === undefined) return '—';
  
  const { showSign = false, compact = false, decimals = 2 } = options || {};
  
  const absValue = Math.abs(value);
  const sign = value >= 0 ? (showSign ? '+' : '') : '-';
  
  if (compact) {
    if (absValue >= 10000000) {
      return `${sign}₹${(absValue / 10000000).toFixed(2)}Cr`;
    }
    if (absValue >= 100000) {
      return `${sign}₹${(absValue / 100000).toFixed(2)}L`;
    }
    if (absValue >= 1000) {
      return `${sign}₹${(absValue / 1000).toFixed(1)}K`;
    }
  }
  
  const formatted = absValue.toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  
  return `${sign}₹${formatted}`;
}

// Percentage formatting
export function formatPercent(
  value: number | null | undefined,
  options?: {
    showSign?: boolean;
    decimals?: number;
  }
): string {
  if (value === null || value === undefined) return '—';
  
  const { showSign = true, decimals = 1 } = options || {};
  const sign = value >= 0 ? (showSign ? '+' : '') : '';
  
  return `${sign}${value.toFixed(decimals)}%`;
}

// Number formatting
export function formatNumber(
  value: number | null | undefined,
  options?: {
    decimals?: number;
    compact?: boolean;
  }
): string {
  if (value === null || value === undefined) return '—';
  
  const { decimals = 0, compact = false } = options || {};
  
  if (compact) {
    const absValue = Math.abs(value);
    if (absValue >= 1000000) {
      return `${(value / 1000000).toFixed(1)}M`;
    }
    if (absValue >= 1000) {
      return `${(value / 1000).toFixed(1)}K`;
    }
  }
  
  return value.toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

// Date formatting (IST)
export function formatDate(
  value: string | Date | null | undefined,
  formatStr: string = 'dd MMM yyyy'
): string {
  if (!value) return '—';
  
  try {
    const date = typeof value === 'string' ? parseISO(value) : value;
    return format(date, formatStr);
  } catch {
    return '—';
  }
}

// DateTime formatting (IST)
export function formatDateTime(
  value: string | Date | null | undefined,
  formatStr: string = 'dd MMM yyyy HH:mm'
): string {
  return formatDate(value, formatStr);
}

// Time formatting (IST)
export function formatTime(
  value: string | Date | null | undefined,
  formatStr: string = 'HH:mm:ss'
): string {
  return formatDate(value, formatStr);
}

// Relative time (e.g., "2 hours ago")
export function formatRelativeTime(
  value: string | Date | null | undefined
): string {
  if (!value) return '—';
  
  try {
    const date = typeof value === 'string' ? parseISO(value) : value;
    return formatDistanceToNow(date, { addSuffix: true });
  } catch {
    return '—';
  }
}

// P&L formatting with color class
export function formatPnL(
  value: number | null | undefined,
  type: 'currency' | 'percent' = 'currency'
): { text: string; className: string } {
  if (value === null || value === undefined) {
    return { text: '—', className: 'text-muted' };
  }
  
  const text = type === 'currency'
    ? formatCurrency(value, { showSign: true })
    : formatPercent(value);
  
  const className = value > 0
    ? 'text-profit'
    : value < 0
    ? 'text-loss'
    : 'text-muted';
  
  return { text, className };
}

// Win rate formatting
export function formatWinRate(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

// Ratio formatting (e.g., risk/reward)
export function formatRatio(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `1:${value.toFixed(2)}`;
}

// Status badge formatting
export function getStatusBadge(
  status: string,
  type: 'signal' | 'position' | 'proposal' | 'regime' = 'signal'
): { text: string; variant: 'success' | 'warning' | 'danger' | 'info' | 'default' } {
  const statusMap: Record<string, Record<string, { text: string; variant: 'success' | 'warning' | 'danger' | 'info' | 'default' }>> = {
    signal: {
      PENDING: { text: 'Pending', variant: 'warning' },
      EXECUTED: { text: 'Executed', variant: 'success' },
      REJECTED: { text: 'Rejected', variant: 'danger' },
      EXPIRED: { text: 'Expired', variant: 'default' },
    },
    position: {
      OPEN: { text: 'Open', variant: 'info' },
      CLOSED: { text: 'Closed', variant: 'default' },
    },
    proposal: {
      PENDING: { text: 'Pending', variant: 'warning' },
      APPROVED: { text: 'Approved', variant: 'success' },
      REJECTED: { text: 'Rejected', variant: 'danger' },
      APPLIED: { text: 'Applied', variant: 'success' },
      ROLLED_BACK: { text: 'Rolled Back', variant: 'warning' },
    },
    regime: {
      RISK_ON: { text: 'Risk On', variant: 'success' },
      RISK_OFF: { text: 'Risk Off', variant: 'danger' },
      NEUTRAL: { text: 'Neutral', variant: 'default' },
    },
  };
  
  return statusMap[type]?.[status] || { text: status, variant: 'default' };
}

// Conviction score formatting
export function formatConviction(value: number | null | undefined): {
  text: string;
  level: 'high' | 'medium' | 'low';
} {
  if (value === null || value === undefined) {
    return { text: '—', level: 'low' };
  }
  
  const text = `${(value * 100).toFixed(0)}%`;
  const level = value >= 0.8 ? 'high' : value >= 0.6 ? 'medium' : 'low';
  
  return { text, level };
}

// Duration formatting (ms to readable)
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`;
  
  const hours = Math.floor(ms / 3600000);
  const minutes = Math.floor((ms % 3600000) / 60000);
  return `${hours}h ${minutes}m`;
}

// Symbol formatting (ensure uppercase, trim)
export function formatSymbol(symbol: string | null | undefined): string {
  if (!symbol) return '—';
  return symbol.trim().toUpperCase();
}

// Truncate text with ellipsis
export function truncateText(
  text: string | null | undefined,
  maxLength: number
): string {
  if (!text) return '—';
  if (text.length <= maxLength) return text;
  return `${text.slice(0, maxLength - 3)}...`;
}

// Get current IST time
export function getCurrentIST(): Date {
  const now = new Date();
  // IST is UTC+5:30
  const istOffset = 5.5 * 60 * 60 * 1000;
  const utc = now.getTime() + now.getTimezoneOffset() * 60 * 1000;
  return new Date(utc + istOffset);
}

// Format IST datetime
export function formatISTDateTime(formatStr: string = 'dd MMM yyyy, HH:mm IST'): string {
  return format(getCurrentIST(), formatStr);
}
