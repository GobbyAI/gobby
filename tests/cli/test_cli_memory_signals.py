"""Tests for the recall-signals CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from gobby.cli.memory.signals import recall_signals
from gobby.memory.recall_constants import RECALL_QUERY_CONSTRUCTION_VERSION
from gobby.memory.recall_fit import split_request_ids_per_project
from gobby.memory.services._search_constants import _GRAPH_CONFIDENCE_SELECTION_FLOOR
from gobby.memory.shadow_relevance import SHADOW_PROTOCOL_VERSION
from gobby.storage.recall_shadow_signals import ShadowCohortAmbiguityError

pytestmark = pytest.mark.unit


def _invoke_backfill(args: list[str]) -> tuple[MagicMock, object]:
    store = MagicMock()
    store.load_signal_events_jsonl.return_value = 2
    with (
        patch("gobby.cli.memory.signals.require_cli_database") as mock_open_db,
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
    ):
        mock_open_db.return_value = MagicMock()
        result = CliRunner().invoke(recall_signals, ["backfill-events", *args])
    return store, result


def test_backfill_default_loads_rotated_files_oldest_first(tmp_path: Path) -> None:
    live = tmp_path / "recall_signal.jsonl"
    backup_one = tmp_path / "recall_signal.jsonl.1"
    backup_two = tmp_path / "recall_signal.jsonl.2"
    for file in (live, backup_one, backup_two):
        file.write_text("{}\n", encoding="utf-8")

    with patch("gobby.cli.memory.signals.resolve_recall_signal_path", return_value=live):
        store, result = _invoke_backfill([])

    assert result.exit_code == 0, result.output
    loaded = [call.args[0] for call in store.load_signal_events_jsonl.call_args_list]
    assert loaded == [backup_two, backup_one, live]


def test_backfill_explicit_path_loads_only_that_file(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.jsonl"
    explicit.write_text("{}\n", encoding="utf-8")

    store, result = _invoke_backfill(["--path", str(explicit)])

    assert result.exit_code == 0, result.output
    loaded = [call.args[0] for call in store.load_signal_events_jsonl.call_args_list]
    assert loaded == [explicit]


def test_backfill_errors_when_no_log_exists(tmp_path: Path) -> None:
    with patch(
        "gobby.cli.memory.signals.resolve_recall_signal_path",
        return_value=tmp_path / "recall_signal.jsonl",
    ):
        store, result = _invoke_backfill([])

    assert result.exit_code != 0
    assert "No recall-signal log" in result.output
    store.load_signal_events_jsonl.assert_not_called()


_DATA_CUTOFF = "2026-07-17T12:00:00+00:00"
_COMPLETION_CUTOFF = "2026-07-17T13:00:00+00:00"


def _cohort_args(command: str) -> list[str]:
    return [
        command,
        "--label-source",
        "digest_shadow",
        "--protocol-version",
        SHADOW_PROTOCOL_VERSION,
        "--regime-key",
        "regime-v1",
        "--judge-model-key",
        "judge-v1",
        "--judge-config-fingerprint",
        "config-v1",
        "--data-cutoff",
        _DATA_CUTOFF,
        "--completion-cutoff",
        _COMPLETION_CUTOFF,
        "--candidate-scope",
        "full",
    ]


def _audit_rows(count: int = 100) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    cohorts: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    for index in range(count):
        request_id = f"req-{index:03d}"
        cohorts.append({"project_id": "project-1", "recall_request_id": request_id})
        for rank, useful in ((1, True), (5, False)):
            rows.append(
                {
                    "project_id": "project-1",
                    "recall_request_id": request_id,
                    "memory_id": f"mem-{request_id}-{rank}",
                    "rank": rank,
                    "judge_useful": useful,
                    "prompt_hash": f"prompt-{request_id}",
                    "system_prompt": "score relevance",
                    "query_text": f"query {request_id}",
                    "presented": [
                        {
                            "memory_id": f"mem-{request_id}-{rank}",
                            "excerpt": f"snapshot {request_id}/{rank}",
                        }
                    ],
                }
            )
    return cohorts, rows


def test_gate_forwards_exact_fences_and_writes_decision(tmp_path: Path) -> None:
    store = MagicMock()
    decision = SimpleNamespace(ship=True, to_record=lambda: {"ship": True, "decision_digest": "d1"})
    output_path = tmp_path / "decision.json"
    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
        patch("gobby.cli.memory.signals.run_ship_gate_from_store", return_value=decision) as run,
    ):
        result = CliRunner().invoke(
            recall_signals,
            [*_cohort_args("gate"), "--write-decision", str(output_path)],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ship"] is True
    assert json.loads(output_path.read_text())["decision_digest"] == "d1"
    kwargs = run.call_args.kwargs
    assert kwargs["judge_protocol_version"] == SHADOW_PROTOCOL_VERSION
    assert kwargs["query_construction_version"] == RECALL_QUERY_CONSTRUCTION_VERSION
    assert kwargs["data_cutoff"] == datetime(2026, 7, 17, 12, tzinfo=UTC)
    assert kwargs["completion_cutoff"] == datetime(2026, 7, 17, 13, tzinfo=UTC)
    assert kwargs["candidate_scope"] == "full"


def test_gate_exits_one_when_decision_rejects() -> None:
    decision = SimpleNamespace(ship=False, to_record=lambda: {"ship": False})
    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore"),
        patch("gobby.cli.memory.signals.run_ship_gate_from_store", return_value=decision),
    ):
        result = CliRunner().invoke(recall_signals, _cohort_args("gate"))

    assert result.exit_code == 1
    assert json.loads(result.output) == {"ship": False}


def test_gate_ambiguity_lists_per_cohort_counts() -> None:
    ambiguity = ShadowCohortAmbiguityError("judge_model_key", {"judge-a": 12, "judge-b": 7})
    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore"),
        patch("gobby.cli.memory.signals.run_ship_gate_from_store", side_effect=ambiguity),
    ):
        result = CliRunner().invoke(recall_signals, _cohort_args("gate"))

    assert result.exit_code == 1
    assert "judge_model_key" in result.output
    assert "judge-a: 12" in result.output
    assert "judge-b: 7" in result.output


def test_audit_labels_uses_deterministic_training_partition_sample() -> None:
    cohorts, rows = _audit_rows()
    store = MagicMock()
    store.shadow_cohort_query.return_value = cohorts
    store.fetch_shadow_replay_rows.return_value = rows

    outputs: list[dict[str, object]] = []
    for _ in range(2):
        with (
            patch("gobby.cli.memory.signals.require_cli_database"),
            patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
        ):
            result = CliRunner().invoke(recall_signals, _cohort_args("audit-labels"))
        assert result.exit_code == 0, result.output
        outputs.append(json.loads(result.output))

    assert outputs[0] == outputs[1]
    sample = outputs[0]["sample"]
    assert isinstance(sample, list)
    assert len(sample) == 50
    sampled_ids = {row["request_id"] for row in sample}
    assert len(sampled_ids) == 50
    train_ids, holdout_ids = split_request_ids_per_project(
        [("project-1", str(row["recall_request_id"])) for row in cohorts]
    )
    assert sampled_ids <= train_ids
    assert sampled_ids.isdisjoint(holdout_ids)
    assert all(row["presentation"]["presented"] for row in sample)
    replay_kwargs = store.fetch_shadow_replay_rows.call_args.kwargs
    assert replay_kwargs["phase"] == "audit_scored"
    assert replay_kwargs["query_construction_version"] == RECALL_QUERY_CONSTRUCTION_VERSION
    cohort_kwargs = store.shadow_cohort_query.call_args.kwargs
    assert cohort_kwargs["query_construction_version"] == RECALL_QUERY_CONSTRUCTION_VERSION


def test_supersede_legacy_cohort_reports_the_swept_row_count() -> None:
    """The pending pre-cutover backlog retires through a named idempotent command."""
    store = MagicMock()
    store.supersede_legacy_cohort.return_value = 7

    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
    ):
        result = CliRunner().invoke(
            recall_signals,
            [
                "supersede-legacy-cohort",
                "--label-source",
                "digest_shadow",
                "--protocol-version",
                SHADOW_PROTOCOL_VERSION,
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "label_source": "digest_shadow",
        "judge_protocol_version": SHADOW_PROTOCOL_VERSION,
        "superseded": 7,
    }
    assert store.supersede_legacy_cohort.call_args.kwargs == {
        "label_source": "digest_shadow",
        "judge_protocol_version": SHADOW_PROTOCOL_VERSION,
    }


def test_audit_labels_records_prompt_bound_agreement() -> None:
    cohorts, rows = _audit_rows()
    store = MagicMock()
    store.shadow_cohort_query.return_value = cohorts
    store.fetch_shadow_replay_rows.return_value = rows
    store.insert_audit_verdicts.return_value = 50
    store.fetch_audit_verdicts.side_effect = lambda cohort, sample, **kwargs: [
        {
            "cohort_digest": cohort,
            "sample_digest": sample,
            "request_id": request_id,
            "memory_id": memory_id,
            "prompt_hash": prompt_hash,
            "human_verdict": True,
        }
        for (request_id, memory_id), prompt_hash in kwargs["expected_prompt_hashes"].items()
    ]
    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
        patch("gobby.cli.memory.signals.click.confirm", return_value=True),
    ):
        result = CliRunner().invoke(
            recall_signals,
            [*_cohort_args("audit-labels"), "--record-agreement", "--reviewer", "human-1"],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agreement"]["unit_count"] == 50
    verdicts = store.insert_audit_verdicts.call_args.args[0]
    assert len(verdicts) == 50
    assert {row["reviewer"] for row in verdicts} == {"human-1"}
    assert all(row["prompt_hash"].startswith("prompt-req-") for row in verdicts)
    assert store.insert_audit_verdicts.call_args.kwargs == {
        "cohort_digest": payload["cohort_digest"],
        "sample_digest": payload["sample_digest"],
    }


def test_audit_labels_diagnostic_is_training_only_and_excludes_ship_statistic() -> None:
    cohorts, rows = _audit_rows()
    store = MagicMock()
    store.shadow_cohort_query.return_value = cohorts
    store.fetch_shadow_replay_rows.return_value = rows
    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
    ):
        result = CliRunner().invoke(
            recall_signals,
            [*_cohort_args("audit-labels"), "--diagnostic", "--n-requests", "12"],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "diagnostic"
    assert "agreement" not in payload
    assert len(payload["sample"]) == 12
    request_ids = {row["request_id"] for row in payload["sample"]}
    train_ids, holdout_ids = split_request_ids_per_project(
        [("project-1", str(row["recall_request_id"])) for row in cohorts]
    )
    assert request_ids <= train_ids
    assert request_ids.isdisjoint(holdout_ids)
    assert {row["diagnostic_cell"]["rank_band"] for row in payload["sample"]} == {
        "ranks_1_4",
        "ranks_5_8",
    }


def test_audit_labels_cutoff_change_produces_distinct_cohort_binding() -> None:
    cohorts, rows = _audit_rows()
    store = MagicMock()
    store.shadow_cohort_query.return_value = cohorts
    store.fetch_shadow_replay_rows.return_value = rows

    payloads: list[dict[str, object]] = []
    for completion_cutoff in (_COMPLETION_CUTOFF, "2026-07-17T14:00:00+00:00"):
        args = _cohort_args("audit-labels")
        args[args.index("--completion-cutoff") + 1] = completion_cutoff
        with (
            patch("gobby.cli.memory.signals.require_cli_database"),
            patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
        ):
            result = CliRunner().invoke(recall_signals, args)
        assert result.exit_code == 0, result.output
        payloads.append(json.loads(result.output))

    assert payloads[0]["cohort_digest"] != payloads[1]["cohort_digest"]
    assert payloads[0]["sample_digest"] != payloads[1]["sample_digest"]


def _replay_rows() -> list[dict[str, object]]:
    """Two labeled requests: one topically matched, one not."""
    rows: list[dict[str, object]] = []
    for request_id, query, candidates in (
        (
            "req-1",
            "postgres connection pool exhaustion",
            (("mem-1", "postgres connection pool exhaustion", 0.90, True),),
        ),
        (
            "req-2",
            "tailscale funnel certificate renewal",
            (("mem-2", "worktree cleanup after a merge", 0.95, False),),
        ),
    ):
        presented = [
            {"memory_id": memory_id, "excerpt": excerpt}
            for memory_id, excerpt, _similarity, _useful in candidates
        ]
        for rank, (memory_id, _excerpt, similarity, useful) in enumerate(candidates):
            rows.append(
                {
                    "project_id": "project-1",
                    "recall_request_id": request_id,
                    "memory_id": memory_id,
                    "rank": rank,
                    "similarity": similarity,
                    # Undecayed, so the score the static arm thresholds on is
                    # the same number it orders by.
                    "temporal_decay_factor": 1.0,
                    "judge_useful": useful,
                    "query_text": query,
                    "presented": presented,
                }
            )
    return rows


def _invoke_replay(extra: list[str]) -> tuple[MagicMock, Result]:
    store = MagicMock()
    store.fetch_shadow_replay_rows.return_value = _replay_rows()
    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
    ):
        result = CliRunner().invoke(
            recall_signals,
            [*_cohort_args("replay-candidate-filter"), *extra],
        )
    return store, result


def test_replay_candidate_filter_writes_the_report_json_to_out(tmp_path: Path) -> None:
    out_path = tmp_path / "candidate-filter.json"

    _store, result = _invoke_replay(["--static-min-similarity", "0.65", "--out", str(out_path)])

    assert result.exit_code == 0, result.output
    written = json.loads(out_path.read_text())
    assert written == json.loads(result.output)
    assert written["report_version"] == "recall-candidate-filter-replay-v2"


def test_replay_candidate_filter_report_names_its_cohort_identity() -> None:
    _store, result = _invoke_replay(["--static-min-similarity", "0.65"])

    assert result.exit_code == 0, result.output
    identity = json.loads(result.output)["cohort_identity"]
    assert identity["query_construction_version"] == RECALL_QUERY_CONSTRUCTION_VERSION
    assert identity["judge_protocol_version"] == SHADOW_PROTOCOL_VERSION
    assert identity["label_source"] == "digest_shadow"
    assert identity["candidate_scope"] == "full"


def test_replay_candidate_filter_reports_both_arms_request_level() -> None:
    _store, result = _invoke_replay(["--static-min-similarity", "0.65"])

    assert result.exit_code == 0, result.output
    arms = json.loads(result.output)["arms"]
    required = {
        "requests_evaluated",
        "abstention_rate",
        "abstain_correct",
        "abstain_regret",
        "mean_selected",
        "pairwise_accuracy",
        "pairwise_requests",
    }
    for arm in ("candidate_filter", "static_constants"):
        assert required <= set(arms[arm]), arm
    # The filter abstains on the unrelated request; static admits both.
    assert arms["candidate_filter"]["abstention_rate"] == 0.5
    assert arms["static_constants"]["abstention_rate"] == 0.0


def test_replay_candidate_filter_forwards_the_exact_cohort_fences() -> None:
    store, result = _invoke_replay(["--static-min-similarity", "0.65"])

    assert result.exit_code == 0, result.output
    kwargs = store.fetch_shadow_replay_rows.call_args.kwargs
    assert kwargs["judge_protocol_version"] == SHADOW_PROTOCOL_VERSION
    assert kwargs["query_construction_version"] == RECALL_QUERY_CONSTRUCTION_VERSION
    assert kwargs["data_cutoff"] == datetime(2026, 7, 17, 12, tzinfo=UTC)
    assert kwargs["completion_cutoff"] == datetime(2026, 7, 17, 13, tzinfo=UTC)
    assert kwargs["candidate_scope"] == "full"


def test_replay_candidate_filter_defaults_the_static_arm_to_selection_min_score() -> None:
    runtime = SimpleNamespace(
        config=SimpleNamespace(memory_recall=SimpleNamespace(selection_min_score=0.92))
    )
    with patch("gobby.cli.memory.signals.get_cli_runtime", return_value=runtime):
        _store, result = _invoke_replay([])

    assert result.exit_code == 0, result.output
    arms = json.loads(result.output)["arms"]
    assert arms["static_constants"]["selection_threshold"] == 0.92


def test_replay_candidate_filter_records_the_graph_confidence_floor_it_ran_under() -> None:
    """#20879: the static arm gates two axes and the report has to name both.

    `selection_threshold` carries only the cosine floor, so a report that did
    not record the confidence floor beside it could not be told apart from one
    run under a different gate -- which is precisely how the 0.65 fossil
    #20771 unwound survived as long as it did.
    """
    _store, result = _invoke_replay(
        ["--static-min-similarity", "0.65", "--static-graph-confidence-min-score", "0.71"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["static_graph_confidence_min_score"] == 0.71


def test_replay_candidate_filter_defaults_the_graph_confidence_floor_to_the_shipped_one() -> None:
    _store, result = _invoke_replay(["--static-min-similarity", "0.65"])

    assert result.exit_code == 0, result.output
    assert (
        json.loads(result.output)["static_graph_confidence_min_score"]
        == _GRAPH_CONFIDENCE_SELECTION_FLOOR
    )


def test_replay_candidate_filter_surfaces_an_ambiguous_cohort() -> None:
    store = MagicMock()
    store.fetch_shadow_replay_rows.side_effect = ShadowCohortAmbiguityError(
        "judge_model_key", {"judge-v1": 3, "judge-v2": 4}
    )
    with (
        patch("gobby.cli.memory.signals.require_cli_database"),
        patch("gobby.cli.memory.signals.RecallSignalStore", return_value=store),
    ):
        result = CliRunner().invoke(
            recall_signals,
            [*_cohort_args("replay-candidate-filter"), "--static-min-similarity", "0.65"],
        )

    assert result.exit_code != 0
    assert "Ambiguous judge_model_key cohorts" in result.output
