"""Contract tests for the frozen native Ask cohort harness."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import yaml
from ask_cohort import (
    EXPECTED_TOOL_IDENTITIES,
    AttemptError,
    CommandResult,
    PreparationError,
    cohort_contract,
    prepare_cohort,
    primary_accounting,
    run_primary,
    run_supplement,
)
from ask_scoring import (
    Q14_CHANGED_PATHS,
    build_answer_score,
    build_review_packet,
    citation_integrity,
    render_report,
    score_packet,
    score_retrieval,
    scoring_contract,
)

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import ProfileSnapshot
from gobby.ask.publication import publish_answer
from gobby.ask.validation import validate_claims, validate_review
from gobby.workflows.agent_models import AgentDefinitionBody

EXPECTED_QUESTIONS = (
    ("Q01", "What is the shared platform, and which systems remain standalone?"),
    ("Q02", "Which data source is authoritative and what is PostgreSQL's role?"),
    ("Q03", "How does a paged synchronization protect progress?"),
    ("Q04", "What exactly does the daily replenishment cadence do?"),
    ("Q05", "What is the weekly lane order and why does order matter?"),
    ("Q06", "What prevents an unreviewed run from mutating Lightspeed?"),
    ("Q07", "What is in the vendor workbook and when is it uploaded?"),
    ("Q08", "What recovery behavior is implemented after interruption?"),
    ("Q09", "What does Restocks read, calculate, and publish?"),
    ("Q10", "What does Buylist produce and where does it publish?"),
    ("Q11", "How does Buylist catalog refresh and failure retention work?"),
    ("Q12", "Is Buylist already part of the shared platform?"),
    ("Q13", "Which artifacts express intent rather than implemented truth?"),
    ("Q14", "What changed at the C3 commit?"),
)

REPO_ROOT = Path(__file__).parents[3]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _publication_export(attempt_dir: Path) -> dict[str, object]:
    answer = {
        "schema_version": 1,
        "run_id": "run-q01",
        "outcome": "unknown",
        "question": EXPECTED_QUESTIONS[0][1],
        "claims": [],
    }
    evidence = {
        "schema_version": 1,
        "run_id": "run-q01",
        "snapshot_binding": {},
        "inventory": {},
        "records": [],
    }
    payloads = {
        "answer.json": json.dumps(answer, sort_keys=True).encode(),
        "evidence-manifest.json": json.dumps(evidence, sort_keys=True).encode(),
    }
    manifest = {
        "schema_version": 1,
        "run_id": "run-q01",
        "files": [
            {"path": path, "sha256": _sha256(payload), "size_bytes": len(payload)}
            for path, payload in sorted(payloads.items())
        ],
    }
    payloads["manifest.json"] = json.dumps(manifest, sort_keys=True).encode()
    export_dir = attempt_dir / "export"
    export_dir.mkdir()
    tar_path = export_dir / "run-q01.tar"
    with tarfile.open(tar_path, "w") as archive:
        for path, payload in sorted(payloads.items()):
            info = tarfile.TarInfo(path)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return {
        "tar_path": str(tar_path),
        "tar_sha256": _sha256(tar_path.read_bytes()),
        "files": [
            {"path": path, "sha256": _sha256(payload), "size_bytes": len(payload)}
            for path, payload in sorted(payloads.items())
        ],
    }


def _profile(identifier: str, digest: str) -> dict[str, object]:
    definition_path = (
        REPO_ROOT
        / "src"
        / "gobby"
        / "install"
        / "shared"
        / "workflows"
        / "agents"
        / f"{identifier}.yaml"
    )
    body = AgentDefinitionBody.model_validate(yaml.safe_load(definition_path.read_bytes()))
    snapshot = ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-10T12:00:00+00:00",
        effective=body.model_dump(mode="json"),
        content_hash=digest,
    )
    return cast(dict[str, object], snapshot.model_dump(mode="json"))


def _receipt(path: Path, payload: object) -> dict[str, str]:
    encoded = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(encoded)
    path.chmod(0o600)
    return {"path": str(path.resolve()), "sha256": _sha256(encoded)}


def _runtime_identity(gcode_bytes: bytes, root: Path) -> dict[str, object]:
    gobby_home = root / "contained-gobby-home"
    gobby_home.mkdir(mode=0o700)
    bootstrap = gobby_home / "bootstrap.yaml"
    bootstrap.write_text("daemon_port: 61999\nbind_host: 127.0.0.1\n", encoding="utf-8")
    bootstrap.chmod(0o600)
    daemon_url = "http://127.0.0.1:61999"
    database = {
        "host": "127.0.0.1",
        "port": 60892,
        "name": "gobby_test",
        "schema": "gobby_test_askcohort_unit",
    }
    service = {"identity": "ask-daemon-unit", "daemon_url": daemon_url}
    return {
        "schema_version": 1,
        "project_id": "project-frozen-ask",
        "gcode": {
            "version": "gcode 9.9.9",
            "contract_version": 10,
            "executable_sha256": _sha256(gcode_bytes),
        },
        "profiles": {
            "investigator": _profile("ask-investigator", "1" * 64),
            "reviewer": _profile("ask-reviewer", "2" * 64),
        },
        "tool_identities": list(EXPECTED_TOOL_IDENTITIES),
        "isolation": {
            "mode": "contained",
            "daemon_url": daemon_url,
            "gobby_home": str(gobby_home.resolve()),
            "bootstrap": {
                "path": str(bootstrap.resolve()),
                "sha256": _sha256(bootstrap.read_bytes()),
            },
            "database": {**database, "receipt": _receipt(root / "database.json", database)},
            "service": {**service, "receipt": _receipt(root / "service.json", service)},
        },
        "execution_gates": {
            "#22018": {"status": "accepted", "evidence_sha256": "3" * 64},
            "#22019": {"status": "accepted", "evidence_sha256": "4" * 64},
            "normal_loader_runtime_admission": {
                "status": "accepted",
                "evidence_sha256": "5" * 64,
            },
            "installed_cli_acceptance": {
                "status": "accepted",
                "evidence_sha256": "6" * 64,
            },
        },
    }


def _contract() -> dict[str, object]:
    return {
        "tool": "gcode",
        "contract_version": 10,
        "commands": [
            {
                "name": "ask",
                "positionals": [{"name": "QUESTION", "required": False}],
                "flags": [
                    {"name": name, "allowed_values": allowed}
                    for name, allowed in [
                        ("--commit", []),
                        ("--timeout-seconds", []),
                        ("--retrieval", ["deterministic", "hybrid"]),
                        ("--background", []),
                        ("--status", []),
                        ("--resume", []),
                        ("--cancel", []),
                        ("--export", []),
                        ("--output", []),
                        ("--format", ["json", "text"]),
                    ]
                ],
                "json_output_keys": [
                    "run_id",
                    "status",
                    "current_stage",
                    "answer_outcome",
                    "typed_error",
                    "deadline_at",
                    "profile_identities",
                    "tool_identities",
                    "artifact_manifest",
                    "attempt_count",
                    "repair_count",
                    "binding",
                    "evidence",
                    "result_artifact",
                    "usage",
                    "output",
                ],
            }
        ],
    }


def _prepare(tmp_path: Path) -> Path:
    source_root = tmp_path / "source"
    source_root.mkdir()
    gcode = tmp_path / "gcode"
    gcode_bytes = b"fake-gcode-binary"
    gcode.write_bytes(gcode_bytes)
    identity = tmp_path / "runtime-identity.json"
    identity.write_text(json.dumps(_runtime_identity(gcode_bytes, tmp_path)), encoding="utf-8")
    trees = {
        "0216f1e33f05962d49467d95fe84609041c6dba8": "7" * 40,
        "8b24ac26699aac8b24254a647aa70b208287b492": "8" * 40,
    }

    def preflight(
        argv: tuple[str, ...], timeout: float, environment: Mapping[str, str]
    ) -> CommandResult:
        assert timeout > 0
        assert environment["GOBBY_DAEMON_URL"] == "http://127.0.0.1:61999"
        assert environment["GOBBY_HOME"] == str((tmp_path / "contained-gobby-home").resolve())
        assert environment["GOBBY_TEST_PROTECT"] == "1"
        assert "DATABASE_URL" not in environment
        if argv == (str(gcode.resolve()), "--version"):
            stdout = b"gcode 9.9.9\n"
        elif argv == (str(gcode.resolve()), "contract", "--format", "json"):
            stdout = json.dumps(_contract()).encode()
        elif argv[-1] == "8b24ac26699aac8b24254a647aa70b208287b492^1":
            stdout = b"0216f1e33f05962d49467d95fe84609041c6dba8\n"
        elif "diff-tree" in argv:
            stdout = "".join(
                f"{status}\t{path}\n"
                for status, path in [
                    ("M", "config/replenishment.toml"),
                    ("M", "docs/replenishment.md"),
                    ("M", "src/game_goblins/platform/settings.py"),
                    ("M", "src/game_goblins/replenishment/daily.py"),
                    ("M", "src/game_goblins/replenishment/planner.py"),
                    ("M", "src/game_goblins/replenishment/store_targets.py"),
                    ("M", "tests/platform/test_settings.py"),
                    ("M", "tests/replenishment/test_planning_store.py"),
                    ("A", "tests/replenishment/test_store_targets.py"),
                ]
            ).encode()
        elif argv[-1].endswith("^{commit}"):
            stdout = f"{argv[-1][:-9]}\n".encode()
        elif argv[-1].endswith("^{tree}"):
            stdout = f"{trees[argv[-1][:-7]]}\n".encode()
        else:
            raise AssertionError(f"unexpected preflight command: {argv}")
        return CommandResult(exit_code=0, stdout=stdout, stderr=b"", wall_seconds=0.01)

    return prepare_cohort(
        runtime_identity_path=identity,
        gcode_binary=gcode,
        project_root=source_root,
        output_root=tmp_path / "cohort",
        command_runner=preflight,
        now=lambda: "2026-09-10T12:30:00+00:00",
    )


def _failed_result(question_id: str, commit: str) -> dict[str, object]:
    return {
        "run_id": f"run-{question_id.lower()}",
        "status": "failed",
        "current_stage": "investigate",
        "answer_outcome": None,
        "typed_error": {"code": "agent_failed", "message": "recorded failure"},
        "deadline_at": "2026-09-10T12:40:00+00:00",
        "profile_identities": {
            "investigator": "ask-investigator",
            "reviewer": "ask-reviewer",
        },
        "tool_identities": list(EXPECTED_TOOL_IDENTITIES),
        "binding": {
            "project_id": "project-frozen-ask",
            "commit_oid": commit,
            "tree_oid": "7" * 40 if question_id != "Q14" else "8" * 40,
            "retrieval_mode": "deterministic",
        },
        "artifact_manifest": None,
        "attempt_count": 1,
        "repair_count": 0,
        "evidence": [],
        "result_artifact": None,
        "usage": None,
        "output": None,
    }


def _completed_result(question_id: str, commit: str) -> dict[str, object]:
    result = _failed_result(question_id, commit)
    result.update(
        {
            "status": "completed",
            "current_stage": "publish",
            "answer_outcome": "unknown",
            "typed_error": None,
        }
    )
    return result


def test_frozen_cohort_and_scoring_contract() -> None:
    cohort = cohort_contract()
    scoring = scoring_contract()
    runner_source = Path(__file__).with_name("ask_cohort.py").read_text(encoding="utf-8")

    assert tuple((item["id"], item["question"]) for item in cohort["questions"]) == (
        EXPECTED_QUESTIONS
    )
    assert {item["source_commit"] for item in cohort["questions"][:13]} == {
        "0216f1e33f05962d49467d95fe84609041c6dba8"
    }
    assert cohort["questions"][13]["source_commit"] == ("8b24ac26699aac8b24254a647aa70b208287b492")
    assert cohort["timeout_seconds"] == 600
    assert cohort["retrieval_mode"] == "deterministic"
    assert cohort["profiles"] == ["ask-investigator", "ask-reviewer"]
    assert all(set(item) == {"id", "question", "source_commit"} for item in cohort["questions"])

    assert scoring["historical_retrieval_hits"] == [
        "Q01",
        "Q07",
        "Q10",
        "Q11",
        "Q12",
        "Q13",
    ]
    assert scoring["expected_answer_class"]["Q05"] == "inferred"
    assert scoring["expected_answer_class"]["Q08"] == "unknown"
    assert scoring["exact_checks"]["Q14"] == {
        "automatic_non_excluded_scope": "required",
        "case_sensitive": True,
        "changed_path_count": 9,
        "hobby_supplies_minimum": 2,
        "longest_prefix_precedence": "required",
        "sleeves_prefix": "Sleeves: ",
        "sleeves_prefix_minimum": 4,
    }
    assert all(
        marker not in runner_source
        for marker in ("GOLD_COMPONENTS", "DOMAIN_SPANS", "hobby_supplies_minimum")
    )


def test_prepare_freezes_cli_source_profiles_and_execution_gates(tmp_path: Path) -> None:
    manifest_path = _prepare(tmp_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = cast(dict[str, object], json.loads(manifest_bytes))

    assert manifest["prepared_at"] == "2026-09-10T12:30:00+00:00"
    assert cast(dict[str, object], manifest["gcode"])["contract_version"] == 10
    assert cast(dict[str, object], manifest["source"])["commits"] == {
        "0216f1e33f05962d49467d95fe84609041c6dba8": {"tree_oid": "7" * 40},
        "8b24ac26699aac8b24254a647aa70b208287b492": {
            "tree_oid": "8" * 40,
            "first_parent_oid": "0216f1e33f05962d49467d95fe84609041c6dba8",
            "first_parent_changes": [
                {"status": "M", "path": "config/replenishment.toml"},
                {"status": "M", "path": "docs/replenishment.md"},
                {"status": "M", "path": "src/game_goblins/platform/settings.py"},
                {"status": "M", "path": "src/game_goblins/replenishment/daily.py"},
                {"status": "M", "path": "src/game_goblins/replenishment/planner.py"},
                {"status": "M", "path": "src/game_goblins/replenishment/store_targets.py"},
                {"status": "M", "path": "tests/platform/test_settings.py"},
                {"status": "M", "path": "tests/replenishment/test_planning_store.py"},
                {"status": "A", "path": "tests/replenishment/test_store_targets.py"},
            ],
        },
    }
    runtime = cast(dict[str, object], manifest["runtime_identity"])
    profiles = cast(dict[str, dict[str, object]], runtime["profiles"])
    assert profiles["investigator"]["content_hash"] == "1" * 64
    reviewer_effective = cast(dict[str, object], profiles["reviewer"]["effective"])
    assert reviewer_effective["blocked_tools"]
    assert reviewer_effective["prompts"]
    assert reviewer_effective["step_workflow"]
    isolation = cast(dict[str, object], runtime["isolation"])
    assert isolation["mode"] == "contained"
    assert isolation["daemon_url"] == "http://127.0.0.1:61999"
    execution = cast(dict[str, object], manifest["execution"])
    assert execution["environment"] == {
        "GOBBY_DAEMON_URL": "http://127.0.0.1:61999",
        "GOBBY_HOME": str((tmp_path / "contained-gobby-home").resolve()),
        "GOBBY_TEST_PROTECT": "1",
    }
    assert isinstance(execution["environment_sha256"], str)
    assert manifest_path.with_suffix(".sha256").read_text(encoding="ascii").strip() == (
        _sha256(manifest_bytes)
    )


def test_prepare_preserves_profiles_serialized_by_production_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    from tests.ask.test_validation import _valid_case

    manifest_path = _prepare(tmp_path)
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    runtime = cast(dict[str, object], manifest["runtime_identity"])
    prepared_profiles = cast(dict[str, object], runtime["profiles"])
    production_profiles = {
        "investigator": _profile("ask-investigator", "1" * 64),
        "reviewer": _profile("ask-reviewer", "2" * 64),
    }

    draft, evidence, blobs, review = _valid_case()
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    published = publish_answer(
        AskArtifactStore(tmp_path / "published", "project", draft.run_id),
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.snapshot_binding.model_dump(mode="json"),
        profiles=production_profiles,
        tool_identities=("gobby-code@0.5.0",),
        attempt_history=({"attempt": 1, "status": "reviewed"},),
    )
    publication = cast(
        dict[str, object], json.loads((published.root / "manifest.json").read_bytes())
    )
    provenance = cast(dict[str, object], publication["provenance"])

    assert prepared_profiles == provenance["profiles"] == production_profiles


def test_prepare_refuses_unaccepted_final_prerequisite(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    gcode = tmp_path / "gcode"
    gcode.write_bytes(b"fake-gcode-binary")
    identity_value = _runtime_identity(b"fake-gcode-binary", tmp_path)
    gates = cast(dict[str, dict[str, str]], identity_value["execution_gates"])
    gates["#22019"]["status"] = "pending"
    identity = tmp_path / "runtime-identity.json"
    identity.write_text(json.dumps(identity_value), encoding="utf-8")

    with pytest.raises(PreparationError, match="#22019.*accepted"):
        prepare_cohort(
            runtime_identity_path=identity,
            gcode_binary=gcode,
            project_root=source_root,
            output_root=tmp_path / "cohort",
            command_runner=lambda _argv, _timeout, _environment: pytest.fail(
                "must fail before commands"
            ),
        )


def test_runtime_isolation_drift_is_recorded_before_primary_and_export(tmp_path: Path) -> None:
    manifest_path = _prepare(tmp_path)
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    runtime = cast(dict[str, object], manifest["runtime_identity"])
    isolation = cast(dict[str, object], runtime["isolation"])
    service = cast(dict[str, object], isolation["service"])
    receipt = cast(dict[str, str], service["receipt"])
    receipt_path = Path(receipt["path"])
    receipt_bytes = receipt_path.read_bytes()
    receipt_path.write_text('{"identity":"different-service"}', encoding="utf-8")

    def never_run(
        _argv: tuple[str, ...], _timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        pytest.fail("isolation drift must fail before the primary process")

    with pytest.raises(PreparationError, match="service receipt"):
        run_primary(manifest_path, command_runner=never_run)

    primary = manifest_path.parent / "attempts" / "Q01" / "primary"
    assert not primary.exists()

    receipt_path.write_bytes(receipt_bytes)
    receipt_path.chmod(0o600)

    def drift_after_primary(
        argv: tuple[str, ...], _timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        assert "--export" not in argv
        receipt_path.write_text('{"identity":"different-service"}', encoding="utf-8")
        return CommandResult(
            exit_code=0,
            stdout=json.dumps(
                _completed_result("Q01", "0216f1e33f05962d49467d95fe84609041c6dba8")
            ).encode(),
            stderr=b"",
            wall_seconds=1.0,
        )

    with pytest.raises(PreparationError, match="service receipt"):
        run_primary(manifest_path, command_runner=drift_after_primary)

    before_export = cast(
        dict[str, object], json.loads(primary.joinpath("outcome.json").read_bytes())
    )
    assert before_export["disposition"] == "contract_error"
    assert cast(dict[str, str], before_export["error"])["stage"] == "export"


def test_primary_runner_is_serial_exact_and_append_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-leak")
    monkeypatch.setenv("GOBBY_PORT", "1")
    monkeypatch.setenv("GOBBY_SESSION_ID", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    manifest_path = _prepare(tmp_path)
    calls: list[tuple[str, ...]] = []

    def invoke(
        argv: tuple[str, ...], timeout: float, environment: Mapping[str, str]
    ) -> CommandResult:
        assert timeout == 630
        assert environment["GOBBY_DAEMON_URL"] == "http://127.0.0.1:61999"
        assert {
            "DATABASE_URL",
            "GOBBY_PORT",
            "GOBBY_SESSION_ID",
            "OPENAI_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
        }.isdisjoint(environment)
        calls.append(argv)
        question_id = f"Q{len(calls):02d}"
        commit = EXPECTED_QUESTIONS[len(calls) - 1][0]
        source_commit = (
            "8b24ac26699aac8b24254a647aa70b208287b492"
            if commit == "Q14"
            else "0216f1e33f05962d49467d95fe84609041c6dba8"
        )
        return CommandResult(
            exit_code=2,
            stdout=json.dumps(_failed_result(question_id, source_commit)).encode(),
            stderr=b"Ask failed\n",
            wall_seconds=float(len(calls)),
        )

    accounting = run_primary(manifest_path, command_runner=invoke)

    assert [row["question_id"] for row in accounting] == [f"Q{i:02d}" for i in range(1, 15)]
    assert len(calls) == 14
    for index, argv in enumerate(calls):
        identifier, prompt = EXPECTED_QUESTIONS[index]
        expected_commit = (
            "8b24ac26699aac8b24254a647aa70b208287b492"
            if identifier == "Q14"
            else "0216f1e33f05962d49467d95fe84609041c6dba8"
        )
        assert argv[5:] == (
            "ask",
            prompt,
            "--commit",
            expected_commit,
            "--timeout-seconds",
            "600",
            "--retrieval",
            "deterministic",
        )
        assert all("query" not in argument and "turn" not in argument for argument in argv)

    first = manifest_path.parent / "attempts" / "Q01" / "primary" / "attempt.json"
    first_bytes = first.read_bytes()
    assert run_primary(manifest_path, command_runner=invoke) == accounting
    assert len(calls) == 14
    assert first.read_bytes() == first_bytes


def test_primary_rechecks_installed_binary_before_every_invocation(tmp_path: Path) -> None:
    manifest_path = _prepare(tmp_path)
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    binary = Path(cast(str, cast(dict[str, object], manifest["gcode"])["path"]))
    accepted_bytes = binary.read_bytes()
    calls: list[tuple[str, ...]] = []

    def replace_after_first(
        argv: tuple[str, ...], timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        assert timeout == 630
        calls.append(argv)
        binary.write_bytes(b"replaced-gcode-binary")
        return CommandResult(
            exit_code=2,
            stdout=json.dumps(
                _failed_result("Q01", "0216f1e33f05962d49467d95fe84609041c6dba8")
            ).encode(),
            stderr=b"Ask failed\n",
            wall_seconds=1.0,
        )

    with pytest.raises(PreparationError, match="installed CLI acceptance"):
        run_primary(manifest_path, command_runner=replace_after_first)

    assert len(calls) == 1
    binary.write_bytes(accepted_bytes)
    accounting = primary_accounting(manifest_path)
    assert [row["disposition"] for row in accounting[:3]] == [
        "failed",
        "contract_error",
        "unrun",
    ]
    q02 = manifest_path.parent / "attempts" / "Q02" / "primary"
    preserved_attempt = q02.joinpath("attempt.json").read_bytes()
    preserved_outcome = q02.joinpath("outcome.json").read_bytes()

    resumed_calls: list[tuple[str, ...]] = []

    def resume(
        argv: tuple[str, ...], timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        resumed_calls.append(argv)
        question_index = len(resumed_calls) + 1
        question_id = f"Q{question_index + 1:02d}"
        commit = (
            "8b24ac26699aac8b24254a647aa70b208287b492"
            if question_id == "Q14"
            else "0216f1e33f05962d49467d95fe84609041c6dba8"
        )
        return CommandResult(
            exit_code=2,
            stdout=json.dumps(_failed_result(question_id, commit)).encode(),
            stderr=b"Ask failed\n",
            wall_seconds=1.0,
        )

    run_primary(manifest_path, command_runner=resume)

    assert len(resumed_calls) == 12
    assert EXPECTED_QUESTIONS[2][1] in resumed_calls[0]
    assert q02.joinpath("attempt.json").read_bytes() == preserved_attempt
    assert q02.joinpath("outcome.json").read_bytes() == preserved_outcome


def test_supplement_rechecks_installed_binary_immediately_before_invocation(
    tmp_path: Path,
) -> None:
    manifest_path = _prepare(tmp_path)
    manifest = cast(dict[str, object], json.loads(manifest_path.read_bytes()))
    binary = Path(cast(str, cast(dict[str, object], manifest["gcode"])["path"]))
    accepted_bytes = binary.read_bytes()
    primary = manifest_path.parent / "attempts" / "Q01" / "primary"
    primary.mkdir(parents=True)
    primary.joinpath("attempt.json").write_text("{}", encoding="utf-8")
    invoked = False

    def replace_after_manifest_load() -> str:
        binary.write_bytes(b"replaced-gcode-binary")
        return "2026-09-10T13:00:00+00:00"

    def unexpected_invocation(
        argv: tuple[str, ...], timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        nonlocal invoked
        invoked = True
        raise AssertionError((argv, timeout))

    with pytest.raises(PreparationError, match="installed CLI acceptance"):
        run_supplement(
            manifest_path,
            question_id="Q01",
            kind="retry",
            reason="diagnosed retry",
            command_runner=unexpected_invocation,
            now=replace_after_manifest_load,
        )

    assert invoked is False
    supplement = manifest_path.parent / "attempts" / "Q01" / "retry-001"
    assert json.loads(supplement.joinpath("outcome.json").read_bytes())["disposition"] == (
        "contract_error"
    )
    binary.write_bytes(accepted_bytes)
    with pytest.raises(AttemptError, match="only one retry supplement"):
        run_supplement(
            manifest_path,
            question_id="Q01",
            kind="retry",
            reason="must not replace the failed supplement",
            command_runner=unexpected_invocation,
        )


def test_interruption_and_retry_are_separate_from_primary(tmp_path: Path) -> None:
    manifest_path = _prepare(tmp_path)

    def interrupted(
        argv: tuple[str, ...], timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        assert argv
        assert timeout == 630
        return CommandResult(
            exit_code=None,
            stdout=b"partial output",
            stderr=b"",
            wall_seconds=630.0,
            termination_signal="SIGTERM",
            interruption="runner_timeout",
        )

    run_primary(manifest_path, command_runner=interrupted)
    primary = manifest_path.parent / "attempts" / "Q01" / "primary"
    primary_attempt = primary.joinpath("attempt.json").read_bytes()
    primary_outcome = primary.joinpath("outcome.json").read_bytes()

    retry = run_supplement(
        manifest_path,
        question_id="Q01",
        kind="retry",
        reason="operator-approved diagnosed retry",
        command_runner=interrupted,
    )

    assert retry["attempt_kind"] == "retry"
    assert retry["attempt_index"] == 1
    assert primary.joinpath("attempt.json").read_bytes() == primary_attempt
    assert primary.joinpath("outcome.json").read_bytes() == primary_outcome
    assert (manifest_path.parent / "attempts" / "Q01" / "retry-001").is_dir()


def test_export_oserror_is_typed_and_primary_is_not_replaced_on_resume(tmp_path: Path) -> None:
    manifest_path = _prepare(tmp_path)

    def export_failure(
        argv: tuple[str, ...], _timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        if "--export" in argv:
            raise OSError("export transport unavailable")
        if EXPECTED_QUESTIONS[0][1] in argv:
            return CommandResult(
                exit_code=0,
                stdout=json.dumps(
                    _completed_result("Q01", "0216f1e33f05962d49467d95fe84609041c6dba8")
                ).encode(),
                stderr=b"",
                wall_seconds=1.0,
            )
        return CommandResult(
            exit_code=None,
            stdout=b"",
            stderr=b"",
            wall_seconds=0.1,
            interruption="operator_interrupt",
        )

    run_primary(manifest_path, command_runner=export_failure)
    primary = manifest_path.parent / "attempts" / "Q01" / "primary"
    attempt_bytes = primary.joinpath("attempt.json").read_bytes()
    outcome_bytes = primary.joinpath("outcome.json").read_bytes()
    outcome = cast(dict[str, object], json.loads(outcome_bytes))
    assert outcome["disposition"] == "export_error"
    assert outcome["interruption"] is None
    assert cast(dict[str, str], outcome["error"]) == {
        "type": "OSError",
        "message": "export transport unavailable",
        "stage": "export",
    }

    resumed_calls: list[tuple[str, ...]] = []

    def resume(
        argv: tuple[str, ...], _timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        resumed_calls.append(argv)
        return CommandResult(
            exit_code=None,
            stdout=b"",
            stderr=b"",
            wall_seconds=0.1,
            interruption="operator_interrupt",
        )

    run_primary(manifest_path, command_runner=resume)
    assert EXPECTED_QUESTIONS[2][1] in resumed_calls[0]
    assert primary.joinpath("attempt.json").read_bytes() == attempt_bytes
    assert primary.joinpath("outcome.json").read_bytes() == outcome_bytes


def test_keyboard_interrupt_during_export_is_persisted_and_stops_cohort(tmp_path: Path) -> None:
    manifest_path = _prepare(tmp_path)
    calls: list[tuple[str, ...]] = []

    def interrupt_export(
        argv: tuple[str, ...], _timeout: float, _environment: Mapping[str, str]
    ) -> CommandResult:
        calls.append(argv)
        if "--export" in argv:
            raise KeyboardInterrupt
        return CommandResult(
            exit_code=0,
            stdout=json.dumps(
                _completed_result("Q01", "0216f1e33f05962d49467d95fe84609041c6dba8")
            ).encode(),
            stderr=b"",
            wall_seconds=1.0,
        )

    accounting = run_primary(manifest_path, command_runner=interrupt_export)

    assert len(accounting) == 1
    assert len(calls) == 2
    assert accounting[0]["disposition"] == "interrupted"
    assert accounting[0]["interruption"] == "operator_interrupt"
    assert accounting[0]["error"] == {
        "type": "KeyboardInterrupt",
        "message": "",
        "stage": "export",
    }
    assert (manifest_path.parent / "attempts" / "Q01" / "primary" / "outcome.json").is_file()


def test_retrieval_requires_gold_span_overlap_and_tracks_followups() -> None:
    wrong_lines = {
        "item_type": "source",
        "evidence_id": "wrong-lines",
        "path": "README.md",
        "line_start": 100,
        "line_end": 110,
    }
    supporting = {
        "item_type": "source",
        "evidence_id": "supporting",
        "path": "README.md",
        "line_start": 1,
        "line_end": 6,
    }
    evidence = {
        "records": [
            {
                "invocation_id": "query-1",
                "response": {
                    "complete": True,
                    "completeness": "complete",
                    "items": [wrong_lines],
                },
            },
            {
                "invocation_id": "query-2",
                "response": {
                    "complete": True,
                    "completeness": "complete",
                    "items": [supporting],
                },
            },
        ]
    }

    score = score_retrieval("Q01", evidence)

    assert score["query_count"] == 2
    assert score["before"]["supported"] is False
    assert score["after"]["supported"] is True
    assert score["after"]["first_supporting_query"] == 2
    assert score["after"]["reciprocal_rank"] == 1.0
    assert score["after"]["supporting_evidence_ids"] == ["supporting"]


def test_manual_answer_scoring_preserves_q08_ambiguity_and_q14_exact_values() -> None:
    q08_components = cast(dict[str, list[str]], scoring_contract()["gold_components"])["Q08"]
    q08_answer = {
        "outcome": "partial",
        "claims": [
            {
                "id": "known-recovery",
                "classification": "direct",
                "statement": "Committed pages survive.",
                "citations": [{"evidence_id": "e1"}],
            },
            {
                "id": "operator-action",
                "classification": "unknown",
                "statement": "The exact operator action is unknown.",
                "citations": [],
            },
        ],
    }
    q08_judgment = {
        "reviewer": "parent-reviewer",
        "gold_components": {
            component: {"covered": True, "claim_ids": ["known-recovery"]}
            for component in q08_components
        },
        "claim_reviews": {
            "known-recovery": {
                "correct": True,
                "source_supported": True,
                "classification_correct": True,
                "notes": "Source excerpt supports the bounded recovery claim.",
            },
            "operator-action": {
                "correct": True,
                "source_supported": True,
                "classification_correct": True,
                "notes": "Honest source-limited unknown.",
            },
        },
        "honest_abstention": True,
        "ambiguity_reason": "The source leaves partial external settings-write recovery open.",
        "exact_checks": {},
    }

    q08 = build_answer_score("Q08", q08_answer, q08_judgment, citation_integrity=1.0)

    assert q08["expected_class"] == "unknown"
    assert q08["classification_correct"] is True
    assert q08["honest_abstention"] is True
    assert q08["unsupported_statements"] == []

    q14_answer = {
        "outcome": "complete",
        "claims": [
            {
                "id": "change",
                "classification": "direct",
                "statement": "The reviewed C3 change is fully enumerated.",
                "citations": [{"evidence_id": "source"}],
            }
        ],
    }
    components = cast(dict[str, list[str]], scoring_contract()["gold_components"])["Q14"]
    checks = cast(dict[str, dict[str, object]], scoring_contract()["exact_checks"])["Q14"]
    q14_judgment = {
        "reviewer": "parent-reviewer",
        "gold_components": {
            component: {"covered": True, "claim_ids": ["change"]} for component in components
        },
        "claim_reviews": {
            "change": {
                "correct": True,
                "source_supported": True,
                "classification_correct": True,
                "notes": "Reviewed against exported source excerpts and Git metadata.",
            }
        },
        "honest_abstention": False,
        "ambiguity_reason": None,
        "exact_checks": {
            check: {"correct": True, "claim_ids": ["change"], "notes": "Verified."}
            for check in checks
        },
        "observed_changed_paths": list(Q14_CHANGED_PATHS),
    }

    q14 = build_answer_score("Q14", q14_answer, q14_judgment, citation_integrity=1.0)

    assert q14["gold_key_coverage"] == 1.0
    assert q14["exact_value_coverage"] == 1.0
    assert q14["q14_changed_paths_exact"] is True
    assert q14["source_supported_claim_precision"] == 1.0


def test_answer_correct_requires_every_claim_and_valid_citations() -> None:
    components = cast(dict[str, list[str]], scoring_contract()["gold_components"])["Q01"]
    answer = {
        "outcome": "complete",
        "claims": [
            {
                "id": "gold",
                "classification": "direct",
                "statement": "The shared platform owns replenishment.",
                "citations": [{"evidence_id": "source-1"}],
            },
            {
                "id": "extra",
                "classification": "direct",
                "statement": "An extra but incorrect assertion.",
                "citations": [{"evidence_id": "source-2"}],
            },
        ],
    }
    claim_reviews: dict[str, dict[str, object]] = {
        "gold": {
            "correct": True,
            "source_supported": True,
            "classification_correct": True,
            "notes": "Verified against the raw source excerpt.",
        },
        "extra": {
            "correct": False,
            "source_supported": True,
            "classification_correct": True,
            "notes": "The excerpt contradicts this additional assertion.",
        },
    }
    judgment = {
        "reviewer": "validation-agent",
        "gold_components": {
            component: {"covered": True, "claim_ids": ["gold"]} for component in components
        },
        "claim_reviews": claim_reviews,
        "honest_abstention": False,
        "ambiguity_reason": None,
        "exact_checks": {},
    }

    wrong_claim = build_answer_score("Q01", answer, judgment, citation_integrity=1.0)
    assert wrong_claim["answer_correct"] is False

    claim_reviews["extra"]["correct"] = True
    invalid_citation = build_answer_score("Q01", answer, judgment, citation_integrity=0.5)
    assert invalid_citation["answer_correct"] is False

    fully_valid = build_answer_score("Q01", answer, judgment, citation_integrity=1.0)
    assert fully_valid["answer_correct"] is True


def test_citation_integrity_requires_exact_source_span_identity() -> None:
    source = {
        "item_type": "source",
        "evidence_id": "source-1",
        "path": "src/order.py",
        "blob_oid": "1" * 40,
        "content_hash": "2" * 64,
        "excerpt_hash": "3" * 64,
        "qualified_name": "Order.submit",
        "line_start": 40,
        "line_end": 52,
        "byte_start": 800,
        "byte_end": 1100,
        "excerpt": "bounded source excerpt",
    }
    citation = {key: value for key, value in source.items() if key not in {"item_type", "excerpt"}}
    citation.update({"citation_type": "source", "run_id": "run-1"})
    evidence = {
        "run_id": "run-1",
        "records": [{"response": {"items": [source]}}],
    }
    answer = {"claims": [{"id": "claim-1", "classification": "direct", "citations": [citation]}]}

    valid = citation_integrity(answer, evidence)
    assert valid["score"] == 1.0

    answer["claims"].append({"id": "claim-2", "classification": "inferred", "citations": []})
    missing = citation_integrity(answer, evidence)
    assert missing["score"] == 0.5
    assert missing["uncited_claim_ids"] == ["claim-2"]

    unknown = {"claims": [{"id": "unknown", "classification": "unknown", "citations": []}]}
    honest_unknown = citation_integrity(unknown, {"run_id": "run-1", "records": []})
    assert honest_unknown["score"] is None
    assert honest_unknown["uncited_claim_ids"] == []

    citation["line_start"] = 39
    invalid = citation_integrity(answer, evidence)
    assert invalid["score"] == 0.0
    assert invalid["invalid_citations"][0]["claim_id"] == "claim-1"


def test_score_packet_blocks_partial_cohort_and_never_substitutes_supplements() -> None:
    rows: list[dict[str, object]] = []
    judgments: dict[str, object] = {}
    contract = scoring_contract()
    components_by_question = cast(dict[str, list[str]], contract["gold_components"])
    expected_classes = cast(dict[str, str], contract["expected_answer_class"])
    for question_id, _question in EXPECTED_QUESTIONS:
        claim = {
            "id": "claim",
            "classification": expected_classes[question_id],
            "statement": f"Reviewed answer for {question_id}.",
            "citations": [],
        }
        rows.append(
            {
                "question_id": question_id,
                "disposition": "completed" if question_id != "Q14" else "unrun",
                "scorable": question_id != "Q14",
                "wall_seconds": 1.0,
                "usage": {"input_tokens": 10},
                "answer": {"outcome": "complete", "claims": [claim]},
                "citation_integrity": {
                    "citation_count": 0,
                    "valid_citation_count": 0,
                    "score": None,
                    "invalid_citations": [],
                },
                "retrieval": {
                    "before": {"supported": question_id == "Q01"},
                    "after": {"supported": question_id in {"Q01", "Q02"}},
                },
            }
        )
        if question_id == "Q14":
            continue
        judgments[question_id] = {
            "reviewer": "parent-reviewer",
            "gold_components": {
                component: {"covered": True, "claim_ids": ["claim"]}
                for component in components_by_question[question_id]
            },
            "claim_reviews": {
                "claim": {
                    "correct": True,
                    "source_supported": True,
                    "classification_correct": True,
                    "notes": "Reviewed against raw exported evidence.",
                }
            },
            "honest_abstention": question_id == "Q08",
            "ambiguity_reason": (
                "The precise recovery action is not source-determined."
                if question_id == "Q08"
                else None
            ),
            "exact_checks": {},
        }

    scored = score_packet(
        {
            "questions": rows,
            "supplements": [
                {
                    "question_id": "Q14",
                    "attempt_kind": "retry",
                    "disposition": "completed",
                }
            ],
        },
        {"schema_version": 1, "judgments": judgments},
    )

    comparison = scored["retrieval_comparison"]
    assert comparison["historical_supported_count"] == 6
    assert comparison["new_after_supported_count"] == 2
    assert comparison["delta_after_vs_historical"] is None
    assert comparison["lost_historical_hits"] == ["Q07", "Q10", "Q11", "Q12", "Q13"]
    assert comparison["gained_hits"] == ["Q02"]
    assert comparison["conclusion"] == "blocked: 13/14 primary answers are scorable"
    assert scored["supplements_excluded_from_primary_scoring"] is True


def test_report_materializes_measurements_raw_hashes_and_missing_values() -> None:
    report = render_report(
        {
            "retrieval_comparison": {
                "conclusion": "improved",
                "historical_supported_count": 6,
                "new_before_supported_count": 5,
                "new_after_supported_count": 7,
                "lost_historical_hits": ["Q07"],
                "gained_hits": ["Q02", "Q14"],
                "delta_after_vs_historical": 1,
            },
            "answer_quality": {
                "scored_question_count": 14,
                "correct_question_count": 10,
                "mean_gold_key_coverage": 0.8,
                "mean_source_supported_claim_precision": 0.9,
                "mean_citation_integrity": 0.95,
            },
            "runtime": {
                "latency": {"observed_count": 14, "sum_seconds": 123.0},
                "usage": {"observed_count": 14, "numeric_totals": {"input_tokens": 456}},
            },
            "questions": [
                {
                    "question_id": "Q14",
                    "disposition": "completed",
                    "retrieval": {
                        "query_count": 3,
                        "before": {
                            "supported": False,
                            "reciprocal_rank": 0.0,
                            "gold_span_recall": 0.0,
                            "query_completeness": [{"complete": True}],
                        },
                        "after": {
                            "supported": True,
                            "reciprocal_rank": 0.5,
                            "gold_span_recall": 1.0,
                            "query_completeness": [
                                {"complete": True},
                                {"complete": False},
                                {"complete": True},
                            ],
                        },
                    },
                    "answer_score": {
                        "answer_correct": False,
                        "gold_key_coverage": 0.75,
                        "source_supported_claim_precision": 0.5,
                        "citation_integrity": 1.0,
                        "exact_value_coverage": 0.5,
                        "unsupported_statements": [
                            {"claim_id": "c1", "statement": "unsupported claim"}
                        ],
                        "q14_changed_paths_exact": False,
                        "exact_checks": {
                            "case_sensitive": {
                                "expected": "longer named prefix wins",
                                "verified": False,
                            },
                            "changed_path_count": {"verified": True},
                        },
                    },
                    "raw": {
                        "attempt": {"path": "/evidence/Q14/attempt.json", "sha256": "a" * 64},
                        "outcome": {"path": "/evidence/Q14/outcome.json", "sha256": "b" * 64},
                    },
                }
            ],
        }
    )

    assert "| Q14 | completed | 3 | False / 0.000" in report
    assert "## Runtime and usage" in report
    assert '"input_tokens":456' in report
    assert "## Raw artifact inventory" in report
    assert "/evidence/Q14/attempt.json" in report
    assert "a" * 64 in report
    assert "## Unsupported statements and missing exact values" in report
    assert "unsupported claim" in report
    assert "case_sensitive" in report
    assert "longer named prefix wins" in report
    assert "changed_paths" in report


def test_review_packet_preserves_unrun_primary_accounting(tmp_path: Path) -> None:
    packet = build_review_packet(_prepare(tmp_path))

    assert packet["execution_state"] == "UNRUN"
    assert len(packet["questions"]) == 14
    assert all(row["disposition"] == "unrun" for row in packet["questions"])
    assert all(row["scorable"] is False for row in packet["questions"])
    assert packet["supplements"] == []
    assert packet["historical_retrieval_hits"] == [
        "Q01",
        "Q07",
        "Q10",
        "Q11",
        "Q12",
        "Q13",
    ]


def test_review_packet_verifies_completed_publication_bytes(tmp_path: Path) -> None:
    manifest_path = _prepare(tmp_path)
    attempt_dir = manifest_path.parent / "attempts" / "Q01" / "primary"
    attempt_dir.mkdir(parents=True)
    (attempt_dir / "attempt.json").write_text("{}", encoding="utf-8")
    export = _publication_export(attempt_dir)
    outcome = {
        "question_id": "Q01",
        "attempt_kind": "primary",
        "attempt_index": 1,
        "disposition": "completed",
        "wall_seconds": 4.5,
        "usage": {"input_tokens": 12},
        "result": {"run_id": "run-q01"},
        "export": export,
    }
    (attempt_dir / "outcome.json").write_text(json.dumps(outcome), encoding="utf-8")

    packet = build_review_packet(manifest_path)
    q01 = packet["questions"][0]
    assert q01["scorable"] is True
    assert q01["answer"]["run_id"] == "run-q01"
    assert q01["retrieval"]["after"]["supported"] is False
    assert q01["raw"]["attempt"] == {
        "path": str(attempt_dir / "attempt.json"),
        "sha256": _sha256((attempt_dir / "attempt.json").read_bytes()),
    }
    assert q01["raw"]["outcome"] == {
        "path": str(attempt_dir / "outcome.json"),
        "sha256": _sha256((attempt_dir / "outcome.json").read_bytes()),
    }
    assert q01["raw"]["publication"]["tar_sha256"] == export["tar_sha256"]

    Path(cast(str, export["tar_path"])).write_bytes(b"tampered")
    with pytest.raises(AttemptError, match="tar hash changed"):
        build_review_packet(manifest_path)
