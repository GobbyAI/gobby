"""Branch-native gcode emission coverage for Ask claim validation."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.claims import (
    AnswerDraft,
    AnswerSection,
    Claim,
    ClaimClassification,
    GitMetadataCitation,
    QuestionPart,
    SourceCitation,
)
from gobby.ask.contracts import AskRequest
from gobby.ask.evidence import EvidenceAdmission
from gobby.ask.snapshots import AskSnapshotManager
from gobby.ask.storage import AskRunStorage
from gobby.runtime_grants.service import DeploymentGrantContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.managed_credentials import ManagedCredentialManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.worktrees import LocalWorktreeManager
from tests.ask.test_native_integration import (
    _branch_gcode,
    _commit,
    _git,
    _profile,
)
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_native_source_and_git_metadata_validate_from_exact_emissions(
    temp_db: HubDatabase,
    isolated_checkout_factory: IsolatedCheckoutFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.ask.validation import EvidenceManifest, validate_claims

    isolated = isolated_checkout_factory(temp_db, "ask-validation-native", root=tmp_path / "repo")
    repo = Path(isolated.root_path)
    _git(repo, "init", "--quiet", "-b", "main")
    (repo / "src").mkdir()
    source_path = repo / "src" / "app.py"
    source_path.write_text("def answer():\n    return 41\n", encoding="utf-8")
    _commit(repo, "root")
    source_path.write_text("def answer():\n    return 42\n", encoding="utf-8")
    commit_oid = _commit(repo, "pin answer")

    project_id = isolated.project.id
    session = SessionManager(temp_db).register(
        external_id="ask-validation-native",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=project_id,
        workspace_path=str(repo),
    )
    storage = AskRunStorage(
        LocalPipelineExecutionManager(temp_db, project_id=project_id),
        profile_resolver=_profile,
    )
    record = storage.start(
        AskRequest(
            question="What does answer return and which commit changed it?",
            project_id=project_id,
            commit_ref=commit_oid,
            investigator_profile="investigator",
            reviewer_profile="reviewer",
        ),
        repo,
    )
    storage.bind_execution_context(
        record.run_id,
        project_root=repo,
        caller_session_id=session.id,
    )
    artifacts = AskArtifactStore(tmp_path / "state", project_id, record.run_id)
    runtime_root = tmp_path / "managed-runtimes"
    credentials = ManagedCredentialManager(
        database=temp_db,
        machine_id=UUID(isolated.machine_id),
        runtime_root=runtime_root,
    )
    source_home = Path(os.environ["GOBBY_HOME"])
    source_home.mkdir(parents=True, exist_ok=True)
    (source_home / "local_cli_token").write_text("isolated-operator-token\n", encoding="utf-8")
    (source_home / "local_cli_token").chmod(0o600)
    monkeypatch.setattr(
        "gobby.agents.code_index._active_deployment_grant_context",
        lambda: DeploymentGrantContext(
            token="a" * 16,
            fencing_epoch=1,
            signing_secret="ask-validation-native-signing-secret",
        ),
    )
    monkeypatch.setattr(
        "gobby.agents.code_index.resolve_native_bin",
        lambda _name: (_ for _ in ()).throw(AssertionError("global gcode lookup")),
    )
    gcode_bin = _branch_gcode()
    manager = AskSnapshotManager(
        worktree_storage=LocalWorktreeManager(temp_db),
        run_storage=storage,
        snapshot_executable=gcode_bin,
        credential_manager=credentials,
    )
    snapshot = await manager.prepare_async(
        run_id=record.run_id,
        repository_root=repo,
        artifacts=artifacts,
    )
    try:
        assert snapshot.generation == 1
        assert snapshot.runtime.executable == gcode_bin
        admission = EvidenceAdmission(
            run_id=record.run_id,
            runtime=snapshot.runtime,
            permitted_operations={"read"},
            page_size=128 * 1024,
            artifacts=artifacts,
            storage=storage,
        )
        source_response = await admission.query(
            "read",
            {"kind": "range", "path": "src/app.py", "start_line": 1, "end_line": 2},
        )
        metadata_response = await admission.query("read", {"kind": "commit_metadata"})

        references = tuple(storage.evidence_references(record.run_id))
        responses = (source_response, metadata_response)
        assert len(references) == len(responses) == 2
        assert len({reference.invocation_id for reference in references}) == 2
        assert all(reference.status == "succeeded" for reference in references)
        manifest = EvidenceManifest.model_validate(
            {
                "run_id": record.run_id,
                "snapshot_binding": snapshot.binding,
                "inventory": snapshot.inventory,
                "records": [
                    {
                        "run_id": record.run_id,
                        "invocation_id": reference.invocation_id,
                        "snapshot_inventory_digest": reference.snapshot_inventory_digest,
                        "request_hash": reference.request_hash,
                        "response_hash": reference.response_hash,
                        "response": response,
                    }
                    for reference, response in zip(references, responses, strict=True)
                ],
            }
        )
        source_item = source_response["items"][0]
        metadata_item = metadata_response["items"][0]
        source_citation = SourceCitation.model_validate(
            {
                "citation_type": "source",
                "run_id": record.run_id,
                **{
                    key: value
                    for key, value in source_item.items()
                    if key not in {"item_type", "excerpt"}
                },
            }
        )
        metadata_citation = GitMetadataCitation.model_validate(
            {
                "citation_type": "git_metadata",
                "run_id": record.run_id,
                **{key: value for key, value in metadata_item.items() if key != "item_type"},
            }
        )
        assert not {"line_start", "line_end", "byte_start", "byte_end"} & metadata_item.keys()
        draft = AnswerDraft(
            run_id=record.run_id,
            investigator_run_id="native-investigator",
            question=record.request.question,
            question_parts=(
                QuestionPart(id="return", text="Return value"),
                QuestionPart(id="change", text="Pinned commit change"),
            ),
            claims=(
                Claim(
                    id="claim-return",
                    classification=ClaimClassification.DIRECT,
                    statement="answer returns 42.",
                    citations=(source_citation,),
                    question_part_ids=("return",),
                ),
                Claim(
                    id="claim-change",
                    classification=ClaimClassification.DIRECT,
                    statement="The pinned commit changed src/app.py.",
                    citations=(metadata_citation,),
                    question_part_ids=("change",),
                ),
            ),
            sections=(
                AnswerSection(
                    id="answer",
                    title="Answer",
                    claim_ids=("claim-return", "claim-change"),
                ),
            ),
        )
        pinned_blobs = {
            (source_item["path"], source_item["blob_oid"]): (
                snapshot.source_root / source_item["path"]
            ).read_bytes()
        }

        report = validate_claims(draft, manifest, pinned_blobs=pinned_blobs)
        assert report.accepted_claim_ids == ("claim-return", "claim-change")
        assert report.diagnostic_codes == ()
    finally:
        manager.release(snapshot, artifacts=artifacts)
