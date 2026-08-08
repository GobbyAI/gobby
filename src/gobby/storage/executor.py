"""Bounded executor for daemon-owned database work."""

from gobby.threaded_executor import ManagedExecutorStats, ManagedThreadPoolExecutor

DatabaseExecutorStats = ManagedExecutorStats


class DatabaseExecutor(ManagedThreadPoolExecutor):
    """Bounded async bridge for blocking database storage calls."""

    def __init__(self, *, max_workers: int, thread_name_prefix: str = "gobby-db") -> None:
        super().__init__(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
            queue_wait_metric="database_executor_queue_wait_seconds",
            executor_name="DatabaseExecutor",
        )
