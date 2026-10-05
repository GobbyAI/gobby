"""#22103 probe 2, same-user credential access: managed SRT path verdicts.

Local stub. Computes the managed policy with compute_sandbox_paths and
render_srt_settings for a temporary workspace, then classifies candidate paths
with sandbox-runtime precedence as documented in sandbox_policy.py
(allowRead wins over denyRead; denyWrite wins over allowWrite; reads outside
denyRead are default-allowed). It resolves path names only, opens no candidate
file and calls no daemon. compute_sandbox_paths runs one read-only
`git rev-parse --git-dir --git-common-dir` in the temporary workspace per
provider (sandbox.py:371-389); the workspace is not a repository, so that
adds no paths. It also matches the bundled
no-secret-read rule pattern against command strings.
Run: uv run python <this file>
"""

import asyncio
import os
import re
import tempfile
from pathlib import Path

import yaml

from gobby.agents.sandbox import SandboxConfig, compute_sandbox_paths, web_chat_sandbox_config
from gobby.agents.srt_runtime import render_srt_settings

CANDIDATES = (
    "~/.ssh/id_ed25519",
    "~/.aws/credentials",
    "~/.config/gh/hosts.yml",
    "~/.netrc",
    "~/.docker/config.json",
    "~/.cargo/credentials.toml",
    "~/.gobby/bootstrap.yaml",
    "~/.gobby/local_cli_token",
    "~/.claude/settings.json",
    "~/.claude.json",
    "~/.codex/auth.json",
    "~/.codex/config.toml",
    "~/.codex/hooks.json",
    "~/Library/Keychains/login.keychain-db",
)


def under(path: Path, roots: list[str]) -> bool:
    return any(path == Path(root) or path.is_relative_to(root) for root in roots)


async def main() -> None:
    env = {"PATH": os.environ.get("PATH", "")}
    with tempfile.TemporaryDirectory() as workspace:
        for provider in ("claude", "codex"):
            paths = await compute_sandbox_paths(
                SandboxConfig(enabled=True, backend="srt"),
                workspace,
                provider=provider,
                env=env,
            )
            settings = render_srt_settings(paths)
            network = settings["network"]
            print(f"== provider={provider}")
            print(
                f"allowAppleEvents={settings['allowAppleEvents']} "
                f"allowLocalBinding={network['allowLocalBinding']} "
                f"allowUnixSockets={network['allowUnixSockets']} "
                f"allowedDomains={len(network['allowedDomains'])}"
            )
            print(f"{'path':40} read   write")
            for raw in CANDIDATES:
                path = Path(raw).expanduser().resolve(strict=False)
                read_denied = under(path, paths.deny_read_paths) and not under(
                    path, paths.read_paths
                )
                writable = under(path, paths.write_paths) and not under(
                    path, paths.deny_write_paths
                )
                print(
                    f"{raw:40} {'deny ' if read_denied else 'ALLOW'}  "
                    f"{'ALLOW' if writable else 'deny'}"
                )


asyncio.run(main())

web_chat = web_chat_sandbox_config(None)
print(
    f"== web chat default: enabled={web_chat.enabled} backend={web_chat.backend} "
    f"allow_network={web_chat.allow_network} extra_deny_read={web_chat.extra_deny_read_paths}"
)

rule_file = Path("src/gobby/install/shared/workflows/rules/worker-safety/no-data-exfiltration.yaml")
rule = yaml.safe_load(rule_file.read_text())["rules"]["no-secret-read"]
effect = rule["effects"][0]
pattern = re.compile(effect["command_pattern"])
print(f"== no-secret-read: when={rule['when']!r} tools={effect['tools']}")
for command in (
    "cat ~/.ssh/id_ed25519",
    "cat ~/.s''sh/id_ed25519",
    'cd ~ && cat ".ss"h/id_ed25519',
    "python3 -c \"print(open('/Users/x/.'+'ssh/id_ed25519').read())\"",
    "cat ~/.docker/config.json",
):
    print(f"{'BLOCK' if pattern.search(command) else 'allow':5}  {command}")
