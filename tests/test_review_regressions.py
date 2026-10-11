import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.provider import claim_checkpoint, finish_checkpoint, poll_metrics
from app.repository import LocalRepository
from app.scenarios import Scenarios
from app.telemetry import Telemetry

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def test_checkpoint_concurrent_same_and_distinct_messages(tmp_path):
    repo = LocalRepository(str(tmp_path / "db"))
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: claim_checkpoint(repo, "logs", "same", now=100), range(8)))
    assert [state for state, _ in results].count("claimed") == 1
    assert [state for state, _ in results].count("busy") == 7
    token = next(token for state, token in results if state == "claimed")
    finish_checkpoint(repo, "logs", "same", token, True)
    assert claim_checkpoint(repo, "logs", "same", now=101)[0] == "duplicate"
    with ThreadPoolExecutor(max_workers=8) as workers:
        states = list(workers.map(lambda index: claim_checkpoint(repo, "logs", str(index), now=100), range(32)))
    assert all(state == "claimed" for state, _ in states)
    assert len(repo.get("logs")) == 33
    repo.close()


def test_failed_export_releases_claim_and_expired_lease_can_retry(tmp_path):
    repo = LocalRepository(str(tmp_path / "db"))
    _, first = claim_checkpoint(repo, "logs", "id", now=100)
    finish_checkpoint(repo, "logs", "id", first, False)
    assert claim_checkpoint(repo, "logs", "id", now=101)[0] == "claimed"
    _, replacement = claim_checkpoint(repo, "logs", "id", now=162)
    assert replacement
    finish_checkpoint(repo, "logs", "id", first, True)
    assert repo.get("logs")["id"]["phase"] == "pending"
    finish_checkpoint(repo, "logs", "id", replacement, True)
    assert repo.get("logs")["id"]["phase"] == "complete"
    repo.close()


def test_native_log_failed_flush_is_retryable_and_not_checkpointed(tmp_path, monkeypatch):
    app = create_app(Settings(state_path=str(tmp_path / "db"), control_token="control"))
    state = {"success": False}
    monkeypatch.setattr(app.state.adapter_otel, "flush", lambda: state["success"])
    envelope = {"message": {"data": base64.b64encode(json.dumps({"insertId": "native-1"}).encode()).decode()}}
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer control"}
        assert client.post("/internal/provider/logs", headers=headers, json=envelope).status_code == 503
        assert app.state.repo.get("provider-log-checkpoint") == {}
        state["success"] = True
        assert client.post("/internal/provider/logs", headers=headers, json=envelope).status_code == 200
        assert client.post("/internal/provider/logs", headers=headers, json=envelope).json()["duplicate"]


def test_overlapping_metric_points_export_once_after_success_and_retry_failures(tmp_path, monkeypatch):
    import google.auth
    import google.auth.transport.requests
    repo = LocalRepository(str(tmp_path / "db"))
    recorded = []
    instrument = SimpleNamespace(record=lambda value, attrs: recorded.append((value, attrs)))
    state = {"success": False}
    telemetry = SimpleNamespace(native_requests=instrument, native_latency=instrument,
        event=lambda *a, **k: None, flush=lambda: state["success"])
    class Session:
        def __init__(self, credentials):
            pass
        def get(self, url, params, timeout):
            metric = "count" if "request_count" in params["filter"] else "latency"
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {"timeSeries": [{"metric": {"type": metric}, "resource": {"labels": {"revision": "v1"}},
                    "points": [{"interval": {"startTime": "2026-10-11T13:40:00Z", "endTime": "2026-10-11T13:45:00Z"}, "value": {"int64Value": "10"}}]}]})
    monkeypatch.setattr(google.auth, "default", lambda **kwargs: (object(), None))
    monkeypatch.setattr(google.auth.transport.requests, "AuthorizedSession", Session)
    with pytest.raises(RuntimeError, match="export failed"):
        poll_metrics(Settings(), telemetry, repo)
    assert repo.get("provider-metric-checkpoint") == {}
    state["success"] = True
    poll_metrics(Settings(), telemetry, repo)
    size = len(recorded)
    poll_metrics(Settings(), telemetry, repo)
    assert len(recorded) == size
    assert len(repo.get("provider-metric-checkpoint")) == 2
    repo.close()


def test_midnight_daily_window_and_configured_watchdog_duration(tmp_path):
    from daily import needs_replay
    config = Settings(state_path=str(tmp_path / "db"), daily_start="23:55", window_seconds=600)
    repo = LocalRepository(config.state_path)
    telemetry = Telemetry(config)
    now = [datetime(2026, 10, 11, 23, 57, tzinfo=timezone.utc)]
    scenarios = Scenarios(config, repo, telemetry, clock=lambda: now[0])
    before = scenarios.status()
    assert before["phase"] == "regression"
    now[0] = datetime(2026, 10, 12, 0, 3, tzinfo=timezone.utc)
    after = scenarios.status()
    assert after["phase"] == "regression" and after["release"] == before["release"]
    state = {"daily_start_utc": "23:55", "window_seconds": 600}
    assert not needs_replay(state, now[0])
    now[0] = now[0].replace(minute=6)
    assert scenarios.status()["phase"] == "healthy" and needs_replay(state, now[0])
    state["window_seconds"] = 120
    assert needs_replay(state, now[0].replace(hour=23, minute=58))
    assert not needs_replay(state, now[0].replace(hour=0, minute=0))
    state["heartbeat"] = {"utc_date": "2026-10-12"}
    assert not needs_replay(state, now[0])
    telemetry.close()
    repo.close()


def test_resource_identity_change_rejected_before_any_cloud_commands(tmp_path, monkeypatch):
    import gcp
    import setup
    monkeypatch.setattr(gcp, "STATE", tmp_path)
    config = dict(setup.DEFAULTS, GCP_PROJECT="fixture", PUBSUB_TOPIC="original-topic")
    cloud = gcp.GCP(config)
    gcp.write_private(cloud.path, cloud.inventory)
    monkeypatch.setattr(gcp, "execute", lambda *a, **k: pytest.fail("must reject before cloud commands"))
    for changes in ({"PUBSUB_TOPIC": "new-topic"}, {"PUSH_IDENTITY": "new@fixture.iam.gserviceaccount.com"}, {"GCP_REGION": "us-east1"}):
        with pytest.raises(ValueError, match="Immutable deployed resource settings changed"):
            gcp.GCP({**config, **changes})
