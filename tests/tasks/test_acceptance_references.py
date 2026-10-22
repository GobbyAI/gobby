"""Reference parsing shared by task creation, expansion, and skill selection."""

from __future__ import annotations

import pytest

from gobby.tasks import acceptance_references as artifacts_module
from gobby.tasks.acceptance_references import malformed_test_reference_findings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("prefix", ["- TDD cycle:", "Named test:"])
def test_unlabeled_test_reference_is_not_silently_dropped(prefix: str) -> None:
    reference = "tests/x_test.py::test_a"
    findings = malformed_test_reference_findings(f"{prefix} test: `{reference}`")
    accepted = artifacts_module.extract_artifact_references(f"{prefix} test: `{reference}`", "test")
    assert reference in accepted or any(reference in finding for finding in findings)


def test_artifact_references_ignore_prose_file_line_token() -> None:
    criteria = "The plan uses file:line anchors.\nfile: `docs/evidence.md`."

    assert artifacts_module.extract_artifact_references(criteria, "file") == ("docs/evidence.md",)


def test_artifact_references_ignore_bare_prose_words() -> None:
    criteria = (
        "3) Dispatch test: an openapi-template server registers. "
        "The file: config is regenerated. Test: select the newest row."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == ()
    assert artifacts_module.extract_artifact_references(criteria, "file") == ()


@pytest.mark.parametrize(
    "criteria",
    [
        "- Run the e2e-test: `npm run e2e`",
        "1. Run the e2e-test: `npm run e2e`",
        "1. Check source-file: `docs/evidence.md`",
        "```\ntest: `tests/example.py::test_example`\n```",
        "~~~python\ntest: `tests/example.py::test_example`\n~~~",
        "```\ntest: `tests/example.py::test_example`",
    ],
)
def test_artifact_references_ignore_compound_labels_and_fenced_examples(criteria: str) -> None:
    assert artifacts_module.extract_artifact_references(criteria, "test") == ()
    assert artifacts_module.extract_artifact_references(criteria, "file") == ()
    assert malformed_test_reference_findings(criteria) == ()


def test_artifact_references_keep_real_tests_after_fenced_examples() -> None:
    criteria = (
        "```python\n"
        "test: `tests/example.py::test_example`\n"
        "~~~\n"
        "```still an example\n"
        "````\n"
        "1. Run the e2e-test: `npm run e2e`; test: `tests/live.py::test_live`\n"
        "file: `docs/evidence.md`"
    )
    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/live.py::test_live",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == ("docs/evidence.md",)
    assert malformed_test_reference_findings(criteria) == ()


@pytest.mark.parametrize(
    "opener",
    ["```npm run e2e``` must pass", "````npm run e2e```` must pass", "```py`thon"],
)
def test_artifact_references_keep_tests_after_backtick_info_span(opener: str) -> None:
    criteria = f"{opener}\ntest: `tests/live.py::test_live`"
    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/live.py::test_live",
    )


@pytest.mark.parametrize("opener", ["```python", "~~~python", "~~~`python`"])
def test_artifact_references_mask_real_backtick_and_tilde_fences(opener: str) -> None:
    criteria = (
        f"{opener}\n"
        "test: `tests/example.py::test_example`\n"
        f"{opener[:3]}\n"
        "test: `tests/live.py::test_live`"
    )
    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/live.py::test_live",
    )


def test_artifact_references_keep_pathlike_bare_tokens() -> None:
    criteria = (
        "test: tests/tasks/test_validation.py::test_gate.\n"
        "file: docs/evidence.md,\n"
        "file: .gobby/plans/x.yaml."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/tasks/test_validation.py::test_gate",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == (
        "docs/evidence.md",
        ".gobby/plans/x.yaml",
    )


def test_backticked_references_bypass_the_bare_token_shape_filter() -> None:
    criteria = "test: `oops`\nfile: `notes`"

    assert artifacts_module.extract_artifact_references(criteria, "test") == ("oops",)
    assert artifacts_module.extract_artifact_references(criteria, "file") == ("notes",)


def test_line_leading_references_support_markdown_prefixes() -> None:
    criteria = (
        "test: `tests/test_plain.py::test_plain`\n"
        "- file: docs/bullet.md\n"
        "2) test: tests/test_numbered.py::test_numbered\n"
        "**test**: `tests/test_bold.py::test_bold`\n"
        "**file**: `docs/bold.md`\n"
        "> test: `tests/test_quote.py::test_quote`"
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/test_plain.py::test_plain",
        "tests/test_numbered.py::test_numbered",
        "tests/test_bold.py::test_bold",
        "tests/test_quote.py::test_quote",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == (
        "docs/bullet.md",
        "docs/bold.md",
    )


def test_expansion_prefixed_references_are_extracted() -> None:
    criteria = (
        "Acceptance artifacts:\n"
        "- 2.3.1: test: `tests/test_expanded.py::test_expanded`\n"
        "- A.2b.3: test: `tests/test_alphanumeric.py::test_alphanumeric`\n"
        "- 2.3.2: file: `src/gobby/tasks/acceptance_artifacts.py`\n"
        "- 2: test: `tests/test_single_segment.py::test_ignored`\n"
        "Prose containing 2.3.3: test: `tests/test_ignored.py::test_ignored`.\n"
        "- Prose containing 2.3.4: file: `docs/ignored.md`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/test_expanded.py::test_expanded",
        "tests/test_alphanumeric.py::test_alphanumeric",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == (
        "src/gobby/tasks/acceptance_artifacts.py",
    )


def test_inline_criterion_test_reference_is_extracted() -> None:
    criteria = (
        "3.2.2: A partition skips the write. test: "
        "`crates/x/src/a_tests.rs::unchanged_partition_skips_write`.\n"
        "- 3.2.3: The `test: examples/example.rs::not_a_test` sample is inert; "
        "test: `tests/test_real.py::test_real`.\n"
        "- test: `tests/test_list.py::test_list`\n"
        "1) 3.2.5: A numbered criterion. test: `tests/test_number.py::test_number`.\n"
        "Prose containing 3.2.4: test: `tests/test_prose.py::test_prose`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "crates/x/src/a_tests.rs::unchanged_partition_skips_write",
        "tests/test_real.py::test_real",
        "tests/test_list.py::test_list",
        "tests/test_number.py::test_number",
    )


def test_multiple_inline_test_references_on_one_criterion_are_extracted() -> None:
    criteria = (
        "3.2.2: test: `tests/a.py::test_a` and test: `tests/b.py::test_b`; "
        "the `test: examples/example.py::not_a_test` example is inert."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/a.py::test_a",
        "tests/b.py::test_b",
    )


def test_numbered_criteria_extract_real_tests_after_quoted_example() -> None:
    criteria = (
        "7. Extract an inline test, such as '3.2.2: skips the write. test: "
        "`crates/x/src/a_tests.rs::unchanged_partition_skips_write`.' "
        "test: `tests/tasks/test_acceptance_artifacts.py::test_inline_criterion_test_reference_is_extracted`.\n"
        "8. A task with no named test fails. test: "
        "`tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_tdd_required_task_without_resolved_tests_fails_gate_12`.\n"
        "9. A task without TDD skips the gates. test: "
        "`tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_non_tdd_task_without_tests_still_skips_gates_11_and_12`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/tasks/test_acceptance_artifacts.py::test_inline_criterion_test_reference_is_extracted",
        "tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_tdd_required_task_without_resolved_tests_fails_gate_12",
        "tests/mcp_proxy/tools/tasks/test_close_task_flow.py::test_non_tdd_task_without_tests_still_skips_gates_11_and_12",
    )


def test_numbered_criterion_keeps_a_quoted_real_test_reference() -> None:
    criteria = '7. Verify the named "test: `tests/x.py::test_x`" passes.'

    assert artifacts_module.extract_artifact_references(criteria, "test") == ("tests/x.py::test_x",)


def test_numbered_criterion_ignores_example_with_an_apostrophe() -> None:
    criteria = (
        "7. For example, 'it's a test: `tests/fake.py::test_fake`'; "
        "test: `tests/real.py::test_real`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/real.py::test_real",
    )


@pytest.mark.parametrize("cue", ["For example", "Example", "e.g.", "such as"])
@pytest.mark.parametrize("separator", [",", ":"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_numbered_criterion_ignores_illustrative_quoted_test(
    cue: str, separator: str, quote: str
) -> None:
    criteria = (
        f"7. {cue}{separator} {quote}it's a test: `tests/fake.py::test_fake`{quote}; "
        "test: `tests/real.py::test_real`."
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/real.py::test_real",
    )


def test_inline_code_examples_are_not_artifact_references() -> None:
    criteria = (
        "The schema describes `test: path::test_symbol`, examples use "
        "`test: tests/x.py::test_y`, and docs mention `file: docs/foo.md`.\n"
        "- test: `tests/a/test_b.py::test_c`"
    )

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/a/test_b.py::test_c",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == ()


def test_bare_reference_never_keeps_trailing_backtick() -> None:
    criteria = "test: tests/test_feature.py::test_feature`\nfile: docs/evidence.md`"

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/test_feature.py::test_feature",
    )
    assert artifacts_module.extract_artifact_references(criteria, "file") == ("docs/evidence.md",)


@pytest.mark.parametrize(
    "criteria",
    [
        "test: tests/x.py::test_x: a taskless reviewer is parked",
        "test: tests/x.py::test_x)",
        "1. A taskless reviewer is parked. test: tests/x.py::test_x: then reported",
        "1. A taskless reviewer is parked (test: tests/x.py::test_x)",
        "1. A taskless reviewer is parked (test: tests/x.py::test_x).",
    ],
)
def test_bare_reference_drops_trailing_colon_and_unbalanced_paren(criteria: str) -> None:
    assert artifacts_module.extract_artifact_references(criteria, "test") == ("tests/x.py::test_x",)


@pytest.mark.parametrize("suffix", ["", ":", ".", ").", "):"])
def test_bare_reference_keeps_parametrized_brackets(suffix: str) -> None:
    criteria = f"test: tests/x.py::test_x[a-(1)]{suffix}"

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/x.py::test_x[a-(1)]",
    )


def test_bare_reference_drops_sentence_ending_period() -> None:
    criteria = "1. The gate resolves test: tests/x.py::test_x."

    assert artifacts_module.extract_artifact_references(criteria, "test") == ("tests/x.py::test_x",)


def test_backticked_reference_keeps_trailing_colon() -> None:
    criteria = "test: `tests/x.py::test_x:`"

    assert artifacts_module.extract_artifact_references(criteria, "test") == (
        "tests/x.py::test_x:",
    )


def test_malformed_test_reference_findings_reports_every_invalid_reference() -> None:
    findings = malformed_test_reference_findings(
        """\
test: `tests/x/test_y.py`
test: `oops`
test: tests/z/test_w.py
test: `tests/x/test_y.py::test_valid`
3) Dispatch test: an openapi-template server registers.
"""
    )

    assert findings == (
        "tests/x/test_y.py: malformed test reference; expected path::test_symbol",
        "oops: malformed test reference; expected path::test_symbol",
        "tests/z/test_w.py: malformed test reference; expected path::test_symbol",
    )
