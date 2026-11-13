-- Pre-trade risk state and the pause switch.
--
-- Both are single-row tables: there is one account being mirrored and one
-- switch. The `id = 1` check is what enforces that, so a second writer cannot
-- create a competing row that the service would never read.
--
-- These are deliberately not per-trade. The daily-loss breaker and the
-- drawdown stop are properties of the account on a given day, and the pause
-- switch is a property of the service. Keeping them in the same store as the
-- trades is what makes them survive a Render restart, which wipes the disk.

create table if not exists public.mirror_risk_state (
    id               integer primary key check (id = 1),
    day              text        not null,
    day_start_equity double precision not null default 0,
    peak_equity      double precision not null default 0,
    updated_at       timestamptz not null default now()
);

create table if not exists public.mirror_control (
    id               integer primary key check (id = 1),
    execution_paused boolean     not null default false,
    reason           text        not null default '',
    changed_at       timestamptz not null default now(),
    changed_by       text        not null default ''
);

-- The service is the only writer, and it uses the service-role key. RLS is
-- enabled with no policies, which denies every request that is not made with
-- that key — the same posture as the trade tables.
alter table public.mirror_risk_state enable row level security;
alter table public.mirror_control    enable row level security;

-- The switch starts unset. The service treats "no row" as "not paused" and
-- says so on the status page, rather than silently assuming a default.
insert into public.mirror_control (id, execution_paused, reason, changed_by)
values (1, false, 'initial state', 'migration')
on conflict (id) do nothing;
