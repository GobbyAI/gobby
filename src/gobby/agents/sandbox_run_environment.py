"""Run-local sandbox paths and subprocess environment redirects."""

from dataclasses import dataclass
from pathlib import Path

# CARGO_TARGET_DIR is deliberately absent: it is checkout-specific
# (gobby.agents.cargo_target), not privatized per run.
RUN_CACHE_ENV_VARS = (
    "UV_CACHE_DIR",
    "CARGO_HOME",
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
)


@dataclass(frozen=True)
class SandboxRunPaths:
    root: Path
    assets: Path
    tmp: Path
    hooks: Path
    logs: Path
    cache: Path

    @property
    def writable(self) -> tuple[Path, Path, Path, Path]:
        return (self.tmp, self.hooks, self.logs, self.cache)

    def environment(self, provider: str) -> dict[str, str]:
        values = {
            name: str(self.cache / name.replace("_", "-").lower()) for name in RUN_CACHE_ENV_VARS
        }
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
        return values
