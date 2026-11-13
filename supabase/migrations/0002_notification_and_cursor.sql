-- 0002_notification_and_cursor.sql
--
-- Two changes the security review forced on the mirror:
--
-- 1. notification_log gains a real state machine. It used to hold a single
--    "ok" flag that was written *before* the send, so a failed delivery was
--    indistinguishable from a delivered one and there was nothing left to
--    retry. The slot is now reserved as 'pending' and completed as 'sent' or
--    'failed'.
--
-- 2. mirror_cursor holds the "start from here" floor. The hosted disk is
--    wiped on every restart, so without a durable cursor a fresh install
--    would fetch the channel's entire history and turn old calls into
--    today's paper intents.
--
-- Apply with:  supabase db push   (after 0001)
-- Versioned on purpose: this file is part of the schema of record.

-- ---------------------------------------------------------------------------
-- notifications
-- ---------------------------------------------------------------------------

alter table public.notification_log
    add column if not exists status text not null default 'pending';
alter table public.notification_log
    add column if not exists attempts integer not null default 0;
alter table public.notification_log
    add column if not exists last_error text;
alter table public.notification_log
    add column if not exists updated_at timestamptz not null default now();

do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'notification_log_status_valid') then
        alter table public.notification_log
            add constraint notification_log_status_valid
            check (status in ('pending', 'sent', 'failed'));
    end if;
end $$;

-- Backfill the rows written by the old code, where "ok" was the only record
-- of the outcome. A row the new code has touched has attempts > 0 and is
-- left alone.
update public.notification_log
   set status = case when ok then 'sent' else 'failed' end
 where status = 'pending' and attempts = 0;

create index if not exists ix_notification_log_status
    on public.notification_log (status, sent_at);

-- ---------------------------------------------------------------------------
-- the durable cursor
-- ---------------------------------------------------------------------------

create table if not exists public.mirror_cursor (
    channel         text        primary key,
    last_message_id bigint      not null,
    start_source    text        not null default '',
    updated_at      timestamptz not null default now(),

    constraint mirror_cursor_message_id_positive check (last_message_id >= 0)
);

-- Row level security with no policies, like every other table here: only the
-- service-role key can read or write it.
alter table public.mirror_cursor enable row level security;
