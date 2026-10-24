-- Agent proposals. Nothing here changes user data until the user accepts.
create table proposal (
    id uuid primary key,
    tenant_id uuid not null,
    kind text not null check (kind in ('obligation', 'scenario')),
    title text not null,
    payload jsonb not null,
    status text not null default 'pending' check (status in ('pending', 'accepted', 'rejected', 'failed')),
    created_by text not null,
    created_at timestamptz not null default now(),
    decided_at timestamptz
);

create table outbox (
    id uuid primary key,
    tenant_id uuid not null,
    subject text not null,
    payload jsonb not null,
    created_at timestamptz not null default clock_timestamp(),
    published_at timestamptz
);
create index outbox_pending on outbox (created_at) where published_at is null;

alter table proposal enable row level security;
alter table proposal force row level security;
create policy tenant_isolation on proposal
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
