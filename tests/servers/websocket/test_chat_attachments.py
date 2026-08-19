"""Tests for WebSocket chat attachment binding helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import gobby.storage.chat_attachments as chat_attachments
from gobby.config.app import DaemonConfig
from gobby.paths import FilesHomeNotOnThisDaemonError, get_gobby_home
from gobby.servers.chat_attachment_files import (
    attachment_relative_locator,
    resolve_attachment_dir,
    resolve_attachment_locator,
)
from gobby.servers.chat_attachment_limits import ChatAttachmentLimits
from gobby.servers.websocket.chat_attachments import (
    append_prepared_attachment_context,
    legacy_attachment_items,
    partition_attachment_items,
    prepare_message_attachments,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit

# chat_attachments.id is a native uuid column; stored attachment ids must be
# valid UUID strings. Client-side ids (conversation_id, message_id, draft_id)
# remain plain text.
ATT_ID_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
ATT_ID_2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_bytes": 0},
        {"max_total_bytes_per_message": 0},
        {"max_files_per_message": 0},
    ],
)
def test_chat_attachment_limits_reject_non_positive_values(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        ChatAttachmentLimits(**kwargs)


@pytest.fixture(autouse=True)
def _restore_bootstrap() -> Iterator[None]:
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    previous = bootstrap.read_bytes() if bootstrap.exists() else None
    yield
    if previous is None:
        bootstrap.unlink(missing_ok=True)
    else:
        bootstrap.write_bytes(previous)
        bootstrap.chmod(0o600)


def _write_local_bootstrap(files_home: Path) -> None:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        f"datastore_mode: local\nfiles_home: {files_home}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


def _write_remote_bootstrap() -> None:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


def test_resolve_attachment_dir_uses_files_home_personal_tree(tmp_path: Path) -> None:
    files_home = tmp_path / "files_home"
    files_home.mkdir()
    _write_local_bootstrap(files_home)

    resolved = resolve_attachment_dir(PERSONAL_PROJECT_ID, ATT_ID_1)

    assert resolved == (
        files_home
        / "_personal"
        / "attachments"
        / PERSONAL_PROJECT_ID
        / ATT_ID_1[:2]
        / ATT_ID_1
    )
    assert not (get_gobby_home() / "projects").exists()
    assert attachment_relative_locator(PERSONAL_PROJECT_ID, ATT_ID_1, "note.txt") == (
        f"_personal/attachments/{PERSONAL_PROJECT_ID}/{ATT_ID_1[:2]}/{ATT_ID_1}/note.txt"
    )


def test_node_resolver_does_not_create_gobby_home_projects(tmp_path: Path) -> None:
    _write_remote_bootstrap()
    gobby_home = get_gobby_home()

    with pytest.raises(FilesHomeNotOnThisDaemonError):
        resolve_attachment_dir(PERSONAL_PROJECT_ID, ATT_ID_1)

    assert not (gobby_home / "projects").exists()
    assert not (tmp_path / "projects").exists()


def test_resolve_attachment_locator_ignores_absolute_stored_path() -> None:
    locator = resolve_attachment_locator(
        PERSONAL_PROJECT_ID,
        ATT_ID_1,
        "note.txt",
        stored_local_path="/tmp/stale/note.txt",
    )
    assert locator == attachment_relative_locator(PERSONAL_PROJECT_ID, ATT_ID_1, "note.txt")
    assert not Path(locator).is_absolute()


def _owner(
    temp_db: HubDatabase,
    *,
    daemon_config: DaemonConfig | None = None,
    config_runtime: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        session_manager=SimpleNamespace(db=temp_db),
        daemon_config=daemon_config or DaemonConfig(),
        config_runtime=config_runtime,
    )


def _attachment(
    temp_db: HubDatabase,
    tmp_path: Path,
    *,
    attachment_id: str = ATT_ID_1,
    size_bytes: int = 5,
) -> str:
    """Create a stored attachment row backed by a temporary local file."""
    path = tmp_path / f"{attachment_id}.txt"
    path.write_bytes(b"x" * size_bytes)
    chat_attachments.create_attachment(
        temp_db,
        attachment_id=attachment_id,
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename=path.name,
        mime_type="text/plain",
        size_bytes=size_bytes,
        local_path=f"_personal/attachments/{PERSONAL_PROJECT_ID}/{attachment_id[:2]}/{attachment_id}/{path.name}",
        published=True,
    )
    return attachment_id


@pytest.mark.asyncio
async def test_prepare_message_attachments_binds_ids_and_formats_safe_context(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _attachment(temp_db, tmp_path)

    prepared = await prepare_message_attachments(
        _owner(temp_db),
        [{"id": attachment_id}],
        conversation_id="conv-1",
        message_id="msg-1",
    )

    assert prepared.records[0].id == attachment_id
    assert prepared.content_blocks[0]["attachment"]["id"] == attachment_id
    assert f"{attachment_id}.txt" in (prepared.prompt_context or "")
    assert f"id={attachment_id}" in (prepared.prompt_context or "")
    assert str(tmp_path) not in (prepared.prompt_context or "")
    assert "base64" not in (prepared.prompt_context or "").lower()

    row = temp_db.fetchone("SELECT * FROM chat_attachments WHERE id = %s", (attachment_id,))
    assert row is not None
    assert row["conversation_id"] == "conv-1"
    assert row["message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_prepare_message_attachments_honors_active_file_count(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    active_config = DaemonConfig(
        chat={
            "attachment_max_files_per_message": 1,
            "attachment_max_total_bytes_per_message": 100_000_000,
        }
    )
    runtime = SimpleNamespace(
        ready=True,
        capture=lambda: SimpleNamespace(snapshot=SimpleNamespace(active=active_config)),
    )
    first = _attachment(temp_db, tmp_path, attachment_id=ATT_ID_1)
    second = _attachment(temp_db, tmp_path, attachment_id=ATT_ID_2)

    with pytest.raises(ValueError, match="Too many attachments"):
        await prepare_message_attachments(
            _owner(temp_db, config_runtime=runtime),
            [{"id": first}, {"id": second}],
        )


@pytest.mark.asyncio
async def test_prepare_message_attachments_checks_current_file_size_limit_before_binding(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    daemon_config = DaemonConfig(
        chat={
            "attachment_max_file_bytes": 4,
            "attachment_max_total_bytes_per_message": 4,
        }
    )
    attachment_id = _attachment(temp_db, tmp_path, size_bytes=5)

    with pytest.raises(ValueError, match="exceeds configured 4 byte limit"):
        await prepare_message_attachments(
            _owner(temp_db, daemon_config=daemon_config),
            [{"id": attachment_id}],
            conversation_id="conv-1",
        )

    row = temp_db.fetchone(
        "SELECT conversation_id FROM chat_attachments WHERE id = %s", (attachment_id,)
    )
    assert row is not None
    assert row["conversation_id"] is None


@pytest.mark.asyncio
async def test_prepare_message_attachments_checks_current_total_size_limit_before_binding(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    daemon_config = DaemonConfig(chat={"attachment_max_total_bytes_per_message": 8})
    first = _attachment(temp_db, tmp_path, attachment_id=ATT_ID_1, size_bytes=5)
    second = _attachment(temp_db, tmp_path, attachment_id=ATT_ID_2, size_bytes=5)

    with pytest.raises(ValueError, match="exceed configured 8 byte total limit"):
        await prepare_message_attachments(
            _owner(temp_db, daemon_config=daemon_config),
            [{"id": first}, {"id": second}],
            conversation_id="conv-1",
        )

    rows = temp_db.fetchall(
        "SELECT conversation_id FROM chat_attachments WHERE id IN (%s, %s)",
        (first, second),
    )
    assert [row["conversation_id"] for row in rows] == [None, None]


@pytest.mark.asyncio
async def test_prepare_message_attachments_requires_session_manager() -> None:
    with pytest.raises(ValueError, match="requires session_manager"):
        await prepare_message_attachments(SimpleNamespace(), [{"id": "att-1"}])


@pytest.mark.asyncio
async def test_prepare_message_attachments_requires_session_manager_db() -> None:
    owner = SimpleNamespace(session_manager=SimpleNamespace(db=None), daemon_config=DaemonConfig())

    with pytest.raises(ValueError, match="requires session_manager.db"):
        await prepare_message_attachments(owner, [{"id": "att-1"}])


def test_legacy_attachment_items_filters_id_references() -> None:
    assert legacy_attachment_items(
        [
            {"id": "stored"},
            {"name": "legacy.txt", "base64": "aGVsbG8="},
        ]
    ) == [{"name": "legacy.txt", "base64": "aGVsbG8="}]


def test_partition_attachment_items_splits_stored_ids_and_legacy_items() -> None:
    partitions = partition_attachment_items(
        [
            {"id": " stored "},
            {"id": "stored"},
            {"name": "legacy.txt", "base64": "aGVsbG8="},
            "legacy-string",
        ]
    )

    assert partitions.ids == ["stored"]
    assert partitions.legacy_items == [
        {"name": "legacy.txt", "base64": "aGVsbG8="},
        "legacy-string",
    ]


def test_append_prepared_attachment_context() -> None:
    prepared = SimpleNamespace(prompt_context="Attached files:\n- /tmp/a.txt")

    assert append_prepared_attachment_context("Look", prepared) == (
        "Look\n\nAttached files:\n- /tmp/a.txt"
    )


def test_prepared_attachment_context_hides_local_path() -> None:
    record = chat_attachments.ChatAttachmentRecord(
        id="att-1",
        machine_id="bbbbbbbb-bbbb-4bbb-8bbb-000000000001",
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        conversation_id=None,
        message_id=None,
        target_session_id=None,
        filename="missing.txt",
        mime_type="text/plain",
        size_bytes=1,
        local_path="",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        bound_at=None,
    )
    from gobby.servers.websocket.chat_attachments import PreparedMessageAttachments

    prepared = PreparedMessageAttachments(records=[record])

    assert "missing.txt" in (prepared.prompt_context or "")
    assert "id=att-1" in (prepared.prompt_context or "")
    assert "<no local path>" not in (prepared.prompt_context or "")
