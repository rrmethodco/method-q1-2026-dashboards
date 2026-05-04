-- Validation files bucket — the agent worker reads from here.
-- Synced by the GH Actions workflows after each successful commit.
insert into storage.buckets (id, name, public, file_size_limit)
values ('validation', 'validation', false, 1048576)  -- 1MB cap per file
on conflict (id) do nothing;

-- Banner state bucket — agent worker writes here; dashboard reads via
-- public URL (renders client-side from GH Pages).
insert into storage.buckets (id, name, public, file_size_limit)
values ('banner', 'banner', true, 16384)  -- 16KB cap, public
on conflict (id) do nothing;

-- Audit log bucket — append-only, agent-write only.
insert into storage.buckets (id, name, public, file_size_limit)
values ('audit', 'audit', false, 10485760)  -- 10MB cap (single rolling file)
on conflict (id) do nothing;
