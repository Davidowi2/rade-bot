-- 0007: let lifecycle notifications use trade ids as their event key.
--
-- The foreign key added in 0001 (notification_log.event_key references
-- mirror_events.event_key) matched only the pipeline's disposition alerts,
-- whose keys are event hashes with a mirror_events row. The execution
-- engine's lifecycle notifications ([SIGNAL], [FILLED], [CLOSED], ...) are
-- keyed by trade id ("RADE-..."), which has no mirror_events row, so every
-- begin_notification upsert failed with SQLSTATE 23503. notify_event
-- swallows that failure by design (a notification must never be lost because
-- the store hiccupped), so the send still went out -- but no slot was ever
-- recorded, the "already notified" check never matched, and each management
-- pass re-sent the same message. The first live trade produced ~44 duplicate
-- [FILLED] alerts in 7 minutes through exactly this path.
--
-- Dropping the reference is safe: the unique (event_key, kind) constraint is
-- what dedup and restart-recovery actually rely on. Nothing joins
-- notification_log to mirror_events in code, and cascade deletion was never
-- exercised (mirror_events rows are never deleted).

alter table public.notification_log
    drop constraint if exists notification_log_event_key_fkey;
