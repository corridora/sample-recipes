import base64
from datetime import datetime, timezone, timedelta
import json
import time
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from conftest import spans, logs


def settings(tmp_path, **kwargs):
    return Settings(state_path=str(tmp_path / "state.db"), control_token="test-control", **kwargs)


def test_w3c_parentage_queue_and_three_otlp_signals(tmp_path, capture):
    endpoint, records = capture
    app = create_app(settings(tmp_path, endpoint=endpoint, ingest_key="fake-key"))
    trace_id, parent_id = "1" * 32, "2" * 16
    headers = {"traceparent": f"00-{trace_id}-{parent_id}-01"}
    with TestClient(app) as client:
        result = client.get("/api/trace-check", headers=headers)
        assert result.json()["trace_id"] == trace_id
        assert result.headers["cache-control"] == "no-store"
        assert result.headers["x-trace-id"] == trace_id
        client.post("/api/recipes/demo-recipe/enrich", headers=headers).raise_for_status()
        ready = client.get("/api/recipes/demo-recipe/enrichment").json()
        assert ready["status"] == "ready"
    captured = spans(records)
    server = next(s for s in captured if s.name == "GET /api/trace-check")
    assert server.trace_id.hex() == trace_id
    assert server.parent_span_id.hex() == parent_id
    producer = next(s for s in captured if s.kind == 4)
    consumer = next(s for s in captured if s.kind == 5)
    assert consumer.trace_id == producer.trace_id
    assert consumer.parent_span_id == producer.span_id
    assert {"/v1/traces", "/v1/logs", "/v1/metrics"} <= {r[0] for r in records}
    assert any(log.trace_id.hex() == trace_id for log in logs(records))
    assert all({k.lower(): v for k, v in header.items()}.get("x-corridora-key") == "fake-key" for _, header, _ in records)


def test_control_auth_window_expiry_and_seeded_control(tmp_path, monkeypatch):
    now = [datetime(2026, 10, 11, 12, tzinfo=timezone.utc)]
    app = create_app(settings(tmp_path), clock=lambda: now[0])
    sleeps = []
    monkeypatch.setattr("app.main.time.sleep", sleeps.append)
    auth = {"Authorization": "Bearer test-control"}
    with TestClient(app) as client:
        assert client.post("/api/demo/scenario", json={"phase": "regression", "duration_seconds": 10}).status_code == 401
        assert client.post("/api/demo/scenario", headers=auth, json={"phase": "regression", "duration_seconds": 601}).status_code == 422
        assert client.post("/api/demo/scenario", headers=auth, json={"phase": "regression", "duration_seconds": True}).status_code == 422
        assert client.post("/api/demo/scenario", headers=auth, json={"phase": "regression", "duration_seconds": 10}).status_code == 200
        bad = client.get("/api/recipes/demo-recipe").json()
        assert bad["read_mode"] == "sequential"
        assert sleeps == [0.25] * 6
        assert client.get("/api/recipes/control-recipe").json()["read_mode"] == "batch"
        now[0] += timedelta(seconds=11)
        assert client.get("/api/recipes/demo-recipe").json()["read_mode"] == "batch"
        now[0] = now[0].replace(hour=13, minute=42)
        assert client.get("/api/demo/status").json()["phase"] == "regression"
        now[0] = now[0].replace(minute=51)
        assert client.get("/api/demo/status").json()["phase"] == "healthy"


def test_empty_control_token_rejected_and_no_backend_key_in_html(tmp_path):
    config = settings(tmp_path, rum_key="visible-rum")
    config.control_token = ""
    with TestClient(create_app(config)) as client:
        assert client.post("/api/demo/scenario", headers={"Authorization": "Bearer "}, json={"phase": "regression", "duration_seconds": 10}).status_code == 401
        body = client.get("/").text
        assert "visible-rum" in body and "data-trace-origins" in body
        assert body.index("corridora-rum.js") < body.index('type="module"')
        assert "DEMO_CONTROL_TOKEN" not in body
        assert "CORRIDORA_INGEST_KEY" not in body
        assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_push_auth_poison_ack_and_consumer_extract(tmp_path, capture):
    endpoint, records = capture
    with TestClient(create_app(settings(tmp_path, endpoint=endpoint, ingest_key="fake"))) as client:
        envelope = {"message": {"data": base64.b64encode(json.dumps({"recipe_id": "demo-recipe"}).encode()).decode(),
            "attributes": {"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"}}, "deliveryAttempt": 2}
        assert client.post("/internal/enrich", json=envelope).status_code == 401
        result = client.post("/internal/enrich", headers={"Authorization": "Bearer test-control"}, json=envelope)
        assert result.json()["attempt"] == 2
        envelope["message"]["data"] = "bad-base64"
        assert client.post("/internal/enrich", headers={"Authorization": "Bearer test-control"}, json=envelope).json()["status"] == "poison-rejected"
    consumer = next(s for s in spans(records) if s.kind == 5)
    assert consumer.trace_id.hex() == "a" * 32
    assert consumer.parent_span_id.hex() == "b" * 16


def test_expiring_override_survives_new_process_repository(tmp_path):
    from app.repository import LocalRepository
    from app.scenarios import Scenarios
    from app.telemetry import Telemetry
    config = settings(tmp_path)
    first = LocalRepository(config.state_path)
    otel = Telemetry(config)
    scenario = Scenarios(config, first, otel)
    activated = scenario.activate("regression", 10)
    first.close()
    second = LocalRepository(config.state_path)
    assert Scenarios(config, second, otel).status()["release"] == activated["release"]
    second.close()
    otel.close()
