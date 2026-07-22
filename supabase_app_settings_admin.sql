-- ============================================================================
-- FTC Whisper — app_settings super-admin write access
-- Run ONCE in the shared FTC Supabase project
--   (ijeeghdxokfvlfarojlm) → Dashboard → SQL Editor → New query → Run.
--
-- Why: the desktop app lets the super-admin account push fleet-wide defaults
-- (currently `default_window_size`, written when ryan.murphy@ftc-ss.com
-- resizes the window). app_settings already exists with read-for-authenticated
-- (see supabase_shared_auth.sql); this adds insert/update for that one account
-- so the client-side upsert succeeds. Additive and idempotent.
-- ============================================================================

drop policy if exists "app_settings admin insert" on public.app_settings;
create policy "app_settings admin insert"
  on public.app_settings for insert
  to authenticated
  with check ((auth.jwt() ->> 'email') = 'ryan.murphy@ftc-ss.com');

drop policy if exists "app_settings admin update" on public.app_settings;
create policy "app_settings admin update"
  on public.app_settings for update
  to authenticated
  using ((auth.jwt() ->> 'email') = 'ryan.murphy@ftc-ss.com')
  with check ((auth.jwt() ->> 'email') = 'ryan.murphy@ftc-ss.com');

-- Optional: seed the install default now instead of waiting for the first
-- super-admin resize. "WxH" in pixels; new installs read it once at first
-- sign-in and existing installs are never affected.
--
-- insert into public.app_settings (key, value) values
--   ('default_window_size', '420x640')
-- on conflict (key) do update set value = excluded.value;
