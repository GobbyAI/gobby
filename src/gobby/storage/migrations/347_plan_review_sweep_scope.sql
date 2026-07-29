-- Rename the pre-0.5 repair-universe wire contract in all persisted locations.

UPDATE plan_review_evidence
SET prior_round_context =
    (
        prior_round_context
        - 'repair_universe'
        - 'repair_universe_digest'
        - 'repair_attestations'
    )
    || CASE
        WHEN prior_round_context ? 'repair_universe' THEN
            jsonb_build_object(
                'submitted_sweep_scope',
                (prior_round_context -> 'repair_universe') - 'digest',
                'current_sweep_scope',
                (prior_round_context -> 'repair_universe') - 'digest',
                'required_scope_delta', '{
                    "requirements": {"added": [], "removed": [], "changed": []},
                    "candidate_sites": {"added": [], "removed": [], "changed": []},
                    "interaction_edges": {"added": [], "removed": [], "changed": []}
                }'::jsonb,
                'inventory_churn', '{
                    "requirements": {"added": [], "removed": [], "changed": []},
                    "candidate_sites": {"added": [], "removed": [], "changed": []},
                    "interaction_edges": {"added": [], "removed": [], "changed": []}
                }'::jsonb
            )
        ELSE '{}'::jsonb
    END
    || CASE
        WHEN prior_round_context ? 'repair_universe_digest' THEN
            jsonb_build_object(
                'submitted_sweep_scope_digest',
                prior_round_context -> 'repair_universe_digest'
            )
        ELSE '{}'::jsonb
    END
    || CASE
        WHEN jsonb_typeof(prior_round_context -> 'repair_attestations') = 'array' THEN
            jsonb_build_object(
                'repair_attestations',
                (
                    SELECT COALESCE(
                        jsonb_agg(
                            CASE
                                WHEN jsonb_typeof(attestation) = 'object'
                                    AND attestation ? 'repair_universe_digest'
                                THEN
                                    (attestation - 'repair_universe_digest')
                                    || jsonb_build_object(
                                        'sweep_scope_digest',
                                        attestation -> 'repair_universe_digest'
                                    )
                                ELSE attestation
                            END
                            ORDER BY ordinal
                        ),
                        '[]'::jsonb
                    )
                    FROM jsonb_array_elements(
                        prior_round_context -> 'repair_attestations'
                    ) WITH ORDINALITY AS nested(attestation, ordinal)
                )
            )
        WHEN prior_round_context ? 'repair_attestations' THEN
            jsonb_build_object(
                'repair_attestations',
                prior_round_context -> 'repair_attestations'
            )
        ELSE '{}'::jsonb
    END
WHERE prior_round_context IS NOT NULL
  AND (
      prior_round_context ? 'repair_universe'
      OR prior_round_context ? 'repair_universe_digest'
      OR EXISTS (
          SELECT 1
          FROM jsonb_array_elements(
              CASE
                  WHEN jsonb_typeof(prior_round_context -> 'repair_attestations') = 'array'
                  THEN prior_round_context -> 'repair_attestations'
                  ELSE '[]'::jsonb
              END
          ) AS nested(attestation)
          WHERE jsonb_typeof(attestation) = 'object'
            AND attestation ? 'repair_universe_digest'
      )
  );

UPDATE plan_review_evidence
SET repair_attestations = (
    SELECT COALESCE(
        jsonb_agg(
            CASE
                WHEN jsonb_typeof(attestation) = 'object'
                    AND attestation ? 'repair_universe_digest'
                THEN
                    (attestation - 'repair_universe_digest')
                    || jsonb_build_object(
                        'sweep_scope_digest',
                        attestation -> 'repair_universe_digest'
                    )
                ELSE attestation
            END
            ORDER BY ordinal
        ),
        '[]'::jsonb
    )
    FROM jsonb_array_elements(repair_attestations)
        WITH ORDINALITY AS stored(attestation, ordinal)
)
WHERE jsonb_typeof(repair_attestations) = 'array'
  AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(repair_attestations) AS stored(attestation)
      WHERE jsonb_typeof(attestation) = 'object'
        AND attestation ? 'repair_universe_digest'
  );
