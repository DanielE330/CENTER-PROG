#!/bin/bash
# One role and one schema per service. Roles have no password (certificate login),
# no BYPASSRLS and no access to other schemas.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
revoke all on schema public from public;
do $$
declare
  r record;
begin
  for r in select * from (values
      ('tenancy', 'tenancy'), ('ingest', 'ingest'), ('obligations', 'obligations'), ('alerts', 'alerts'),
      ('llm-gateway', 'llm_gateway'), ('advisor-tools', 'advisor_tools'), ('categorizer', 'categorizer'),
      ('forecast', 'forecast')) as t(role, schema)
  loop
    execute format('create role %I login nobypassrls', r.role);
    execute format('create schema %I authorization %I', r.schema, r.role);
    execute format('alter role %I set search_path = %I', r.role, r.schema);
    execute format('grant connect on database ci1 to %I', r.role);
  end loop;
end $$;
create role keycloak login nobypassrls;
create database keycloak owner keycloak;
revoke connect on database ci1 from public;
SQL
