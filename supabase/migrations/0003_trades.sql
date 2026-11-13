-- 0003 — trades and their state transitions
--
-- An event is a Telegram message and exists once. A trade spans many broker
-- interactions and changes state, so it needs its own row and its own
-- append-only history.
--
-- `trade_id` is the correlation tag sent to the broker as `strategyId`. It is
-- derived from the event key, so it can be recomputed after a restart without
-- reading the database, and the broker's own order history can be searched
-- for it. That is what makes reconciliation possible without ever resubmitting
-- an order blindly.

create table if not exists mirror_trades (
    trade_id           text primary key,
    event_key          text not null,
    channel            text not null default '',
    message_id         bigint not null default 0,
    revision           integer not null default 0,
    symbol             text not null default '',
    side               text not null default '',
    size               numeric not null default 0,
    entry              numeric not null default 0,
    stop               numeric not null default 0,
    current_stop       numeric,
    targets_json       jsonb not null default '[]'::jsonb,
    plan_json          jsonb not null default '{}'::jsonb,
    state              text not null default 'validated',
    legs_done          integer not null default 0,
    broker_order_id    bigint,
    broker_position_id bigint,
    fill_price         numeric,
    management_stopped boolean not null default false,
    note               text not null default '',
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint mirror_trades_state_check check (state in (
        'validated', 'not_copied', 'blocked', 'submitted', 'accepted',
        'rejected', 'open', 'partial', 'closed', 'review'
    ))
);

create index if not exists ix_mirror_trades_state on mirror_trades (state);
create index if not exists ix_mirror_trades_event on mirror_trades (event_key);

create table if not exists mirror_transitions (
    id                 bigserial primary key,
    trade_id           text not null references mirror_trades (trade_id) on delete cascade,
    event_key          text not null default '',
    stage              text not null,
    status             text not null,
    detail             text not null default '',
    broker_order_id    bigint,
    broker_position_id bigint,
    message_id         bigint,
    revision           integer,
    payload_json       jsonb,
    created_at         timestamptz not null default now()
);

create index if not exists ix_mirror_transitions_trade on mirror_transitions (trade_id, id);

-- Both tables hold the trader's positions and the account's sizing. The
-- backend uses the service-role key, which bypasses RLS; enabling RLS with no
-- policies means the anon key can read nothing at all.
alter table mirror_trades enable row level security;
alter table mirror_transitions enable row level security;

-- The dashboard never reads these tables directly: it reads the backend's
-- read-only API, which decides what a browser may see.
