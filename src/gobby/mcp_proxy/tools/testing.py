"""Internal MCP tools for gobby-tests.

Exposes:
- run_check: Run a verification command by category
- get_run_status: Check if a run is complete
- get_run_result: Get summary or paginated raw output
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.config.features import TestSummarizerConfig
    from gobby.llm.service import LLMService
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


def create_testing_registry(
    db: DatabaseProtocol,
    llm_service: LLMService | None = None,
    config: TestSummarizerConfig | None = None,
) -> InternalToolRegistry:
    """Create a testing tool registry with verification command tools.

    Args:
        db: Database for test run storage
        llm_service: LLM service for failure summarization
        config: Test summarizer configuration

    Returns:
        InternalToolRegistry with testing tools registered
    """
    from gobby.storage.test_runs import TestRunStorage
    from gobby.testing.runner import TestRunner

    storage = TestRunStorage(db)
    storage.cleanup_stale_runs()
    runner = TestRunner(storage=storage, llm_service=llm_service, config=config)

    registry = InternalToolRegistry(
        name="gobby-tests",
        description="Run project tests/lint/typecheck with token-efficient output summaries",
    )

    @registry.tool(
        name="run_check",
        description=(
            "Run a project verification command (tests, lint, typecheck) by category. "
            "Resolves the command from .gobby/project.json verification config. "
            "Returns a concise summary instead of raw output to save tokens."
        ),
    )
    async def run_check(
        category: str,
        paths: str | None = None,
        extra_args: str | None = None,
        timeout: int = 300,
    ) -> dict[str, Any]:
        """Run a verification check by category.

        Args:
            category: Verification category (e.g., 'unit_tests', 'lint', 'type_check', 'format', 'ts_check')
            paths: Override target paths (replaces path args in command)
            extra_args: Extra arguments to append to the command
            timeout: Command timeout in seconds (default 300)
        """
        from gobby.utils.project_context import get_project_context, get_verification_config

        try:
            # Resolve project context
            context = get_project_context()
            if not context:
                return {
                    "success": False,
                    "error": "No project context found. Run 'gobby init' first.",
                }

            project_path = context.get("project_path")
            project_id = context.get("id")

            # Resolve verification config
            verification = get_verification_config()
            if not verification:
                return {
                    "success": False,
                    "error": "No verification commands configured in .gobby/project.json.",
                }

            command = verification.get_command(category)
            if not command:
                available = list(verification.all_commands().keys())
                return {
                    "success": False,
                    "error": f"Category '{category}' not found.",
                    "available_categories": available,
                }

            result = await runner.run_check(
                category=category,
                command=command,
                cwd=project_path,
                paths=paths,
                extra_args=extra_args,
                timeout=timeout,
                project_id=project_id,
            )

            return {"success": True, **result.to_brief()}

        except Exception as e:
            logger.exception("run_check failed for category=%s", category)
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_run_status",
        description="Check the status of a test/lint/typecheck run. Use after timeout recovery.",
    )
    def get_run_status(run_id: str) -> dict[str, Any]:
        """Get the current status of a test run.

        Args:
            run_id: The test run ID (tr-xxxxx)
        """
        try:
            run = storage.get_run(run_id)
            if not run:
                return {"success": False, "error": f"Run not found: {run_id}"}
            return {"success": True, **run.to_brief()}
        except Exception as e:
            logger.exception("get_run_status failed for run_id=%s", run_id)
            return {"success": False, "error": str(e)}

    @registry.tool(
        name="get_run_result",
        description=(
            "Get detailed results of a test run. Returns the summary by default. "
            "Set include_output=true for paginated raw output (escape hatch when summary isn't enough)."
        ),
    )
    def get_run_result(
        run_id: str,
        include_output: bool = False,
        output_offset: int = 0,
        output_limit: int = 50,
    ) -> dict[str, Any]:
        """Get test run results with optional raw output.

        Args:
            run_id: The test run ID (tr-xxxxx)
            include_output: Include paginated raw output from log file
            output_offset: Line offset for raw output pagination
            output_limit: Number of lines per page (default 50)
        """
        try:
            run = storage.get_run(run_id)
            if not run:
                return {"success": False, "error": f"Run not found: {run_id}"}

            result: dict[str, Any] = {"success": True, **run.to_dict()}

            if include_output:
                result["output"] = runner.get_output(run, offset=output_offset, limit=output_limit)

            return result

        except Exception as e:
            logger.exception("get_run_result failed for run_id=%s", run_id)
            return {"success": False, "error": str(e)}

    return registry
