DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'auth_sessions'
          AND column_name = 'token'
    ) THEN
        ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS token_hash TEXT;

        UPDATE auth_sessions
        SET token_hash = encode(sha256(convert_to(token, 'UTF8')), 'hex')
        WHERE token_hash IS NULL
          AND token IS NOT NULL;

        ALTER TABLE auth_sessions ALTER COLUMN token_hash SET NOT NULL;
        ALTER TABLE auth_sessions DROP CONSTRAINT IF EXISTS auth_sessions_pkey;
        ALTER TABLE auth_sessions ADD CONSTRAINT auth_sessions_pkey PRIMARY KEY (token_hash);
        ALTER TABLE auth_sessions DROP COLUMN token;
    END IF;
END $$;
