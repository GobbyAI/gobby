ALTER TABLE gh_issues_triaged
    ADD COLUMN IF NOT EXISTS source_text TEXT;
