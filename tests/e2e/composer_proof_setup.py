"""Install real hooks/MCP only inside the admitted isolated proof home/project."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import tomlkit

from tests.e2e.composer_proof import ProofRefused, require_private_root


def verify_setup(home: Path, project: Path) -> None:
    marker = json.loads((project / ".gobby" / "project.json").read_text())
    if not isinstance(marker, dict) or marker.get("id") != "00000000-0000-0000-0000-000000000e2e":
        raise ProofRefused("isolated project marker required")
    if home.resolve(strict=True) != (project / ".gobby-home").resolve(strict=True):
        raise ProofRefused("isolated project home required")
    require_private_root(home)


def main() -> None:
    if os.environ.get("GOBBY_COMPOSER_PROOF_EXECUTION") != "PD_AUTHORIZED_ISOLATED_EXECUTION":
        raise ProofRefused("isolated execution grant required")
    home = require_private_root(Path(os.environ["HOME"]))
    project = Path(sys.argv[1]).resolve(strict=True)
    verify_setup(home, project)
    from gobby.cli.installers.claude import install_claude
    from gobby.cli.installers.codex import install_codex_project_hooks

    for result in (install_claude(project), install_codex_project_hooks(project)):
        if result.get("success") is not True:
            raise ProofRefused("isolated hook installation failed")
    # Pin stdio to the reviewed source interpreter, inheriting only the sealed
    # proof environment. Installer discovery must never choose a production shim.
    mcp = {
        "command": sys.executable,
        "args": ["-c", "from gobby.cli import cli; cli()", "mcp-server"],
    }
    claude_file = home / ".claude.json"
    claude = json.loads(claude_file.read_text())
    claude.setdefault("mcpServers", {})["gobby"] = mcp
    claude_file.write_text(json.dumps(claude))
    codex_file = home / ".codex" / "config.toml"
    codex = tomlkit.parse(codex_file.read_text())
    codex["mcp_servers"] = {"gobby": {**mcp, "required": True}}
    codex["cli_auth_credentials_store"] = "file"
    codex_file.write_text(tomlkit.dumps(codex))
    (project / "AGENTS.md").write_text(
        "Disposable composer acceptance session. Never edit files, run tools, spawn agents, "
        "or contact any other session. Respond READY to setup; acknowledge notifications "
        "with one short word. Gobby owns the test state.\n"
    )


if __name__ == "__main__":
    main()
