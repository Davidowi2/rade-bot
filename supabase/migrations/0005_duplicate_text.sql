-- Recognise the same call posted twice.
--
-- The channel re-posts its signals as separate messages with separate ids
-- (observed: 18788 and 18789, identical text and identical timestamp). The
-- event key is a hash of the channel, the message id and the revision, so the
-- uniqueness constraint on mirror_events sees two distinct events and the
-- copier would place two orders for one idea.
--
-- The trade row now records the hash of the text it came from, which is what
-- lets the engine ask "have I already copied this exact call?" without
-- depending on the event row having been written first.

alter table public.mirror_trades
    add column if not exists text_sha256 text not null default '';

-- The lookup is always "same hash, recent", so it is indexed that way.
create index if not exists ix_mirror_trades_text_sha256
    on public.mirror_trades (text_sha256, created_at desc);
