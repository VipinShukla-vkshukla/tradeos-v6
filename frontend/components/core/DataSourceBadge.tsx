'use client';

import { Database, Zap, Cpu } from 'lucide-react';
import {
  Tooltip, TooltipContent, TooltipProvider, TooltipTrigger,
} from '@/components/ui/tooltip';

// 'api' source type removed — everything is Supabase or realtime.
// Globe / FastAPI label eliminated entirely.
type DataSource = 'supabase' | 'realtime' | 'rpc';

interface DataSourceBadgeProps {
  source: DataSource;
  tableName?: string;
  endpoint?: string;       // kept for backward compat but unused in UI
  lastUpdated?: string;
  isLive?: boolean;
  className?: string;
}

const sourceConfig: Record<DataSource, {
  icon: typeof Database;
  label: string;
  color: string;
  bgColor: string;
}> = {
  supabase: {
    icon: Database,
    label: 'Supabase',
    color: 'text-emerald-400',
    bgColor: 'bg-emerald-400/10',
  },
  realtime: {
    icon: Zap,
    label: 'Realtime',
    color: 'text-amber-400',
    bgColor: 'bg-amber-400/10',
  },
  rpc: {
    icon: Cpu,
    label: 'RPC',
    color: 'text-violet-400',
    bgColor: 'bg-violet-400/10',
  },
};

export function DataSourceBadge({
  source,
  tableName,
  lastUpdated,
  isLive,
  className = '',
}: DataSourceBadgeProps) {
  // Treat any legacy 'api' source as 'supabase'
  const resolvedSource: DataSource =
    ['supabase', 'realtime', 'rpc'].includes(source) ? source : 'supabase';

  const config = sourceConfig[resolvedSource];
  const Icon = config.icon;

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div
            className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs ${config.bgColor} ${config.color} ${className}`}
          >
            <Icon className="h-3 w-3" />
            {isLive && (
              <span className="relative flex h-1.5 w-1.5">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-current opacity-75" />
                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-current" />
              </span>
            )}
          </div>
        </TooltipTrigger>
        <TooltipContent side="bottom" className="max-w-xs">
          <div className="space-y-1 text-xs">
            <div className="font-medium">{config.label}</div>
            {tableName && (
              <div className="text-muted-foreground">
                Table: <span className="font-mono text-foreground">{tableName}</span>
              </div>
            )}
            {lastUpdated && (
              <div className="text-muted-foreground">Last updated: {lastUpdated}</div>
            )}
            {isLive && (
              <div className="flex items-center gap-1 text-emerald-400">
                <span className="relative flex h-2 w-2">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                  <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-400" />
                </span>
                Live subscription active
              </div>
            )}
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

// Development warning banner (shown when Supabase not configured)
export function DevelopmentBanner({ message }: { message: string }) {
  return (
    <div className="bg-warning-subtle border border-warning/20 rounded-md px-4 py-2 mb-4">
      <div className="flex items-center gap-2">
        <Database className="h-4 w-4 text-warning" />
        <span className="text-sm text-warning">{message}</span>
      </div>
    </div>
  );
}

export default DataSourceBadge;
