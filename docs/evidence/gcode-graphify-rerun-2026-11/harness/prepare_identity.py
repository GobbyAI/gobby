"""Provision the isolated daemon's required local account and machine identity."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

import yaml
from prepare_daemon import isolated_environment, run_runtime_gobby
from provision_environment import DEFAULT_RUNTIME_ROOT, _write_json
from runtime_boundary import assert_owned_runtime, contained_file, require


def prepare_identity(root: Path) -> None:
    clean = isolated_environment(root, dict(os.environ))
    os.environ.clear()
    os.environ.update(clean)

    bootstrap = yaml.safe_load(contained_file(root, "gobby-home/bootstrap.yaml").read_text())
    parsed = urlparse(bootstrap["database_url"])
    require(
        parsed.hostname == "127.0.0.1" and parsed.port == 61234,
        "check failed: parsed.hostname == '127.0.0.1' and parsed.port == 61234",
    )
    require(
        parsed.path == "/gobby_bakeoff_22948", "check failed: parsed.path == '/gobby_bakeoff_22948'"
    )
    account_path = root / "config/daemon-account.json"
    account_is_new = not account_path.exists()
    if account_is_new:
        account = {"email": "bakeoff-22948@example.invalid", "password": secrets.token_urlsafe(32)}
    else:
        account = json.loads(contained_file(root, account_path).read_text())
    require(
        account["email"] == "bakeoff-22948@example.invalid",
        "check failed: account['email'] == 'bakeoff-22948@example.invalid'",
    )
    # The runtime writes the account file before creating the user, and only when no user exists.
    result = run_runtime_gobby(
        root,
        clean,
        "prepare-identity",
        {
            "database_url": bootstrap["database_url"],
            "account": account,
            "account_path": str(account_path),
        },
    )
    require(
        result["email"] == "bakeoff-22948@example.invalid",
        "check failed: user.email == 'bakeoff-22948@example.invalid'",
    )
    _write_json(root / "receipts/daemon-identity.json", {"user_id": result["user_id"]})
    print("isolated account and machine identity prepared")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-session", required=True)
    args = parser.parse_args()
    assert_owned_runtime(DEFAULT_RUNTIME_ROOT, args.owner_session)
    prepare_identity(DEFAULT_RUNTIME_ROOT)
