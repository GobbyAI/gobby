-- Relational receipt-effects authority for one-shot hook delivery. DDL lives
-- only here; Python storage/hook_receipts.py is DML.

CREATE TABLE IF NOT EXISTS hook_receipt_effects (
    receipt_id uuid PRIMARY KEY,
    original_envelope_id text NOT NULL,
    current_envelope_id text NOT NULL,
    session_id uuid NOT NULL,
    delivery_generation integer NOT NULL DEFAULT 1,
    state text NOT NULL,
    staged_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    transition_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT hook_receipt_effects_state_valid CHECK (
        state IN ('prepared', 'acknowledged', 'released', 'terminal-undelivered')
    ),
    CONSTRAINT hook_receipt_effects_delivery_generation_positive CHECK (
        delivery_generation >= 1
    )
);

CREATE INDEX IF NOT EXISTS hook_receipt_effects_session_id_idx
    ON hook_receipt_effects (session_id);

CREATE INDEX IF NOT EXISTS hook_receipt_effects_state_transition_idx
    ON hook_receipt_effects (state, transition_at);

CREATE INDEX IF NOT EXISTS hook_receipt_effects_current_envelope_idx
    ON hook_receipt_effects (current_envelope_id);

CREATE TABLE IF NOT EXISTS hook_force_continue_budgets (
    session_id uuid NOT NULL,
    execution_num integer NOT NULL,
    count integer NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, execution_num),
    CONSTRAINT hook_force_continue_budgets_count_nonnegative CHECK (count >= 0)
);
