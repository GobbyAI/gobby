"""Run-local sandbox paths and subprocess environment redirects."""

from dataclasses import dataclass
from pathlib import Path

# CARGO_TARGET_DIR is deliberately absent: the cargo build directory is shared
# per project (gobby.agents.cargo_target), never privatized per run.
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
        values["CLAUDE_CODE_TMPDIR" if provider == "claude" else "TMPDIR"] = str(self.tmp)
        # zsh uses TMPPREFIX for heredocs independently of TMPDIR.
        values["TMPPREFIX"] = str(self.tmp / "zsh")
        values["GOBBY_LOG_DIR"] = str(self.logs)
        return values
