-- update_events — fleet-wide auto-update telemetry for FTC Whisper.
--
-- Each row is one stage of one update attempt on one device, so you can see
-- across all installs how many update successfully vs. get stuck. The app
-- writes to this table fire-and-forget; if the table is absent the logging is
-- silently skipped and updates are unaffected. Run this once in the Supabase
-- SQL editor to enable monitoring.

create table if not exists public.update_events (
    id           bigint generated always as identity primary key,
    created_at   timestamptz not null default now(),
    user_id      uuid,
    stage        text not null,   -- download_start | download_ok | download_fail
                                  -- | swap_started | manual_fallback_browser | announced
    from_version text,
    to_version   text,
    ok           boolean,
    detail       text
);

create index if not exists update_events_created_at_idx on public.update_events (created_at desc);
create index if not exists update_events_stage_idx      on public.update_events (stage);

alter table public.update_events enable row level security;

-- Clients (authenticated app installs) may only INSERT their own events.
-- Reads are intended via the service role / Supabase dashboard, so no SELECT
-- policy is granted to app clients.
drop policy if exists "clients insert own update events" on public.update_events;
create policy "clients insert own update events"
    on public.update_events for insert
    to authenticated
    with check (auth.uid() = user_id or user_id is null);

-- Handy monitoring views ----------------------------------------------------

-- Latest known outcome per device+target (did this device reach the new build?)
create or replace view public.update_outcomes as
select
    user_id,
    to_version,
    max(created_at)                                              as last_event_at,
    bool_or(stage = 'swap_started')                             as swap_started,
    bool_or(stage = 'download_ok')                              as download_ok,
    bool_or(stage = 'download_fail')                            as download_failed,
    bool_or(stage = 'manual_fallback_browser')                 as fell_back_to_browser
from public.update_events
group by user_id, to_version;

-- Rollup: per target version, how many devices succeeded vs. fell back.
create or replace view public.update_rollup as
select
    to_version,
    count(*)                                        filter (where swap_started)          as swaps_started,
    count(*)                                        filter (where download_failed)       as download_failures,
    count(*)                                        filter (where fell_back_to_browser)  as browser_fallbacks,
    count(*)                                                                             as devices
from public.update_outcomes
group by to_version
order by to_version desc;
