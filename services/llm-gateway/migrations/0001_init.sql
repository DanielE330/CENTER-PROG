-- Who used which provider and how much. Message contents are never stored.
create table audit (
    id uuid primary key,
    tenant_id uuid not null,
    provider text not null,
    total_tokens bigint not null default 0,
    masked_entities int not null default 0,
    external boolean not null,
    at timestamptz not null default now()
);
create index audit_tenant_day on audit (tenant_id, at);
