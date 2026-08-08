CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    key_prefix VARCHAR(12) NOT NULL,
    user_name VARCHAR(255) NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[],
    description VARCHAR(500),
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by VARCHAR(255),
    CONSTRAINT api_keys_key_hash_sha256 CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT api_keys_key_prefix_format CHECK (key_prefix ~ '^krlk_[0-9a-f]{7}$'),
    CONSTRAINT api_keys_scopes_nonempty CHECK (cardinality(scopes) > 0)
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user_created
    ON api_keys (user_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_api_keys_expiration
    ON api_keys (expires_at)
    WHERE is_active = TRUE AND expires_at IS NOT NULL;
