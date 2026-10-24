-- Business settings of an organization. Users, membership and roles live in Keycloak;
-- organization.id is the Keycloak organization id and the tenant id everywhere else.

create table organization (
    id uuid primary key,
    name text not null,
    alias text not null unique,
    tax_regime text,
    tax_regime_suggested text,
    tax_regime_confidence real,
    tax_estimate jsonb not null default '{}',
    as_of date not null default current_date,
    overdraft_limit_kop bigint not null default 0,
    min_balance_kop bigint not null default 0,
    llm_policy jsonb not null default '{"provider": "local", "external_allowed": false, "masking": true}',
    demo_company text,
    updated_at timestamptz not null default now()
);

create table feature_flag (
    tenant_id uuid not null references organization (id),
    flag text not null,
    enabled boolean not null,
    primary key (tenant_id, flag)
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

alter table organization enable row level security;
alter table organization force row level security;
create policy tenant_isolation on organization
    using (id = nullif(current_setting('app.tenant_id', true), '')::uuid);

alter table feature_flag enable row level security;
alter table feature_flag force row level security;
create policy tenant_isolation on feature_flag
    using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
