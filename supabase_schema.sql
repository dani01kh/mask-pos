-- Run this in Supabase SQL Editor before enabling POS cloud sync.
-- This stores POS sync events. The POS still uses local SQLite for normal work.

create table if not exists public.pos_sync_events (
    event_id text primary key,
    device_id text not null,
    event_type text not null,
    entity_type text not null,
    entity_id text,
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null,
    schema_version integer not null default 1,
    inserted_at timestamptz not null default now()
);

create index if not exists idx_pos_sync_events_created_at
    on public.pos_sync_events (created_at);

create index if not exists idx_pos_sync_events_device_id
    on public.pos_sync_events (device_id);

alter table public.pos_sync_events enable row level security;

-- This project is using a publishable key in the desktop POS.
-- These policies allow that key to upload/read sync events.
-- For stronger security later, replace this with authenticated users or a small server-side API.
drop policy if exists "pos_sync_events_anon_select" on public.pos_sync_events;
create policy "pos_sync_events_anon_select"
    on public.pos_sync_events
    for select
    to anon
    using (true);

drop policy if exists "pos_sync_events_anon_insert" on public.pos_sync_events;
create policy "pos_sync_events_anon_insert"
    on public.pos_sync_events
    for insert
    to anon
    with check (true);

drop policy if exists "pos_sync_events_anon_update" on public.pos_sync_events;
create policy "pos_sync_events_anon_update"
    on public.pos_sync_events
    for update
    to anon
    using (true)
    with check (true);

-- Cloud print queue. Cloud/Join PCs insert jobs; Host mode reads pending jobs,
-- prints through the local Windows printer, then marks them printed/failed.
create table if not exists public.pos_print_jobs (
    job_id text primary key,
    device_id text not null,
    job_type text not null,
    payload jsonb not null default '{}'::jsonb,
    status text not null default 'pending',
    created_at timestamptz not null default now(),
    claimed_by text,
    claimed_at timestamptz,
    printed_at timestamptz,
    failed_at timestamptz,
    last_error text
);

create index if not exists idx_pos_print_jobs_status_created
    on public.pos_print_jobs (status, created_at);

alter table public.pos_print_jobs enable row level security;

drop policy if exists "pos_print_jobs_anon_select" on public.pos_print_jobs;
create policy "pos_print_jobs_anon_select"
    on public.pos_print_jobs
    for select
    to anon
    using (true);

drop policy if exists "pos_print_jobs_anon_insert" on public.pos_print_jobs;
create policy "pos_print_jobs_anon_insert"
    on public.pos_print_jobs
    for insert
    to anon
    with check (true);

drop policy if exists "pos_print_jobs_anon_update" on public.pos_print_jobs;
create policy "pos_print_jobs_anon_update"
    on public.pos_print_jobs
    for update
    to anon
    using (true)
    with check (true);
