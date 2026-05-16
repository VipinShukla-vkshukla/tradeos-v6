import React from 'react'

export function DataGuard({ data, minRows = 10, coverage, name, children }) {
  if (!data || data.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-40 text-text-muted gap-2">
        <span className="text-2xl">📭</span>
        <p className="text-sm">No {name} data yet.</p>
        <p className="text-xs text-text-dim">Run the pipeline to populate.</p>
      </div>
    )
  }

  if (data.length < minRows) {
    return (
      <div>
        <div className="border border-trade-yellow/30 rounded p-4 text-trade-yellow text-sm mb-4">
          ⚠️ Only {data.length} records (need {minRows} for reliable analysis).
          Results shown but treat with caution.
        </div>
        {children}
      </div>
    )
  }

  if (coverage !== undefined && coverage < 20) {
    return (
      <div className="border border-trade-orange/30 rounded p-4 text-trade-orange text-sm mb-4">
        ⚠️ Only {coverage.toFixed(0)}% of signals have confirmed outcomes.
        Statistics are unreliable at this coverage level.
      </div>
    )
  }

  return children
}
