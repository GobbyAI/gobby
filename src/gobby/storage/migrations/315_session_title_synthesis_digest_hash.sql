ALTER TABLE sessions
ADD COLUMN IF NOT EXISTS last_title_synthesis_digest_hash TEXT;
