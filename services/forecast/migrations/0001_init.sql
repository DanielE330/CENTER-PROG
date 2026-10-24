-- Projections built from events (no purpose text, no names) and forecast runs.

create table flow (
    transaction_id uuid primary key,
    tenant_id uuid not null,
    op_date date not null,
    amount_kop bigint not null,
    balance_eod_kop bigint not null,
    counterparty_ref text,
    subcategory text not null,
    grp text not null,
    is_rescue boolean not null,
    doc_no text,
    doc_date date
);
create index flow_tenant_date on flow (tenant_id, op_date);

create table obligation (
    tenant_id uuid not null,
    id uuid not null,
    payload jsonb not null,
    primary key (tenant_id, id)
);

create table settings (
    tenant_id uuid primary key,
    as_of date not null,
    min_balance_kop bigint not null default 0,
    overdraft_limit_kop bigint not null default 0
);

create table registry (
    tenant_id uuid not null,
    counterparty_ref text not null,
    status text not null,
    primary key (tenant_id, counterparty_ref)
);

create table scenario (
    id uuid primary key,
    tenant_id uuid not null,
    name text not null,
    transforms jsonb not null,
    created_at timestamptz not null default now()
);

create table run (
    id uuid primary key,
    tenant_id uuid not null,
    as_of date not null,
    scenario_id uuid,
    horizon int not null,
    model text not null,
    inputs_hash text not null,
    payload jsonb not null,
    created_at timestamptz not null default now()
);
create index run_tenant on run (tenant_id, scenario_id, created_at desc);

create table processed_event (
    id uuid primary key,
    at timestamptz not null default now()
);

do $$
declare t text;
begin
  foreach t in array array['flow', 'obligation', 'settings', 'registry', 'scenario', 'run'] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format($p$create policy tenant_isolation on %I using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)$p$, t);
  end loop;
end $$;
