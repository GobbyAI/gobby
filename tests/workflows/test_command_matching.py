"""Segment-scoped command matching for block-effect command patterns (#21056).

``command_pattern`` sees one executable shell segment at a time. Heredoc bodies
are stdin data and leave the subject unless something can run them; the
consumer allow-list is ``cat``, ``tee``, ``git``, ``gh`` and a bare redirection,
and everything else fails closed.
"""

from __future__ import annotations

import pytest

from gobby.workflows.engine.command_matching import (
    command_patterns_match,
    executable_command_subjects,
)

pytestmark = pytest.mark.unit

PROSE_BODY = "Run `git commit` in the shared checkout, never `git push --force`."
COMMIT_PATTERN = r"(^|(?<=[;&|(`\n]))\s*git\s+commit\b"
PYTEST_PATTERN = r"(^|(?<=[;&|(`\n]))\s*(?:uv\s+run\s+)?pytest\b"
GUARD_EXEMPTION = r"(?s)(?=.*GOBBY_TEST_PROTECT=1)(?=.*DATABASE_URL=)"


class TestExecutableCommandSubjects:
    def test_segments_are_pipelines_with_raw_quotes_and_substitutions(self) -> None:
        command = 'cd /repo && echo "a; b" |\n  wc -l\nls $(pwd) || true'

        assert executable_command_subjects(command) == [
            "cd /repo",
            'echo "a; b" |\n  wc -l',
            "ls $(pwd)",
            "true",
        ]

    @pytest.mark.parametrize(
        "opener",
        [
            "cat >> notes.md",
            "tee -a notes.md",
            "> notes.md",
            "git commit -F -",
            "gh pr create --body-file -",
            "GIT_EDITOR=true cat",
        ],
    )
    def test_quoted_heredoc_to_a_data_consumer_is_dropped(self, opener: str) -> None:
        command = f"{opener} <<'EOF'\n{PROSE_BODY}\nEOF\necho done"

        assert executable_command_subjects(command) == [f"{opener} <<'EOF'", "echo done"]

    @pytest.mark.parametrize(
        "opener",
        [
            "bash",
            "sh",
            "python -",
            "zsh -s",
            "ssh build-host",
            "unknown-tool",
            "sudo cat",
            "xargs -0",
        ],
    )
    def test_heredoc_to_an_interpreter_or_unknown_consumer_stays_attached(
        self, opener: str
    ) -> None:
        command = f"{opener} <<'EOF'\n{PROSE_BODY}\nEOF"

        assert executable_command_subjects(command) == [f"{opener} <<'EOF'\n{PROSE_BODY}"]

    def test_body_piped_onward_stays_attached_when_a_stage_can_run_it(self) -> None:
        assert executable_command_subjects("cat <<'EOF' | bash\nbody\nEOF") == [
            "cat <<'EOF' | bash\nbody"
        ]
        assert executable_command_subjects("cat <<'EOF' | tee out.txt\nbody\nEOF") == [
            "cat <<'EOF' | tee out.txt"
        ]
        # A pipeline continuation defers the body past the next stage; the
        # pipeline is still one segment and its downstream shell runs the body.
        assert executable_command_subjects("cat <<'EOF' |\n  bash\nbody\nEOF") == [
            "cat <<'EOF' |\n  bash\nbody"
        ]

    def test_output_process_substitution_keeps_the_body(self) -> None:
        command = "cat <<'EOF' > >(bash)\nbody\nEOF"

        assert executable_command_subjects(command) == ["cat <<'EOF' > >(bash)\nbody"]

    def test_unquoted_delimiter_keeps_the_body_only_with_live_expansion(self) -> None:
        assert executable_command_subjects("cat <<EOF\n$(git push --force)\nEOF") == [
            "cat <<EOF\n$(git push --force)"
        ]
        assert executable_command_subjects("cat <<EOF\n`git push --force`\nEOF") == [
            "cat <<EOF\n`git push --force`"
        ]
        assert executable_command_subjects("cat <<EOF\nplain $HOME prose\nEOF") == ["cat <<EOF"]

    def test_unterminated_heredoc_keeps_its_swallowed_text(self) -> None:
        command = "cat <<'EOF'\ngit push --force"

        assert executable_command_subjects(command) == [command]

    def test_unparseable_or_empty_commands_are_matched_whole(self) -> None:
        assert executable_command_subjects("echo 'open") == ["echo 'open"]
        assert executable_command_subjects("   ") == ["   "]


class TestCommandPatternsMatch:
    def test_missing_pattern_is_unconstrained(self) -> None:
        assert command_patterns_match("anything", pattern=None)
        assert command_patterns_match("anything", pattern="")

    def test_line_anchored_pattern_sees_each_segment_as_its_own_line(self) -> None:
        assert command_patterns_match("cd /repo && uv run pytest", pattern=r"^uv run pytest\s*$")
        assert not command_patterns_match(
            "cd /repo && uv run pytest tests/x.py", pattern=r"^uv run pytest\s*$"
        )

    def test_exemption_reads_the_whole_executable_text(self) -> None:
        exported = (
            "export GOBBY_TEST_PROTECT=1\n"
            "export DATABASE_URL=postgresql://gobby_test@127.0.0.1:60892/gobby_test\n"
            "uv run pytest tests/x.py"
        )

        assert not command_patterns_match(
            exported, pattern=PYTEST_PATTERN, not_pattern=GUARD_EXEMPTION
        )
        assert command_patterns_match(
            "cd /repo && uv run pytest tests/x.py",
            pattern=PYTEST_PATTERN,
            not_pattern=GUARD_EXEMPTION,
        )

    def test_inert_heredoc_prose_cannot_select_a_block(self) -> None:
        assert not command_patterns_match(
            f"cat >> notes.md <<'EOF'\n{PROSE_BODY}\nEOF", pattern=COMMIT_PATTERN
        )
        assert command_patterns_match(f"bash <<'EOF'\n{PROSE_BODY}\nEOF", pattern=COMMIT_PATTERN)

    def test_mask_quoted_blanks_string_data_inside_each_subject(self) -> None:
        command = "bash <<'EOF'\necho 'x; git commit'\nEOF"

        assert command_patterns_match(command, pattern=COMMIT_PATTERN)
        assert not command_patterns_match(command, pattern=COMMIT_PATTERN, mask_quoted=True)
