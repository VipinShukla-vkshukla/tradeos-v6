// One-off diagnostic for the two health items.
import { createClient } from '@supabase/supabase-js';
import fs from 'fs';

const env = {};
for (const line of fs.readFileSync('.env.local', 'utf8').split(/\r?\n/)) {
  const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
  if (m) env[m[1]] = m[2].trim().replace(/^["']|["']$/g, '');
}
const sb = createClient(env.NEXT_PUBLIC_SUPABASE_URL, env.SUPABASE_SERVICE_ROLE_KEY);

const { data: snap } = await sb.from('system_config').select('key,value,updated_at')
  .eq('key', 'capital_snapshot');
if (snap?.length) {
  const s = JSON.parse(snap[0].value);
  console.log('capital_snapshot updated_at:', snap[0].updated_at);
  console.log('configured:', s.configured, '| broker_total:', s.broker_total,
    '| broker_cash:', s.broker_cash, '| broker_invested:', s.broker_invested);
  console.log('gap:', s.gap, '| gap_pct:', s.gap_pct, '| severity:', s.severity, '| fallback:', s.is_fallback);
  console.log('age_minutes:', Math.round((Date.now() - new Date(snap[0].updated_at).getTime()) / 60000));
} else {
  console.log('capital_snapshot: NOT PRESENT');
}

const { count: nClosed } = await sb.from('closed_positions')
  .select('id', { count: 'exact', head: true });
const { data: attrRows } = await sb.from('closed_positions')
  .select('id').not('signal_date', 'is', null);
console.log('closed_positions total:', nClosed, '| attributed (signal_date set):', attrRows?.length);

const { data: mlRow } = await sb.from('ml_training_log')
  .select('trained_at,model_type,trades_used,accuracy,notes')
  .order('trained_at', { ascending: false }).limit(1);
console.log('last ml_training_log:', JSON.stringify(mlRow));
