"""Tests for template_writer.py — YAML write/read/delete for user templates."""

from pathlib import Path
from typing import TextIO

import pytest
import yaml

from gobby.workflows.template_writer import read_template


@pytest.mark.parametrize("content", ["- item\n", "plain scalar\n", ""])
def test_read_template_rejects_non_mapping_yaml(tmp_path: Path, content: str) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="expected YAML object"):
        read_template(path)


class TestWriteRuleTemplate:
    """Tests for write_rule_template."""

    def test_round_trip(self, tmp_path):
        from gobby.workflows.template_writer import read_template, write_rule_template

        definition = {
            "event": {"type": "pre_tool_use"},
            "effect": {"action": "block", "message": "Blocked"},
        }
        path = write_rule_template(
            name="my-rule",
            definition=definition,
            output_dir=tmp_path / "rules",
        )

        assert path.exists()
        assert path.name == "my-rule.yaml"

        data = read_template(path)
        assert "rules" in data
        assert "my-rule" in data["rules"]
        assert data["rules"]["my-rule"]["event"]["type"] == "pre_tool_use"

    def test_creates_directory(self, tmp_path):
        from gobby.workflows.template_writer import write_rule_template

        output_dir = tmp_path / "deep" / "nested" / "rules"
        write_rule_template(
            name="test-rule",
            definition={"event": {"type": "pre_tool_use"}, "effect": {"action": "allow"}},
            output_dir=output_dir,
        )
        assert output_dir.exists()

    def test_overwrites_existing(self, tmp_path):
        from gobby.workflows.template_writer import write_rule_template

        output_dir = tmp_path / "rules"
        write_rule_template(
            name="evolving",
            definition={"event": {"type": "pre_tool_use"}, "effect": {"action": "allow"}},
            output_dir=output_dir,
        )
        write_rule_template(
            name="evolving",
            definition={
                "event": {"type": "pre_tool_use"},
                "effect": {"action": "block", "message": "no"},
            },
            output_dir=output_dir,
        )

        data = yaml.safe_load((output_dir / "evolving.yaml").read_text())
        assert data["rules"]["evolving"]["effect"]["action"] == "block"

    def test_partial_write_preserves_existing_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.workflows import template_writer

        output_dir = tmp_path / "rules"
        output_dir.mkdir()
        path = output_dir / "evolving.yaml"
        original = "rules:\n  evolving:\n    effect:\n      action: allow\n"
        path.write_text(original, encoding="utf-8")

        def fail_after_partial_write(_data: object, stream: TextIO, **_kwargs: object) -> None:
            stream.write("rules:\n  evolving:")
            raise OSError("simulated interrupted write")

        monkeypatch.setattr(template_writer.yaml, "dump", fail_after_partial_write)

        with pytest.raises(OSError, match="simulated interrupted write"):
            template_writer.write_rule_template(
                name="evolving",
                definition={
                    "event": {"type": "pre_tool_use"},
                    "effect": {"action": "block"},
                },
                output_dir=output_dir,
            )

        assert path.read_text(encoding="utf-8") == original
        assert list(output_dir.glob(".evolving.yaml.*.tmp")) == []

    def test_preserves_metadata(self, tmp_path):
        from gobby.workflows.template_writer import write_rule_template

        write_rule_template(
            name="meta-rule",
            definition={"event": {"type": "pre_tool_use"}, "effect": {"action": "allow"}},
            output_dir=tmp_path,
            group="security",
            tags=["user", "custom"],
        )
        data = yaml.safe_load((tmp_path / "meta-rule.yaml").read_text())
        assert data.get("group") == "security"
        assert data.get("tags") == ["user", "custom"]


class TestWritePipelineTemplate:
    """Tests for write_pipeline_template."""

    def test_round_trip(self, tmp_path):
        from gobby.workflows.template_writer import read_template, write_pipeline_template

        definition = {
            "name": "my-pipe",
            "type": "pipeline",
            "version": "1.0",
            "steps": [{"id": "step1", "exec": "echo hello"}],
        }
        path = write_pipeline_template(
            name="my-pipe",
            definition=definition,
            output_dir=tmp_path / "pipelines",
        )
        assert path.exists()

        data = read_template(path)
        assert data["name"] == "my-pipe"
        assert data["steps"][0]["id"] == "step1"


class TestWriteAgentTemplate:
    """Tests for write_agent_template."""

    def test_round_trip(self, tmp_path):
        from gobby.workflows.template_writer import read_template, write_agent_template

        definition = {
            "name": "my-agent",
            "cli": "claude",
            "description": "A test agent",
        }
        path = write_agent_template(
            name="my-agent",
            definition=definition,
            output_dir=tmp_path / "agents",
        )
        assert path.exists()

        data = read_template(path)
        assert data["name"] == "my-agent"
        assert data["cli"] == "claude"


class TestWriteVariableTemplate:
    """Tests for write_variable_template."""

    def test_round_trip(self, tmp_path):
        from gobby.workflows.template_writer import read_template, write_variable_template

        path = write_variable_template(
            name="my-var",
            definition={"type": "string", "default": "hello", "description": "A greeting"},
            output_dir=tmp_path / "variables",
        )
        assert path.exists()

        data = read_template(path)
        assert "variables" in data
        assert "my-var" in data["variables"]
        assert data["variables"]["my-var"]["default"] == "hello"


class TestDeleteTemplateFile:
    """Tests for delete_template_file."""

    def test_deletes_existing(self, tmp_path):
        from gobby.workflows.template_writer import delete_template_file, write_rule_template

        write_rule_template(
            name="doomed",
            definition={"event": {"type": "pre_tool_use"}, "effect": {"action": "allow"}},
            output_dir=tmp_path,
        )
        assert (tmp_path / "doomed.yaml").exists()

        deleted = delete_template_file("doomed", tmp_path)
        assert deleted is True
        assert not (tmp_path / "doomed.yaml").exists()

    def test_returns_false_for_missing(self, tmp_path):
        from gobby.workflows.template_writer import delete_template_file

        deleted = delete_template_file("nonexistent", tmp_path)
        assert deleted is False
