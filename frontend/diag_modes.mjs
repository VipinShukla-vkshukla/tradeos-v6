// One-off diagnostic: trading modes — read-only.
import { createClient } from '@supabase/supabase-js';
import fs from 'fs';

const env = {};
for (const line of fs.readFileSync('.env.local', 'utf8').split(/\r?\n/)) {
  const m = /^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line);
  if (m) env[m[1]] = m[2].trim().replace(/^["']|["']$/g, '');
}
const sb = createClient(env.NEXT_PUBLIC_SUPABASE_URL, env.SUPABASE_SERVICE_ROLE_KEY);

const keys = ['swing_trading_mode', 'intraday_trading_mode', 'intraday_orders_enabled',
              'intraday_trading_mode_enabled', 'swing_direct_order', 'live_trading_enabled'];
const { data, error } = await sb
  .from('system_config')
  .select('key,value,updated_at')
  .in('key', keys);
if (error) { console.log('query error:', error.message); process.exit(1); }
console.log(JSON.stringify(data, null, 2));

const { count: nAttr } = await sb.from('closed_positions')
  .select('id', { count: 'exact', head: true }).not('signal_date', 'is', null);
console.log('signal-attributed closed trades:', nAttr);
