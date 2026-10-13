-- Host epochs are opaque identities. Only observed exits authorize settlement.
CREATE TABLE public.terminal_host_epochs (
    machine_id uuid NOT NULL,
    epoch text NOT NULL,
    is_current boolean NOT NULL DEFAULT false,
    exited_at timestamptz,
    PRIMARY KEY (machine_id, epoch),
    CHECK (NOT is_current OR exited_at IS NULL)
);
CREATE UNIQUE INDEX terminal_host_epochs_current_machine
    ON public.terminal_host_epochs (machine_id) WHERE is_current;
REVOKE ALL ON public.terminal_host_epochs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON public.terminal_host_epochs TO gobby_daemon_runtime;
