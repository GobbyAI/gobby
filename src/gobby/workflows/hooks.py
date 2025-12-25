import asyncio
import logging
import threading

from gobby.hooks.events import HookEvent, HookResponse

from .engine import WorkflowEngine

logger = logging.getLogger(__name__)


class WorkflowHookHandler:
    """
    Integrates WorkflowEngine into the HookManager.
    Wraps the async engine to be callable from synchronous hooks.
    """

    def __init__(
        self,
        engine: WorkflowEngine,
        loop: asyncio.AbstractEventLoop | None = None,
        timeout: float = 30.0,
    ):
        self.engine = engine
        self._loop = loop
        self.timeout = timeout

        # If no loop provided, try to get one or create one for this thread
        if not self._loop:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

    def handle_all_lifecycles(self, event: HookEvent) -> HookResponse:
        """
        Handle a hook event by discovering and evaluating all lifecycle workflows.

        This is the preferred method - it automatically discovers all lifecycle
        workflows and evaluates them in priority order. Replaces the need to
        call handle_lifecycle() with a specific workflow name.

        Args:
            event: The hook event to handle

        Returns:
            Merged HookResponse from all workflows
        """
        try:
            if self._loop and self._loop.is_running():
                if threading.current_thread() is threading.main_thread():
                    return HookResponse(decision="allow")
                else:
                    future = asyncio.run_coroutine_threadsafe(
                        self.engine.evaluate_all_lifecycle_workflows(event),
                        self._loop,
                    )
                    return future.result(timeout=self.timeout)

            try:
                return asyncio.run(self.engine.evaluate_all_lifecycle_workflows(event))
            except RuntimeError:
                logger.warning(
                    "Could not run workflow engine: Event loop is already running."
                )
                return HookResponse(decision="allow")

        except Exception as e:
            logger.error(f"Error handling all lifecycle workflows: {e}", exc_info=True)
            return HookResponse(decision="allow")

    def handle(self, event: HookEvent) -> HookResponse:
        """
        Handle a hook event by delegating to the workflow engine.
        Handles the sync/async bridge.
        """
        try:
            # We need to run the async self.engine.handle_event(event) synchronously

            # Case 1: We have a captured loop (main loop) and we are likely in a thread
            # This is the common case for FastAPI sync endpoints
            if self._loop and self._loop.is_running():
                if threading.current_thread() is threading.main_thread():
                    # We are on the main thread and the loop is running.
                    # We cannot block here without deadlock if we use run_until_complete.
                    # But HookManager.handle is synchronous, so this is a tricky spot.
                    # Ideally, HookManager should await, but it's not async.
                    # For now, we return allow and log a warning if we can't run.
                    # OR we create a task and return allow (fire and forget), but we need the result.

                    # Actually, if we are here, we are blocking the event loop!
                    # This implementation assumes HookManager.handle is run in a threadpool (def handle vs async def handle).
                    # Pydantic/FastAPI runs sync def routes in threadpool.
                    pass
                else:
                    # We are in a thread, loop is in another thread.
                    # Safe to block this thread waiting for loop.
                    future = asyncio.run_coroutine_threadsafe(
                        self.engine.handle_event(event), self._loop
                    )
                    return future.result(timeout=self.timeout)

            # Case 2: No loop running, or we just want to run it.
            # Create a new loop or use asyncio.run if appropriate
            try:
                return asyncio.run(self.engine.handle_event(event))
            except RuntimeError:
                # Loop likely already running
                logger.warning(
                    "Could not run workflow engine: Event loop is already running and we are blocking it."
                )
                return HookResponse(decision="allow")

        except Exception as e:
            logger.error(f"Error handling workflow hook: {e}", exc_info=True)
            return HookResponse(decision="allow")

    def handle_lifecycle(
        self, workflow_name: str, event: HookEvent, context_data: dict | None = None
    ) -> HookResponse:
        """
        Handle a lifecycle workflow event.
        """
        logger.debug(
            f"handle_lifecycle called: workflow={workflow_name}, event_type={event.event_type}"
        )
        try:
            if self._loop and self._loop.is_running():
                if threading.current_thread() is threading.main_thread():
                    # See comment in handle() about blocking main thread loop
                    return HookResponse(decision="allow")
                else:
                    future = asyncio.run_coroutine_threadsafe(
                        self.engine.evaluate_lifecycle_triggers(workflow_name, event, context_data),
                        self._loop,
                    )
                    return future.result(timeout=self.timeout)

            try:
                return asyncio.run(
                    self.engine.evaluate_lifecycle_triggers(workflow_name, event, context_data)
                )
            except RuntimeError:
                logger.warning("Could not run workflow engine: Event loop is already running.")
                return HookResponse(decision="allow")

        except Exception as e:
            logger.error(f"Error handling lifecycle workflow: {e}", exc_info=True)
            return HookResponse(decision="allow")
