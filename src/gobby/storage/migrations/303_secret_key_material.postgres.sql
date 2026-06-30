CREATE TABLE IF NOT EXISTS secret_key_material (
    id TEXT PRIMARY KEY,
    wrapped_dek TEXT NOT NULL,
    kek_posture TEXT NOT NULL,
    kek_salt TEXT,
    kek_kdf_n INTEGER,
    kek_kdf_r INTEGER,
    kek_kdf_p INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
