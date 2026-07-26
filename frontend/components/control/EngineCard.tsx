'use client';

// TradeOS v6 — Engine Card
//
// One engine, one card. Everything needed to decide its fate is on the card:
//   what it is, what it produces TODAY, what it has historically RETURNED,
//   the gates doing the filtering, and the controls to change them.
//
// The design principle throughout: never show a control without showing its
// consequence. A threshold slider next to a live funnel means the operator can
// see that `rsi_daily >= 55` is removing 255 of 329 stocks BEFORE committing —
// which is how the SBS gate tension (vol_ratio wants expansion, consol_range
// wants tightness) becomes obvious rather than requiring a code audit.

import { useMemo, useState } from 'react';
import {
  gateFunnel, diffGates, gateLabel, gateSummary, editableKeys, sliderRange,
  LIFECYCLE_META,
  type EngineConfig, type EnginePerf, type Gates, type Lifecycle, type Stock,
} from '@/lib/engineGates';

interface Props {
  engine: EngineConfig;
  perf: EnginePerf[];
  /**
   * The universe this engine actually screens. For most engines that is the
   * full stock list, but TPO runs over the CTL candidate set — feeding it the
   * full universe made its card read 75 candidates when the real engine
   * produces 9. The preview has to screen the same population the engine does
   * or the numbers are fiction.
   */
  stocks: Stock[];
  /** Set when `stocks` is narrower than the full universe, for the caption. */
  upstream?: { engine: string; size: number } | null;
  sectorRank: Record<string, number>;
  onSave: (engine: string, patch: { lifecycle?: Lifecycle; gates?: Gates; reason: string; evidence: unknown }) => Promise<void>;
}

const TONE: Record<string, string> = {
  emerald: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  amber: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  zinc: 'bg-zinc-500/15 text-zinc-400 border-zinc-500/30',
};

export function EngineCard({ engine, perf, stocks, upstream, sectorRank, onSave }: Props) {
  const baseGates = (engine.params?.gates ?? {}) as Gates;

  const [draft, setDraft] = useState<Gates>(() => structuredClone(baseGates));
  const [lifecycle, setLifecycle] = useState<Lifecycle>(engine.lifecycle);
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [reason, setReason] = useState('');

  const dirty =
    JSON.stringify(draft) !== JSON.stringify(baseGates) || lifecycle !== engine.lifecycle;

  // Recomputed on every keystroke — this is the whole point of holding the
  // universe client-side.
  const live = useMemo(() => gateFunnel(stocks, draft, sectorRank), [stocks, draft, sectorRank]);
  const delta = useMemo(
    () => diffGates(stocks, baseGates, draft, sectorRank),
    [stocks, baseGates, draft, sectorRank]
  );

  const perf15 = perf.find((p) => p.hold_days === 15) ?? perf[0];

  function setGateValue(field: string, key: 'min' | 'max' | 'abs_max', value: number) {
    setDraft((d) => ({ ...d, [field]: { ...d[field], [key]: value } }));
  }

  async function handleSave() {
    if (!dirty) return;
    setSaving(true);
    try {
      await onSave(engine.strategy, {
        lifecycle: lifecycle !== engine.lifecycle ? lifecycle : undefined,
        gates: JSON.stringify(draft) !== JSON.stringify(baseGates) ? draft : undefined,
        reason: reason || 'adjusted from Control Room',
        // Record what the operator was looking at, not just what changed.
        evidence: {
          candidates_before: delta.beforeCount,
          candidates_after: delta.afterCount,
          added: delta.added.slice(0, 20).map((s) => s.symbol),
          removed: delta.removed.slice(0, 20).map((s) => s.symbol),
          hit_rate_pct: perf15?.hit_rate_pct ?? null,
          n_signals: perf15?.n_signals ?? null,
        },
      });
      setReason('');
    } finally {
      setSaving(false);
    }
  }

  function reset() {
    setDraft(structuredClone(baseGates));
    setLifecycle(engine.lifecycle);
    setReason('');
  }

  const meta = LIFECYCLE_META[lifecycle];
  const dimmed = lifecycle === 'RETIRED';

  return (
    <div
      className={`rounded-xl border bg-zinc-900/60 backdrop-blur transition-all ${
        dirty ? 'border-sky-500/50 shadow-lg shadow-sky-500/10' : 'border-zinc-800'
      } ${dimmed ? 'opacity-55' : ''}`}
    >
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3 p-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm font-bold tracking-wider text-zinc-100">
              {engine.strategy}
            </span>
            <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${TONE[meta.tone]}`}>
              {meta.label}
            </span>
            {engine.engine_type === 'SCANNER' && (
              <span className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-500">
                scanner
              </span>
            )}
          </div>
          <div className="mt-0.5 truncate text-xs text-zinc-400">{engine.label}</div>
          {/* Some engines apply extra logic in Python that is not expressible
              as a declarative gate (TPO's regime-dependent RSI window, MOM's
              RISK OFF skip). Saying so on the card prevents the funnel count
              from being read as the engine's final output. */}
          {typeof engine.params?.note === 'string' && (
            <div className="mt-1 text-[10px] leading-snug text-amber-400/70">
              ⓘ {engine.params.note}
            </div>
          )}
        </div>

        <div className="shrink-0 text-right">
          <div className="font-mono text-2xl font-semibold leading-none text-zinc-100">
            {live.survivors.length}
          </div>
          <div className="text-[10px] uppercase tracking-wide text-zinc-500">
            {upstream ? `of ${upstream.size} from ${upstream.engine}` : 'passes gates'}
          </div>
          {delta.afterCount !== delta.beforeCount && (
            <div
              className={`mt-1 font-mono text-xs ${
                delta.afterCount > delta.beforeCount ? 'text-emerald-400' : 'text-rose-400'
              }`}
            >
              {delta.afterCount > delta.beforeCount ? '+' : ''}
              {delta.afterCount - delta.beforeCount} vs saved
            </div>
          )}
        </div>
      </div>

      {/* ── Measured performance ───────────────────────────────────────── */}
      <div className="grid grid-cols-4 gap-px border-y border-zinc-800 bg-zinc-800/50 text-center">
        {[
          { label: 'hit rate', value: perf15?.hit_rate_pct != null ? `${perf15.hit_rate_pct}%` : '—' },
          { label: 'avg ret', value: perf15?.avg_return_pct != null ? `${perf15.avg_return_pct}%` : '—' },
          { label: 'payoff', value: perf15?.payoff_ratio != null ? `${perf15.payoff_ratio}` : '—' },
          { label: 'samples', value: perf15?.n_signals ?? '—' },
        ].map((m) => (
          <div key={m.label} className="bg-zinc-900/80 px-2 py-2">
            <div className="font-mono text-sm text-zinc-200">{m.value}</div>
            <div className="text-[10px] uppercase tracking-wide text-zinc-500">{m.label}</div>
          </div>
        ))}
      </div>

      {/* ── Funnel ─────────────────────────────────────────────────────── */}
      <div className="p-4">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[11px] uppercase tracking-wide text-zinc-500">gate funnel</span>
          <button
            onClick={() => setExpanded((v) => !v)}
            className="text-[11px] text-sky-400 hover:text-sky-300"
          >
            {expanded ? 'hide controls' : 'tune gates'}
          </button>
        </div>

        <div className="space-y-1">
          {live.steps.map((s) => {
            const pct = s.before ? (s.after / s.before) * 100 : 0;
            const heavy = s.removed > s.before * 0.5;
            return (
              <div key={s.gate} className="group flex items-center gap-2">
                <div className="w-32 shrink-0 truncate font-mono text-[11px] text-zinc-400">
                  {gateLabel(s.gate, s.spec)}
                </div>
                <div className="relative h-4 flex-1 overflow-hidden rounded bg-zinc-800">
                  <div
                    className={`h-full transition-all duration-200 ${
                      heavy ? 'bg-rose-500/60' : 'bg-sky-500/50'
                    }`}
                    style={{ width: `${Math.max(pct, 1.5)}%` }}
                  />
                  <span className="absolute inset-y-0 right-1.5 flex items-center font-mono text-[10px] text-zinc-300">
                    {s.after}
                  </span>
                </div>
                <div className="w-14 shrink-0 text-right font-mono text-[10px] text-zinc-500">
                  −{s.removed}
                </div>
              </div>
            );
          })}
        </div>

        {/* ── Editable gates ───────────────────────────────────────────── */}
        {expanded && (
          <div className="mt-4 space-y-3 rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
            {Object.entries(draft).map(([field, spec]) => {
              const keys = editableKeys(spec);
              if (!keys.length) {
                return (
                  <div key={field} className="flex items-center justify-between text-[11px]">
                    <span className="font-mono text-zinc-400">{gateLabel(field, spec)}</span>
                    <span className="text-zinc-600">{gateSummary(field, spec)}</span>
                  </div>
                );
              }
              return (
                <div key={field} className="space-y-1">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-[11px] text-zinc-300">{field}</span>
                    <span className="text-[10px] text-zinc-600">{gateSummary(field, spec)}</span>
                  </div>
                  {keys.map((k) => {
                    const cur = Number(spec[k]);
                    const r = sliderRange(field, k, cur);
                    const orig = Number((baseGates[field] as never)?.[k]);
                    const changed = cur !== orig;
                    return (
                      <div key={k} className="flex items-center gap-2">
                        <span className="w-12 shrink-0 text-[10px] uppercase text-zinc-500">{k}</span>
                        <input
                          type="range"
                          min={r.min}
                          max={r.max}
                          step={r.step}
                          value={cur}
                          onChange={(e) => setGateValue(field, k, Number(e.target.value))}
                          className="h-1 flex-1 cursor-pointer appearance-none rounded bg-zinc-700 accent-sky-500"
                        />
                        <span
                          className={`w-14 shrink-0 text-right font-mono text-[11px] ${
                            changed ? 'text-sky-400' : 'text-zinc-400'
                          }`}
                        >
                          {cur}
                        </span>
                      </div>
                    );
                  })}
                </div>
              );
            })}

            {/* Lifecycle */}
            <div className="border-t border-zinc-800 pt-3">
              <div className="mb-1.5 text-[10px] uppercase tracking-wide text-zinc-500">lifecycle</div>
              <div className="flex gap-1">
                {(['ACTIVE', 'SHADOW', 'RETIRED'] as Lifecycle[]).map((lc) => (
                  <button
                    key={lc}
                    onClick={() => setLifecycle(lc)}
                    title={LIFECYCLE_META[lc].hint}
                    className={`flex-1 rounded border px-2 py-1 text-[11px] transition ${
                      lifecycle === lc
                        ? TONE[LIFECYCLE_META[lc].tone]
                        : 'border-zinc-800 text-zinc-500 hover:border-zinc-700'
                    }`}
                  >
                    {LIFECYCLE_META[lc].label}
                  </button>
                ))}
              </div>
              <p className="mt-1.5 text-[10px] leading-relaxed text-zinc-600">{meta.hint}</p>
            </div>
          </div>
        )}

        {/* ── What-if impact ───────────────────────────────────────────── */}
        {dirty && (delta.added.length > 0 || delta.removed.length > 0) && (
          <div className="mt-3 rounded-lg border border-sky-500/30 bg-sky-500/5 p-3">
            <div className="mb-1.5 text-[10px] uppercase tracking-wide text-sky-400">
              impact on today&apos;s universe
            </div>
            {delta.added.length > 0 && (
              <div className="mb-1 text-[11px]">
                <span className="text-emerald-400">+{delta.added.length} in </span>
                <span className="font-mono text-zinc-400">
                  {delta.added.slice(0, 8).map((s) => s.symbol).join(', ')}
                  {delta.added.length > 8 && ` +${delta.added.length - 8}`}
                </span>
              </div>
            )}
            {delta.removed.length > 0 && (
              <div className="text-[11px]">
                <span className="text-rose-400">−{delta.removed.length} out </span>
                <span className="font-mono text-zinc-400">
                  {delta.removed.slice(0, 8).map((s) => s.symbol).join(', ')}
                  {delta.removed.length > 8 && ` +${delta.removed.length - 8}`}
                </span>
              </div>
            )}
          </div>
        )}

        {/* ── Commit ───────────────────────────────────────────────────── */}
        {dirty && (
          <div className="mt-3 flex items-center gap-2">
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="why? (recorded in the audit log)"
              className="flex-1 rounded border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-200 placeholder:text-zinc-600 focus:border-sky-500/50 focus:outline-none"
            />
            <button
              onClick={reset}
              className="rounded border border-zinc-800 px-3 py-1.5 text-[11px] text-zinc-400 hover:text-zinc-200"
            >
              Reset
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="rounded bg-sky-600 px-3 py-1.5 text-[11px] font-medium text-white hover:bg-sky-500 disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Apply'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
