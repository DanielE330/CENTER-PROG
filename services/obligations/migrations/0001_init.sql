-- Everything that will move money in the future but is not in the statement yet.

create table event (
    id uuid primary key,
    tenant_id uuid not null,
    kind text not null check (kind in ('manual', 'tax', 'invoice_out', 'invoice_in', 'recurring', 'contract', 'loan', 'payroll', 'payment_order')),
    direction text not null check (direction in ('inflow', 'outflow')),
    amount_kop bigint not null check (amount_kop > 0),
    due_date date not null,
    certainty real not null default 1 check (certainty between 0 and 1),
    counterparty_ref text,
    doc_no text,
    doc_date date,
    source text not null,
    source_ref text not null,
    status text not null default 'planned' check (status in ('planned', 'paid', 'overdue', 'cancelled')),
    matched_transaction_id uuid,
    paid_date date,
    note text,
    created_at timestamptz not null default now(),
    unique (tenant_id, source, source_ref)
);
create index event_tenant_status on event (tenant_id, status, due_date);

-- Projection of tenancy settings: "today" and the tax regime.
create table tenant_settings (
    tenant_id uuid primary key,
    as_of date not null,
    tax_regime text,
    tax_estimate jsonb not null default '{}'
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

alter table event enable row level security;
alter table event force row level security;
create policy tenant_isolation on event
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

alter table tenant_settings enable row level security;
alter table tenant_settings force row level security;
create policy tenant_isolation on tenant_settings
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
