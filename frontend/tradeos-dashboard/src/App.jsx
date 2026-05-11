import React, { useState } from 'react'
import { Header } from './components/Header'
import { TabBar } from './components/TabBar'
import { PerformanceTab } from './tabs/PerformanceTab'
import { PositionsTab } from './tabs/PositionsTab'
import { AIIntelTab } from './tabs/AIIntelTab'
import { BrainEngineTab } from './tabs/BrainEngineTab'
import { DataManagementTab } from './tabs/DataManagementTab'

export default function App() {
  const [activeTab, setActiveTab] = useState('performance')

  const renderTab = () => {
    switch (activeTab) {
      case 'performance': return <PerformanceTab />
      case 'positions': return <PositionsTab />
      case 'aiintel': return <AIIntelTab />
      case 'brain': return <BrainEngineTab />
      case 'data': return <DataManagementTab />
      default: return <PerformanceTab />
    }
  }

  return (
    <div className="min-h-screen bg-bg-base text-text-primary">
      <Header regime="RISK ON" pipelineLastRun="2026-05-09 14:02 IST" />
      <TabBar activeTab={activeTab} onTabChange={setActiveTab} />
      <main className="p-6">
        {renderTab()}
      </main>
    </div>
  )
}
