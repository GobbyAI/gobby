-- Drop provider speed routes. The fast-mode capability is removed: of the six
-- supported CLIs only Droid exposes an invocation-time speed choice, and there it
-- is an ordinary model id (`<id>-fast`), so route rows carried nothing a model row
-- does not already carry.

DROP TABLE IF EXISTS provider_model_routes;
