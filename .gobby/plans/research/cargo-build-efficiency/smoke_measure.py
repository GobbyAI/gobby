"""Exercise recorder lifecycle with fake tools; produces no benchmark evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def main() -> None:
    recorder = Path(__file__).with_name("measure.py").resolve()
    with tempfile.TemporaryDirectory(prefix="gobby-23902-recorder-smoke-") as directory:
        root = Path(directory)
        shim = (
            f"#!{sys.executable}\n"
            + """
import os
from pathlib import Path
import sys
import time
if "--version" in sys.argv or "-Vv" in sys.argv:
    print("synthetic recorder smoke tool, not Cargo")
else:
    Path(os.environ["CARGO_TARGET_DIR"]).mkdir(parents=True, exist_ok=True)
    if os.environ.get("RECORDER_SMOKE_MODE") == "wait":
        print("synthetic child ready", flush=True)
        time.sleep(60)
    print("synthetic success")
"""
        )
        for name in ("cargo", "rustc"):
            executable = root / name
            executable.write_text(shim)
            executable.chmod(0o700)
        env = os.environ.copy()
        env["PATH"] = str(root) + os.pathsep + env["PATH"]
        for key in list(env):
            if key.startswith("CARGO_PROFILE_") or key in (
                "RUSTFLAGS",
                "CARGO_ENCODED_RUSTFLAGS",
                "RUSTC_WRAPPER",
                "RUSTC_WORKSPACE_WRAPPER",
                "CARGO_BUILD_RUSTFLAGS",
            ):
                del env[key]
        base = [
            sys.executable,
            str(recorder),
            "--workload",
            "coherent",
            "--context",
            "sandbox",
            "--seconds",
            "5",
        ]

        dry_root = root / "dry"
        rows = []
        for workload, variant in (
            ("coherent", "baseline"),
            ("gclient", "thin-lto"),
            ("nextest", "baseline"),
            ("nextest", "test-threads1"),
            ("nextest", "line-tables"),
            ("coherent", "jobs1"),
            ("gclient", "nice10"),
        ):
            result = subprocess.run(
                [
                    *base,
                    "--workload",
                    workload,
                    "--variant",
                    variant,
                    "--root",
                    str(dry_root),
                    "--cache",
                    "cold",
                    "--dry-run",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, result.stderr
            rows.append(json.loads(result.stdout))
        assert "-j" not in rows[2]["command"]
        assert rows[3]["command"][-2:] == ["-j", "1"]
        assert rows[1]["overrides"]["CARGO_PROFILE_RELEASE_LTO"] == "thin"
        assert rows[4]["overrides"]["CARGO_PROFILE_TEST_DEBUG"] == "line-tables-only"
        assert rows[5]["overrides"]["CARGO_BUILD_JOBS"] == "1"
        assert rows[6]["command"][:3] == ["nice", "-n", "10"]
        assert not dry_root.exists()

        def run(name: str, cache: str, expected: int) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [
                    *base,
                    "--root",
                    str(root / name),
                    "--cache",
                    cache,
                    "--seconds",
                    "1" if name == "timeout" else "5",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == expected, (result.stdout, result.stderr)
            return result

        cold = json.loads(run("pass", "cold", 0).stdout)
        assert cold["returncode"] == 0 and not cold["timed_out"]
        assert cold["cpu_user_seconds"] >= 0 and cold["cpu_sys_seconds"] >= 0
        assert cold["max_child_rss_bytes"] > 0
        assert cold["peak_load1"] >= 0 and cold["peak_load5"] >= 0
        warm = json.loads(run("pass", "warm", 0).stdout)
        assert warm["cache"] == "warm"
        assert "observation exists" in run("pass", "cold", 2).stderr
        assert "recorded cold" in run("missing", "warm", 2).stderr
        env["RECORDER_SMOKE_MODE"] = "wait"
        started = time.monotonic()
        timeout = json.loads(run("timeout", "cold", 124).stdout)
        assert timeout["timed_out"] and timeout["returncode"] < 0
        assert time.monotonic() - started < 10
        assert "passing cold" in run("timeout", "warm", 2).stderr
        command = [*base, "--root", str(root / "interrupt"), "--cache", "cold"]
        process = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        log = root / "interrupt/sandbox-coherent-baseline/cold.log"
        deadline = time.monotonic() + 5
        while not log.exists() or "ready" not in log.read_text():
            assert process.poll() is None
            if time.monotonic() > deadline:
                process.kill()
                process.wait()
                raise AssertionError("synthetic child did not become ready")
            time.sleep(0.01)
        process.terminate()
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 130, (stdout, stderr)
        assert json.loads(stdout)["interrupted"]
    print("PASS: synthetic cold/warm, metrics, overwrite/missing guards, timeout, interruption")


if __name__ == "__main__":
    main()
