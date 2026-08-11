from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from gobby.ai.embedding_switch import PHASE_ACTIVE, PHASE_BUILDING, PHASE_FLIPPING

if TYPE_CHECKING:
    from tests.integration.config.conftest import DaemonWorker, TwoDaemonCluster


pytestmark = pytest.mark.integration


def _value(snapshot: dict[str, object], projection: str, key: str) -> object:
    return cast(dict[str, object], snapshot[projection])[key]


def _subscriber_revisions(snapshot: dict[str, object]) -> list[int]:
    subscriber = cast(dict[str, object], snapshot["subscriber"])
    return cast(list[int], subscriber["revisions"])


def test_remote_daemon_converges_after_commit(two_daemons: TwoDaemonCluster) -> None:
    revision = two_daemons.first.patch(
        expected_revision=0,
        values={"rules.enforcement_enabled": False},
    )

    second = two_daemons.second.wait_for_revision(revision)

    assert second["revision"] == revision
    assert _value(second, "desired", "rules.enforcement_enabled") is False
    assert _value(second, "active", "rules.enforcement_enabled") is False


def test_listener_restart_recovers_latest_snapshot(two_daemons: TwoDaemonCluster) -> None:
    terminated_pid = two_daemons.terminate_listener(two_daemons.second)
    first_revision = two_daemons.first.patch(
        expected_revision=0,
        values={"rules.enforcement_enabled": False},
    )
    latest_revision = two_daemons.first.patch(
        expected_revision=first_revision,
        values={"rules.enforcement_enabled": True},
    )

    recovered = two_daemons.second.wait_for_revision(latest_revision)

    assert terminated_pid > 0
    assert recovered["revision"] == latest_revision
    assert _value(recovered, "active", "rules.enforcement_enabled") is True


def test_apply_failure_is_process_local(two_daemons: TwoDaemonCluster) -> None:
    two_daemons.second.set_fail("rules.enforcement_enabled")

    revision = two_daemons.first.patch(
        expected_revision=0,
        values={"rules.enforcement_enabled": False},
    )
    first = two_daemons.first.wait_for_revision(revision)
    second = two_daemons.second.wait_for_revision(revision)

    assert _value(first, "active", "rules.enforcement_enabled") is False
    assert _value(second, "desired", "rules.enforcement_enabled") is False
    assert _value(second, "active", "rules.enforcement_enabled") is True
    assert second["failed_live_keys"] == ["rules.enforcement_enabled"]


def test_local_commit_reconciles_once(two_daemons: TwoDaemonCluster) -> None:
    revision = two_daemons.first.patch(
        expected_revision=0,
        values={"rules.enforcement_enabled": False},
    )
    two_daemons.second.wait_for_revision(revision)
    two_daemons.first.wait_for_notification(revision)

    first = two_daemons.first.snapshot()
    second_read = two_daemons.first.snapshot()

    assert _subscriber_revisions(first).count(revision) == 1
    assert _subscriber_revisions(second_read) == _subscriber_revisions(first)


def test_managed_switch_converges_across_daemons(two_daemons: TwoDaemonCluster) -> None:
    journal = two_daemons.first.switch_start()
    run_id = journal["run_id"]
    target_model = journal["target_model"]

    two_daemons.first.crash()
    two_daemons.first.start()
    recovered_journal = two_daemons.first.switch_status()
    assert recovered_journal is not None
    assert recovered_journal["run_id"] == run_id

    record = two_daemons.first.switch_complete(reconcile=False)
    committed_revision = cast(int, record["committed_revision"])
    two_daemons.first.crash()
    two_daemons.first.start()

    first = two_daemons.first.wait_for_revision(committed_revision)
    second = two_daemons.second.wait_for_revision(committed_revision)

    assert two_daemons.first.switch_status() is None
    assert _value(first, "active", "ai.embeddings.model") == target_model
    assert _value(second, "active", "ai.embeddings.model") == target_model
    assert record["physical_names"] == journal["physical_names"]


def test_managed_mapping_propagates_across_daemons(two_daemons: TwoDaemonCluster) -> None:
    journal = two_daemons.first.switch_start()
    journal_target = cast(dict[str, str], journal["physical_names"])["memories"]
    journal_revision = two_daemons.first.current_revision()
    two_daemons.first.wait_for_revision(journal_revision)
    two_daemons.second.wait_for_revision(journal_revision)

    assert two_daemons.first.managed_targets("memory", "memories") == [
        "memories",
        journal_target,
    ]
    assert two_daemons.second.managed_targets("memory", "memories") == [
        "memories",
        journal_target,
    ]

    record = two_daemons.first.switch_complete()
    committed_revision = cast(int, record["committed_revision"])
    completed_target = cast(dict[str, str], record["physical_names"])["memories"]
    two_daemons.first.wait_for_revision(committed_revision)
    two_daemons.second.wait_for_revision(committed_revision)

    assert two_daemons.first.managed_targets("memory", "memories") == [
        "memories",
        completed_target,
    ]
    assert two_daemons.second.managed_targets("memory", "memories") == [
        "memories",
        completed_target,
    ]


def test_remote_secret_rotation_with_shared_kek(two_daemons: TwoDaemonCluster) -> None:
    revision = two_daemons.first.patch(
        expected_revision=0,
        secrets={
            "ai.embeddings.api_key": {
                "plaintext": "shared-secret-one",
                "name": "embeddings_api_key",
            }
        },
    )
    two_daemons.second.wait_for_revision(revision)

    first = two_daemons.first.secret_state()
    second = two_daemons.second.secret_state()

    assert two_daemons.first.spec.home != two_daemons.second.spec.home
    assert first["active"] == "shared-secret-one"
    assert second["active"] == "shared-secret-one"
    assert first["active_fingerprint"] == second["active_fingerprint"]


def test_wrong_kek_daemon_fails_closed(two_daemons: TwoDaemonCluster) -> None:
    revision = two_daemons.first.patch(
        expected_revision=0,
        secrets={
            "ai.embeddings.api_key": {
                "plaintext": "shared-secret",
                "name": "embeddings_api_key",
            }
        },
    )
    two_daemons.second.wait_for_revision(revision)

    wrong_key = two_daemons.add_worker("wrong-key", "incorrect-passphrase")
    status = wrong_key.status()

    assert status["ready"] is False
    assert status["healthy"] is False
    assert "SecretKeyUnavailable" in cast(str, status["startup_error"])


def test_same_reference_rotation_failure_isolation(two_daemons: TwoDaemonCluster) -> None:
    first_revision = two_daemons.first.patch(
        expected_revision=0,
        secrets={
            "ai.embeddings.api_key": {
                "plaintext": "old-payload",
                "name": "embeddings_api_key",
            }
        },
    )
    two_daemons.second.wait_for_revision(first_revision)
    two_daemons.second.set_fail("ai.embeddings.api_key")

    rotated_revision = two_daemons.first.patch(
        expected_revision=first_revision,
        secrets={
            "ai.embeddings.api_key": {
                "plaintext": "new-payload",
                "name": "embeddings_api_key",
            }
        },
    )
    public_snapshot = two_daemons.second.wait_for_revision(rotated_revision)
    first = two_daemons.first.secret_state()
    second = two_daemons.second.secret_state()

    assert first["active"] == "new-payload"
    assert second["desired"] == "new-payload"
    assert second["active"] == "old-payload"
    assert second["desired_fingerprint"] != second["active_fingerprint"]
    assert _value(public_snapshot, "desired", "ai.embeddings.api_key") == (
        "$secret:embeddings_api_key"
    )
    assert "old-payload" not in repr(public_snapshot)
    assert "new-payload" not in repr(public_snapshot)


def test_cross_process_cas_conflict(two_daemons: TwoDaemonCluster) -> None:
    def write(worker: DaemonWorker, key: str, value: bool) -> int | RuntimeError:
        try:
            return worker.patch(expected_revision=0, values={key: value})
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            write,
            two_daemons.first,
            "rules.enforcement_enabled",
            False,
        )
        second = executor.submit(
            write,
            two_daemons.second,
            "rules.aggregate_blocks",
            False,
        )
        outcomes = (first.result(), second.result())

    assert sum(outcome == 1 for outcome in outcomes) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, RuntimeError)]
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert type(conflict).__name__ == "RemoteConfigConflict"
    assert vars(conflict)["expected_revision"] == 0
    assert vars(conflict)["actual_revision"] == 1


def test_restart_pending_state_across_daemons(two_daemons: TwoDaemonCluster) -> None:
    revision = two_daemons.first.patch(
        expected_revision=0,
        values={"ui.enabled": True},
    )
    first_pending = two_daemons.first.wait_for_revision(revision)
    second_pending = two_daemons.second.wait_for_revision(revision)

    for snapshot in (first_pending, second_pending):
        assert _value(snapshot, "desired", "ui.enabled") is True
        assert _value(snapshot, "active", "ui.enabled") is False
        assert snapshot["pending_restart_keys"] == ["ui.enabled"]

    two_daemons.first.restart()
    first_restarted = two_daemons.first.snapshot()
    assert _value(first_restarted, "active", "ui.enabled") is True
    assert _value(two_daemons.second.snapshot(), "active", "ui.enabled") is False

    two_daemons.second.restart()
    second_restarted = two_daemons.second.snapshot()
    assert _value(second_restarted, "active", "ui.enabled") is True
    assert second_restarted["pending_restart_keys"] == []


def test_partitioned_daemon_self_fences_before_gc(two_daemons: TwoDaemonCluster) -> None:
    daemon_id = uuid4()
    lease_seconds = 1.0
    activated = time.monotonic()
    two_daemons.first.lease_activate(
        daemon_id,
        "old-generation",
        0,
        lease_seconds=lease_seconds,
    )
    assert (
        two_daemons.second.can_collect(
            "new-generation",
            1,
        )
        is False
    )

    two_daemons.first.partition()
    if time.monotonic() - activated < lease_seconds * 0.5:
        # Immediately after the partition the ack row is still live: GC of
        # the incompatible generation must stay blocked until the lease
        # expires or fences. (Guarded: on a slow machine the TTL may already
        # have lapsed by the time the partition lands.)
        assert two_daemons.second.can_collect("new-generation", 1) is False
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="lease renewal failed"):
        two_daemons.first.lease_renew()
    assert time.monotonic() - started < lease_seconds
    if time.monotonic() - activated < lease_seconds * 0.5:
        # A transient renewal failure alone does not fence; the local
        # deadline (TTL minus margin) is the fence of last resort.
        two_daemons.first.lease_assert()

    fenced = two_daemons.first.lease_wait_fenced(timeout=lease_seconds + 1.0)

    # The self-fence must land strictly before the database TTL measured from
    # activation — the deadline margin guarantees the ack row is still live
    # when the daemon stops serving, never the other way around.
    assert cast(float, fenced["fenced_after"]) < lease_seconds
    message = cast(str, fenced["message"])
    assert "lease expired" in message or "serving is fenced" in message

    assert (
        two_daemons.second.wait_until_collectible(
            "new-generation",
            1,
            timeout=lease_seconds + 1.0,
        )
        is True
    )

    two_daemons.first.restart()
    two_daemons.first.lease_activate(
        daemon_id,
        "new-generation",
        1,
        lease_seconds=lease_seconds,
    )
    two_daemons.first.lease_assert()


def test_partition_heal_reconciles_in_process(two_daemons: TwoDaemonCluster) -> None:
    lease_seconds = 5.0
    daemon_id = uuid4()
    first_revision = two_daemons.first.patch(
        expected_revision=0,
        values={"rules.enforcement_enabled": False},
    )
    two_daemons.second.wait_for_revision(first_revision)
    two_daemons.second.lease_activate(
        daemon_id,
        "healed-generation",
        first_revision,
        lease_seconds=lease_seconds,
    )

    two_daemons.second.partition()
    partitioned_revision = two_daemons.first.patch(
        expected_revision=first_revision,
        values={"rules.enforcement_enabled": True},
    )
    with pytest.raises(RuntimeError, match="lease renewal failed"):
        two_daemons.second.lease_renew()

    two_daemons.second.heal()

    # The healed daemon reconciles in process: same runtime, same listener
    # loop, no restart — the reconnect loop recovers the missed revision.
    healed = two_daemons.second.wait_for_revision(partitioned_revision, timeout=10.0)
    assert _value(healed, "active", "rules.enforcement_enabled") is True
    status = two_daemons.second.status()
    assert status["ready"] is True
    assert status["healthy"] is True
    # The reopened pool also carries the serving lease again: within the TTL
    # window a renewal through the healed pool succeeds without re-activation.
    two_daemons.second.lease_renew()
    two_daemons.second.lease_assert()


def test_switch_write_races_converge(two_daemons: TwoDaemonCluster) -> None:
    create_sequence = two_daemons.first.append_change("memory", "record", is_tombstone=False)
    journal = two_daemons.first.switch_start()
    two_daemons.first.switch_phase(PHASE_BUILDING)
    update_sequence = two_daemons.second.append_change("memory", "record")
    two_daemons.first.switch_phase(PHASE_FLIPPING)
    delete_sequence = two_daemons.second.append_change(
        "memory",
        "record",
        is_tombstone=True,
    )
    two_daemons.first.switch_phase(PHASE_ACTIVE)
    record = two_daemons.first.switch_complete(reconcile=False)
    promotion_sequence = two_daemons.second.append_change("memory", "promoted-record")
    committed_revision = cast(int, record["committed_revision"])
    two_daemons.second.wait_for_revision(committed_revision)

    changes = two_daemons.first.changes_after(0)
    promoted_target = cast(dict[str, str], record["physical_names"])["memories"]
    targets = two_daemons.second.projection_targets("memory", "memory_active")

    assert [change["sequence"] for change in changes] == [
        create_sequence,
        update_sequence,
        delete_sequence,
        promotion_sequence,
    ]
    assert [change["is_tombstone"] for change in changes] == [False, False, True, False]
    assert journal["physical_names"] == record["physical_names"]
    assert promoted_target in targets

    # Replay the ledger into a recording fake store: every raced write must
    # reach the promoted collection, and the final contents must reflect the
    # create/update/tombstone ordering.
    replayed: list[int] = []
    promoted_collection: dict[str, int] = {}
    for change in changes:
        replayed.append(cast(int, change["sequence"]))
        source_id = cast(str, change["source_id"])
        if cast(bool, change["is_tombstone"]):
            promoted_collection.pop(source_id, None)
        else:
            promoted_collection[source_id] = cast(int, change["sequence"])
    assert replayed == [create_sequence, update_sequence, delete_sequence, promotion_sequence]
    assert promoted_collection == {"promoted-record": promotion_sequence}

    # A lease prepared after the race carries a caught-up watermark that
    # covers every raced sequence; acknowledging at that watermark succeeds.
    assert two_daemons.first.watermark() >= promotion_sequence
    two_daemons.second.lease_activate(
        uuid4(),
        cast(str, record["run_id"]),
        committed_revision,
        lease_seconds=5.0,
    )
    two_daemons.second.lease_assert()
