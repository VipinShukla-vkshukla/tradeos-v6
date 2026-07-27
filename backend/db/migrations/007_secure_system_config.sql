-- ═══════════════════════════════════════════════════════════════════════════
-- TradeOS v6 — Migration 007: stop the browser key from reading secrets
-- ═══════════════════════════════════════════════════════════════════════════
--
-- THE PROBLEM
--   system_config is readable by the `anon` role. That key is compiled into
--   the dashboard's JavaScript bundle (NEXT_PUBLIC_SUPABASE_ANON_KEY), so it
--   is readable by anyone who loads the page or opens devtools. Verified by
--   querying the live project with the anon key: it returned
--       kite_access_token      -- with api_key, this can PLACE ORDERS
--       email_password         -- Gmail app password, plaintext
--       discord_webhook_url    -- anyone can post to the channel
--
--   The Kite token is the serious one. api_key is not a secret (it is in every
--   login URL), so access_token is the only thing standing between a reader of
--   that bundle and the ability to transact on the account.
--
-- THE FIX
--   Mark rows as secret and let RLS hide them from anon/authenticated.
--   service_role BYPASSES RLS entirely, so every backend script keeps working
--   with no code change. The dashboard keeps reading ordinary config; it only
--   loses access to the rows it never legitimately needed.
--
-- ⚠️  ROTATE AFTER APPLYING. These values were readable, so treat them as
--     disclosed:
--       1. Zerodha  — regenerate the API secret in the Kite developer console
--       2. Gmail    — revoke the app password at myaccount.google.com/apppasswords
--       3. Discord  — delete and recreate the channel webhook
--     The Kite ACCESS token rotates itself every morning, so that one expires
--     naturally, but the API SECRET does not.
-- ═══════════════════════════════════════════════════════════════════════════

ALTER TABLE public.system_config
    ADD COLUMN IF NOT EXISTS is_secret boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.system_config.is_secret IS
  'When true the row is hidden from the anon/authenticated roles by RLS. '
  'service_role bypasses RLS, so backend scripts are unaffected. Any new '
  'credential written here MUST set this.';

-- Mark everything credential-shaped. Pattern-based so a key added later that
-- follows the same naming is caught without another migration.
UPDATE public.system_config
   SET is_secret = true
 WHERE key IN (
        'kite_access_token', 'kite_access_token_date',
        'email_password', 'discord_webhook_url',
        'telegram_bot_token', 'telegram_chat_id'
       )
    OR key ~* '(password|secret|token|webhook|api_key|credential|private)';

-- ── RLS ────────────────────────────────────────────────────────────────────
ALTER TABLE public.system_config ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS system_config_anon_read      ON public.system_config;
DROP POLICY IF EXISTS system_config_service_all    ON public.system_config;

-- Browser roles: non-secret rows only, read-only. Writes go through the
-- Next.js API routes, which use the service role server-side.
CREATE POLICY system_config_anon_read
    ON public.system_config
    FOR SELECT
    TO anon, authenticated
    USING (is_secret = false);

-- Explicit service_role policy. Not strictly required (service_role bypasses
-- RLS) but makes the intent legible to anyone reading the policy list.
CREATE POLICY system_config_service_all
    ON public.system_config
    FOR ALL
    TO service_role
    USING (true) WITH CHECK (true);

-- ── Verification ───────────────────────────────────────────────────────────
DO $$
DECLARE n_secret int; n_total int;
BEGIN
  SELECT count(*) FILTER (WHERE is_secret), count(*)
    INTO n_secret, n_total FROM public.system_config;
  RAISE NOTICE 'system_config: % of % rows marked secret and now hidden from anon',
    n_secret, n_total;
  RAISE NOTICE 'ROTATE NOW: Zerodha API secret, Gmail app password, Discord webhook.';
END $$;
