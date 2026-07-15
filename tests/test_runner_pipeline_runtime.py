from unittest.mock import MagicMock, patch

from gobby.runner_pipeline_runtime import build_pipeline_runtime
from gobby.workflows.pipeline_webhooks import WebhookNotifier


def test_build_pipeline_runtime_wires_webhook_notifier() -> None:
    runner = MagicMock()

    with (
        patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as manager_cls,
        patch("gobby.workflows.pipeline_executor.PipelineExecutor") as executor_cls,
        patch("gobby.utils.daemon_url.daemon_url", return_value="http://127.0.0.1:7778"),
    ):
        build_pipeline_runtime(runner, "project-123")

    notifier = executor_cls.call_args.kwargs["webhook_notifier"]
    assert isinstance(notifier, WebhookNotifier)
    assert notifier.base_url == "http://127.0.0.1:7778"
    manager_cls.assert_called_once_with(db=runner.database, project_id="project-123")
