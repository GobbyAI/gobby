import pytest

from gobby.storage.spans import SpanStorage


@pytest.fixture
def span_storage(temp_db):
    return SpanStorage(temp_db)


def test_save_and_get_span(span_storage):
    span_data = {
        "span_id": "span1",
        "trace_id": "trace1",
        "name": "test-span",
        "start_time_ns": 1000,
        "end_time_ns": 2000,
        "attributes": {"key": "value", "session_id": "sess1"},
        "events": [{"name": "event1", "timestamp": 1500}],
    }

    span_storage.save_span(span_data)

    trace_spans = span_storage.get_trace("trace1")
    assert len(trace_spans) == 1
    assert trace_spans[0]["span_id"] == "span1"
    assert trace_spans[0]["attributes"]["key"] == "value"
    assert trace_spans[0]["events"][0]["name"] == "event1"


def test_get_recent_traces(span_storage):
    # Trace 1
    span_storage.save_spans(
        [
            {"span_id": "s1", "trace_id": "t1", "name": "root1", "start_time_ns": 100},
            {
                "span_id": "s2",
                "trace_id": "t1",
                "name": "child1",
                "start_time_ns": 200,
                "parent_span_id": "s1",
            },
        ]
    )

    # Trace 2 (later)
    span_storage.save_spans(
        [
            {"span_id": "s3", "trace_id": "t2", "name": "root2", "start_time_ns": 300},
        ]
    )

    recent = span_storage.get_recent_traces(limit=10)
    assert len(recent) == 2
    assert recent[0]["trace_id"] == "t2"  # t2 is later
    assert recent[1]["trace_id"] == "t1"


def test_get_traces_by_session(span_storage):
    span_storage.save_spans(
        [
            {
                "span_id": "s1",
                "trace_id": "t1",
                "name": "root1",
                "start_time_ns": 100,
                "attributes": {"session_id": "sess1"},
            },
            {
                "span_id": "s2",
                "trace_id": "t2",
                "name": "root2",
                "start_time_ns": 200,
                "attributes": {"session_id": "sess2"},
            },
        ]
    )

    sess1_traces = span_storage.get_traces_by_session("sess1")
    assert len(sess1_traces) == 1
    assert sess1_traces[0]["trace_id"] == "t1"


def test_delete_old_spans(span_storage, temp_db):
    span_storage.save_span({"span_id": "old", "trace_id": "t1", "name": "old", "start_time_ns": 0})

    # Manually backdate created_at
    temp_db.execute(
        "UPDATE spans SET created_at = NOW() - INTERVAL '10 days' WHERE span_id = 'old'"
    )

    count = span_storage.delete_old_spans(retention_days=7)
    assert count == 1
    assert span_storage.get_span_count() == 0


def test_get_span_count(span_storage):
    assert span_storage.get_span_count() == 0
    span_storage.save_span({"span_id": "s1", "trace_id": "t1", "name": "n1", "start_time_ns": 100})
    assert span_storage.get_span_count() == 1
