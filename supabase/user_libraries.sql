-- FTC Whisper — per-account Custom Vocabulary and Snippets.
--
-- Both libraries are stored locally in config.json and that local copy is what
-- drives dictation; these tables only carry them between a user's machines.
-- The app is fully tolerant of them not existing (entries then stay on the
-- machine that created them) — run this once in the Supabase SQL editor
-- (project ijeeghdxokfvlfarojlm) to enable cross-device sync.
--
-- `id` is generated CLIENT-side so an entry created offline keeps its identity
-- when it later syncs, which is what makes the last-write-wins merge on
-- updated_at work. `deleted` is a tombstone, never a hard delete: a row
-- removed on one machine has to out-live the other machine's stale copy or it
-- simply comes back on the next merge.

-- ── Custom vocabulary ───────────────────────────────────────────────────────

create table if not exists public.user_vocabulary (
  id          uuid        primary key,
  user_id     uuid        not null references auth.users (id) on delete cascade,
  term        text        not null default '',
  -- The ways the recogniser tends to get `term` wrong. Corrected after
  -- transcription; never fed back to the engine as a hotword.
  sounds_like text[]      not null default '{}',
  deleted     boolean     not null default false,
  updated_at  timestamptz not null default now()
);

create index if not exists user_vocabulary_user_idx
  on public.user_vocabulary (user_id);

alter table public.user_vocabulary enable row level security;

drop policy if exists "own vocabulary select" on public.user_vocabulary;
create policy "own vocabulary select" on public.user_vocabulary
  for select using (auth.uid() = user_id);

drop policy if exists "own vocabulary insert" on public.user_vocabulary;
create policy "own vocabulary insert" on public.user_vocabulary
  for insert with check (auth.uid() = user_id);

drop policy if exists "own vocabulary update" on public.user_vocabulary;
create policy "own vocabulary update" on public.user_vocabulary
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ── Snippets ────────────────────────────────────────────────────────────────

create table if not exists public.user_snippets (
  id          uuid        primary key,
  user_id     uuid        not null references auth.users (id) on delete cascade,
  name        text        not null default '',
  -- Quoted because "trigger" is a reserved word in Postgres.
  "trigger"   text        not null default '',
  body        text        not null default '',
  deleted     boolean     not null default false,
  updated_at  timestamptz not null default now()
);

create index if not exists user_snippets_user_idx
  on public.user_snippets (user_id);

alter table public.user_snippets enable row level security;

drop policy if exists "own snippets select" on public.user_snippets;
create policy "own snippets select" on public.user_snippets
  for select using (auth.uid() = user_id);

drop policy if exists "own snippets insert" on public.user_snippets;
create policy "own snippets insert" on public.user_snippets
  for insert with check (auth.uid() = user_id);

drop policy if exists "own snippets update" on public.user_snippets;
create policy "own snippets update" on public.user_snippets
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
