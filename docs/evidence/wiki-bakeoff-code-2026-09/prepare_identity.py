"""Provision the isolated daemon's required local account and machine identity."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

import yaml
from prepare_daemon import isolated_environment
from provision_environment import DEFAULT_RUNTIME_ROOT, _write_json
from runtime_boundary import assert_owned_runtime, contained_file


def prepare_identity(root: Path) -> None:
    clean = isolated_environment(root, dict(os.environ))
    os.environ.clear()
    os.environ.update(clean)

    import gobby
    from gobby.cli.install_identity import ensure_install_identity
    from gobby.identity import hash_password
    from gobby.storage.hub.postgres import PostgresHubDatabase
    from gobby.storage.users import LocalUserManager

    assert Path(gobby.__file__).resolve().is_relative_to(root / "sources/gobby")
    bootstrap = yaml.safe_load(contained_file(root, "gobby-home/bootstrap.yaml").read_text())
    parsed = urlparse(bootstrap["database_url"])
    assert parsed.hostname == "127.0.0.1" and parsed.port == 61234
    assert parsed.path == "/gobby_bakeoff_21942"
    db = PostgresHubDatabase(bootstrap["database_url"])
    try:
        users = LocalUserManager(db)
        existing = users.list()
        assert len(existing) <= 1, "ambiguous isolated installation identity"
        if not existing:
            account_path = root / "config/daemon-account.json"
            if account_path.exists():
                import json

                account = json.loads(contained_file(root, account_path).read_text())
            else:
                account = {
                    "email": "bakeoff-21942@example.invalid",
                    "password": secrets.token_urlsafe(32),
                }
                _write_json(account_path, account, mode=0o600)
            assert account["email"] == "bakeoff-21942@example.invalid"
            users.create(
                name="Wiki bakeoff 21942",
                email=account["email"],
                password_hash=hash_password(account["password"]),
            )
        user = ensure_install_identity(db, no_interactive=True)
        assert user.email == "bakeoff-21942@example.invalid"
        _write_json(root / "receipts/daemon-identity.json", {"user_id": user.id})
    finally:
        db.close()
    print("isolated account and machine identity prepared")


if __name__ == "__main__":
    assert_owned_runtime(DEFAULT_RUNTIME_ROOT, "#12034")
    prepare_identity(DEFAULT_RUNTIME_ROOT)
