"""Tests for MetricSnapshotStorage."""

import pytest

from gobby.storage.metric_snapshots import MetricSnapshotStorage


@pytest.fixture
def storage(temp_db):
    """Create MetricSnapshotStorage with test database."""
    return MetricSnapshotStorage(temp_db)


@pytest.fixture
def sample_metrics():
    return {
        "counters": {"http_requests_total": {"value": 42}},
        "gauges": {"daemon_memory_usage_bytes": {"value": 104857600}},
        "histograms": {},
        "uptime_seconds": 3600,
    }


class TestMetricSnapshotStorage:
    def test_save_and_get_snapshot(self, storage, sample_metrics):
        storage.save_snapshot(sample_metrics)
        snapshots = storage.get_snapshots(hours=1)
        assert len(snapshots) == 1
        assert snapshots[0]["metrics"]["uptime_seconds"] == 3600
        assert snapshots[0]["metrics"]["counters"]["http_requests_total"]["value"] == 42

    def test_get_snapshots_empty(self, storage):
        snapshots = storage.get_snapshots(hours=1)
        assert snapshots == []

    def test_get_snapshots_respects_limit(self, storage, sample_metrics):
        for _ in range(5):
            storage.save_snapshot(sample_metrics)
        snapshots = storage.get_snapshots(hours=1, limit=3)
        assert len(snapshots) == 3

    def test_delete_old_snapshots(self, storage, sample_metrics, temp_db):
        storage.save_snapshot(sample_metrics)
        # Manually backdate the snapshot
        temp_db.execute("UPDATE metric_snapshots SET timestamp = NOW() - INTERVAL '25 hours'")
        deleted = storage.delete_old_snapshots(retention_hours=24)
        assert deleted == 1
        assert storage.get_snapshot_count() == 0

    def test_get_snapshot_count(self, storage, sample_metrics):
        assert storage.get_snapshot_count() == 0
        storage.save_snapshot(sample_metrics)
        storage.save_snapshot(sample_metrics)
        assert storage.get_snapshot_count() == 2

    def test_snapshot_json_serialization(self, storage):
        """Verify complex metrics serialize/deserialize correctly."""
        metrics = {
            "counters": {"a": {"value": 1}, "b": {"value": 2}},
            "gauges": {"cpu": {"value": 45.5}},
            "histograms": {"latency": {"count": 10, "sum": 5.5, "avg": 0.55}},
            "uptime_seconds": 100,
        }
        storage.save_snapshot(metrics)
        result = storage.get_snapshots(hours=1)
        assert result[0]["metrics"] == metrics
