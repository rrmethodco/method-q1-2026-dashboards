-- Schedule the agent-worker Edge Function to run every 5 minutes.
-- Requires pg_cron + pg_net extensions (Supabase enables both by default).
--
-- Service role key is fetched from Supabase Vault (vault.decrypted_secrets)
-- with secret name 'agent_worker_service_role_key'. This must be created
-- ONCE before this migration runs, via the Supabase SQL editor:
--
--   select vault.create_secret(
--     '<SERVICE_ROLE_KEY>',
--     'agent_worker_service_role_key',
--     'Used by pg_cron to invoke the agent-worker Edge Function'
--   );
--
-- Why Vault and not `alter database postgres set "app.settings...."`?
-- 1. The supabase-managed postgres role can't ALTER DATABASE settings —
--    it returns ERROR 42501 "permission denied to set parameter".
-- 2. Even if it could, `current_setting('app.settings.x')` is readable
--    by every connected role with access to that database, leaking the
--    key. Security review 2026-05-04 flagged this as a High finding.
--    Vault encrypts at rest and only the postgres role can decrypt.
--
-- pg_cron's command is itself only readable by the postgres role
-- (cron.job table is restricted), so the inlined Vault lookup is safe.
create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Idempotent reschedule: if the job already exists (e.g. the operator
-- scheduled it manually before this migration ran), unschedule first
-- so cron.schedule doesn't error on the duplicate name.
do $$
begin
  if exists (select 1 from cron.job where jobname = 'agent-worker-tick') then
    perform cron.unschedule('agent-worker-tick');
  end if;
end $$;

select cron.schedule(
  'agent-worker-tick',
  '*/5 * * * *',  -- every 5 minutes
  $$
    select net.http_get(
      url := 'https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker',
      headers := jsonb_build_object(
        'Authorization',
        'Bearer ' || (
          select decrypted_secret
          from vault.decrypted_secrets
          where name = 'agent_worker_service_role_key'
        )
      )
    );
  $$
);
