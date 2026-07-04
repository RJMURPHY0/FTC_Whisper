-- ============================================================================
-- FTC Whisper — shared-auth migration
-- Run this ONCE in the FTC Contacts Supabase project
--   (ijeeghdxokfvlfarojlm) → Dashboard → SQL Editor → New query → Run.
--
-- Why: Whisper now authenticates against the SAME Supabase project as FTC
-- Contacts, so one login works in both apps. Contacts' project is missing the
-- two tables Whisper needs. This creates them (additive — nothing existing is
-- touched) with row-level security matching the old dedicated whisper project.
-- ============================================================================

-- ── app_settings ───────────────────────────────────────────────────────────
-- Server-side config the desktop client fetches AFTER sign-in (the public
-- release exe ships with the AI keys stripped, so this table is the ONLY source
-- of AI-refinement keys for released users — it must be populated, see bottom).
create table if not exists public.app_settings (
  key   text primary key,
  value text
);

alter table public.app_settings enable row level security;

drop policy if exists "app_settings read for authenticated" on public.app_settings;
create policy "app_settings read for authenticated"
  on public.app_settings for select
  to authenticated
  using (true);

-- ── transcriptions ─────────────────────────────────────────────────────────
-- Per-user dictation / refinement history. Each user sees only their own rows.
create table if not exists public.transcriptions (
  id               bigint generated always as identity primary key,
  user_id          uuid references auth.users(id) on delete cascade,
  transcribed_text text,
  refined_text     text,
  refinement_mode  text,
  created_at       timestamptz not null default now()
);

alter table public.transcriptions enable row level security;

drop policy if exists "transcriptions owner select" on public.transcriptions;
create policy "transcriptions owner select"
  on public.transcriptions for select
  to authenticated
  using (auth.uid() = user_id);

drop policy if exists "transcriptions owner insert" on public.transcriptions;
create policy "transcriptions owner insert"
  on public.transcriptions for insert
  to authenticated
  with check (auth.uid() = user_id);

drop policy if exists "transcriptions owner delete" on public.transcriptions;
create policy "transcriptions owner delete"
  on public.transcriptions for delete
  to authenticated
  using (auth.uid() = user_id);

create index if not exists transcriptions_user_created_idx
  on public.transcriptions (user_id, created_at desc);

-- ── Populate the AI keys ─────────────────────────────────────────────────────
-- REQUIRED for AI refinement to work in released builds. Copy the values from
-- the OLD whisper project (mbxwqtsesxgpcfyicphs → Table editor → app_settings),
-- or paste your own keys, then run this block. Uncomment and fill in:
--
-- insert into public.app_settings (key, value) values
--   ('openrouter_api_key', 'sk-or-...'),
--   ('anthropic_api_key',  'sk-ant-...')
-- on conflict (key) do update set value = excluded.value;
