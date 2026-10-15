"""Keep the frozen hook ingress statements visible to route-port authors."""

from pathlib import Path


def test_doc_states_frozen_contract() -> None:
    document = (Path(__file__).parents[2] / "docs/contracts/hook-ingress.md").read_text(
        encoding="utf-8"
    )
    statements = (
        "POST /api/hooks/execute",
        "Authorization: Bearer",
        '"schema_version": 1',
        '"hook_type"',
        '"input_data"',
        '"source"',
        '"critical"',
        '"enqueued_at"',
        '"response_capability"',
        "X-Gobby-Envelope-Id",
        "X-Gobby-Session-Id",
        "hook-response.v1",
        "hook-response capability below floor",
        '"machine_id"',
        "HTTP 403",
        '{"code":"hook_machine_mismatch"}',
        "HTTP 400",
        '{"detail":"hook_type required"}',
        '{"detail":"source required"}',
        '{"detail":"Unsupported source: unsupported. Supported: claude, grok, codex, droid, agy"}',
        "Unsupported schema_version: None. Supported: 1",
        "HTTP 200",
        "HTTP 409",
        '"status":"processing"',
        '"status":"processed"',
        '"status":"malformed_marker"',
        "HTTP 503",
        '{"detail":"HookManager not initialized"}',
        "not a retry envelope",
        '"status":"retry"',
        '"retry_kind":"preflight_timeout"',
        '"retry_kind":"ingress_backpressure"',
        '"retry_kind":"adapter_timeout"',
        "_gobby_delivery_receipt",
        '"kind": "delivery-receipt"',
        '"receipt_id"',
        '"original_envelope_id"',
        '"delivery_generation"',
        "120-second grace",
        ".stop_replay_epoch",
        "## Node forwarding",
        "node-halves.md#15-",
    )
    for statement in statements:
        assert statement in document, f"Missing frozen contract statement: {statement}"
