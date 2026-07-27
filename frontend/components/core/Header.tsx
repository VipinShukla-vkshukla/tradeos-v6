'use client';
import { useState, useEffect } from 'react';
import { 
  Settings, 
  Bell, 
  LayoutGrid, 
  TrendingUp, 
  TrendingDown,
  Minus,
  Clock,
  Activity,
  Cpu
} from 'lucide-react';
import type { RegimeState } from '@/types/database';
import { SystemNav } from '@/components/core/SystemNav';
import { Button } from '@/components/ui/button';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { formatISTDateTime } from '@/lib/formatters';

interface HeaderProps {
  marketRegime?: RegimeState;         // 'TRENDING' | 'RISK ON' | 'NEUTRAL' | 'RECOVERING' | 'RISK OFF'
  pipelineLastRun?: string;
  aiProviderStatus?: 'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED';
  onSettingsClick?: () => void;
  onNotificationsClick?: () => void;
  onLayoutClick?: () => void;
  notificationCount?: number;
}

function RegimeBadge({ regime }: { regime: RegimeState }) {
  const config: Record<
    RegimeState,
    { label: string; icon: typeof TrendingUp; className: string }
  > = {
    'TRENDING':   { label: 'TRENDING',   icon: TrendingUp,   className: 'badge-success' },
    'RISK ON':    { label: 'RISK ON',    icon: TrendingUp,   className: 'badge-success' },
    'NEUTRAL':    { label: 'NEUTRAL',    icon: Minus,        className: 'badge-warning' },
    'RECOVERING': { label: 'RECOVERING', icon: Minus,        className: 'badge-warning' },
    'RISK OFF':   { label: 'RISK OFF',   icon: TrendingDown, className: 'badge-destructive' },
  };

  const { label, icon: Icon, className } = config[regime] ?? config['NEUTRAL'];

  return (
    <div className={`flex items-center gap-1 text-xs font-medium px-2 py-1 rounded ${className}`}>
      <Icon className="w-3 h-3" />
      <span>{label}</span>
    </div>
  );
}

function AIProviderBadge({ status }: { status: 'AVAILABLE' | 'DEGRADED' | 'UNAVAILABLE' | 'DISABLED' }) {
  const config = {
    AVAILABLE: {
      label: 'AI Ready',
      className: 'badge-success',
      dot: 'bg-success',
    },
    DEGRADED: {
      label: 'AI Degraded',
      className: 'badge-warning',
      dot: 'bg-warning',
    },
    UNAVAILABLE: {
      label: 'AI Offline',
      className: 'badge-danger',
      dot: 'bg-danger',
    },
    DISABLED: {
      label: 'AI Disabled',
      className: 'bg-muted text-muted-foreground',
      dot: 'bg-muted-foreground',
    },
  };

  const { label, className, dot } = config[status];

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div className={`flex items-center gap-1.5 px-2 py-1 rounded-md text-xs ${className}`}>
            <Cpu className="h-3 w-3" />
            <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
          </div>
        </TooltipTrigger>
        <TooltipContent>
          <p>{label}</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function PipelineIndicator({ lastRun }: { lastRun?: string }) {
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <div className="flex items-center gap-1.5 px-2 py-1 rounded-md text-xs bg-muted text-muted-foreground">
            <Activity className="h-3 w-3" />
            <span className="h-1.5 w-1.5 rounded-full bg-success animate-pulse" />
          </div>
        </TooltipTrigger>
        <TooltipContent>
          <p>Pipeline last run: {lastRun || 'Unknown'}</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function Header({
  marketRegime = 'NEUTRAL',
  pipelineLastRun,
  aiProviderStatus = 'AVAILABLE',
  onSettingsClick,
  onNotificationsClick,
  onLayoutClick,
  notificationCount = 0,
}: HeaderProps) {
  const [currentTime, setCurrentTime] = useState<string>('');

  useEffect(() => {
    // Set initial time
    setCurrentTime(formatISTDateTime('dd MMM yyyy, HH:mm'));
    
    // Update every minute
    const interval = setInterval(() => {
      setCurrentTime(formatISTDateTime('dd MMM yyyy, HH:mm'));
    }, 60000);

    return () => clearInterval(interval);
  }, []);

  return (
    <header className="h-14 bg-header border-b border-border px-4 flex items-center justify-between">
      {/* Left side - Title and status badges */}
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-semibold tracking-tight text-foreground">
          TRADEOS INTELLIGENCE CONSOLE
        </h1>
        
        <div className="flex items-center gap-2">
          <RegimeBadge regime={marketRegime} />
          <PipelineIndicator lastRun={pipelineLastRun} />
          <AIProviderBadge status={aiProviderStatus} />
          {/* Preflight verdict + Control Room. Header rather than a tab so it
              is visible regardless of persisted layout state — see SystemNav. */}
          <SystemNav />
        </div>
      </div>

      {/* Right side - Time and action buttons */}
      <div className="flex items-center gap-3">
        {/* Current time */}
        <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
          <Clock className="h-4 w-4" />
          <span className="font-mono">{currentTime} IST</span>
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-1">
          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-foreground"
                  onClick={onLayoutClick}
                  aria-label="Layout manager"
                >
                  <LayoutGrid className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                <p>Manage Layouts</p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>

          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-foreground relative"
                  onClick={onNotificationsClick}
                  aria-label={`Notifications${notificationCount > 0 ? ` (${notificationCount} unread)` : ''}`}
                >
                  <Bell className="h-4 w-4" />
                  {notificationCount > 0 && (
                    <span className="absolute -top-0.5 -right-0.5 h-4 w-4 rounded-full bg-danger text-[10px] font-medium flex items-center justify-center text-white">
                      {notificationCount > 9 ? '9+' : notificationCount}
                    </span>
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                <p>Notifications</p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>

          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-foreground"
                  onClick={onSettingsClick}
                  aria-label="Settings"
                >
                  <Settings className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                <p>Settings & Theme</p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>
    </header>
  );
}

export default Header;
