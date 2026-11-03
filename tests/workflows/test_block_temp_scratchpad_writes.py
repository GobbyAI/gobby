"""Generic admitted workspace directory helper."""

import pytest

from gobby.workflows.safe_evaluator import build_condition_helpers

pytestmark = pytest.mark.unit

SRT_SOCKET_ROOT_VARIABLE = "_agent_srt_socket_root"

SRT_RUN_ROOT = "/private/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-aaaa1111"

EPIC_WORKSPACE = (
    "/Users/dev/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/epics/epic-7"
)

ADMITTED_DIRS_HELPER = "temp_write_admitted_dirs"

WORKSPACE = (
    "/Users/dev/.gobby/agents_workspaces/d45545c5-ded5-4335-b115-0245752edacf/seat/session-1"
)


def test_admitted_dirs_are_the_workspace_folders_and_the_own_socket_root() -> None:
    helper = build_condition_helpers()[ADMITTED_DIRS_HELPER]
    variables = {
        "_agent_workspace_dir": WORKSPACE,
        "_agent_workspace_epic_dir": EPIC_WORKSPACE,
        SRT_SOCKET_ROOT_VARIABLE: SRT_RUN_ROOT,
    }
    assert helper(variables) == [WORKSPACE, EPIC_WORKSPACE, SRT_RUN_ROOT]


def test_admitted_dirs_without_a_socket_root_are_the_workspace_folders_only() -> None:
    helper = build_condition_helpers()[ADMITTED_DIRS_HELPER]
    assert helper({"_agent_workspace_dir": WORKSPACE}) == [WORKSPACE]


@pytest.mark.parametrize("value", ["", None, 7], ids=["empty", "null", "not-a-string"])
def test_admitted_dirs_ignore_a_malformed_socket_root(value: object) -> None:
    helper = build_condition_helpers()[ADMITTED_DIRS_HELPER]
    assert helper({"_agent_workspace_dir": WORKSPACE, SRT_SOCKET_ROOT_VARIABLE: value}) == [
        WORKSPACE
    ]
