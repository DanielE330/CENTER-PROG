-- The consultant works only with cloud models: no provider means template answers.
alter table organization
    alter column llm_policy set default '{"provider": "template", "external_allowed": false, "masking": true}';
-- A data fix across all tenants: RLS would hide every row from this session.
alter table organization no force row level security;
update organization
   set llm_policy = jsonb_set(jsonb_set(llm_policy, '{provider}', '"template"'), '{masking}', 'true')
 where llm_policy->>'provider' = 'local';
alter table organization force row level security;
