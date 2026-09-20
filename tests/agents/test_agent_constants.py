"""Tests for agent constants module."""

import hashlib
import os
import re
from pathlib import Path

import pytest

from gobby.agents.cargo_target import checkout_cargo_target_dir
from gobby.agents.constants import (
    ALL_TERMINAL_ENV_VARS,
    CARGO_HOME,
    CARGO_TARGET_DIR,
    GOBBY_AGENT_API_TOKEN,
    GOBBY_AGENT_DEPTH,
    GOBBY_AGENT_RUN_ID,
    GOBBY_MAX_AGENT_DEPTH,
    GOBBY_PARENT_SESSION_ID,
    GOBBY_PROJECT_ID,
    GOBBY_PROMPT,
    GOBBY_PROMPT_FILE,
    GOBBY_SESSION_ID,
    GOBBY_TERMINAL_ID,
    GOBBY_WORKFLOW_NAME,
    IDENTITY_ENV_VARS,
    UV_CACHE_DIR,
    get_agent_cargo_home_dir,
    get_agent_uv_cache_dir,
    get_terminal_env_vars,
)
from gobby.agents.spawn_cache_policy import PATH_ENV_VAR, managed_tool_bin_dir
from gobby.utils.local_token import (
    AGENT_TOKEN_MAX_TTL_SECONDS,
    local_token_path,
    verify_agent_api_token,
)

pytestmark = pytest.mark.unit
_HASH_SUFFIX_RE = re.compile(r".+-[0-9a-f]{16}$")


def _expected_cache_leaf(prefix: str, session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


class TestEnvironmentVariableConstants:
    """Tests for environment variable constant definitions."""

    def test_constants_are_strings(self) -> None:
        """All constants are string values."""
        assert isinstance(GOBBY_SESSION_ID, str)
        assert isinstance(GOBBY_PARENT_SESSION_ID, str)
        assert isinstance(GOBBY_AGENT_RUN_ID, str)
        assert isinstance(GOBBY_WORKFLOW_NAME, str)
        assert isinstance(GOBBY_PROJECT_ID, str)
        assert isinstance(GOBBY_AGENT_DEPTH, str)
        assert isinstance(GOBBY_MAX_AGENT_DEPTH, str)
        assert isinstance(GOBBY_PROMPT, str)
        assert isinstance(GOBBY_PROMPT_FILE, str)
        assert isinstance(UV_CACHE_DIR, str)
        assert isinstance(CARGO_HOME, str)

    def test_constants_are_uppercase(self) -> None:
        """All constants follow ENV_VAR naming convention."""
        for var in ALL_TERMINAL_ENV_VARS:
            assert var == var.upper(), f"{var} should be uppercase"

    def test_constants_start_with_gobby(self) -> None:
        """All Gobby-owned constants are prefixed with GOBBY_."""
        for var in ALL_TERMINAL_ENV_VARS:
            if var in {UV_CACHE_DIR, CARGO_HOME, CARGO_TARGET_DIR}:
                continue
            assert var.startswith("GOBBY_"), f"{var} should start with GOBBY_"

    def test_all_terminal_env_vars_complete(self) -> None:
        """ALL_TERMINAL_ENV_VARS contains all constants."""
        expected = {
            "GOBBY_DAEMON_URL",
            GOBBY_SESSION_ID,
            GOBBY_PARENT_SESSION_ID,
            GOBBY_AGENT_RUN_ID,
            GOBBY_AGENT_API_TOKEN,
            GOBBY_WORKFLOW_NAME,
            GOBBY_PROJECT_ID,
            GOBBY_AGENT_DEPTH,
            GOBBY_MAX_AGENT_DEPTH,
            GOBBY_PROMPT,
            GOBBY_PROMPT_FILE,
            UV_CACHE_DIR,
            CARGO_HOME,
            CARGO_TARGET_DIR,
            *IDENTITY_ENV_VARS,
        }
        assert set(ALL_TERMINAL_ENV_VARS) == expected

    def test_identity_env_vars_name_the_terminal_and_pane(self) -> None:
        """Identity names stay distinct from the agent-only project and daemon variables."""
        assert IDENTITY_ENV_VARS == (
            "GOBBY_TERMINAL_ID",
            "GOBBY_NODE_ID",
            "GOBBY_NODE_REF",
            "GOBBY_WORKSPACE_ID",
            "GOBBY_TAB_ID",
            "GOBBY_PANE_ID",
            "GOBBY_PANE_REF",
        )
        assert GOBBY_TERMINAL_ID == "GOBBY_TERMINAL_ID"


class TestGetTerminalEnvVars:
    """Tests for get_terminal_env_vars function."""

    def test_returns_all_required_vars(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Function returns all required environment variables."""
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        checkout = tmp_path / "checkout"
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            checkout_root=checkout,
        )

        assert result[GOBBY_SESSION_ID] == "sess-child"
        assert result[GOBBY_PARENT_SESSION_ID] == "sess-parent"
        assert result[GOBBY_AGENT_RUN_ID] == "run-123"
        assert result[GOBBY_PROJECT_ID] == "proj-abc"
        uv_cache_parts = Path(result[UV_CACHE_DIR]).parts
        assert uv_cache_parts[-3:-1] == ("gobby", "uv-cache")
        assert uv_cache_parts[-1].startswith("sess-child-")
        assert _HASH_SUFFIX_RE.fullmatch(uv_cache_parts[-1])
        assert uv_cache_parts[-1] == _expected_cache_leaf("sess-child", "sess-child")
        shared_cargo_home = tmp_path / "cache" / "cargo-home"
        assert result[CARGO_HOME] == str(shared_cargo_home)
        checkout_target = checkout_cargo_target_dir(checkout, "proj-abc")
        assert result[CARGO_TARGET_DIR] == str(checkout_target)
        assert checkout_target.is_dir()

    def test_includes_run_bound_agent_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        local_token_path().write_text("operator-token\n")

        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            operator_token="operator-token",
        )

        claims = verify_agent_api_token(result[GOBBY_AGENT_API_TOKEN], "operator-token")
        assert claims is not None
        assert claims.agent_run_id == "run-123"
        assert claims.session_id == "sess-child"
        assert claims.project_id == "proj-abc"
        # Untimed runs get the fixed expiry ceiling.
        assert claims.exp - claims.iat == AGENT_TOKEN_MAX_TTL_SECONDS

    def test_run_timeout_bounds_agent_token_expiry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        local_token_path().write_text("operator-token\n")

        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            operator_token="operator-token",
            timeout_seconds=300,
        )

        claims = verify_agent_api_token(result[GOBBY_AGENT_API_TOKEN], "operator-token")
        assert claims is not None
        # Declared run timeout plus the fixed 60-second grace.
        assert claims.exp - claims.iat == 360

    def test_uv_cache_dir_sanitizes_session_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """uv cache paths are writable temp paths scoped to safe session IDs."""
        monkeypatch.setattr("gobby.agents.constants.tempfile.gettempdir", lambda: "/tmp/test-tmp")

        result = get_agent_uv_cache_dir("sess/child:one")

        result_path = Path(result)
        assert result_path.parts[-3:-1] == ("gobby", "uv-cache")
        assert result_path.parts[-1].startswith("sess-child-one-")
        assert _HASH_SUFFIX_RE.fullmatch(result_path.parts[-1])
        assert result_path.parts[-1] == _expected_cache_leaf("sess-child-one", "sess/child:one")

    def test_cargo_home_is_shared_across_sessions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Every spawned agent gets one Cargo home under Gobby home, not a per-session one.

        Cargo fingerprints embed dependency source paths, so a per-session home
        would invalidate every dependency in checkout-specific CARGO_TARGET_DIRs.
        """
        monkeypatch.setattr("gobby.agents.constants.get_gobby_home", lambda: tmp_path)

        first = get_agent_cargo_home_dir("sess/child:one")
        second = get_agent_cargo_home_dir("a-totally-different-session")

        assert first == second
        assert Path(first) == tmp_path / "cache" / "cargo-home"

    def test_cargo_target_is_checkout_specific_while_home_is_shared(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "home"))
        first_checkout = tmp_path / "main"
        second_checkout = tmp_path / "worktree"

        first = get_terminal_env_vars(
            session_id="first",
            parent_session_id="parent",
            agent_run_id="run-first",
            project_id="project",
            checkout_root=first_checkout,
        )
        second = get_terminal_env_vars(
            session_id="second",
            parent_session_id="parent",
            agent_run_id="run-second",
            project_id="project",
            checkout_root=second_checkout,
        )

        assert first[CARGO_HOME] == second[CARGO_HOME]
        assert first[CARGO_TARGET_DIR] != second[CARGO_TARGET_DIR]

    def test_cargo_home_never_touches_operator_cargo_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The shared home stays under Gobby home and never resolves to ~/.cargo."""
        monkeypatch.setattr("gobby.agents.constants.get_gobby_home", lambda: tmp_path)

        result = Path(get_agent_cargo_home_dir("sess-one"))

        assert result.is_relative_to(tmp_path)
        assert result != Path.home() / ".cargo"

    def test_includes_managed_tool_bin_on_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Spawned agents inherit access to all ~/.gobby/bin tools."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("PATH", os.pathsep.join(("/usr/bin", "/bin")))

        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert result[PATH_ENV_VAR].split(os.pathsep)[:3] == [
            managed_tool_bin_dir(),
            "/usr/bin",
            "/bin",
        ]

    def test_includes_workflow_when_provided(self) -> None:
        """Function includes workflow name when provided."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            workflow_name="plan-execute",
        )

        assert result[GOBBY_WORKFLOW_NAME] == "plan-execute"

    def test_omits_workflow_when_none(self) -> None:
        """Function omits workflow name when not provided."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            workflow_name=None,
        )

        assert GOBBY_WORKFLOW_NAME not in result

    def test_includes_depth_info(self) -> None:
        """Function includes agent depth information."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            agent_depth=2,
            max_agent_depth=5,
        )

        assert result[GOBBY_AGENT_DEPTH] == "2"
        assert result[GOBBY_MAX_AGENT_DEPTH] == "5"

    def test_default_depth_values(self) -> None:
        """Function uses default depth values."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert result[GOBBY_AGENT_DEPTH] == "1"
        assert result[GOBBY_MAX_AGENT_DEPTH] == "5"

    def test_omits_parent_session_id_when_empty(self) -> None:
        """Function omits parent_session_id when empty string."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert GOBBY_PARENT_SESSION_ID not in result

    def test_includes_parent_session_id_when_provided(self) -> None:
        """Function includes parent_session_id when non-empty."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
        )

        assert result[GOBBY_PARENT_SESSION_ID] == "sess-parent"

    def test_all_values_are_strings(self) -> None:
        """Function returns all values as strings."""
        result = get_terminal_env_vars(
            session_id="sess-child",
            parent_session_id="sess-parent",
            agent_run_id="run-123",
            project_id="proj-abc",
            workflow_name="test",
            agent_depth=1,
            max_agent_depth=3,
        )

        for key, value in result.items():
            assert isinstance(value, str), f"Value for {key} should be string"
