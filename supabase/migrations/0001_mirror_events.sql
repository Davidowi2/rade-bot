-- 0001_mirror_events.sql
--
-- Persistent store for the Telegram -> paper-intent mirror.
--
-- Idempotency is a database constraint, not an application convention:
--   * event_key is the primary key (sha256 of channel, message id, revision)
--   * (channel, message_id, revision) is additionally unique
-- so a redelivered message, a restart, or two overlapping listeners cannot
-- create a second intent. The application treats a unique violation as
-- "already handled" and compares the body hash to detect a conflicting edit.
--
-- Run with:  supabase db push   (or paste into the SQL editor)
-- Versioned on purpose: this file is the schema of record.

create table if not exists public.mirror_events (
    event_key     text        primary key,
    channel       text        not null,
    message_id    bigint      not null,
    revision      integer     not null default 0,
    posted_at     timestamptz not null,
    received_at   timestamptz not null,
    text_sha256   text        not null,
    raw_text      text        not null,
    status        text        not null,
    reason        text        not null default '',
    signal_json   jsonb,
    intent_json   jsonb,
    source        text        not null default 'telegram_live',
    created_at    timestamptz not null default now(),

    constraint mirror_events_revision_non_negative check (revision >= 0),
    constraint mirror_events_status_valid check (
        status in (
            'paper_intent',
            'ignored',
            'manual_review',
            'blocked',
            'skipped_minimum_lot',
            'duplicate',
            'conflict'
        )
    ),
    constraint mirror_events_channel_message_revision
        unique (channel, message_id, revision)
);

create index if not exists ix_mirror_events_received_at
    on public.mirror_events (received_at desc);

create index if not exists ix_mirror_events_status_received_at
    on public.mirror_events (status, received_at desc);

create index if not exists ix_mirror_events_message
    on public.mirror_events (channel, message_id);

-- Row level security with no policies: the anon and authenticated roles can
-- neither read nor write. The backend uses the service-role key, which
-- bypasses RLS, and that key never leaves the backend environment.
alter table public.mirror_events enable row level security;

-- One row per notification actually attempted. The unique constraint is what
-- stops a restart from re-notifying the same event.
create table if not exists public.notification_log (
    id         bigserial   primary key,
    event_key  text        not null references public.mirror_events (event_key) on delete cascade,
    kind       text        not null,
    ok         boolean     not null,
    detail     text,
    sent_at    timestamptz not null default now(),

    constraint notification_log_event_kind unique (event_key, kind)
);

create index if not exists ix_notification_log_sent_at
    on public.notification_log (sent_at desc);

alter table public.notification_log enable row level security;
