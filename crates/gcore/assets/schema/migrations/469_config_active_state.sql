-- Python's active configuration state, published on every active-bundle swap.
CREATE TABLE public.config_active_state (
    deployment_token text PRIMARY KEY,
    revision bigint NOT NULL,
    state jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON public.config_active_state FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON public.config_active_state TO gobby_daemon_runtime;
