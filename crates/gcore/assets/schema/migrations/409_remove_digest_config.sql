-- Cut stored configuration over to the independent shadow-relevance trigger.

UPDATE config_store
SET key = 'memory.shadow_relevance_judging'
WHERE key = 'memory.digest_shadow_usefulness';

DELETE FROM config_store
WHERE key LIKE 'digest.%'
   OR key LIKE 'compact_handoff.%'
   OR key LIKE 'session_title.%';
