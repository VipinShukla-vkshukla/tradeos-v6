// TradeOS v6 — Engine Gate Evaluator (client-side mirror)
//
// A faithful TypeScript port of backend/signals/engine_registry.py.
//
// WHY MIRROR IT RATHER THAN CALL THE BACKEND
//   The Control Room's whole point is that adjusting a threshold shows its
//   consequence *immediately* — which stocks enter, which leave, how the funnel
//   shifts. A server round-trip per keystroke would make that feel like a form,
//   not an instrument. Today's universe is ~499 rows x ~25 fields (roughly
//   200KB of JSON), small enough to hold in memory and re-evaluate on every
//   drag at 60fps.
//
// CONTRACT WITH THE BACKEND
//   checkGate() must stay semantically identical to engine_registry.check_gate.
//   If they drift, the preview lies — which is worse than having no preview.
//   The gate spec shapes are deliberately narrow (min/max/eq/abs_max/or_field/
//   any_of/gt_field) precisely so both implementations stay small enough to
//   verify against each other by eye.

export type GateSpec = {
  min?: number;
  max?: number;
  eq?: boolean;
  abs_max?: number;
  or_field?: string;
  any_of?: string[];
  gt_field?: [string, string];
  missing?: 'block' | 'pass';
};

export type Gates = Record<string, GateSpec>;

export type Stock = Record<string, unknown> & {
  symbol: string;
  sector?: string | null;
};

export type Lifecycle = 'ACTIVE' | 'SHADOW' | 'RETIRED';

export interface EngineConfig {
  strategy: string;
  label: string | null;
  description: string | null;
  engine_type: string | null;
  lifecycle: Lifecycle;
  enabled: boolean;
  params: { gates?: Gates; min_score?: number | null; note?: string } & Record<string, unknown>;
}

export interface EnginePerf {
  engine: string;
  hold_days: number;
  n_signals: number;
  hit_rate_pct: number | null;
  avg_return_pct: number | null;
  payoff_ratio: number | null;
}

// ── Field access ────────────────────────────────────────────────────────────
function val(stock: Stock, field: string): number | boolean | null {
  const v = stock[field];
  if (v === null || v === undefined || v === '') return null;
  return v as number | boolean;
}

function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Evaluate one gate against one stock. Mirrors engine_registry.check_gate. */
export function checkGate(
  stock: Stock,
  field: string,
  spec: GateSpec,
  sectorRank?: Record<string, number>
): boolean {
  const missingPass = spec.missing === 'pass';

  // Composite forms
  if (spec.any_of) return spec.any_of.some((f) => Boolean(val(stock, f)));

  if (spec.gt_field) {
    const [a, b] = spec.gt_field;
    const va = num(val(stock, a));
    const vb = num(val(stock, b));
    if (va === null || vb === null) return missingPass;
    return va > vb;
  }

  // sector_rank is derived, not a column on the stock row
  if (field === 'sector_rank') {
    const rank = sectorRank?.[(stock.sector ?? '') as string] ?? 99;
    if (spec.max !== undefined && rank > spec.max) return false;
    return true;
  }

  const raw = val(stock, field);
  if (raw === null) {
    if (spec.or_field && Boolean(val(stock, spec.or_field))) return true;
    return missingPass;
  }

  if (spec.eq !== undefined) return Boolean(raw) === Boolean(spec.eq);

  const n = num(raw);
  if (n === null) return missingPass;

  if (spec.abs_max !== undefined && Math.abs(n) > spec.abs_max) return false;
  if (spec.min !== undefined && n < spec.min) {
    if (spec.or_field && Boolean(val(stock, spec.or_field))) return true;
    return false;
  }
  if (spec.max !== undefined && n > spec.max) {
    if (spec.or_field && Boolean(val(stock, spec.or_field))) return true;
    return false;
  }
  return true;
}

export function passesGates(
  stock: Stock,
  gates: Gates,
  sectorRank?: Record<string, number>
): { passed: boolean; failedGate: string | null } {
  for (const [field, spec] of Object.entries(gates ?? {})) {
    if (!checkGate(stock, field, spec, sectorRank)) return { passed: false, failedGate: field };
  }
  return { passed: true, failedGate: null };
}

export interface FunnelStep {
  gate: string;
  spec: GateSpec;
  before: number;
  after: number;
  removed: number;
}

/** Cumulative survivor count per gate, in declaration order. */
export function gateFunnel(
  stocks: Stock[],
  gates: Gates,
  sectorRank?: Record<string, number>
): { steps: FunnelStep[]; survivors: Stock[] } {
  let remaining = stocks;
  const steps: FunnelStep[] = [];
  for (const [field, spec] of Object.entries(gates ?? {})) {
    const before = remaining.length;
    remaining = remaining.filter((s) => checkGate(s, field, spec, sectorRank));
    steps.push({ gate: field, spec, before, after: remaining.length, removed: before - remaining.length });
  }
  return { steps, survivors: remaining };
}

/**
 * Difference between two gate configurations.
 *
 * This is the number that turns tuning into an experiment: not just "how many
 * pass now" but exactly which symbols a change lets in and which it drops.
 */
export function diffGates(
  stocks: Stock[],
  before: Gates,
  after: Gates,
  sectorRank?: Record<string, number>
): { added: Stock[]; removed: Stock[]; beforeCount: number; afterCount: number } {
  const b = new Set(gateFunnel(stocks, before, sectorRank).survivors.map((s) => s.symbol));
  const aSurv = gateFunnel(stocks, after, sectorRank).survivors;
  const a = new Set(aSurv.map((s) => s.symbol));
  return {
    added: aSurv.filter((s) => !b.has(s.symbol)),
    removed: stocks.filter((s) => b.has(s.symbol) && !a.has(s.symbol)),
    beforeCount: b.size,
    afterCount: a.size,
  };
}

// ── Presentation helpers ────────────────────────────────────────────────────

/** Human-readable gate label. `__di__` / `__wk__` are composite pseudo-fields. */
export function gateLabel(field: string, spec: GateSpec): string {
  if (spec.gt_field) return `${spec.gt_field[0]} > ${spec.gt_field[1]}`;
  if (spec.any_of) return spec.any_of.join(' or ');
  return field;
}

export function gateSummary(field: string, spec: GateSpec): string {
  if (spec.gt_field) return 'required';
  if (spec.any_of) return 'at least one';
  if (spec.eq !== undefined) return spec.eq ? 'must be true' : 'must be false';
  if (spec.abs_max !== undefined) return `|x| ≤ ${spec.abs_max}`;
  const parts: string[] = [];
  if (spec.min !== undefined) parts.push(`≥ ${spec.min}`);
  if (spec.max !== undefined) parts.push(`≤ ${spec.max}`);
  let s = parts.join(' and ') || 'any';
  if (spec.or_field) s += ` (or ${spec.or_field})`;
  return s;
}

/** Which numeric knobs a gate exposes — drives which sliders render. */
export function editableKeys(spec: GateSpec): Array<'min' | 'max' | 'abs_max'> {
  const keys: Array<'min' | 'max' | 'abs_max'> = [];
  if (spec.min !== undefined) keys.push('min');
  if (spec.max !== undefined) keys.push('max');
  if (spec.abs_max !== undefined) keys.push('abs_max');
  return keys;
}

/**
 * Sensible slider bounds per field, so a threshold cannot be dragged somewhere
 * meaningless (a negative RSI, a 400% delivery).
 */
export function sliderRange(field: string, key: string, current: number): { min: number; max: number; step: number } {
  const F: Record<string, { min: number; max: number; step: number }> = {
    rsi_daily: { min: 0, max: 100, step: 1 },
    rsi_weekly: { min: 0, max: 100, step: 1 },
    rsi_monthly: { min: 0, max: 100, step: 1 },
    adx: { min: 0, max: 60, step: 1 },
    vol_ratio: { min: 0, max: 6, step: 0.1 },
    delivery_pct: { min: 0, max: 100, step: 1 },
    atr_pct: { min: 0, max: 15, step: 0.25 },
    consol_range: { min: 0, max: 40, step: 1 },
    pct_change: { min: -15, max: 20, step: 0.5 },
    market_cap: { min: 0, max: 5000, step: 100 },
    sector_rank: { min: 1, max: 23, step: 1 },
    rs_vs_nifty: { min: -30, max: 60, step: 1 },
    dist_sma50: { min: -30, max: 30, step: 0.5 },
    ret_6m: { min: -50, max: 150, step: 5 },
    macd_hist: { min: -5, max: 5, step: 0.05 },
  };
  const r = F[field];
  if (r) return r;
  const span = Math.max(Math.abs(current) * 2, 10);
  return { min: 0, max: Math.ceil(span), step: span > 50 ? 1 : 0.1 };
}

export const LIFECYCLE_META: Record<Lifecycle, { label: string; hint: string; tone: string }> = {
  ACTIVE: {
    label: 'Active',
    hint: 'Contributes to composite_score and can produce live candidates.',
    tone: 'emerald',
  },
  SHADOW: {
    label: 'Shadow',
    hint: 'Runs and logs candidates for measurement, but is excluded from composite_score. Use this to trial an engine with zero effect on live decisions.',
    tone: 'amber',
  },
  RETIRED: {
    label: 'Retired',
    hint: 'Does not run at all.',
    tone: 'zinc',
  },
};
