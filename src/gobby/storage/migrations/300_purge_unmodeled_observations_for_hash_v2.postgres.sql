DO $$
BEGIN
    IF to_regclass('public.unmodeled_observation_events') IS NOT NULL THEN
        DELETE FROM unmodeled_observation_events;
    END IF;

    IF to_regclass('public.unmodeled_observations') IS NOT NULL THEN
        DELETE FROM unmodeled_observations;
    END IF;
END $$;
