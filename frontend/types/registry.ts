// TradeOS Dynamic View Registry Types

// Schema types for database tables
export interface ColumnSchema {
  name: string;
  type: string;
  nullable: boolean;
  isPrimary?: boolean;
  isForeign?: boolean;
  foreignTable?: string;
  foreignColumn?: string;
  defaultValue?: string;
}

export interface TableSchema {
  name: string;
  schema: string;
  columns: ColumnSchema[];
  primaryKey?: string[];
  foreignKeys?: Array<{
    column: string;
    referencesTable: string;
    referencesColumn: string;
  }>;
}

// Dynamic View types
export interface DynamicView {
  id: string;
  name: string;
  description?: string;
  sourceMode: DataSourceMode;
  primaryTable: string;
  baseTable?: string; // Alias for primaryTable for backward compatibility
  columns: ViewColumn[];
  joins?: ViewJoin[];
  filters: ViewFilter[];
  sorts?: SortConfig[];
  viewType: ViewType;
  chartConfig?: ChartConfig;
  createdAt: string;
  updatedAt: string;
}

export interface ViewColumn {
  id: string;
  sourceColumn: string;
  sourceTable: string;
  alias?: string;
  visible: boolean;
  type: string;
}

export interface ViewJoin {
  id: string;
  type: 'INNER' | 'LEFT' | 'RIGHT';
  table: string;
  leftColumn: string;
  rightColumn: string;
}

export interface ViewFilter {
  id: string;
  column: string;
  operator: 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'in' | 'between' | 'is_null' | 'not_null';
  value: unknown;
  value2?: unknown;
}

export type ViewType = 'table' | 'line_chart' | 'bar_chart' | 'scatter_plot' | 'area_chart' | 'heatmap' | 'pie_chart' | 'leaderboard' | 'kpi_card';

export type DataSourceMode = 'single_table' | 'join' | 'rpc' | 'api_endpoint';

export interface ColumnDef {
  id: string;
  accessorKey: string;
  header: string;
  type: 'string' | 'number' | 'date' | 'boolean' | 'currency' | 'percent' | 'pnl';
  visible: boolean;
  sortable: boolean;
  filterable: boolean;
  width?: number;
  format?: string;
  computed?: ComputedColumnConfig;
}

export interface ComputedColumnConfig {
  formula: string;
  dependencies: string[];
  resultType: 'number' | 'string' | 'boolean';
}

export interface JoinConfig {
  type: 'INNER' | 'LEFT' | 'RIGHT';
  leftTable: string;
  rightTable: string;
  leftColumn: string;
  rightColumn: string;
}

export interface FilterConfig {
  column: string;
  operator: 'eq' | 'neq' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'in' | 'between' | 'is_null' | 'not_null';
  value: unknown;
  value2?: unknown; // For 'between' operator
}

export interface SortConfig {
  column: string;
  direction: 'asc' | 'desc';
}

export interface ChartConfig {
  type: ViewType;
  xAxis?: string;
  yAxis?: string | string[];
  groupBy?: string;
  aggregation?: 'sum' | 'avg' | 'count' | 'min' | 'max';
  colorBy?: string;
  showLegend?: boolean;
  showGrid?: boolean;
  showTooltip?: boolean;
  stacked?: boolean;
}

export interface RegistryEntry {
  id: string;
  name: string;
  description?: string;
  
  // Data source configuration
  sourceMode: DataSourceMode;
  primaryTable?: string;
  joins?: JoinConfig[];
  rpcName?: string;
  apiEndpoint?: string;
  
  // Column configuration
  columns: ColumnDef[];
  selectedColumns: string[];
  
  // Query configuration
  filters: FilterConfig[];
  sorts: SortConfig[];
  limit?: number;
  
  // Visualization
  viewType: ViewType;
  chartConfig?: ChartConfig;
  
  // Layout
  tabId: string;
  position: { x: number; y: number; w: number; h: number };
  
  // Metadata
  createdAt: string;
  updatedAt: string;
  isCore: boolean; // Core views can't be deleted
  tags?: string[];
}

export interface DynamicQueryParams {
  entry: RegistryEntry;
  additionalFilters?: FilterConfig[];
  additionalSorts?: SortConfig[];
  page?: number;
  pageSize?: number;
}

export interface DynamicQueryResult<T = Record<string, unknown>> {
  data: T[];
  total: number;
  columns: ColumnDef[];
  sql?: string; // Preview mode only
  executionTime?: number;
}

export interface ViewTemplate {
  id: string;
  name: string;
  description: string;
  category: 'performance' | 'positions' | 'signals' | 'brain' | 'ai' | 'custom';
  previewImage?: string;
  config: Partial<RegistryEntry>;
}

// Pre-built view templates
export const VIEW_TEMPLATES: ViewTemplate[] = [
  {
    id: 'signal-performance',
    name: 'Signal Performance by Engine',
    description: 'Compare win rates and P&L across different signal engines',
    category: 'performance',
    config: {
      sourceMode: 'single_table',
      primaryTable: 'engine_stats',
      viewType: 'bar_chart',
      chartConfig: {
        type: 'bar_chart',
        xAxis: 'engine_name',
        yAxis: ['win_rate', 'avg_pnl_percent'],
        showLegend: true,
        showGrid: true,
      },
    },
  },
  {
    id: 'sector-heatmap',
    name: 'Sector Performance Heatmap',
    description: 'Visual heatmap of P&L by sector and time',
    category: 'performance',
    config: {
      sourceMode: 'rpc',
      rpcName: 'get_sector_heatmap',
      viewType: 'heatmap',
    },
  },
  {
    id: 'position-tracker',
    name: 'Open Position Tracker',
    description: 'Real-time tracking of open positions with P&L',
    category: 'positions',
    config: {
      sourceMode: 'api_endpoint',
      apiEndpoint: '/api/positions/open',
      viewType: 'table',
    },
  },
  {
    id: 'conviction-accuracy',
    name: 'Conviction Score Accuracy',
    description: 'Scatter plot of conviction scores vs actual outcomes',
    category: 'ai',
    config: {
      sourceMode: 'api_endpoint',
      apiEndpoint: '/api/performance/score-correlation',
      viewType: 'scatter_plot',
      chartConfig: {
        type: 'scatter_plot',
        xAxis: 'conviction_score',
        yAxis: 'actual_pnl_percent',
        colorBy: 'engine_name',
        showLegend: true,
      },
    },
  },
  {
    id: 'brain-proposals',
    name: 'Brain Engine Proposals',
    description: 'Queue of pending and recent proposals from Brain Engine',
    category: 'brain',
    config: {
      sourceMode: 'api_endpoint',
      apiEndpoint: '/api/brain/proposals',
      viewType: 'table',
      filters: [{ column: 'status', operator: 'in', value: ['PENDING', 'APPROVED'] }],
    },
  },
];

// Tab configuration
export interface TabConfig {
  id: string;
  name: string;
  icon: string;
  isCore: boolean;
  isVisible: boolean;
  order: number;
  views: string[]; // RegistryEntry IDs
}

export const CORE_TABS: TabConfig[] = [
  { id: 'performance', name: 'System Performance', icon: 'TrendingUp', isCore: true, isVisible: true, order: 0, views: [] },
  { id: 'positions', name: 'Positions & P&L', icon: 'Wallet', isCore: true, isVisible: true, order: 1, views: [] },
  { id: 'ai-intelligence', name: 'AI Intelligence', icon: 'Brain', isCore: true, isVisible: true, order: 2, views: [] },
  { id: 'brain-engine', name: 'Brain Engine', icon: 'Cpu', isCore: true, isVisible: true, order: 3, views: [] },
  { id: 'data-management', name: 'Data Management', icon: 'Database', isCore: true, isVisible: true, order: 4, views: [] },
];
