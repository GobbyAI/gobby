-- Retire the Ask pipeline's artifact store after migration 454.
-- Qualify the target to the runner's schema so an absent table never falls
-- through search_path to another schema.
DO $retire_ask_artifacts$
BEGIN
    EXECUTE format('DROP TABLE IF EXISTS %I.ask_artifacts RESTRICT', current_schema());
END
$retire_ask_artifacts$;
