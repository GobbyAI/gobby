DO $provider_capability_matrix$
BEGIN
IF to_regclass('provider_capability_refresh_state') IS NULL THEN
CREATE TABLE provider_capability_refresh_state (
    provider TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT,
    required BOOLEAN NOT NULL DEFAULT TRUE,
    generation BIGINT NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT provider_capability_refresh_state_pkey
        PRIMARY KEY (provider, source_key)
);
END IF;

IF to_regclass('provider_model_capabilities') IS NULL THEN
CREATE TABLE provider_model_capabilities (
    provider TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    display_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]',
    available BOOLEAN NOT NULL DEFAULT TRUE,
    hidden BOOLEAN NOT NULL DEFAULT FALSE,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    context_length INTEGER,
    max_output_tokens INTEGER,
    reasoning TEXT NOT NULL DEFAULT 'unknown',
    supported_efforts JSONB,
    default_effort TEXT,
    latency_class TEXT,
    input_modalities JSONB,
    supports_tools BOOLEAN,
    generation BIGINT NOT NULL,
    provenance JSONB NOT NULL,
    CONSTRAINT provider_model_capabilities_pkey
        PRIMARY KEY (provider, canonical_model)
);
END IF;

IF to_regclass('provider_model_routes') IS NULL THEN
CREATE TABLE provider_model_routes (
    provider TEXT NOT NULL,
    canonical_model TEXT NOT NULL,
    speed_mode TEXT NOT NULL,
    selector TEXT NOT NULL,
    available BOOLEAN NOT NULL DEFAULT TRUE,
    usage_multiplier NUMERIC,
    throughput_multiplier NUMERIC,
    latency_class TEXT,
    activations JSONB NOT NULL DEFAULT '[]',
    generation BIGINT NOT NULL,
    provenance JSONB NOT NULL,
    CONSTRAINT provider_model_routes_pkey
        PRIMARY KEY (provider, canonical_model, speed_mode),
    CONSTRAINT provider_model_routes_capability_fkey
        FOREIGN KEY (provider, canonical_model)
        REFERENCES provider_model_capabilities (provider, canonical_model)
        ON DELETE CASCADE
);
END IF;
END;
$provider_capability_matrix$;
