-- Categorized operations. Purpose text is kept here for retraining; it never
-- leaves this service (the categorized event carries no free text).

create table assignment (
    transaction_id uuid primary key,
    tenant_id uuid not null,
    op_date date not null,
    amount_kop bigint not null,
    balance_eod_kop bigint not null,
    counterparty_ref text,
    counterparty_name text,
    purpose text,
    subcategory text not null,
    category_id uuid not null,
    method text not null check (method in ('rule', 'ml', 'manual', 'tenant_rule', 'fallback')),
    confidence real not null,
    is_rescue boolean not null default false,
    doc_no text,
    doc_date date
);
create index assignment_tenant_date on assignment (tenant_id, op_date);
create index assignment_tenant_cp on assignment (tenant_id, counterparty_ref);

create table category (
    id uuid primary key,
    tenant_id uuid not null,
    name text not null,
    grp text not null,
    system_subcategory text,
    archived boolean not null default false,
    unique (tenant_id, name)
);

-- Which tenant category a system subcategory lands in (merge, rename, split).
create table category_map (
    tenant_id uuid not null,
    subcategory text not null,
    category_id uuid not null references category (id),
    primary key (tenant_id, subcategory)
);

-- "Apply to similar": the owner's correction for a counterparty wins over rules and the model.
create table tenant_rule (
    tenant_id uuid not null,
    counterparty_ref text not null,
    category_id uuid not null references category (id),
    primary key (tenant_id, counterparty_ref)
);

create table processed_event (
    id uuid primary key,
    at timestamptz not null default now()
);

alter table assignment enable row level security;
alter table assignment force row level security;
create policy tenant_isolation on assignment using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
alter table category enable row level security;
alter table category force row level security;
create policy tenant_isolation on category using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
alter table category_map enable row level security;
alter table category_map force row level security;
create policy tenant_isolation on category_map using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
alter table tenant_rule enable row level security;
alter table tenant_rule force row level security;
create policy tenant_isolation on tenant_rule using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
