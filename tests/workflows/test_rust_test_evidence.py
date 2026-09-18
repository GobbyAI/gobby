"""Rust cfg(test) evidence for the TDD gate.

Covers the two shapes the gate must recognize beyond path conventions for
Rust: Edit/Write changes that introduce a `#[cfg(test)]` item and changes that
land entirely inside an existing one. Changes that also touch production text
stay production writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.workflows.rust_test_evidence import rust_edit_is_test_writing

RELATIVE_PATH = "crates/gcore/src/store.rs"

PRODUCTION_ONLY = """\
use std::fmt;

pub struct Frame {
    pub id: u64,
}

pub fn encode(frame: &Frame) -> String {
    format!("frame {}", frame.id)
}
"""

TEST_MODULE = """
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_reports_id() {
        assert_eq!(encode(&Frame { id: 7 }), "frame 7");
    }
}
"""

MODULE_WITH_TESTS = PRODUCTION_ONLY + TEST_MODULE

MODULE_WITH_HIDDEN_BRACES = (
    PRODUCTION_ONLY
    + """
#[cfg(test)]
mod tests {
    use super::*;

    const CLOSING: &str = "}";
    // a stray } inside a comment
    const BRACE: char = '}';

    fn borrow<'a>(value: &'a str) -> &'a str {
        value
    }

    #[test]
    fn encode_reports_id() {
        assert_eq!(encode(&Frame { id: 7 }), "frame 7");
    }
}
"""
)

INSIDE_OLD = '        assert_eq!(encode(&Frame { id: 7 }), "frame 7");'
INSIDE_NEW = '        assert_eq!(encode(&Frame { id: 9 }), "frame 9");'

PRODUCTION_BODY = 'pub fn encode(frame: &Frame) -> String {\n    format!("frame {}", frame.id)\n}\n'


def _rust_file(tmp_path: Path, text: str, relative: str = RELATIVE_PATH) -> str:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestIntroducedTestItems:
    """Changes that add cfg(test) code to a production file."""

    def test_edit_appending_inline_test_module_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)
        tool_input = {
            "file_path": path,
            "old_string": PRODUCTION_BODY,
            "new_string": PRODUCTION_BODY + TEST_MODULE,
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_write_adding_inline_test_module_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)

        assert (
            rust_edit_is_test_writing(path, {"file_path": path, "content": MODULE_WITH_TESTS})
            is True
        )

    def test_module_declaration_for_tests_rs_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)
        tool_input = {
            "file_path": path,
            "old_str": PRODUCTION_BODY,
            "new_str": PRODUCTION_BODY + "\n#[cfg(test)]\nmod tests;\n",
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_cfg_test_use_item_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)
        tool_input = {
            "file_path": path,
            "old_str": PRODUCTION_BODY,
            "new_str": PRODUCTION_BODY + "\n#[cfg(test)]\nuse std::sync::{Arc, Mutex};\n",
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_test_among_cfg_predicates_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)
        tool_input = {
            "file_path": path,
            "old_str": PRODUCTION_BODY,
            "new_str": PRODUCTION_BODY
            + "\n#[cfg(all(test, unix))]\nmod tests {\n    #[test]\n    fn unix_only() {}\n}\n",
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_new_file_write_of_only_test_code_counts(self, tmp_path: Path) -> None:
        path = str(tmp_path / RELATIVE_PATH)
        content = "#[cfg(test)]\nmod tests {\n    #[test]\n    fn works() {}\n}\n"

        assert rust_edit_is_test_writing(path, {"file_path": path, "content": content}) is True

    def test_new_file_write_of_production_code_stays_production(self, tmp_path: Path) -> None:
        path = str(tmp_path / RELATIVE_PATH)

        assert (
            rust_edit_is_test_writing(path, {"file_path": path, "content": MODULE_WITH_TESTS})
            is False
        )


class TestEditsInsideExistingTestModule:
    """Changes confined to an existing cfg(test) module."""

    def test_edit_inside_module_before_the_write_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        tool_input = {"file_path": path, "old_str": INSIDE_OLD, "new_str": INSIDE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_edit_inside_module_after_the_write_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS.replace(INSIDE_OLD, INSIDE_NEW))
        tool_input = {"file_path": path, "old_str": INSIDE_OLD, "new_str": INSIDE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_braces_hidden_in_strings_comments_and_literals_keep_the_span(
        self, tmp_path: Path
    ) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_HIDDEN_BRACES)
        tool_input = {"file_path": path, "old_str": INSIDE_OLD, "new_str": INSIDE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_production_edit_beside_hidden_braces_stays_production(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_HIDDEN_BRACES)
        tool_input = {
            "file_path": path,
            "old_str": "pub fn encode(frame: &Frame)",
            "new_str": "pub fn encode(frame: &Frame, pretty: bool)",
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_multi_edit_entirely_inside_module_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "edits": [
                {"old_string": INSIDE_OLD, "new_string": INSIDE_NEW},
                {"old_string": "    use super::*;", "new_string": "    use super::encode;"},
            ],
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_multi_edit_with_one_production_change_stays_production(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "edits": [
                {"old_string": INSIDE_OLD, "new_string": INSIDE_NEW},
                {"old_string": "pub fn encode", "new_string": "pub fn encode_frame"},
            ],
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_replace_all_reaching_production_stays_production(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "old_string": "frame",
            "new_string": "packet",
            "replace_all": True,
        }

        assert rust_edit_is_test_writing(path, tool_input) is False


class TestProductionChanges:
    """Changes the gate must keep treating as production writes."""

    def test_production_edit_outside_module_stays_production(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "old_str": "pub fn encode",
            "new_str": "pub fn encode_frame",
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_write_changing_production_beside_existing_module_stays_production(
        self, tmp_path: Path
    ) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        content = MODULE_WITH_TESTS.replace("pub fn encode(", "pub fn encode_frame(")

        assert rust_edit_is_test_writing(path, {"file_path": path, "content": content}) is False

    def test_edit_spanning_the_module_boundary_stays_production(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        old = '    format!("frame {}", frame.id)\n}\n\n#[cfg(test)]'
        tool_input = {
            "file_path": path,
            "old_str": old,
            "new_str": old.replace("frame {}", "frame #{}"),
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_edit_mixing_production_change_and_new_module_stays_production(
        self, tmp_path: Path
    ) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)
        tool_input = {
            "file_path": path,
            "old_str": PRODUCTION_BODY,
            "new_str": PRODUCTION_BODY.replace("-> String", "-> Option<String>") + TEST_MODULE,
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_cfg_not_test_item_stays_production(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)
        added = "\n#[cfg(not(test))]\nmod release {\n    pub fn go() {}\n}\n"
        tool_input = {
            "file_path": path,
            "old_str": PRODUCTION_BODY,
            "new_str": PRODUCTION_BODY + added,
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    @pytest.mark.parametrize(
        "added",
        [
            'pub const SAMPLE: &str = "#[cfg(test)] mod tests { }";\n',
            "// #[cfg(test)] mod tests { }\npub fn touched() {}\n",
        ],
        ids=["string-literal", "comment"],
    )
    def test_marker_outside_code_stays_production(self, tmp_path: Path, added: str) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)
        tool_input = {
            "file_path": path,
            "old_str": PRODUCTION_BODY,
            "new_str": PRODUCTION_BODY + "\n" + added,
        }

        assert rust_edit_is_test_writing(path, tool_input) is False


class TestInputAndPathScoping:
    """Guards on which payloads and paths the classifier reads."""

    def test_non_rust_path_never_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS, relative="src/gobby/app.py")
        tool_input = {"file_path": path, "content": MODULE_WITH_TESTS}

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_input_without_edit_text_never_counts(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)

        assert rust_edit_is_test_writing(path, {"file_path": path}) is False

    def test_edit_of_a_missing_file_stays_production(self, tmp_path: Path) -> None:
        path = str(tmp_path / RELATIVE_PATH)
        tool_input = {"file_path": path, "old_str": INSIDE_OLD, "new_str": INSIDE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_relative_path_is_never_read_from_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _rust_file(tmp_path, MODULE_WITH_TESTS)
        monkeypatch.chdir(tmp_path)
        tool_input = {"file_path": RELATIVE_PATH, "old_str": INSIDE_OLD, "new_str": INSIDE_NEW}

        assert rust_edit_is_test_writing(RELATIVE_PATH, tool_input) is False

    def test_foreign_file_path_input_is_ignored(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        other = str(tmp_path / "crates/gcore/src/other.rs")
        tool_input = {"file_path": other, "old_str": INSIDE_OLD, "new_str": INSIDE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_batch_changes_are_path_scoped(self, tmp_path: Path) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)
        other = _rust_file(tmp_path, PRODUCTION_ONLY, relative="crates/gcore/src/other.rs")
        changes: list[dict[str, Any]] = [
            {"file_path": other, "old_string": "pub fn encode", "new_string": "pub fn encode_all"},
            {"file_path": path, "old_string": INSIDE_OLD, "new_string": INSIDE_NEW},
        ]

        assert rust_edit_is_test_writing(path, {"changes": changes}) is True

    def test_written_file_already_on_disk_counts_when_it_holds_test_code(
        self, tmp_path: Path
    ) -> None:
        path = _rust_file(tmp_path, MODULE_WITH_TESTS)

        assert (
            rust_edit_is_test_writing(path, {"file_path": path, "content": MODULE_WITH_TESTS})
            is True
        )

    def test_written_file_already_on_disk_without_test_code_stays_production(
        self, tmp_path: Path
    ) -> None:
        path = _rust_file(tmp_path, PRODUCTION_ONLY)

        assert (
            rust_edit_is_test_writing(path, {"file_path": path, "content": PRODUCTION_ONLY})
            is False
        )
