"""Own isolated CLI and daemon trees across interrupted lifecycle operations."""

import subprocess
import time
from pathlib import Path
from types import TracebackType

import psutil

from tests._timing import wait_for_condition
from tests.e2e.conftest import terminate_process_tree


def wait_for_retiring_backend(
    process: psutil.Process, *, log_files: tuple[Path, ...], artifact_path: Path
) -> None:
    """Keep bounded shutdown evidence outside the disposable daemon home on failure."""
    started = time.monotonic()
    try:
        created = str(process.create_time())
    except psutil.Error as error:
        created = f"unavailable ({error})"

    def gone() -> bool:
        try:
            return bool(process.status() == psutil.STATUS_ZOMBIE)
        except psutil.NoSuchProcess:
            return True

    try:
        wait_for_condition(gone, timeout=30.0, description="retiring backend reaped")
    except AssertionError as failure:
        elapsed = time.monotonic() - started
        try:
            status = process.status()
        except psutil.Error as error:
            status = f"unavailable ({error})"
        sections = [
            f"retiring backend: pid={process.pid} create_time={created} "
            f"status={status} elapsed={elapsed:.3f}s budget=30.0s"
        ]
        for log_file in log_files:
            try:
                with log_file.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 8192))
                    tail = stream.read(8192).decode("utf-8", errors="replace")
            except OSError as error:
                tail = f"unavailable ({error})"
            sections.append(f"--- {log_file.name} (last 8192 bytes) ---\n{tail}")
        diagnostics = "\n".join(sections)
        failure.add_note(diagnostics)
        try:
            artifact_path.write_text(diagnostics)
        except OSError as error:
            failure.add_note(f"Could not retain diagnostics at {artifact_path}: {error}")
        else:
            failure.add_note(f"Retained diagnostics: {artifact_path}")
        raise


class LifecycleProcesses:
    """Retain process identities before waiting, including detached replacements."""

    def __init__(self, pid_file: Path) -> None:
        self.pid_file = pid_file
        self.processes: dict[int, psutil.Process] = {}
        self.launchers: list[subprocess.Popen[str]] = []

    def __enter__(self) -> "LifecycleProcesses":
        return self

    def track(self, pid: int) -> None:
        try:
            process = psutil.Process(pid)
            self.processes[pid] = process
            try:
                children = process.children(recursive=True)
            except psutil.AccessDenied:
                # terminate_process_tree has a process-group fallback for seats.
                children = []
            for child in children:
                self.processes[child.pid] = child
        except psutil.NoSuchProcess:
            pass

    def record_daemon(self) -> None:
        try:
            pid = int(self.pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            # The native owner may be replacing its pid file at this instant.
            return
        self.track(pid)

    def run(
        self, argv: list[str], *, env: dict[str, str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        child = subprocess.Popen(
            argv,
            env=env,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.launchers.append(child)
        self.track(child.pid)
        deadline = time.monotonic() + timeout
        while True:
            # Restart detaches the new owner from the CLI. Record it and its
            # backend before the next bounded wait, not after CLI success.
            self.record_daemon()
            remaining = max(0.0, deadline - time.monotonic())
            try:
                stdout, stderr = child.communicate(timeout=min(0.1, remaining))
                self.record_daemon()
                return subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
            except subprocess.TimeoutExpired as expired:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(
                        argv, timeout, output=expired.output, stderr=expired.stderr
                    ) from None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        errors: list[Exception] = []

        def stop(process: psutil.Process) -> None:
            try:
                if process.is_running():
                    terminate_process_tree(process.pid)
            except psutil.NoSuchProcess:
                pass
            except Exception as error:
                errors.append(error)

        # Stop launchers first so they cannot spawn after final pid discovery.
        for child in self.launchers:
            process = self.processes.get(child.pid)
            if process is not None:
                stop(process)
        try:
            self.record_daemon()
        except Exception as error:
            errors.append(error)
        for process in self.processes.values():
            stop(process)
        for child in self.launchers:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired as error:
                errors.append(error)
            finally:
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()
        if errors:
            failure = ExceptionGroup("Isolated lifecycle process cleanup failed", errors)
            if exc is None:
                raise failure
            exc.add_note(str(failure))
