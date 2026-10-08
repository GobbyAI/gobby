"""Standalone shutdown must finish before interpreter module finalization."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._timing import wait_for_condition


@pytest.mark.parametrize("standalone", [True, False])
@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_standalone_shutdown_exits_without_module_finalization(
    tmp_path: Path, exit_code: int, standalone: bool
) -> None:
    ready = tmp_path / "ready"
    released = tmp_path / "released"
    cleanup = tmp_path / "cleanup"
    finalizing = tmp_path / "finalizing"
    resumed = tmp_path / "resumed"
    script = textwrap.dedent(
        f"""
        import atexit
        import gc
        import os
        import sys
        import time
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        import gobby.runner as runner
        # Import the existing exit backstop before starting the exit deadline.
        runner._force_exit_after_expired_settlement()

        ready = Path({str(ready)!r})
        released = Path({str(released)!r})
        cleanup = Path({str(cleanup)!r})
        finalizing = Path({str(finalizing)!r})
        resumed = Path({str(resumed)!r})
        atexit.register(lambda: cleanup.write_text('exit cleanup ran'))

        # Model the observed delay after Py_RunMain enters finalize_modules.
        # A cycle survives refcount cleanup and is collected at interpreter exit.
        gc.disable()
        class ModuleFinalizer:
            def __del__(self, marker=finalizing, sleep=time.sleep):
                marker.write_text('module finalization entered')
                sleep(60)
        if {standalone}:
            victim = ModuleFinalizer()
            victim.cycle = victim

        class FatalShutdown(BaseException):
            pass

        async def completed_daemon(**kwargs):
            ready.write_text('daemon cleanup completed')
            print('shutdown output flushed')
            if {exit_code} == 2:
                raise FatalShutdown('isolated fatal shutdown')
            if {exit_code}:
                raise RuntimeError('isolated startup failure')

        claim = Mock()
        claim.release.side_effect = lambda: released.write_text('claim released')
        bootstrap = SimpleNamespace(daemon_port=0, bind_host='127.0.0.1')
        with (
            patch('gobby.utils.spawn.seal_inherited_descriptors'),
            patch('gobby.utils.dev.worktree_daemon_refusal', return_value=None),
            patch('gobby.config.bootstrap.load_bootstrap', return_value=bootstrap),
            patch('gobby.cli.utils.get_gobby_home', return_value=ready.parent),
            patch('gobby.runner_pid_file.adopt_inherited_claim', return_value=None),
            patch('gobby.runner_pid_file.claim_pid_file', return_value=claim),
            patch.object(runner, '_healthy_daemon_running', return_value=False),
            patch.object(runner, 'run_gobby', completed_daemon),
        ):
            try:
                if {standalone}:
                    runner._standalone_main()
                else:
                    runner.main()
            except SystemExit as exc:
                if {standalone}:
                    raise
                assert exc.code == {exit_code}
            except FatalShutdown:
                if {standalone}:
                    raise
            resumed.write_text('main caller resumed')
            # The embedding caller owns its process. End this probe after
            # observing main's return, without measuring interpreter GC again.
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit({min(exit_code, 1)})
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        wait_for_condition(
            lambda: ready.exists() or process.poll() is not None,
            timeout=60.0,
            description="isolated standalone shutdown reached its exit boundary",
        )
        assert ready.exists(), process.communicate(timeout=5.0)
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            assert cleanup.exists(), "Delay occurred before exit callbacks completed"
            raise
        stdout, stderr = process.communicate(timeout=5.0)
        assert process.returncode == min(exit_code, 1), (stdout, stderr)
        assert "shutdown output flushed" in stdout
        if exit_code == 1:
            assert "isolated startup failure" in stderr
        if exit_code == 2 and standalone:
            assert "FatalShutdown: isolated fatal shutdown" in stderr
        assert released.read_text() == "claim released"
        if standalone:
            assert cleanup.read_text() == "exit cleanup ran"
        else:
            assert not cleanup.exists()
        assert not finalizing.exists()
        assert resumed.exists() is not standalone
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5.0)
