-- 400: GenerationEndpointConfig.vision_extract was replaced by activation-owned
-- probe evidence (probed_model / input_modalities). The registry no longer knows
-- the per-endpoint key, and ConfigRepository rejects a whole snapshot on any
-- unknown stored key, so stored rows for the retired field must go. Nothing is
-- migrated into evidence: such an endpoint presents as probe-unknown until it is
-- activated.

DELETE FROM config_store
WHERE key LIKE 'ai.generation.endpoints.%.vision_extract';
