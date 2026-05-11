import React from 'react'
import { cn } from '../lib/utils'

const tabs = [
  { id: 'performance', label: 'Performance', icon: '📊' },
  { id: 'positions', label: 'Positions', icon: '💼' },
  { id: 'aiintel', label: 'AI Intel', icon: '🧠' },
  { id: 'brain', label: 'Brain Engine', icon: '⚙️' },
  { id: 'data', label: 'Data Management', icon: '🗄️' },
]

export function TabBar({ activeTab, onTabChange }) {
  return (
    <nav className="border-b border-border-custom bg-bg-surface/30">
      <div className="flex px-6">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={cn(
              'relative px-4 py-3 text-sm font-medium transition-colors border-b-2 -mb-px',
              activeTab === tab.id
                ? 'text-trade-blue border-trade-blue'
                : 'text-text-muted border-transparent hover:text-text-primary hover:border-border-custom'
            )}
          >
            <span className="mr-1.5">{tab.icon}</span>
            {tab.label}
          </button>
        ))}
      </div>
    </nav>
  )
}
