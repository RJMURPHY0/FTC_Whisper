-- FTC Whisper — history app-attribution migration.
-- Safe to run repeatedly in the shared Supabase project.
--
-- Deploy this before releasing the matching desktop build. Older clients keep
-- working because both columns are nullable; newer clients stop paying failed
-- schema-fallback requests and preserve the app metadata they already capture.

alter table public.transcriptions
  add column if not exists app_name text,
  add column if not exists app_exe  text;

comment on column public.transcriptions.app_name is
  'Friendly target application/service name captured when dictation starts';
comment on column public.transcriptions.app_exe is
  'Best-effort target executable path used for native icon extraction';
