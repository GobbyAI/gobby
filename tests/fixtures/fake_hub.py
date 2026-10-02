"""A stand-in hub DSN for tests that never connect.

Kept free of imports so spawned test children stay cheap to start.
"""

from __future__ import annotations


def fake_database_url(password: str = "secret") -> str:
    """Return a hub DSN off the live hub's coordinates; nothing listens on port 1."""
    return f"postgresql://gobby:{password}@localhost:1/gobby_fixture"


FAKE_DATABASE_URL = fake_database_url()
