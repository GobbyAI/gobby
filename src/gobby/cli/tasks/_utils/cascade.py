"""Cascade-iteration progress helpers for batched task operations."""

import sys
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager

import click

from gobby.storage.tasks import Task


class _CascadeIterator:
    """Iterator wrapper that handles errors via callback."""

    def __init__(
        self,
        tasks: list[Task],
        label: str,
        on_error: Callable[[Task, Exception], bool] | None,
    ):
        self._tasks = tasks
        self._label = label
        self._on_error = on_error
        self._index = 0
        self._total = len(tasks)
        self._stop = False
        self._current_task: Task | None = None
        self._pending_error: Exception | None = None
        self._completed_count = 0

    def __iter__(self) -> "_CascadeIterator":
        return self

    def __next__(self) -> tuple[Task, Callable[[], None]]:
        # Handle any pending error from previous iteration
        if self._pending_error is not None:
            error = self._pending_error
            self._pending_error = None
            task = self._current_task

            if self._on_error is not None and task is not None:
                should_continue = self._on_error(task, error)
                if not should_continue:
                    self._stop = True
                    raise StopIteration
            else:
                raise error

        if self._stop or self._index >= self._total:
            raise StopIteration

        task = self._tasks[self._index]
        self._current_task = task
        self._index += 1

        task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]

        # Truncate long titles
        max_title_len = 40
        title = task.title
        if len(title) > max_title_len:
            title = title[: max_title_len - 3] + "..."

        # Print progress line with label
        progress_str = f"{self._label} [{self._index}/{self._total}] {task_ref}: {title}"
        click.echo(progress_str)

        def update() -> None:
            """Mark the current task as completed."""
            self._completed_count += 1

        return task, update

    def report_error(self, error: Exception) -> None:
        """Report an error for the current task."""
        self._pending_error = error


@contextmanager
def cascade_progress(
    tasks: list[Task],
    label: str = "Processing",
    on_error: Callable[[Task, Exception], bool] | None = None,
) -> Generator[Iterator[tuple[Task, Callable[[], None]]]]:
    """Context manager for cascade operations with progress display.

    Yields (task, update) pairs for each task. Call update() after
    processing each task to advance the progress bar.

    Args:
        tasks: List of tasks to process
        label: Label to show before progress bar (e.g., "Expanding")
        on_error: Optional callback for errors. Receives (task, error).
                  Return True to continue, False to stop processing.

    Yields:
        Iterator of (task, update_fn) tuples

    Example:
        with cascade_progress(tasks, label="Expanding") as progress:
            for task, update in progress:
                await expand_task(task)
                update()  # Mark complete
    """
    if not tasks:
        yield iter([])
        return

    iterator = _CascadeIterator(tasks, label, on_error)
    try:
        yield iterator
    except KeyboardInterrupt:
        click.echo("\nOperation interrupted by user.")
        raise
    except Exception as e:
        # Handle error on current task
        if on_error is not None and iterator._current_task is not None:
            # Call on_error callback for logging, but always re-raise
            # The on_error return value is only used in next-iteration logic
            on_error(iterator._current_task, e)
        raise
    finally:
        # Handle any pending error from final iteration (report_error called on last task)
        if iterator._pending_error is not None:
            error = iterator._pending_error
            iterator._pending_error = None
            task = iterator._current_task
            # Capture any exception from the iterator body to preserve as __cause__
            body_exception = sys.exc_info()[1]
            if on_error is not None and task is not None:
                # Call on_error callback for pending error, preserving both exceptions if callback fails
                try:
                    on_error(task, error)
                    # Error handled via callback, don't re-raise
                except Exception as callback_exc:
                    # Chain exceptions: pending error from body exception (if any) or callback failure
                    if body_exception is not None:
                        raise error from body_exception
                    raise error from callback_exc
            else:
                # No on_error callback - re-raise the pending error chained to body exception if present
                if body_exception is not None:
                    raise error from body_exception
                raise error from None
