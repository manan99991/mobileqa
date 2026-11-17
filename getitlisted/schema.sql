-- Get It Listed — automated QA report storage.
-- Run once in the Supabase SQL editor (project ljdedtgsijrettyrggpy).

create table if not exists reports (
  id          uuid primary key default gen_random_uuid(),
  app_key     text not null,
  platform    text default 'Android',
  run_id      text,
  score       int,
  critical    int default 0,
  high        int default 0,
  medium      int default 0,
  low         int default 0,
  issues      int default 0,
  report_url  text,
  status      text default 'ready',
  created_at  timestamptz default now()
);
create index if not exists reports_app_key_idx on reports (app_key, created_at desc);

alter table reports enable row level security;
grant select on reports to anon, authenticated;

-- Customers (anon) may read their report row; only the service key (the test
-- workflow) inserts — service role bypasses RLS, so no insert policy is needed.
drop policy if exists reports_read on reports;
create policy reports_read on reports for select using (true);

-- Storage bucket for the rendered report html + screenshots (public read).
insert into storage.buckets (id, name, public)
values ('reports', 'reports', true)
on conflict (id) do update set public = true;

drop policy if exists reports_obj_read on storage.objects;
create policy reports_obj_read on storage.objects for select using (bucket_id = 'reports');
