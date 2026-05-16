// lib/api.ts — TradeOS v2
// Mutation helpers and view management API client.
// All reads go direct Supabase (lib/supabase.ts).
// Mutations go through Next.js API routes (server-side service role key).

const API_BASE = '/api';

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code?: string
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(
  endpoint: string,
  options: RequestInit & { params?: Record<string, string | number | boolean | undefined> } = {}
): Promise<T> {
  const { params, ...fetchOptions } = options;

  let url = `${API_BASE}${endpoint}`;
  if (params) {
    const qs = new URLSearchParams(
      Object.entries(params)
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => [k, String(v)])
    ).toString();
    if (qs) url += `?${qs}`;
  }

  const res = await fetch(url, {
    ...fetchOptions,
    headers: { 'Content-Type': 'application/json', ...fetchOptions.headers },
  });

  if (!res.ok) {
    let body: { message?: string; code?: string } = {};
    try { body = await res.json(); } catch { /* ignore */ }
    throw new ApiError(body.message || res.statusText, res.status, body.code);
  }

  const text = await res.text();
  return text ? JSON.parse(text) : ({} as T);
}

// ---------------------------------------------------------------------------
// brain_proposals mutations
// ---------------------------------------------------------------------------
export const brainApi = {
  approve: (id: number, params?: { reviewed_by?: string }) =>
    request<{ success: boolean }>(`/brain/proposals/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'APPROVED', ...params }),
    }),
  reject: (id: number, params?: { reviewed_by?: string; reason?: string }) =>
    request<{ success: boolean }>(`/brain/proposals/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'REJECTED', ...params }),
    }),
  rollback: (id: number) =>
    request<{ success: boolean }>(`/brain/proposals/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status: 'ROLLED_BACK' }),
    }),
};

// ---------------------------------------------------------------------------
// system_config mutations
// ---------------------------------------------------------------------------
export const configApi = {
  update: (key: string, value: string, reason?: string) =>
    request<{ success: boolean }>(`/config/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      body: JSON.stringify({ value, reason }),
    }),
};

// ---------------------------------------------------------------------------
// Custom views — persisted in Supabase custom_views table
// ---------------------------------------------------------------------------
export interface DynamicViewPayload {
  id?: string;
  name: string;
  description?: string;
  baseTable: string;
  columns: unknown[];
  joins?: unknown[];
  filters?: unknown[];
  sorts?: unknown[];
  viewType?: string;
  chartConfig?: unknown;
  tabId?: string;
}

export const viewsApi = {
  list: () => request<DynamicViewPayload[]>('/views'),
  get: (id: string) => request<DynamicViewPayload>(`/views/${id}`),
  create: (view: DynamicViewPayload) =>
    request<DynamicViewPayload>('/views', {
      method: 'POST',
      body: JSON.stringify(view),
    }),
  update: (id: string, updates: Partial<DynamicViewPayload>) =>
    request<DynamicViewPayload>(`/views/${id}`, {
      method: 'PUT',
      body: JSON.stringify(updates),
    }),
  delete: (id: string) =>
    request<{ success: boolean }>(`/views/${id}`, { method: 'DELETE' }),
};

// ---------------------------------------------------------------------------
// AI view suggestion — reads system_config for provider, calls AI API
// ---------------------------------------------------------------------------
export interface AISuggestRequest {
  context: string;
  baseTable: string;
  availableTables: string[];
  currentColumns?: string[];
}

export interface AISuggestResponse {
  suggestedColumns: string[];
  suggestedJoins: Array<{ table: string; on: string; type: string }>;
  suggestedFilters: Array<{ column: string; operator: string; value: unknown }>;
  explanation: string;
}

export const aiApi = {
  suggest: (req: AISuggestRequest) =>
    request<AISuggestResponse>('/ai/suggest', {
      method: 'POST',
      body: JSON.stringify(req),
    }),
};

// ---------------------------------------------------------------------------
// Schema introspection — used by ViewWizard
// ---------------------------------------------------------------------------
export interface ColumnMeta {
  name: string;
  type: string;
  nullable: boolean;
}

export interface TableMeta {
  name: string;
  schema: string;
  columns: ColumnMeta[];
}

export const schemaApi = {
  getTables: () => request<TableMeta[]>('/schema'),
};

// ---------------------------------------------------------------------------
// SWR fetcher
// ---------------------------------------------------------------------------
export const swrFetcher = async <T>(url: string): Promise<T> => {
  const res = await fetch(url);
  if (!res.ok) throw new ApiError(res.statusText, res.status);
  return res.json();
};

// ---------------------------------------------------------------------------
// Compatibility shim — ChartBuilder.tsx imports this
// ---------------------------------------------------------------------------
export const extendedApi = {
  getSchema: () => schemaApi.getTables(),
  getAISuggestions: (req: AISuggestRequest) => aiApi.suggest(req),
  queryTable: async () => [],
  generateChart: async () => null,
};
