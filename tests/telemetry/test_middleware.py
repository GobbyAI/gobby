"""
Tests for TelemetryMiddleware.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from gobby.telemetry.instruments import TelemetryMetrics
from gobby.telemetry.middleware import TelemetryMiddleware


@pytest.fixture
def meter_provider():
    """Setup a fresh MeterProvider with an InMemoryMetricReader."""
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    return provider, reader


@pytest.fixture
def metrics_collector(meter_provider):
    """Setup a fresh TelemetryMetrics instance and patch the singleton getter."""
    provider, _ = meter_provider
    meter = provider.get_meter("test-meter")
    collector = TelemetryMetrics(meter)

    with patch("gobby.telemetry.middleware.get_telemetry_metrics", return_value=collector):
        yield collector


@pytest.fixture
def app(metrics_collector):
    """Create a FastAPI app with TelemetryMiddleware."""
    app = FastAPI()
    app.add_middleware(TelemetryMiddleware)

    @app.get("/test")
    async def test_endpoint():
        return {"status": "ok"}

    @app.get("/items/{item_id}")
    async def item_endpoint(item_id: str):
        return {"item_id": item_id}

    @app.get("/error")
    async def error_endpoint():
        raise ValueError("Test error")

    return app


def test_middleware_records_request(app, meter_provider, metrics_collector):
    _, reader = meter_provider
    client = TestClient(app)

    response = client.get("/test")
    assert response.status_code == 200

    # Check OTel metrics
    data = reader.get_metrics_data()
    assert data is not None

    metrics_found = {}
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                metrics_found[metric.name] = metric

    assert "http_requests_total" in metrics_found

    # Check attributes
    dp = metrics_found["http_requests_total"].data.data_points[0]
    assert dp.attributes["http.method"] == "GET"
    assert dp.attributes["http.target"] == "/test"
    assert dp.attributes["http.status_code"] == "200"

    # Check internal tracking
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["counters"]["http_requests_total"]["value"] == 1


def test_middleware_records_error(app, meter_provider, metrics_collector):
    _, reader = meter_provider
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/error")
    assert response.status_code == 500

    # Check OTel metrics
    data = reader.get_metrics_data()
    assert data is not None

    metrics_found = {}
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                metrics_found[metric.name] = metric

    assert "http_requests_errors_total" in metrics_found

    # Check attributes
    dp = metrics_found["http_requests_errors_total"].data.data_points[0]
    assert dp.attributes["http.method"] == "GET"
    assert dp.attributes["http.target"] == "/error"
    assert dp.attributes["http.status_code"] == "500"

    # Check internal tracking
    all_metrics = metrics_collector.get_all_metrics()
    assert all_metrics["counters"]["http_requests_errors_total"]["value"] == 1


def test_middleware_bounds_matched_route_attributes(app, meter_provider, metrics_collector):
    _, reader = meter_provider
    client = TestClient(app)

    first = client.get(
        "/items/first?session_id=query-session&project_id=query-project",
        headers={"X-Session-ID": "header-session", "X-Project-ID": "header-project"},
    )
    second = client.get("/items/second")
    assert first.status_code == second.status_code == 200

    data = reader.get_metrics_data()
    assert data is not None

    data_points = []
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http_requests_total":
                    data_points.extend(metric.data.data_points)

    assert len(data_points) == 1
    data_point = data_points[0]
    assert data_point.value == 2
    assert data_point.attributes["http.target"] == "/items/{item_id}"
    assert "session_id" not in data_point.attributes
    assert "project_id" not in data_point.attributes


def test_middleware_uses_fixed_target_for_unmatched_routes(app, meter_provider, metrics_collector):
    _, reader = meter_provider
    client = TestClient(app)

    first = client.get("/missing/first")
    second = client.get("/missing/second")
    assert first.status_code == second.status_code == 404

    data = reader.get_metrics_data()
    assert data is not None

    data_points = []
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http_requests_total":
                    data_points.extend(metric.data.data_points)

    assert len(data_points) == 1
    data_point = data_points[0]
    assert data_point.value == 2
    assert data_point.attributes["http.target"] == "unmatched"
