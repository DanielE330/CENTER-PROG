-- Raw statements and normalized transactions. Only this service sees INN,
-- counterparty names and payment purposes in clear text.

create table import (
    id uuid primary key,
    tenant_id uuid not null,
    file_sha256 text not null,
    format text not null,
    source text not null,
    stats jsonb not null default '{}',
    created_at timestamptz not null default now(),
    unique (tenant_id, file_sha256)
);

create table transaction (
    id uuid primary key,
    tenant_id uuid not null,
    import_id uuid not null references import (id),
    op_date date not null,
    amount_kop bigint not null,             -- signed: inflow > 0, outflow < 0
    counterparty_ref text,
    counterparty_inn text,
    counterparty_name text,
    purpose text,
    balance_eod_kop bigint not null,
    dedup_key text not null,
    category_id text,
    category_group text,
    created_at timestamptz not null default now(),
    unique (tenant_id, dedup_key)
);
create index transaction_tenant_date on transaction (tenant_id, op_date);

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

alter table import enable row level security;
alter table import force row level security;
create policy tenant_isolation on import
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

alter table transaction enable row level security;
alter table transaction force row level security;
create policy tenant_isolation on transaction
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
