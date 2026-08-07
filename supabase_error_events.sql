-- error_events — fleet-wide reliability telemetry for FTC Whisper.
--
-- Each row is one client-side failure: a transcription that never landed in
-- the target app (inject_failed), a success claim the read-back disproved
-- (inject_false_success), a mic that heard nothing (mic_silent), an automatic
-- evidence-based mic switch (mic_switched), an empty transcription of real
-- audio (transcribe_empty), a press that had to reopen the stream and so may
-- have lost first words (mic_press_recovered), a dictation that captured no
-- audio at all from a stream that looked alive (capture_dead), or the watchdog
-- reopening a stream still callbacking with pure silence (mic_stream_silent).
-- The `detail` jsonb carries the per-cause facts
-- (foreground exe/class, injection method, elevated-target flag, mic names and
-- RMS levels), so every event answers "what was the issue" on its own.
--
-- The app writes fire-and-forget; if this table is absent the logging is
-- silently skipped and dictation is unaffected. Run ONCE in the shared FTC
-- Supabase project (ijeeghdxokfvlfarojlm) → Dashboard → SQL Editor → Run.

create table if not exists public.error_events (
    id            bigint generated always as identity primary key,
    created_at    timestamptz not null default now(),
    user_id       uuid,
    event_type    text not null,  -- inject_failed | inject_false_success
                                  -- | mic_silent | mic_switched | transcribe_empty
    app_version   text,
    app_name      text,
    app_exe       text,
    window_class  text,
    detail        jsonb,
    -- Links the event to its transcriptions row (same created_at identity the
    -- history and local audio clip share), so the super-admin can see exactly
    -- which dictation failed to land.
    transcription_created_at timestamptz
);

create index if not exists error_events_created_at_idx on public.error_events (created_at desc);
create index if not exists error_events_type_idx       on public.error_events (event_type);
create index if not exists error_events_user_idx       on public.error_events (user_id);

alter table public.error_events enable row level security;

-- Clients (authenticated app installs) may only INSERT their own events.
drop policy if exists "clients insert own error events" on public.error_events;
create policy "clients insert own error events"
    on public.error_events for insert
    to authenticated
    with check (auth.uid() = user_id or user_id is null);

-- Only the super-admin account may read (in-app admin error log / dashboard).
drop policy if exists "super admin reads error events" on public.error_events;
create policy "super admin reads error events"
    on public.error_events for select
    to authenticated
    using ((auth.jwt() ->> 'email') = 'ryan.murphy@ftc-ss.com');

-- Rollup: failures per day per type per version — is reliability improving
-- release over release? security_invoker keeps RLS in force through the view.
create or replace view public.error_rollup
with (security_invoker = true) as
select
    date_trunc('day', created_at) as day,
    event_type,
    app_version,
    count(*)                      as events,
    count(distinct user_id)       as devices
from public.error_events
group by 1, 2, 3
order by 1 desc, 2;
