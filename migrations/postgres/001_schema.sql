-- MarketGen -- Postgres schema (production target).
--
-- NOTE: this file is NOT executed by the local test suite, which runs against
-- SQLite (see migrations/sqlite/001_schema.sql). It is the deployment target
-- for a hosted Postgres / Supabase project. Reviewed for correctness, not
-- exercised in CI.

BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS tenants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 160),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- API keys are stored as SHA-256 hashes. The plaintext key is shown to the
-- operator once at creation time and never persisted.
CREATE TABLE IF NOT EXISTS api_keys (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash      text NOT NULL UNIQUE,
    actor_email   text NOT NULL,
    role          text NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'admin')),
    revoked_at    timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS api_keys_tenant_idx ON api_keys (tenant_id);

CREATE TABLE IF NOT EXISTS clients (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 160),
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);
CREATE INDEX IF NOT EXISTS clients_tenant_idx ON clients (tenant_id);

CREATE TABLE IF NOT EXISTS onboarding_sessions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id        uuid NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    status           text NOT NULL DEFAULT 'in_progress'
                       CHECK (status IN ('in_progress', 'completed', 'abandoned')),
    current_step     integer NOT NULL DEFAULT 1 CHECK (current_step BETWEEN 1 AND 7),
    answers          jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_steps  jsonb NOT NULL DEFAULT '[]'::jsonb,
    questionnaire_version text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS onboarding_tenant_idx ON onboarding_sessions (tenant_id);
CREATE INDEX IF NOT EXISTS onboarding_tenant_client_idx ON onboarding_sessions (tenant_id, client_id);
CREATE INDEX IF NOT EXISTS onboarding_answers_gin ON onboarding_sessions USING gin (answers);

CREATE TABLE IF NOT EXISTS campaign_briefs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    session_id   uuid NOT NULL REFERENCES onboarding_sessions(id) ON DELETE CASCADE,
    provider     text NOT NULL,
    brief        jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS briefs_tenant_idx ON campaign_briefs (tenant_id);
CREATE INDEX IF NOT EXISTS briefs_tenant_session_idx ON campaign_briefs (tenant_id, session_id);

CREATE TABLE IF NOT EXISTS generated_posts (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    brief_id      uuid NOT NULL REFERENCES campaign_briefs(id) ON DELETE CASCADE,
    channel       text NOT NULL,
    format        text NOT NULL,
    headline      text NOT NULL,
    body          text NOT NULL,
    call_to_action text NOT NULL,
    hashtags      jsonb NOT NULL DEFAULT '[]'::jsonb,
    image_prompt  text,
    image_ref     text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS posts_tenant_idx ON generated_posts (tenant_id);
CREATE INDEX IF NOT EXISTS posts_tenant_brief_idx ON generated_posts (tenant_id, brief_id);

CREATE TABLE IF NOT EXISTS publish_attempts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    post_id     uuid NOT NULL REFERENCES generated_posts(id) ON DELETE CASCADE,
    adapter     text NOT NULL,
    dry_run     boolean NOT NULL DEFAULT true,
    status      text NOT NULL CHECK (status IN ('dry_run', 'succeeded', 'failed', 'blocked')),
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS attempts_tenant_idx ON publish_attempts (tenant_id);

COMMIT;
