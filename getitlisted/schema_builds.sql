-- Get It Listed: customer build intake for the automated engine.
-- Customers can ADD a build for an existing app, but can never read builds back
-- (test logins stay private). Only the engine (service key) reads them.

create table if not exists public.builds (
  id bigserial primary key,
  app_key text not null,
  apk_path text not null,
  mapping_path text,
  login_email text,
  login_password text,
  notes text,
  status text not null default 'pending',   -- pending | running | done | failed
  last_run_at timestamptz,
  runs int not null default 0,
  created_at timestamptz not null default now()
);
alter table public.builds enable row level security;
drop policy if exists "customers add builds" on public.builds;
create policy "customers add builds" on public.builds for insert to anon, authenticated
  with check (status = 'pending' and runs = 0
              and exists (select 1 from public.clients c where c.app_key = builds.app_key and c.status = 'active'));

insert into storage.buckets (id, name, public, file_size_limit)
values ('builds', 'builds', false, 52428800)
on conflict (id) do update set public = false, file_size_limit = 52428800;
drop policy if exists "customers upload builds" on storage.objects;
create policy "customers upload builds" on storage.objects for insert to anon, authenticated
  with check (bucket_id = 'builds' and (lower(name) like '%/app.apk' or lower(name) like '%/mapping.txt'));
