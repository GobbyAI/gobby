-- #25485 removes the stored session title and its provenance. Every display label now
-- derives from the session ref, the agent definition, the claimed task and the status
-- line, so no session row keeps a title or a title source.
ALTER TABLE sessions DROP COLUMN title;
ALTER TABLE sessions DROP COLUMN title_source;
