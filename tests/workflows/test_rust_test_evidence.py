"""Rust cfg(test) test-writing evidence for the TDD gate.

Covers the two shapes the gate must recognize beyond path conventions for
Rust: Edit/Write changes that introduce an inline `#[cfg(test)]` module and
edits that land entirely inside an existing test module of a production
file. Rust edits outside such blocks stay production writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.workflows.rust_test_evidence import rust_edit_is_test_writing

MODULE_WITH_TESTS = """\
use std::fmt;

pub struct Frame {
    pub id: u64,
}

pub fn encode(frame: &Frame) -> String {
    format!("frame {}", frame.id)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encode_reports_id() {
        assert_eq!(encode(&Frame { id: 7 }), "frame 7");
    }
}
"""

INSIDE_MODULE_OLD = '        assert_eq!(encode(&Frame { id: 7 }), "frame 7");'
INSIDE_MODULE_NEW = '        assert_eq!(encode(&Frame { id: 9 }), "frame 9");'

INLINE_MODULE_ADDITION = (
    "pub fn encode(frame: &Frame) -> String {\n"
    '    format!("frame {}", frame.id)\n'
    "}\n"
    "\n"
    "#[cfg(test)]\n"
    "mod tests {\n"
    "    use super::*;\n"
    "\n"
    "    #[test]\n"
    "    fn encode_reports_id() {\n"
    '        assert_eq!(encode(&Frame { id: 7 }), "frame 7");\n'
    "    }\n"
    "}\n"
)


def _write_rust_file(tmp_path: Path, relative: str, text: str) -> str:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestInlineCfgTestAdditions:
    def test_edit_adding_inline_cfg_test_module(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/store.rs",
            "old_str": "pub fn encode(frame: &Frame) -> String {",
            "new_str": INLINE_MODULE_ADDITION,
        }

        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", tool_input) is True

    def test_claude_edit_key_names(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/store.rs",
            "old_string": "pub fn encode(frame: &Frame) -> String {",
            "new_string": "#[cfg(all(test, unix))]\nmod tests {\n    #[test]\n    fn unix_only() {}\n}\n",
        }

        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", tool_input) is True

    def test_write_content_adding_inline_module(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/store.rs",
            "content": "pub fn encode() {}\n\n#[cfg(test)]\nmod tests {}\n",
        }

        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", tool_input) is True

    def test_cfg_with_test_among_any_predicates(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/store.rs",
            "new_str": "#[cfg(any(unix, test))]\nmod tests {}\n",
        }

        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", tool_input) is True

    def test_path_attribute_declaration_counts(self) -> None:
        tool_input = {
            "file_path": "crates/gterminal/src/host/backpressure.rs",
            "new_str": '#[cfg(test)]\n#[path = "backpressure/tests.rs"]\nmod tests;\n',
        }

        assert (
            rust_edit_is_test_writing("crates/gterminal/src/host/backpressure.rs", tool_input)
            is True
        )

    def test_negated_cfg_not_test_does_not_count(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/store.rs",
            "new_str": "#[cfg(not(test))]\nmod nightly {}\n",
        }

        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", tool_input) is False

    def test_marker_inside_string_literal_does_not_count(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/store.rs",
            "new_str": 'let source = "#[cfg(test)] mod tests {}";\n',
        }

        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", tool_input) is False

    def test_marker_inside_comment_does_not_count(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/store.rs",
            "new_str": "// keep the #[cfg(test)] module below\n",
        }

        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", tool_input) is False

    def test_non_rust_path_never_counts(self) -> None:
        tool_input = {"file_path": "src/gobby/store.py", "new_str": "#[cfg(test)]\nmod tests;\n"}

        assert rust_edit_is_test_writing("src/gobby/store.py", tool_input) is False

    def test_input_without_edit_text_never_counts(self) -> None:
        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", {"file_path": "x"}) is False
        assert rust_edit_is_test_writing("crates/gcore/src/store.rs", {}) is False


class TestEditsInsideExistingModule:
    def test_edit_inside_module_before_write(self, tmp_path: Path) -> None:
        path = _write_rust_file(tmp_path, "src/store.rs", MODULE_WITH_TESTS)
        tool_input = {"file_path": path, "old_str": INSIDE_MODULE_OLD, "new_str": INSIDE_MODULE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_edit_inside_module_after_write(self, tmp_path: Path) -> None:
        edited = MODULE_WITH_TESTS.replace(INSIDE_MODULE_OLD, INSIDE_MODULE_NEW)
        path = _write_rust_file(tmp_path, "src/store.rs", edited)
        tool_input = {"file_path": path, "old_str": INSIDE_MODULE_OLD, "new_str": INSIDE_MODULE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_public_crate_module_visibility_is_recognized(self, tmp_path: Path) -> None:
        source = MODULE_WITH_TESTS.replace(
            "#[cfg(test)]\nmod tests {", "#[cfg(test)]\npub(crate) mod tests {"
        )
        path = _write_rust_file(tmp_path, "src/store.rs", source)
        tool_input = {"file_path": path, "old_str": INSIDE_MODULE_OLD, "new_str": INSIDE_MODULE_NEW}

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_production_edit_outside_module_stays_production(self, tmp_path: Path) -> None:
        path = _write_rust_file(tmp_path, "src/store.rs", MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "old_str": "pub fn encode(frame: &Frame) -> String {",
            "new_str": "pub fn encode(frame: &Frame, quoted: bool) -> String {",
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_edit_spanning_module_boundary_stays_production(self, tmp_path: Path) -> None:
        path = _write_rust_file(tmp_path, "src/store.rs", MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "old_str": '    format!("frame {}", frame.id)\n}\n\n#[cfg(test)]\nmod tests {',
            "new_str": '    format!("frame {}", frame.id)\n}\n\nmod tests {',
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_multi_edit_fully_inside_module_counts(self, tmp_path: Path) -> None:
        path = _write_rust_file(tmp_path, "src/store.rs", MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "edits": [
                {"old_str": "    use super::*;", "new_str": "    use super::*;\n    use std::fmt;"},
                {"old_str": INSIDE_MODULE_OLD, "new_str": INSIDE_MODULE_NEW},
            ],
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_multi_edit_with_one_production_change_stays_production(self, tmp_path: Path) -> None:
        path = _write_rust_file(tmp_path, "src/store.rs", MODULE_WITH_TESTS)
        tool_input = {
            "file_path": path,
            "edits": [
                {"old_str": INSIDE_MODULE_OLD, "new_str": INSIDE_MODULE_NEW},
                {"old_str": "pub fn encode", "new_str": "pub fn encode_quoted"},
            ],
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_unbalanced_string_brace_still_spans_module(self, tmp_path: Path) -> None:
        source = (
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    #[test]\n"
            "    fn string_brace_is_masked() {\n"
            '        let open = "{";\n'
            "        assert_eq!(open.len(), 1);\n"
            "    }\n"
            "}\n"
        )
        path = _write_rust_file(tmp_path, "src/store.rs", source)
        tool_input = {
            "file_path": path,
            "old_str": "        assert_eq!(open.len(), 1);",
            "new_str": "        assert_eq!(open.len(), 2);",
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_comment_close_brace_still_spans_module(self, tmp_path: Path) -> None:
        source = (
            "#[cfg(test)]\n"
            "mod tests {\n"
            "    // } a closing brace inside a comment\n"
            "    #[test]\n"
            "    fn comment_brace_is_masked() {\n"
            "        assert!(true);\n"
            "    }\n"
            "}\n"
        )
        path = _write_rust_file(tmp_path, "src/store.rs", source)
        tool_input = {
            "file_path": path,
            "old_str": "        assert!(true);",
            "new_str": "        assert!(false);",
        }

        assert rust_edit_is_test_writing(path, tool_input) is True

    def test_missing_file_without_marker_stays_production(self) -> None:
        tool_input = {
            "file_path": "crates/gcore/src/absent.rs",
            "old_str": "fn helper() {}",
            "new_str": "fn helper(code: u8) {}",
        }

        assert rust_edit_is_test_writing("crates/gcore/src/absent.rs", tool_input) is False


class TestPathScopedEditInput:
    def test_foreign_file_path_input_is_ignored(self, tmp_path: Path) -> None:
        path = _write_rust_file(tmp_path, "src/store.rs", MODULE_WITH_TESTS)
        tool_input = {
            "file_path": "/other/project/src/lib.rs",
            "new_str": "#[cfg(test)]\nmod tests {}\n",
        }

        assert rust_edit_is_test_writing(path, tool_input) is False

    def test_batch_changes_are_path_scoped(self, tmp_path: Path) -> None:
        store = _write_rust_file(tmp_path, "src/store.rs", MODULE_WITH_TESTS)
        other = _write_rust_file(tmp_path, "src/other.rs", MODULE_WITH_TESTS)
        changes: list[dict[str, Any]] = [
            {"path": other, "old_str": "pub fn encode", "new_str": "pub fn encode_quoted"},
            {"path": store, "old_str": INSIDE_MODULE_OLD, "new_str": INSIDE_MODULE_NEW},
        ]

        assert rust_edit_is_test_writing(store, {"changes": changes}) is True
        assert rust_edit_is_test_writing(other, {"changes": changes}) is False
