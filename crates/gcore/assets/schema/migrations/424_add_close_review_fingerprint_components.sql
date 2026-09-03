ALTER TABLE task_close_reviews
    ADD COLUMN diff_sha text,
    ADD COLUMN test_bodies_sha text,
    ADD COLUMN stable_facts jsonb;

ALTER TABLE task_close_reviews
    ADD CONSTRAINT task_close_reviews_diff_sha_valid
        CHECK (diff_sha IS NULL OR diff_sha ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT task_close_reviews_test_bodies_sha_valid
        CHECK (test_bodies_sha IS NULL OR test_bodies_sha ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT task_close_reviews_stable_facts_object
        CHECK (stable_facts IS NULL OR jsonb_typeof(stable_facts) = 'object');
