"""Record one bounded Cargo observation, only in an approved quiet window."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WORKLOADS = {
    "coherent": [
        "cargo",
        "build",
        "--locked",
        "--release",
        "-p",
        "gobby-code",
        "-p",
        "gobby-daemon",
        "-p",
        "gobby-hooks",
        "--timings",
    ],
    "gclient": ["cargo", "build", "--locked", "--release", "-p", "gobby-client", "--timings"],
    "nextest": ["cargo", "nextest", "run", "--locked", "-p", "gobby-hooks"],
}
VARIANTS = ("baseline", "jobs1", "nice10", "thin-lto", "line-tables", "test-threads1")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def stop_group(pid: int, sig: signal.Signals = signal.SIGTERM) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=WORKLOADS, required=True)
    parser.add_argument("--variant", choices=VARIANTS, default="baseline")
    parser.add_argument("--cache", choices=("cold", "warm"), required=True)
    parser.add_argument("--context", choices=("sandbox", "operator"), required=True)
    parser.add_argument("--root", type=Path, required=True, help="Research-only output root")
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 1800:
        parser.error("--seconds must be between 1 and 1800")
    if args.variant in ("line-tables", "test-threads1") and args.workload != "nextest":
        parser.error("this variant applies only to nextest")
    if args.variant == "thin-lto" and args.workload == "nextest":
        parser.error("thin-lto applies only to release builds")
    now = datetime.now(ZoneInfo("America/Chicago"))
    quiet_start = now.replace(hour=4, minute=45, second=0, microsecond=0)
    quiet_end = now.replace(hour=6, minute=45, second=0, microsecond=0)
    if quiet_start <= now < quiet_end and not args.dry_run:
        parser.error("quiet hours: do not run measurements")
    if now >= quiet_end:
        quiet_start += timedelta(days=1)
    if (quiet_start - now).total_seconds() <= args.seconds + 15 and not args.dry_run:
        parser.error("observation could cross into quiet hours")
    checkout = Path(git("rev-parse", "--show-toplevel")).resolve()
    source = git("rev-parse", "HEAD")
    dirty = git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "Cargo.toml",
        "Cargo.lock",
        ".cargo",
        ".config/nextest.toml",
        "crates",
    )
    if dirty:
        parser.error("build inputs must be committed: " + dirty)
    target = (
        args.root.expanduser().resolve()
        / f"{args.context}-{args.workload}-{args.variant}"
        / "target"
    )
    observation = target.parent / f"{args.cache}.json"
    command = WORKLOADS[args.workload].copy()
    env = os.environ.copy()
    inherited = [
        key
        for key in env
        if key.startswith("CARGO_PROFILE_")
        or key
        in (
            "RUSTFLAGS",
            "CARGO_ENCODED_RUSTFLAGS",
            "RUSTC_WRAPPER",
            "RUSTC_WORKSPACE_WRAPPER",
            "CARGO_BUILD_RUSTFLAGS",
            "CARGO_INCREMENTAL",
            "CARGO_BUILD_BUILD_DIR",
        )
    ]
    if inherited:
        parser.error("unset inherited overrides: " + ", ".join(inherited))
    overrides = {
        "CARGO_BUILD_JOBS": "1" if args.variant == "jobs1" else "3",
        "CARGO_TARGET_DIR": str(target),
    }
    if args.variant == "thin-lto":
        overrides["CARGO_PROFILE_RELEASE_LTO"] = "thin"
    if args.variant == "line-tables":
        overrides["CARGO_PROFILE_DEV_DEBUG"] = "line-tables-only"
        overrides["CARGO_PROFILE_TEST_DEBUG"] = "line-tables-only"
    if args.variant == "test-threads1":
        command.extend(["-j", "1"])
    if args.variant == "nice10":
        command = ["nice", "-n", "10", *command]
    env.update(overrides)
    record: dict[str, object] = {
        "source": source,
        "checkout": str(checkout),
        "context": args.context,
        "workload": args.workload,
        "variant": args.variant,
        "cache": args.cache,
        "command": command,
        "overrides": overrides,
        "cargo_home": env.get("CARGO_HOME", str(Path.home() / ".cargo")),
        "timeout_seconds": args.seconds,
        "platform": sys.platform,
        "ambient_build_environment": {
            key: env[key]
            for key in (
                "CARGO_BUILD_TARGET",
                "MACOSX_DEPLOYMENT_TARGET",
                "SDKROOT",
                "NEXTEST_PROFILE",
                "NEXTEST_TEST_THREADS",
                "NEXTEST_USER_CONFIG_FILE",
            )
            if key in env
        },
    }
    if args.dry_run:
        print(json.dumps(record, indent=2))
        return 0
    if observation.exists():
        parser.error("observation exists; use a new --root for a repetition")
    if args.cache == "cold" and target.exists():
        parser.error("cold requires a nonexistent target; use a new --root")
    if args.cache == "warm":
        cold = target.parent / "cold.json"
        if not cold.exists():
            parser.error("warm requires a recorded cold observation")
        previous = json.loads(cold.read_text())
        for key in (
            "source",
            "checkout",
            "context",
            "workload",
            "variant",
            "cargo_home",
            "command",
            "overrides",
            "ambient_build_environment",
            "platform",
        ):
            if previous[key] != record[key]:
                parser.error("warm observation differs from cold: " + key)
        if (
            previous["returncode"] != 0
            or previous["timed_out"]
            or previous["interrupted"]
            or not target.is_dir()
        ):
            parser.error("warm requires a completed passing cold observation")
    target.parent.mkdir(parents=True, exist_ok=True)
    record["started_utc"] = datetime.now(UTC).isoformat()
    record["toolchain"] = subprocess.check_output(["rustc", "-Vv"], text=True)
    record["nextest_version"] = subprocess.check_output(
        ["cargo", "nextest", "--version"], text=True
    )
    samples = [(0.0, *os.getloadavg()[:2])]
    started = time.monotonic()
    timed_out = False
    interrupted = False
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    with observation.with_suffix(".log").open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=checkout,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        while True:
            try:
                pid, status, usage = os.wait4(process.pid, os.WNOHANG)
                if pid:
                    returncode = os.waitstatus_to_exitcode(status)
                    process.returncode = returncode
                    break
                elapsed = time.monotonic() - started
                if elapsed >= args.seconds and not timed_out:
                    timed_out = True
                    stop_group(process.pid)
                if elapsed >= args.seconds + 10:
                    stop_group(process.pid, signal.SIGKILL)
                time.sleep(0.25)
                samples.append((time.monotonic() - started, *os.getloadavg()[:2]))
            except KeyboardInterrupt:
                interrupted = True
                stop_group(process.pid)
                # Bound cleanup even when interrupted early by a host watcher.
                started_limit = time.monotonic() - started + 10
                args.seconds = min(args.seconds, started_limit)
    if timed_out or interrupted:
        # Cargo may exit before a compiler child that ignored TERM. Clean the
        # original group even after its leader is reaped; never leave a build live.
        stop_group(process.pid, signal.SIGKILL)
    record.update(
        {
            "wall_seconds": time.monotonic() - started,
            "cpu_user_seconds": usage.ru_utime,
            "cpu_sys_seconds": usage.ru_stime,
            "max_child_rss_bytes": usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
            "rss_scope": "wait4 child high-water RSS; not aggregate process-tree peak",
            "peak_load1": max(sample[1] for sample in samples),
            "peak_load5": max(sample[2] for sample in samples),
            "load_samples": samples,
            "returncode": returncode,
            "timed_out": timed_out,
            "interrupted": interrupted,
        }
    )
    observation.write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps({key: value for key, value in record.items() if key != "load_samples"}, indent=2)
    )
    return 124 if timed_out else 130 if interrupted else returncode


if __name__ == "__main__":
    raise SystemExit(main())
