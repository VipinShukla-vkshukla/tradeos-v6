'use client';

import { ReactNode, useState } from 'react';
import { 
  MoreVertical, 
  Maximize2, 
  Minimize2, 
  RefreshCw,
  ChartBar,
  Columns,
  Filter,
  Calculator,
  Link2,
  Move,
  Copy,
  Download,
  Trash2,
  Lightbulb
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { DataSourceBadge } from './DataSourceBadge';

interface PanelProps {
  title: string;
  description?: string;
  children: ReactNode;
  
  // Data source info
  dataSource?: 'supabase' | 'api' | 'rpc' | 'realtime';
  tableName?: string;
  endpoint?: string;
  isLive?: boolean;
  lastUpdated?: string;
  
  // Actions
  onRefresh?: () => void;
  onEnhance?: () => void;
  onExport?: () => void;
  onRemove?: () => void;
  onClone?: () => void;
  onAISuggest?: () => void;
  
  // State
  isLoading?: boolean;
  isExpanded?: boolean;
  onToggleExpand?: () => void;
  
  // Customization
  className?: string;
  headerExtra?: ReactNode;
  showEnhanceMenu?: boolean;
}

export function Panel({
  title,
  description,
  children,
  dataSource,
  tableName,
  endpoint,
  isLive,
  lastUpdated,
  onRefresh,
  onEnhance,
  onExport,
  onRemove,
  onClone,
  onAISuggest,
  isLoading,
  isExpanded,
  onToggleExpand,
  className = '',
  headerExtra,
  showEnhanceMenu = true,
}: PanelProps) {
  const [isHovered, setIsHovered] = useState(false);

  return (
    <div 
      className={`panel flex flex-col ${className}`}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      {/* Panel header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-3 min-w-0">
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-foreground truncate">{title}</h3>
            {description && (
              <p className="text-xs text-muted-foreground truncate">{description}</p>
            )}
          </div>
          {dataSource && (
            <DataSourceBadge
              source={dataSource}
              tableName={tableName}
              endpoint={endpoint}
              isLive={isLive}
              lastUpdated={lastUpdated}
            />
          )}
        </div>

        <div className="flex items-center gap-1 shrink-0">
          {headerExtra}
          
          {/* Refresh button */}
          {onRefresh && (
            <Button
              variant="ghost"
              size="icon"
              className={`h-7 w-7 text-muted-foreground hover:text-foreground transition-opacity ${
                isHovered || isLoading ? 'opacity-100' : 'opacity-0'
              }`}
              onClick={onRefresh}
              disabled={isLoading}
              aria-label="Refresh data"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
            </Button>
          )}

          {/* Expand/Collapse button */}
          {onToggleExpand && (
            <Button
              variant="ghost"
              size="icon"
              className={`h-7 w-7 text-muted-foreground hover:text-foreground transition-opacity ${
                isHovered ? 'opacity-100' : 'opacity-0'
              }`}
              onClick={onToggleExpand}
              aria-label={isExpanded ? 'Collapse panel' : 'Expand panel'}
            >
              {isExpanded ? (
                <Minimize2 className="h-3.5 w-3.5" />
              ) : (
                <Maximize2 className="h-3.5 w-3.5" />
              )}
            </Button>
          )}

          {/* Enhance menu */}
          {showEnhanceMenu && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  className={`h-7 w-7 text-muted-foreground hover:text-foreground transition-opacity ${
                    isHovered ? 'opacity-100' : 'opacity-0'
                  }`}
                  aria-label="Panel options"
                >
                  <MoreVertical className="h-3.5 w-3.5" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-48">
                {onEnhance && (
                  <DropdownMenuItem onClick={onEnhance}>
                    <ChartBar className="h-4 w-4 mr-2" />
                    Change Chart Type
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem disabled>
                  <Columns className="h-4 w-4 mr-2" />
                  Edit Columns
                </DropdownMenuItem>
                <DropdownMenuItem disabled>
                  <Filter className="h-4 w-4 mr-2" />
                  Add Filter
                </DropdownMenuItem>
                <DropdownMenuItem disabled>
                  <Calculator className="h-4 w-4 mr-2" />
                  Add Computed Column
                </DropdownMenuItem>
                <DropdownMenuItem disabled>
                  <Link2 className="h-4 w-4 mr-2" />
                  Link to Panel
                </DropdownMenuItem>
                
                <DropdownMenuSeparator />
                
                <DropdownMenuItem disabled>
                  <Move className="h-4 w-4 mr-2" />
                  Resize Panel
                </DropdownMenuItem>
                {onClone && (
                  <DropdownMenuItem onClick={onClone}>
                    <Copy className="h-4 w-4 mr-2" />
                    Clone Panel
                  </DropdownMenuItem>
                )}
                {onExport && (
                  <DropdownMenuItem onClick={onExport}>
                    <Download className="h-4 w-4 mr-2" />
                    Export Data
                  </DropdownMenuItem>
                )}
                
                <DropdownMenuSeparator />
                
                {onAISuggest && (
                  <DropdownMenuItem onClick={onAISuggest}>
                    <Lightbulb className="h-4 w-4 mr-2" />
                    AI: Suggest Improvements
                  </DropdownMenuItem>
                )}
                
                {onRemove && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem 
                      onClick={onRemove}
                      className="text-destructive focus:text-destructive"
                    >
                      <Trash2 className="h-4 w-4 mr-2" />
                      Remove Panel
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>

      {/* Panel content */}
      <div className="flex-1 p-4 overflow-auto">
        {children}
      </div>
    </div>
  );
}

// Simple KPI card variant
interface KPICardProps {
  title: string;
  value: string | number;
  change?: {
    value: string | number;
    type: 'increase' | 'decrease' | 'neutral';
  };
  icon?: ReactNode;
  description?: string;
  className?: string;
}

export function KPICard({ 
  title, 
  value, 
  change, 
  icon,
  description,
  className = '' 
}: KPICardProps) {
  return (
    <div className={`panel p-4 ${className}`}>
      <div className="flex items-start justify-between mb-2">
        <span className="text-xs text-muted-foreground font-medium uppercase tracking-wide">
          {title}
        </span>
        {icon && (
          <div className="text-muted-foreground">
            {icon}
          </div>
        )}
      </div>
      <div className="text-2xl font-semibold text-foreground mb-1">
        {value}
      </div>
      {change && (
        <div className={`text-xs font-medium ${
          change.type === 'increase' ? 'text-profit' :
          change.type === 'decrease' ? 'text-loss' :
          'text-muted-foreground'
        }`}>
          {change.type === 'increase' && '+'}
          {change.value}
        </div>
      )}
      {description && (
        <div className="text-xs text-muted-foreground mt-1">
          {description}
        </div>
      )}
    </div>
  );
}

export default Panel;
