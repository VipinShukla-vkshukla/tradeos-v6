// lib/api.ts — mutation helpers.
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
