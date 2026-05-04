-- Schedule the agent-worker Edge Function to run every 5 minutes.
-- Requires pg_cron + pg_net extensions (Supabase enables both by default).
-- Service role key MUST be set via:
--   alter database postgres set "app.settings.service_role_key" = '<KEY>';
-- (operator runs this once via the Supabase SQL editor — see Phase A.1
-- runbook step 3.)
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.schedule(
  'agent-worker-tick',
  '*/5 * * * *',  -- every 5 minutes
  $$
    select net.http_get(
      url := 'https://mmwislzsgnjxjxssynwm.supabase.co/functions/v1/agent-worker',
      headers := jsonb_build_object(
        'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key')
      )
    );
  $$
);
