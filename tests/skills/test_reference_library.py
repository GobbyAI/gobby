"""The reference library must cover public operations and verified guide sections."""

from copy import deepcopy
from pathlib import Path

import pytest

from tests.skills.reference_library_helpers import (
    ROOT,
    cli_inventory,
    coverage_errors,
    documentation_errors,
    internal_tool_inventory,
    load_audits,
    local_link_errors,
    markdown_targets,
    native_cli_inventory,
    proxy_tool_inventory,
)

pytestmark = pytest.mark.unit


async def test_reference_contract_3_2_1() -> None:
    audits = load_audits()
    errors = coverage_errors(
        audits,
        internal_tool_inventory() | await proxy_tool_inventory(),
        cli_inventory() | native_cli_inventory(),
    )
    errors.extend(documentation_errors(audits))
    assert errors == [], "\n".join(errors)


def test_reference_contract_3_2_2() -> None:
    errors = documentation_errors(load_audits())
    assert errors == [], "\n".join(errors)


def test_unmapped_public_operations_are_rejected() -> None:
    errors = coverage_errors(
        load_audits(), {("gobby-future", "new_operation")}, {"gobby future operation"}
    )
    assert errors == [
        "Unmapped public tool: gobby-future:new_operation",
        "Unmapped public CLI: gobby future operation",
    ]


def test_unaudited_guide_link_is_rejected() -> None:
    audits = deepcopy(load_audits())
    for audit in audits:
        audit["guides"] = [
            guide for guide in audit["guides"] if guide["path"] != "docs/guides/tasks.md"
        ]
    errors = documentation_errors(audits)
    assert any(
        error.startswith("Unaudited guide link:") and "docs/guides/tasks.md" in error
        for error in errors
    ), errors


def test_markdown_ignores_code_comments_and_resolves_reference_links(tmp_path: Path) -> None:
    document = tmp_path / "reference.md"
    document.write_text(
        "# Actual `heading`\n\n```sh\n# Fake heading\n```\n\n"
        "# Actual `heading`\n\n[Guide][guide]\n\n[guide]: guide.md#section\n"
    )
    assert markdown_targets(document) == (
        {"actual-heading", "actual-heading-1"},
        ["guide.md#section"],
    )


def test_all_absorbed_skills_have_one_audit_owner() -> None:
    expected = {
        "tasks",
        "live-session",
        "plan",
        "plan-draft",
        "plan-enhance",
        "plan-mechanic",
        "plan-review",
        "expand",
        "expansion-agent-selection",
        "build",
        "build-coordinator",
        "handoff-discipline",
        "persona",
        "memory",
        "review-learning",
        "code-index",
        "loading-skills",
        "writing-skills",
        "build-rule",
        "mcp-servers",
        "pipelines-and-cron",
        "source-control",
        "clones",
        "merge",
        "merge-expert",
        "review",
        "epic-review",
        "intro",
        "development-discipline",
        "channel-parity",
    }
    absorbed = [name for audit in load_audits() for name in audit["source_skills"]]
    assert set(absorbed) == expected
    assert len(absorbed) == len(expected)


def test_missing_operation_evidence_is_rejected() -> None:
    audits = deepcopy(load_audits())
    audits[0]["operations"][0]["verification"] = ["unrecorded-proof"]
    assert any(
        error.startswith("Missing operation evidence:") for error in documentation_errors(audits)
    )


@pytest.mark.parametrize("incomplete", ["audit", "guide", "evidence"])
def test_incomplete_verification_is_rejected(incomplete: str) -> None:
    audits = deepcopy(load_audits())
    if incomplete == "audit":
        audits[0]["status"] = "in_progress"
    elif incomplete == "guide":
        audits[0]["guides"][0]["audit_status"] = "in_progress"
    else:
        audits[0]["evidence"][0]["result"] = ""
        audits[0]["evidence"][0]["description"] = ""
    assert any(
        "not verified" in error or "incomplete evidence" in error
        for error in documentation_errors(audits)
    )


def test_same_document_anchor_is_checked(tmp_path: Path) -> None:
    document = tmp_path / "reference.md"
    document.write_text("# Heading\n\n[Valid](#heading)\n")
    assert local_link_errors(document) == []
    document.write_text("# Heading\n\n[Broken](#missing)\n")
    assert local_link_errors(document) == [f"Broken anchor: {document} -> #missing"]


@pytest.mark.parametrize("invalid_field", ["reference", "verification"])
def test_native_mapping_requires_reference_and_evidence(invalid_field: str) -> None:
    audits = deepcopy(load_audits())
    native = next(audit["native_cli"][0] for audit in audits if audit.get("native_cli"))
    native[invalid_field] = "missing.md" if invalid_field == "reference" else ["missing-proof"]
    expected = (
        "Uncataloged operation reference:"
        if invalid_field == "reference"
        else "Missing operation evidence:"
    )
    assert any(error.startswith(expected) for error in documentation_errors(audits))


def test_new_native_command_cannot_hide_behind_stale_contract(tmp_path: Path) -> None:
    for relative in (
        "tests/contracts/gcode.contract.json",
        "crates/gcode/src/contract.rs",
        "crates/gcode/src/cli.rs",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text())
    source = tmp_path / "crates/gcode/src/cli.rs"
    source.write_text(
        source.read_text().replace("enum Command {", "enum Command {\n    FutureOperation,")
    )
    with pytest.raises(ValueError, match="Clap/contract command drift.*future-operation"):
        native_cli_inventory(tmp_path)
