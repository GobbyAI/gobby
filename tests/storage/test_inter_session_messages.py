"""TDD tests for inter_session_messages storage module.

RED phase - these tests define expected behavior before implementation exists.
Tests cover:
- InterSessionMessage dataclass
- InterSessionMessageManager CRUD operations
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


class TestInterSessionMessageDataclass:
    """TDD tests for InterSessionMessage dataclass."""

    def test_import_inter_session_message(self) -> None:
        """Test that InterSessionMessage can be imported from storage module."""
        from gobby.storage.inter_session_messages import InterSessionMessage

        assert InterSessionMessage is not None

    def test_dataclass_has_required_fields(self) -> None:
        """Test that InterSessionMessage has all required fields."""
        from gobby.storage.inter_session_messages import InterSessionMessage

        # Create an instance to verify fields exist
        msg = InterSessionMessage(
            id="msg-123",
            from_session="session-parent",
            to_session="session-child",
            content="Please work on subtask A",
            priority="normal",
            sent_at="2026-01-19T12:00:00Z",
        )

        assert msg.id == "msg-123"
        assert msg.from_session == "session-parent"
        assert msg.to_session == "session-child"
        assert msg.content == "Please work on subtask A"
        assert msg.priority == "normal"
        assert msg.sent_at == datetime(2026, 1, 19, 12, tzinfo=UTC)

    def test_from_row_creates_instance(self, temp_db: HubDatabase) -> None:
        """Test that InterSessionMessage.from_row creates instance from DB row."""
        from gobby.storage.inter_session_messages import InterSessionMessage
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        # Create project first (needed for foreign key)
        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        # Create sessions (needed for foreign key)
        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent-ext",
            machine_id="machine-1",
            source="claude",
            project_id=project.id,
        )
        child = session_mgr.register(
            external_id="child-ext",
            machine_id="machine-1",
            source="claude",
            project_id=project.id,
        )

        # Insert message directly
        import uuid

        msg_id = str(uuid.uuid4())
        temp_db.execute(
            """INSERT INTO inter_session_messages
               (id, from_session, to_session, content, priority, sent_at)
               VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
            (msg_id, parent.id, child.id, "Test content", "normal"),
        )

        # Fetch and convert
        row = temp_db.fetchone("SELECT * FROM inter_session_messages WHERE id = %s", (msg_id,))
        assert row is not None

        msg = InterSessionMessage.from_row(row)
        assert msg.id == msg_id
        assert msg.from_session == parent.id
        assert msg.to_session == child.id
        assert msg.content == "Test content"
        assert msg.priority == "normal"

    def test_to_dict_returns_dictionary(self) -> None:
        """Test that to_dict returns a dictionary with all fields."""
        from gobby.storage.inter_session_messages import InterSessionMessage

        msg = InterSessionMessage(
            id="msg-456",
            from_session="session-1",
            to_session="session-2",
            content="Hello child agent",
            priority="urgent",
            sent_at="2026-01-19T12:30:00Z",
        )

        d = msg.to_dict()
        assert d["id"] == "msg-456"
        assert d["from_session"] == "session-1"
        assert d["to_session"] == "session-2"
        assert d["content"] == "Hello child agent"
        assert d["priority"] == "urgent"
        assert d["sent_at"] == "2026-01-19T12:30:00+00:00"


class TestInterSessionMessageToBrief:
    """Tests for InterSessionMessage.to_brief() slim representation."""

    def test_to_brief_has_fewer_fields_than_to_dict(self) -> None:
        """to_brief returns fewer fields than to_dict."""
        from gobby.storage.inter_session_messages import InterSessionMessage

        msg = InterSessionMessage(
            id="msg-brief",
            from_session="session-1",
            to_session="session-2",
            content="Hello",
            priority="normal",
            sent_at="2026-01-19T12:00:00Z",
            message_type="message",
            metadata_json='{"key": "value"}',
            delivered_at="2026-01-19T12:05:00Z",
        )

        brief = msg.to_brief()
        full = msg.to_dict()
        assert len(brief) < len(full)

    def test_to_brief_essential_fields_present(self) -> None:
        """to_brief includes essential messaging fields."""
        from gobby.storage.inter_session_messages import InterSessionMessage

        msg = InterSessionMessage(
            id="msg-brief2",
            from_session="session-a",
            to_session="session-b",
            content="Important message",
            priority="urgent",
            sent_at="2026-01-19T12:00:00Z",
            message_type="command_result",
        )

        brief = msg.to_brief()
        assert brief["id"] == "msg-brief2"
        assert brief["from_session"] == "session-a"
        assert brief["to_session"] == "session-b"
        assert brief["content"] == "Important message"
        assert brief["priority"] == "urgent"
        assert brief["message_type"] == "command_result"
        assert brief["sent_at"] == "2026-01-19T12:00:00+00:00"

    def test_to_brief_excludes_internal_fields(self) -> None:
        """to_brief omits metadata_json and delivered_at."""
        from gobby.storage.inter_session_messages import InterSessionMessage

        msg = InterSessionMessage(
            id="msg-brief3",
            from_session="session-1",
            to_session="session-2",
            content="Test",
            priority="normal",
            sent_at="2026-01-19T12:00:00Z",
            metadata_json='{"foo": "bar"}',
            delivered_at="2026-01-19T12:01:00Z",
        )

        brief = msg.to_brief()
        assert "metadata_json" not in brief
        assert "delivered_at" not in brief


class TestInterSessionMessageManagerImport:
    """TDD tests for InterSessionMessageManager import and instantiation."""

    def test_import_manager(self) -> None:
        """Test that InterSessionMessageManager can be imported."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        assert InterSessionMessageManager is not None

    def test_manager_accepts_database(self, temp_db: HubDatabase) -> None:
        """Test that manager can be instantiated with database."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        manager = InterSessionMessageManager(temp_db)
        assert manager.db is temp_db


class TestInterSessionMessageManagerCreateMessage:
    """TDD tests for create_message method."""

    def test_create_message_returns_message(self, temp_db: HubDatabase) -> None:
        """Test that create_message returns an InterSessionMessage."""
        from gobby.storage.inter_session_messages import (
            InterSessionMessage,
            InterSessionMessageManager,
        )
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        # Setup project and sessions
        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent", machine_id="m1", source="claude", project_id=project.id
        )
        child = session_mgr.register(
            external_id="child", machine_id="m1", source="claude", project_id=project.id
        )

        manager = InterSessionMessageManager(temp_db)
        msg = manager.create_message(
            from_session=parent.id,
            to_session=child.id,
            content="Work on task X",
            priority="normal",
        )

        assert isinstance(msg, InterSessionMessage)
        assert msg.id is not None
        assert msg.from_session == parent.id
        assert msg.to_session == child.id
        assert msg.content == "Work on task X"
        assert msg.priority == "normal"

    def test_create_message_persists_to_database(self, temp_db: HubDatabase) -> None:
        """Test that created message is persisted to database."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent", machine_id="m1", source="claude", project_id=project.id
        )
        child = session_mgr.register(
            external_id="child", machine_id="m1", source="claude", project_id=project.id
        )

        manager = InterSessionMessageManager(temp_db)
        msg = manager.create_message(
            from_session=parent.id,
            to_session=child.id,
            content="Persistent message",
        )

        # Verify in database
        row = temp_db.fetchone("SELECT * FROM inter_session_messages WHERE id = %s", (msg.id,))
        assert row is not None
        assert row["content"] == "Persistent message"

    def test_create_message_defaults_priority_to_normal(self, temp_db: HubDatabase) -> None:
        """Test that priority defaults to 'normal' if not specified."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent", machine_id="m1", source="claude", project_id=project.id
        )
        child = session_mgr.register(
            external_id="child", machine_id="m1", source="claude", project_id=project.id
        )

        manager = InterSessionMessageManager(temp_db)
        msg = manager.create_message(
            from_session=parent.id,
            to_session=child.id,
            content="Default priority",
        )

        assert msg.priority == "normal"


class TestInterSessionMessageManagerGetMessages:
    """TDD tests for get_messages method."""

    def test_has_completion_notification_matches_metadata(self, temp_db: HubDatabase) -> None:
        """Completion notification lookup checks stable metadata IDs."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent", machine_id="m1", source="claude", project_id=project.id
        )
        child = session_mgr.register(
            external_id="child", machine_id="m1", source="claude", project_id=project.id
        )

        manager = InterSessionMessageManager(temp_db)
        manager.create_message(
            from_session=parent.id,
            to_session=child.id,
            content="Agent interrupted",
            message_type="completion_notification",
            metadata_json='{"completion_id": "run-1", "run_id": "run-1"}',
        )

        assert manager.has_completion_notification(
            child.id,
            "completion_notification",
            "run-1",
        )
        assert not manager.has_completion_notification(
            child.id,
            "completion_notification",
            "run-2",
        )

    def test_get_messages_returns_list(self, temp_db: HubDatabase) -> None:
        """Test that get_messages returns a list of messages."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent", machine_id="m1", source="claude", project_id=project.id
        )
        child = session_mgr.register(
            external_id="child", machine_id="m1", source="claude", project_id=project.id
        )

        manager = InterSessionMessageManager(temp_db)
        manager.create_message(
            from_session=parent.id,
            to_session=child.id,
            content="Message 1",
        )
        manager.create_message(
            from_session=parent.id,
            to_session=child.id,
            content="Message 2",
        )

        messages = manager.get_messages(to_session=child.id)
        assert isinstance(messages, list)
        assert len(messages) == 2

    def test_get_messages_filters_by_recipient(self, temp_db: HubDatabase) -> None:
        """Test that get_messages only returns messages for specified recipient."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent", machine_id="m1", source="claude", project_id=project.id
        )
        child1 = session_mgr.register(
            external_id="child1", machine_id="m1", source="claude", project_id=project.id
        )
        child2 = session_mgr.register(
            external_id="child2", machine_id="m1", source="claude", project_id=project.id
        )

        manager = InterSessionMessageManager(temp_db)
        manager.create_message(from_session=parent.id, to_session=child1.id, content="For child 1")
        manager.create_message(from_session=parent.id, to_session=child2.id, content="For child 2")

        messages = manager.get_messages(to_session=child1.id)
        assert len(messages) == 1
        assert messages[0].content == "For child 1"


class TestInterSessionMessageManagerGetMessage:
    """TDD tests for get_message method."""

    def test_get_message_returns_message(self, temp_db: HubDatabase) -> None:
        """Test that get_message returns the message by ID."""
        from gobby.storage.inter_session_messages import (
            InterSessionMessage,
            InterSessionMessageManager,
        )
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        parent = session_mgr.register(
            external_id="parent", machine_id="m1", source="claude", project_id=project.id
        )
        child = session_mgr.register(
            external_id="child", machine_id="m1", source="claude", project_id=project.id
        )

        manager = InterSessionMessageManager(temp_db)
        created = manager.create_message(
            from_session=parent.id, to_session=child.id, content="Fetch me"
        )

        fetched = manager.get_message(created.id)
        assert isinstance(fetched, InterSessionMessage)
        assert fetched.id == created.id
        assert fetched.content == "Fetch me"

    def test_get_message_returns_none_for_missing(self, temp_db: HubDatabase) -> None:
        """Test that get_message returns None for non-existent message."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        manager = InterSessionMessageManager(temp_db)
        result = manager.get_message(str(uuid.uuid4()))
        assert result is None


class TestInterSessionMessageManagerDeliveryClaims:
    """Atomic delivery claims are scoped to one recipient."""

    @pytest.fixture
    def mailbox(self, temp_db: HubDatabase):
        from gobby.storage.inter_session_messages import InterSessionMessageManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project = LocalProjectManager(temp_db).create(
            name="delivery-claims",
            repo_path="/tmp/delivery-claims",
        )
        sessions = SessionManager(temp_db)
        sender = sessions.register(
            external_id="claim-sender",
            machine_id="m1",
            source="claude",
            project_id=project.id,
        )
        recipient = sessions.register(
            external_id="claim-recipient",
            machine_id="m1",
            source="claude",
            project_id=project.id,
        )
        foreign = sessions.register(
            external_id="claim-foreign",
            machine_id="m1",
            source="claude",
            project_id=project.id,
        )
        return InterSessionMessageManager(temp_db), sender, recipient, foreign

    def test_claim_is_recipient_scoped_and_not_redelivered(self, mailbox) -> None:
        manager, sender, recipient, foreign = mailbox
        recipient_message = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="recipient message",
        )
        foreign_message = manager.create_message(
            from_session=sender.id,
            to_session=foreign.id,
            content="foreign message",
        )

        claimed = manager.claim_undelivered_messages(recipient.id)

        assert [message.id for message in claimed] == [recipient_message.id]
        assert manager.claim_undelivered_messages(recipient.id) == []
        assert manager.get_message(foreign_message.id).delivered_at is None

    def test_concurrent_claim_delivers_each_message_once(self, mailbox) -> None:
        manager, sender, recipient, _foreign = mailbox
        message = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="claim once",
        )
        barrier = threading.Barrier(3)

        def claim() -> list[str]:
            barrier.wait()
            return [item.id for item in manager.claim_undelivered_messages(recipient.id)]

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(lambda _index: claim(), range(3)))

        assert sum(results, []) == [message.id]

    def test_mark_delivered_requires_matching_undelivered_recipient(self, mailbox) -> None:
        manager, sender, recipient, foreign = mailbox
        message = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="recipient guard",
        )

        with pytest.raises(ValueError, match="Undelivered message not found"):
            manager.mark_delivered(message.id, foreign.id)
        assert manager.get_message(message.id).delivered_at is None

        delivered = manager.mark_delivered(message.id, recipient.id)
        assert delivered.delivered_at is not None
        with pytest.raises(ValueError, match="Undelivered message not found"):
            manager.mark_delivered(message.id, recipient.id)

    def test_get_messages_is_oldest_first_with_stable_id_tiebreaker(self, mailbox) -> None:
        manager, sender, recipient, _foreign = mailbox
        first = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="first id",
        )
        second = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="second id",
        )
        older = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="older",
        )
        tied_at = datetime(2026, 1, 2, tzinfo=UTC)
        manager.db.execute(
            "UPDATE inter_session_messages SET sent_at = %s WHERE id IN (%s, %s)",
            (tied_at, first.id, second.id),
        )
        manager.db.execute(
            "UPDATE inter_session_messages SET sent_at = %s WHERE id = %s",
            (tied_at - timedelta(days=1), older.id),
        )

        messages = manager.get_messages(recipient.id)

        assert [message.id for message in messages] == [older.id, *sorted([first.id, second.id])]

    def test_retention_deletes_only_old_delivered_messages(self, mailbox) -> None:
        manager, sender, recipient, _foreign = mailbox
        old = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="old delivered",
        )
        recent = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="recent delivered",
        )
        undelivered = manager.create_message(
            from_session=sender.id,
            to_session=recipient.id,
            content="old undelivered",
        )
        manager.mark_delivered(old.id, recipient.id)
        manager.mark_delivered(recent.id, recipient.id)
        cutoff = datetime(2026, 2, 1, tzinfo=UTC)
        manager.db.execute(
            "UPDATE inter_session_messages SET delivered_at = %s WHERE id = %s",
            (cutoff - timedelta(days=1), old.id),
        )

        deleted = manager.delete_delivered_before(cutoff)

        assert deleted == 1
        assert manager.get_message(old.id) is None
        assert manager.get_message(recent.id) is not None
        assert manager.get_message(undelivered.id) is not None


class TestInterSessionMessageManagerListMessages:
    """Tests for list_messages read-only query method."""

    @pytest.fixture
    def setup(self, temp_db: HubDatabase):
        """Create project, sessions, manager, and seed messages."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager
        from gobby.storage.projects import LocalProjectManager
        from gobby.storage.sessions import SessionManager

        project_mgr = LocalProjectManager(temp_db)
        project = project_mgr.create(name="test-project", repo_path="/tmp/test")

        session_mgr = SessionManager(temp_db)
        s_alpha = session_mgr.register(
            external_id="alpha", machine_id="m1", source="claude", project_id=project.id
        )
        s_beta = session_mgr.register(
            external_id="beta", machine_id="m1", source="claude", project_id=project.id
        )

        mgr = InterSessionMessageManager(temp_db)

        # alpha → beta (inbox for beta, sent for alpha)
        m1 = mgr.create_message(from_session=s_alpha.id, to_session=s_beta.id, content="msg-1")
        m2 = mgr.create_message(
            from_session=s_alpha.id,
            to_session=s_beta.id,
            content="msg-2",
            message_type="command_result",
        )
        # beta → alpha (inbox for alpha, sent for beta)
        m3 = mgr.create_message(from_session=s_beta.id, to_session=s_alpha.id, content="msg-3")

        # Mark m1 as delivered
        mgr.mark_delivered(m1.id, s_beta.id)

        class Setup:
            alpha = s_alpha
            beta = s_beta
            manager = mgr
            messages = (m1, m2, m3)

        return Setup()

    def test_direction_inbox(self, setup) -> None:
        """direction='inbox' returns only received messages."""
        msgs = setup.manager.list_messages(setup.beta.id, direction="inbox")
        assert len(msgs) == 2
        assert all(m.to_session == setup.beta.id for m in msgs)

    def test_direction_received_aliases_inbox(self, setup) -> None:
        """direction='received' returns only received messages."""
        msgs = setup.manager.list_messages(setup.beta.id, direction="received")
        assert len(msgs) == 2
        assert all(m.to_session == setup.beta.id for m in msgs)

    def test_direction_sent(self, setup) -> None:
        """direction='sent' returns only sent messages."""
        msgs = setup.manager.list_messages(setup.beta.id, direction="sent")
        assert len(msgs) == 1
        assert msgs[0].from_session == setup.beta.id

    def test_direction_all(self, setup) -> None:
        """direction='all' returns both sent and received."""
        msgs = setup.manager.list_messages(setup.beta.id, direction="all")
        assert len(msgs) == 3

    def test_undelivered_only(self, setup) -> None:
        """undelivered_only=True excludes messages with delivered_at set."""
        msgs = setup.manager.list_messages(setup.beta.id, direction="inbox", undelivered_only=True)
        # m1 was marked delivered, m2 is undelivered
        assert len(msgs) == 1
        assert msgs[0].content == "msg-2"

    def test_message_type_filter(self, setup) -> None:
        """message_type filters to a specific type."""
        msgs = setup.manager.list_messages(
            setup.beta.id,
            direction="inbox",
            message_type="command_result",
        )
        assert len(msgs) == 1
        assert msgs[0].message_type == "command_result"

    def test_limit_and_offset(self, setup) -> None:
        """limit and offset control pagination."""
        all_msgs = setup.manager.list_messages(setup.beta.id, direction="all")
        assert len(all_msgs) == 3

        page1 = setup.manager.list_messages(setup.beta.id, direction="all", limit=2, offset=0)
        assert len(page1) == 2

        page2 = setup.manager.list_messages(setup.beta.id, direction="all", limit=2, offset=2)
        assert len(page2) == 1

    def test_ordered_by_sent_at_desc(self, setup) -> None:
        """Results are ordered by sent_at DESC (newest first)."""
        msgs = setup.manager.list_messages(setup.beta.id, direction="all")
        sent_times = [m.sent_at for m in msgs]
        assert sent_times == sorted(sent_times, reverse=True)

    def test_empty_result(self, temp_db: HubDatabase) -> None:
        """Returns empty list when no messages match."""
        from gobby.storage.inter_session_messages import InterSessionMessageManager

        mgr = InterSessionMessageManager(temp_db)
        msgs = mgr.list_messages(str(uuid.uuid4()), direction="all")
        assert msgs == []

    def test_invalid_direction_raises_clear_error(self, setup) -> None:
        """Invalid directions are rejected instead of silently returning all messages."""
        with pytest.raises(ValueError, match="Invalid direction 'bogus'"):
            setup.manager.list_messages(setup.beta.id, direction="bogus")


class TestInterSessionMessageManagerExport:
    """TDD tests for module exports."""

    def test_exported_from_storage_init(self) -> None:
        """Test that InterSessionMessageManager is exported from storage package."""
        from gobby.storage import InterSessionMessageManager

        assert InterSessionMessageManager is not None
