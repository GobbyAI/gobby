"""Run-local sandbox paths and subprocess environment redirects."""

from dataclasses import dataclass
from pathlib import Path

# CARGO_HOME and CARGO_TARGET_DIR are deliberately absent: Cargo fingerprints
# embed $CARGO_HOME/registry/src, so a per-run home rebuilds the whole
# dependency graph on every run (#23194). Runs share the pair below
# `SandboxRunPaths.shared_cache` instead.
RUN_CACHE_ENV_VARS = (
    "UV_CACHE_DIR",
    "GOCACHE",
    "GOMODCACHE",
    "npm_config_cache",
    "YARN_CACHE_FOLDER",
    "PNPM_HOME",
    "PIP_CACHE_DIR",
    "GRADLE_USER_HOME",
    "COURSIER_CACHE",
    "NUGET_PACKAGES",
    "COMPOSER_CACHE_DIR",
    "PUB_CACHE",
    "GEM_HOME",
    "BUNDLE_PATH",
    "HEX_HOME",
    "MIX_HOME",
    "XDG_CACHE_HOME",
    "ZIG_GLOBAL_CACHE_DIR",
)


@dataclass(frozen=True)
class SandboxRunPaths:
    root: Path
    assets: Path
    tmp: Path
    hooks: Path
    logs: Path
    cache: Path
    # Stable caches every sandboxed run shares and nothing unsandboxed builds from
    # (constants.sandbox_agent_cache_dir). Runs may write only cargo_home and
    # cargo_target: the daemon rebuilds the Zig mirror here before each run, so
    # no run may plant links in it for the daemon to follow.
    shared_cache: Path
    # This checkout's target below shared_cache (cargo_target.sandbox_checkout_cargo_target_dir).
    cargo_target: Path
    # Complete `zig build --system` package dir; None leaves libghostty-vt fetching.
    zig_system_dir: Path | None = None

    @property
    def writable(self) -> tuple[Path, Path, Path, Path]:
        return (self.tmp, self.hooks, self.logs, self.cache)

    @property
    def cargo_home(self) -> Path:
        return self.shared_cache / "cargo-home"

    def environment(self, provider: str) -> dict[str, str]:
        values = {
            name: str(self.cache / name.replace("_", "-").lower()) for name in RUN_CACHE_ENV_VARS
        }
        values["CARGO_HOME"] = str(self.cargo_home)
        values["CARGO_TARGET_DIR"] = str(self.cargo_target)
        # Every provider child, and everything it spawns, must land temp files in
        # the run's writable tmp. Claude alone used to get only CLAUDE_CODE_TMPDIR,
        # so its children kept the ambient system temp, which is not a write grant:
        # a `uv`-launched MCP bridge had its lock write denied on every start.
        # Measured under SRT: `uv` tolerates that denial and the bridge still
        # serves, so this is a policy violation to remove, not a startup failure
        # to blame. srt_runner.mjs already assumes TMPDIR is the run temp.
        values["TMPDIR"] = str(self.tmp)
        if provider == "claude":
            values["CLAUDE_CODE_TMPDIR"] = str(self.tmp)
        # zsh uses TMPPREFIX for heredocs independently of TMPDIR.
        values["TMPPREFIX"] = str(self.tmp / "zsh")
        values["GOBBY_LOG_DIR"] = str(self.logs)
        # ocr's update check writes ~/.opencodereview/last-update-check, which
        # the policy denies, on every reviewer invocation.
        values["OCR_NO_UPDATE"] = "1"
        if self.zig_system_dir is not None:
            values["LIBGHOSTTY_VT_ZIG_SYSTEM_DIR"] = str(self.zig_system_dir)
        return values
