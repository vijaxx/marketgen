-- MarketGen -- row-level security policies.
--
-- NOT executed by the local test suite (no Postgres in the dev environment).
-- The equivalent guarantee is enforced in application code by
-- app/tenancy.py + app/store.py, and proven by tests/test_tenant_isolation.py.
--
-- Model
-- -----
-- The application connects as the non-superuser role `marketgen_app`, which is
-- explicitly NOT granted BYPASSRLS. At the start of every request the API sets
--
--     SELECT set_config('app.tenant_id', $1, true);   -- true => transaction-local
--
-- and every policy below compares `tenant_id` against that setting. A missing
-- or empty setting matches no rows, so a bug that forgets to set the GUC fails
-- closed (zero rows) rather than open (all rows).

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketgen_app') THEN
        CREATE ROLE marketgen_app NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

-- Returns NULL when unset, which makes every comparison below fail closed.
CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
$$;

-- Tenant registry: a tenant may see only its own row.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenants_self ON tenants;
CREATE POLICY tenants_self ON tenants
    FOR SELECT TO marketgen_app
    USING (id = app_current_tenant());

-- Tenant-scoped tables. USING controls visibility for SELECT/UPDATE/DELETE;
-- WITH CHECK stops a tenant writing a row stamped with someone else's id.
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS api_keys_isolation ON api_keys;
CREATE POLICY api_keys_isolation ON api_keys
    FOR ALL TO marketgen_app
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE clients FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS clients_isolation ON clients;
CREATE POLICY clients_isolation ON clients
    FOR ALL TO marketgen_app
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

ALTER TABLE onboarding_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE onboarding_sessions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS onboarding_isolation ON onboarding_sessions;
CREATE POLICY onboarding_isolation ON onboarding_sessions
    FOR ALL TO marketgen_app
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

ALTER TABLE campaign_briefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_briefs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS briefs_isolation ON campaign_briefs;
CREATE POLICY briefs_isolation ON campaign_briefs
    FOR ALL TO marketgen_app
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

ALTER TABLE generated_posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE generated_posts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS posts_isolation ON generated_posts;
CREATE POLICY posts_isolation ON generated_posts
    FOR ALL TO marketgen_app
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

ALTER TABLE publish_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE publish_attempts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS attempts_isolation ON publish_attempts;
CREATE POLICY attempts_isolation ON publish_attempts
    FOR ALL TO marketgen_app
    USING (tenant_id = app_current_tenant())
    WITH CHECK (tenant_id = app_current_tenant());

GRANT SELECT ON tenants TO marketgen_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    api_keys, clients, onboarding_sessions,
    campaign_briefs, generated_posts, publish_attempts
    TO marketgen_app;

COMMIT;
