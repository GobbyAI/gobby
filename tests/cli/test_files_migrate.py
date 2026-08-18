"""Hub-local `gobby files migrate` campaign (plan 6.1)."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from gobby.config.bootstrap_io import write_bootstrap_yaml
from gobby.files_migrate import FilesMigrateReport
from gobby.runner_pid_file import claim_pid_file
from gobby.servers.chat_attachment_files import attachment_relative_locator
from gobby.storage.chat_attachments import create_attachment, get_attachment
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class OwnerEnv:
    home: Path
    files_home: Path
    personal: Path
    projects: Path
    user_home: Path
    wiki_hub: Path
    topics: Path
    comms: Path
    checkout: Path


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OwnerEnv]:
    home = tmp_path / "gobby-home"
    files_home = tmp_path / "files"
    user_home = tmp_path / "user-home"
    checkout = tmp_path / "checkout"
    home.mkdir()
    files_home.mkdir()
    user_home.mkdir()
    checkout.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setenv("HOME", str(user_home))
    write_bootstrap_yaml(
        home / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(files_home),
            "daemon_port": 60887,
            "bind_host": "127.0.0.1",
        },
    )
    env = OwnerEnv(
        home=home,
        files_home=files_home,
        personal=home / "personal",
        projects=home / "projects",
        user_home=user_home,
        wiki_hub=user_home / "wiki",
        topics=user_home / "wiki" / "topics",
        comms=home / "comms_attachments",
        checkout=checkout,
    )
    yield env


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_scope(vault: Path, *, identity: str, root: Path) -> None:
    _write(
        vault / "_gwiki" / "scope.json",
        json.dumps({"identity": identity, "root": str(root)}, indent=2) + "\n",
    )


def _write_registry(path: Path, payload: dict[str, object]) -> None:
    _write(path, json.dumps(payload, indent=2) + "\n")


def _invoke() -> Result:
    from gobby.cli import cli

    return CliRunner().invoke(cli, ["files", "migrate"])


def _run(**hooks: Any) -> FilesMigrateReport:
    from gobby.files_migrate import FilesMigrateHooks, run_files_migrate

    return run_files_migrate(hooks=FilesMigrateHooks(**hooks) if hooks else None)


def _legacy_layout(owner: OwnerEnv) -> dict[str, Path]:
    profile = owner.personal / "USER.md"
    marker = owner.personal / ".gobby" / "project.json"
    personal_wiki = owner.personal / "wiki" / "page.md"
    notes = owner.personal / "notes" / "n.md"
    reminders = owner.personal / "reminders" / "r.md"
    leftover = owner.personal / "scrap" / "s.md"
    topic = owner.topics / "alpha" / "t.md"
    personal_att = owner.personal / "attachments" / "proj-a" / "aa" / "id-a" / "a.txt"
    project_id = str(uuid.uuid4())
    project_att = owner.projects / project_id / "attachments" / "bb" / "id-b" / "b.txt"
    checkout_wiki = owner.checkout / "wiki" / "stay.md"
    comms = owner.comms / "keep.bin"
    _write(profile, "hello-profile")
    _write(marker, '{"id": "old"}')
    _write(personal_wiki, "personal-wiki")
    _write_scope(
        owner.personal / "wiki",
        identity=f"project:{PERSONAL_PROJECT_ID}",
        root=owner.personal / "wiki",
    )
    _write(notes, "notes")
    _write(reminders, "reminders")
    _write(leftover, "scrap")
    _write(topic, "topic-alpha")
    _write_scope(owner.topics / "alpha", identity="topic:alpha", root=owner.topics / "alpha")
    _write(personal_att, "p-att")
    _write(project_att, "proj-att")
    _write(checkout_wiki, "checkout")
    _write(comms, "comms")
    _write_registry(
        owner.wiki_hub / "wikis.json",
        {
            "topics": {"alpha": {"name": "alpha", "path": str(owner.topics / "alpha")}},
            "projects": {
                PERSONAL_PROJECT_ID: {
                    "project_id": PERSONAL_PROJECT_ID,
                    "project_root": str(owner.personal),
                    "path": str(owner.personal / "wiki"),
                }
            },
        },
    )
    return {
        "profile": profile,
        "marker": marker,
        "personal_wiki": personal_wiki,
        "notes": notes,
        "reminders": reminders,
        "leftover": leftover,
        "topic": topic,
        "personal_att": personal_att,
        "project_att": project_att,
        "checkout_wiki": checkout_wiki,
        "comms": comms,
        "project_id": Path(project_id),
    }


def test_6_1_10_help_is_reachable_from_root() -> None:
    from gobby.cli import cli

    result = CliRunner().invoke(cli, ["files", "migrate", "--help"])
    assert result.exit_code == 0
    assert "migrate" in result.output.lower()


def test_6_1_3_remote_mode_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(
        home / "bootstrap.yaml",
        {
            "datastore_mode": "remote",
            "hub_daemon_url": "https://hub.example.test:7443",
            "daemon_port": 60887,
            "bind_host": "127.0.0.1",
        },
    )
    result = _invoke()
    assert result.exit_code != 0
    assert "hub-local" in result.output.lower() or "remote" in result.output.lower()


def test_6_1_12_empty_source_seeds_baseline_without_creating_root(owner: OwnerEnv) -> None:
    assert owner.files_home.is_dir()
    report = _run()
    assert report.status == "success"
    assert (owner.files_home / "USER.md").is_file()
    assert (owner.files_home / "USER.md").read_text(encoding="utf-8") == ""
    marker = json.loads(
        (owner.files_home / "_personal" / ".gobby" / "project.json").read_text(encoding="utf-8")
    )
    assert marker["id"] == PERSONAL_PROJECT_ID
    assert marker["name"] == "_personal"
    assert (owner.files_home / "_personal" / "notes").is_dir()
    assert (owner.files_home / "_personal" / "reminders").is_dir()
    assert (owner.files_home / "_personal" / "attachments").is_dir()
    assert (owner.files_home / "wiki").is_dir()
    registry = json.loads((owner.files_home / "wiki" / "wikis.json").read_text(encoding="utf-8"))
    assert registry["topics"] == {}
    assert registry["projects"] == {}
    assert not (owner.files_home / "wiki" / "_gwiki" / "scope.json").exists()


def test_6_1_12_missing_files_home_does_not_create_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "gobby-home"
    missing = tmp_path / "missing-files"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    write_bootstrap_yaml(
        home / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(missing),
            "daemon_port": 60887,
            "bind_host": "127.0.0.1",
        },
    )
    result = _invoke()
    assert result.exit_code != 0
    assert not missing.exists()


def test_6_1_1_first_migrate_moves_every_present_class(owner: OwnerEnv) -> None:
    paths = _legacy_layout(owner)
    report = _run()
    assert report.status == "success"
    assert (owner.files_home / "USER.md").read_text(encoding="utf-8") == "hello-profile"
    assert not paths["profile"].exists()
    assert (owner.files_home / "_personal" / ".gobby" / "project.json").is_file()
    assert not paths["marker"].exists()
    assert (owner.files_home / "wiki" / "personal" / "page.md").read_text() == "personal-wiki"
    assert not paths["personal_wiki"].exists()
    assert (owner.files_home / "_personal" / "notes" / "n.md").read_text() == "notes"
    assert (owner.files_home / "_personal" / "reminders" / "r.md").read_text() == "reminders"
    assert (owner.files_home / "_personal" / "scrap" / "s.md").read_text() == "scrap"
    assert (owner.files_home / "wiki" / "alpha" / "t.md").read_text() == "topic-alpha"
    assert not paths["topic"].exists()
    assert (
        owner.files_home / "_personal" / "attachments" / "proj-a" / "aa" / "id-a" / "a.txt"
    ).read_text() == "p-att"
    project_id = paths["project_id"].name
    assert (
        owner.files_home / "_personal" / "attachments" / project_id / "bb" / "id-b" / "b.txt"
    ).read_text() == "proj-att"


def test_6_1_2_second_migrate_is_noop(owner: OwnerEnv) -> None:
    _legacy_layout(owner)
    first = _run()
    second = _run()
    assert first.status == "success"
    assert second.status == "success"
    assert (owner.files_home / "USER.md").read_text(encoding="utf-8") == "hello-profile"
    assert not (owner.personal / "USER.md").exists()


def test_6_1_4_checkout_wiki_and_comms_untouched(owner: OwnerEnv) -> None:
    paths = _legacy_layout(owner)
    _run()
    assert paths["checkout_wiki"].read_text() == "checkout"
    assert paths["comms"].read_text() == "comms"


def test_6_1_5_unrecognized_destination_refuses_before_mutation(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    paths = _legacy_layout(owner)
    stray = owner.files_home / "unexpected.txt"
    stray.write_text("nope", encoding="utf-8")
    with pytest.raises(FilesMigrateError, match="unrecognized|unexpected"):
        _run()
    assert stray.read_text(encoding="utf-8") == "nope"
    assert paths["profile"].read_text(encoding="utf-8") == "hello-profile"


def test_6_1_6_and_6_1_7_scope_and_registry_are_wiki_home_relative(owner: OwnerEnv) -> None:
    _legacy_layout(owner)
    _run()
    personal_scope = json.loads(
        (owner.files_home / "wiki" / "personal" / "_gwiki" / "scope.json").read_text()
    )
    topic_scope = json.loads(
        (owner.files_home / "wiki" / "alpha" / "_gwiki" / "scope.json").read_text()
    )
    assert personal_scope["root"] == str(owner.files_home / "wiki" / "personal")
    assert topic_scope["root"] == str(owner.files_home / "wiki" / "alpha")
    registry = json.loads((owner.files_home / "wiki" / "wikis.json").read_text())
    assert registry["topics"]["alpha"]["path"] == "alpha"
    assert registry["projects"][PERSONAL_PROJECT_ID]["path"] == "personal"
    assert all(not Path(str(entry["path"])).is_absolute() for entry in registry["topics"].values())


def test_6_1_8_recognized_partial_resumes(owner: OwnerEnv) -> None:
    _legacy_layout(owner)
    _write(owner.files_home / "USER.md", "hello-profile")
    (owner.personal / "USER.md").unlink()
    _run()
    assert (owner.files_home / "wiki" / "alpha" / "t.md").read_text() == "topic-alpha"
    assert (owner.files_home / "_personal" / "notes" / "n.md").read_text() == "notes"


def test_6_1_9_injected_failure_after_first_class_leaves_remaining(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigratePartialError

    paths = _legacy_layout(owner)
    with pytest.raises(FilesMigratePartialError):
        _run(after_class="profile")
    assert (owner.files_home / "USER.md").read_text() == "hello-profile"
    assert not paths["profile"].exists()
    assert paths["topic"].read_text() == "topic-alpha"
    _run()
    assert (owner.files_home / "wiki" / "alpha" / "t.md").read_text() == "topic-alpha"
    assert not paths["topic"].exists()


def test_6_1_11_malformed_registry_refuses_before_mutation(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    paths = _legacy_layout(owner)
    _write(owner.wiki_hub / "wikis.json", "{not-json")
    with pytest.raises(FilesMigrateError, match="registry|malformed"):
        _run()
    assert paths["profile"].exists()
    assert paths["topic"].exists()


def test_6_1_11_crash_before_registry_leaves_remaining_sources(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigratePartialError

    paths = _legacy_layout(owner)
    with pytest.raises(FilesMigratePartialError):
        _run(before_registry_publish=True)
    assert not paths["topic"].exists()
    assert (owner.wiki_hub / "wikis.json").exists()
    _run()
    registry = json.loads((owner.files_home / "wiki" / "wikis.json").read_text())
    assert registry["topics"]["alpha"]["path"] == "alpha"
    assert not (owner.wiki_hub / "wikis.json").exists()


def test_6_1_13_refuses_while_daemon_running(owner: OwnerEnv) -> None:
    _legacy_layout(owner)
    claim = claim_pid_file(owner.home / "gobby.pid", role="daemon")
    assert claim is not None
    try:
        result = _invoke()
        assert result.exit_code != 0
        assert "daemon" in result.output.lower() or "maintenance" in result.output.lower()
        assert (owner.personal / "USER.md").exists()
    finally:
        claim.release()


def test_6_1_14_exdev_removes_source_only_after_verify(owner: OwnerEnv) -> None:
    _write(owner.personal / "USER.md", "profile-bytes")
    _run(force_exdev=True)
    assert (owner.files_home / "USER.md").read_text() == "profile-bytes"
    assert not (owner.personal / "USER.md").exists()


def test_6_1_14_exdev_verify_failure_preserves_source(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    _write(owner.personal / "USER.md", "profile-bytes")
    with pytest.raises(FilesMigrateError, match="verify|EXDEV|copy"):
        _run(force_exdev=True, fail_exdev_verify=True)
    assert (owner.personal / "USER.md").read_text() == "profile-bytes"
    dest = owner.files_home / "USER.md"
    assert not dest.exists()


def test_6_1_14_exdev_truncate_failure_preserves_source(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    _write(owner.personal / "USER.md", "profile-bytes")
    with pytest.raises(FilesMigrateError, match="verify|truncat|copy"):
        _run(force_exdev=True, fail_exdev_truncate=True)
    assert (owner.personal / "USER.md").read_text() == "profile-bytes"
    dest = owner.files_home / "USER.md"
    assert not dest.exists()


def test_6_1_15_present_profile_publishes_before_seeding(owner: OwnerEnv) -> None:
    _write(owner.personal / "USER.md", "keep-me")
    _write(owner.personal / "notes" / "n.md", "notes")
    _run()
    assert (owner.files_home / "USER.md").read_text() == "keep-me"
    assert (owner.files_home / "_personal" / "notes" / "n.md").read_text() == "notes"


def test_6_1_17_reserved_topic_refuses(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    _write(owner.personal / "USER.md", "p")
    _write(owner.topics / "personal" / "x.md", "reserved")
    with pytest.raises(FilesMigrateError, match="topic|reserved|personal"):
        _run()
    assert (owner.personal / "USER.md").exists()
    assert (owner.topics / "personal" / "x.md").exists()


def test_6_1_17_prefix_overlap_refuses(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    project_id = str(uuid.uuid4())
    _write(owner.personal / "attachments" / project_id / "aa", "file-leaf")
    _write(
        owner.projects / project_id / "attachments" / "aa" / "id1" / "nested.txt",
        "nested",
    )
    with pytest.raises(FilesMigrateError, match="prefix|collision|overlap"):
        _run()
    assert (owner.personal / "attachments" / project_id / "aa").is_file()
    assert (owner.projects / project_id / "attachments" / "aa" / "id1" / "nested.txt").exists()


def test_6_1_18_equal_both_present_unlinks_source(owner: OwnerEnv) -> None:
    _write(owner.personal / "USER.md", "same")
    _write(owner.files_home / "USER.md", "same")
    _run()
    assert (owner.files_home / "USER.md").read_text() == "same"
    assert not (owner.personal / "USER.md").exists()


def test_6_1_18_divergent_both_present_refuses(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    _write(owner.personal / "USER.md", "src")
    _write(owner.files_home / "USER.md", "dst")
    with pytest.raises(FilesMigrateError, match="differ|divergent|both-present"):
        _run()
    assert (owner.personal / "USER.md").read_text() == "src"
    assert (owner.files_home / "USER.md").read_text() == "dst"


def test_6_1_19_held_claim_blocks_daemon_start(owner: OwnerEnv) -> None:
    _write(owner.personal / "USER.md", "p")
    seen: list[bool] = []

    def _on_claimed(claim: object) -> None:
        del claim
        blocked = claim_pid_file(owner.home / "gobby.pid", role="daemon")
        seen.append(blocked is None)
        if blocked is not None:
            blocked.release()

    _run(on_claimed=_on_claimed)
    assert seen == [True]


def test_6_1_20_wiki_home_is_not_a_vault(owner: OwnerEnv) -> None:
    _legacy_layout(owner)
    _run()
    _run()
    assert not (owner.files_home / "wiki" / "_gwiki" / "scope.json").exists()


def test_6_1_21_scope_rewrite_after_source_retirement(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigratePartialError

    _legacy_layout(owner)
    with pytest.raises(FilesMigratePartialError):
        _run(before_scope_rewrite=True)
    assert not (owner.personal / "wiki" / "page.md").exists()
    old_root = json.loads(
        (owner.files_home / "wiki" / "personal" / "_gwiki" / "scope.json").read_text()
    )["root"]
    assert old_root != str(owner.files_home / "wiki" / "personal")
    _run()
    new_root = json.loads(
        (owner.files_home / "wiki" / "personal" / "_gwiki" / "scope.json").read_text()
    )["root"]
    assert new_root == str(owner.files_home / "wiki" / "personal")


def test_6_1_22_filesystem_root_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text("datastore_mode: local\nfiles_home: /\n", encoding="utf-8")
    bootstrap.chmod(0o600)
    result = _invoke()
    assert result.exit_code != 0
    assert "root" in result.output.lower() or "files_home" in result.output.lower()


def test_6_1_22_overlap_with_legacy_source_refuses(
    owner: OwnerEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.files_migrate import FilesMigrateError

    _write(owner.personal / "USER.md", "keep")
    monkeypatch.setattr("gobby.config.bootstrap._assert_disjoint_files_home", lambda _path: None)
    write_bootstrap_yaml(
        owner.home / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(owner.personal),
            "daemon_port": 60887,
            "bind_host": "127.0.0.1",
        },
    )
    with pytest.raises(FilesMigrateError, match="overlap|disjoint|ancestor"):
        _run()
    assert (owner.personal / "USER.md").read_text() == "keep"


def test_6_1_23_symlink_fifo_socket_hardlink_refuse(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    _write(owner.personal / "USER.md", "p")
    (owner.personal / "link").symlink_to(owner.personal / "USER.md")
    with pytest.raises(FilesMigrateError, match="symlink|special"):
        _run()
    assert (owner.personal / "USER.md").exists()
    (owner.personal / "link").unlink()

    os.mkfifo(owner.personal / "fifo")
    with pytest.raises(FilesMigrateError, match="FIFO|fifo|special"):
        _run()
    (owner.personal / "fifo").unlink()

    created_sock = owner.personal / "sock"
    sock_ok = False
    try:
        os.mknod(created_sock, stat.S_IFSOCK | 0o600)
        sock_ok = True
    except (OSError, AttributeError):
        sock_ok = False
    if sock_ok:
        with pytest.raises(FilesMigrateError, match="socket|special"):
            _run()
        created_sock.unlink()

    _write(owner.personal / "hard", "h")
    os.link(owner.personal / "hard", owner.personal / "hard2")
    with pytest.raises(FilesMigrateError, match="nlink|hard|special"):
        _run()
    assert (owner.personal / "USER.md").exists()


def test_6_1_23_identity_change_before_publish_refuses(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    _write(owner.personal / "USER.md", "first")

    def _swap() -> None:
        (owner.personal / "USER.md").unlink()
        _write(owner.personal / "USER.md", "swapped")

    with pytest.raises(FilesMigrateError, match="identity|changed|swap"):
        _run(before_first_publish=_swap)
    assert (owner.personal / "USER.md").exists()
    assert not (owner.files_home / "USER.md").exists()


def test_6_1_24_source_swap_after_first_class_is_recognized_partial(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigratePartialError

    _legacy_layout(owner)

    def _swap() -> None:
        target = owner.topics / "alpha" / "t.md"
        target.unlink()
        _write(target, "swapped-topic")

    with pytest.raises(FilesMigratePartialError):
        _run(swap_after_class="profile", swap_fn=_swap)
    assert (owner.files_home / "USER.md").read_text() == "hello-profile"
    assert (owner.topics / "alpha" / "t.md").read_text() == "swapped-topic"
    _run()
    assert (owner.files_home / "wiki" / "alpha" / "t.md").read_text() == "swapped-topic"


def test_6_1_25_attachment_merge_and_divergent_leaf(owner: OwnerEnv) -> None:
    from gobby.files_migrate import FilesMigrateError

    project_id = str(uuid.uuid4())
    _write(
        owner.personal / "attachments" / project_id / "aa" / "id1" / "a.txt",
        "from-personal",
    )
    _write(
        owner.projects / project_id / "attachments" / "aa" / "id1" / "b.txt",
        "from-project",
    )
    _run()
    dest = owner.files_home / "_personal" / "attachments" / project_id / "aa" / "id1"
    assert (dest / "a.txt").read_text() == "from-personal"
    assert (dest / "b.txt").read_text() == "from-project"

    _write(owner.personal / "attachments" / project_id / "cc" / "id2" / "same.txt", "left")
    _write(owner.projects / project_id / "attachments" / "cc" / "id2" / "same.txt", "right")
    with pytest.raises(FilesMigrateError, match="collision|divergent"):
        _run()


def test_6_1_16_and_6_1_26_attachment_locator_rewrite(
    owner: OwnerEnv, temp_db: HubDatabase
) -> None:
    from gobby.files_migrate import FilesMigratePartialError
    from gobby.utils.machine_id import require_machine_id
    from tests.fixtures.postgres import TEST_USER_ID

    machine_id = require_machine_id()
    with temp_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO machines (id, owner_user_id)
            VALUES (%s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (machine_id, TEST_USER_ID),
        )
    project_id = LocalProjectManager(temp_db).create(name="migrate-attachments").id
    attachment_id = str(uuid.uuid4())
    missing_id = str(uuid.uuid4())
    filename = "note.txt"
    legacy = (
        owner.projects / project_id / "attachments" / attachment_id[:2] / attachment_id / filename
    )
    _write(legacy, "bytes")
    present = create_attachment(
        temp_db,
        project_id=project_id,
        draft_id=None,
        filename=filename,
        mime_type="text/plain",
        size_bytes=5,
        local_path=str(legacy),
        attachment_id=attachment_id,
        published=True,
    )
    missing = create_attachment(
        temp_db,
        project_id=project_id,
        draft_id=None,
        filename="gone.txt",
        mime_type="text/plain",
        size_bytes=1,
        local_path=str(
            owner.projects / project_id / "attachments" / "zz" / missing_id / "gone.txt"
        ),
        attachment_id=missing_id,
        published=True,
    )
    with pytest.raises(FilesMigratePartialError):
        _run(db=temp_db, before_locator_rewrite=True)
    still_abs = get_attachment(temp_db, present.id, require_published=True)
    assert still_abs is not None
    assert Path(still_abs.local_path).is_absolute()
    dest = owner.files_home / attachment_relative_locator(project_id, attachment_id, filename)
    assert dest.read_text() == "bytes"
    report = _run(db=temp_db)
    rewritten = get_attachment(temp_db, present.id, require_published=True)
    assert rewritten is not None
    assert rewritten.local_path == attachment_relative_locator(project_id, attachment_id, filename)
    leftover = get_attachment(temp_db, missing.id, require_published=True)
    assert leftover is not None
    assert Path(leftover.local_path).is_absolute()
    assert missing.id in report.unrewritten_attachments


def test_6_1_23_device_mode_is_refused() -> None:
    from gobby.files_migrate import special_file_reason

    device = os.stat_result((stat.S_IFCHR | 0o666, 1, 0, 1, 0, 0, 0, 0, 0, 0))
    sock = os.stat_result((stat.S_IFSOCK | 0o666, 1, 0, 1, 0, 0, 0, 0, 0, 0))
    assert special_file_reason(device) is not None
    assert special_file_reason(sock) == "socket"
