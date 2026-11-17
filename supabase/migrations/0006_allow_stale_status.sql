-- 0006: let the ledger store a `stale` event.
--
-- `mirror_pipeline.STATUS_STALE` ("stale") is produced whenever a signal is
-- older than MIRROR_MAX_SIGNAL_AGE_S. It was missing from the CHECK constraint
-- created in 0001, so every stale signal was rejected by Postgres with a 400:
-- the row was never written, the message was never claimed, and `gap_fill` held
-- the cursor below it and retried every poll, forever. The signal did not appear
-- as "stale" - it did not appear at all.
--
-- The local SQLite ledger has no such constraint, which is why no test caught
-- this: the schema only exists in production.
--
-- 0001 used `create table if not exists`, so if the table predates that file the
-- original constraint was never applied. Dropping by name first (if present) and
-- then re-adding makes this correct in both cases.

alter table public.mirror_events
    drop constraint if exists mirror_events_status_valid;

alter table public.mirror_events
    add constraint mirror_events_status_valid check (
        status in (
            'paper_intent',
            'ignored',
            'manual_review',
            'blocked',
            'stale',
            'skipped_minimum_lot',
            'duplicate',
            'conflict'
        )
    );
