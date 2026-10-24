create table alert (
    id uuid primary key,
    tenant_id uuid not null,
    kind text not null,
    severity text not null check (severity in ('info', 'warning', 'high', 'critical')),
    dedup_key text not null,
    title text not null,
    body text not null,
    data jsonb not null default '{}',
    status text not null default 'open' check (status in ('open', 'acked', 'resolved')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (tenant_id, dedup_key)
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

create table processed_event (
    id uuid primary key,
    at timestamptz not null default now()
);

alter table alert enable row level security;
alter table alert force row level security;
create policy tenant_isolation on alert
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
