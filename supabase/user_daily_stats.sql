-- FTC Whisper — per-account daily dictation stats (Home tab "Your impact").
-- One tiny row per user per day; RLS-scoped so each account only ever sees
-- its own rows. The app is fully tolerant of this table not existing (stats
-- then stay local to each machine) — run this once in the Supabase SQL
-- editor (project ijeeghdxokfvlfarojlm) to enable cross-device sync.

create table if not exists public.user_daily_stats (
  user_id        uuid        not null references auth.users (id) on delete cascade,
  day            date        not null,
  words          integer     not null default 0,
  audio_seconds  real        not null default 0,
  -- Speech-only portions (energy above the mic noise floor) — powers the
  -- real dictation-speed (wpm) card. voiced_words counts only words from
  -- dictations with plausible voiced timing.
  voiced_seconds real        not null default 0,
  voiced_words   integer     not null default 0,
  updated_at     timestamptz not null default now(),
  primary key (user_id, day)
);

-- Upgrade path for tables created before the voiced columns existed.
alter table public.user_daily_stats
  add column if not exists voiced_seconds real not null default 0;
alter table public.user_daily_stats
  add column if not exists voiced_words integer not null default 0;

alter table public.user_daily_stats enable row level security;

drop policy if exists "own stats select" on public.user_daily_stats;
create policy "own stats select" on public.user_daily_stats
  for select using (auth.uid() = user_id);

drop policy if exists "own stats insert" on public.user_daily_stats;
create policy "own stats insert" on public.user_daily_stats
  for insert with check (auth.uid() = user_id);

drop policy if exists "own stats update" on public.user_daily_stats;
create policy "own stats update" on public.user_daily_stats
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
