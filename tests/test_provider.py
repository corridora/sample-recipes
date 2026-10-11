import base64
import json
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app.main import create_app
from app.config import Settings
from app.provider import convert_log, convert_metric
from conftest import logs


def test_native_logs_keep_source_trace_timestamp_dedup_and_root_for_uncorrelated(tmp_path, capture):
    endpoint, records = capture
    config = Settings(endpoint=endpoint, ingest_key="fake", control_token="control", state_path=str(tmp_path / "db"))
    entry = {"insertId": "native-1", "timestamp": "2026-10-11T13:42:12.123456789Z", "trace": "projects/x/traces/" + "a" * 32,
             "spanId": "b" * 16, "traceSampled": True, "severity": "WARNING", "textPayload": "native request",
             "resource": {"type": "cloud_run_revision", "labels": {"service_name": "recipes"}}}
    def envelope(entry):
        return {"message": {"data": base64.b64encode(json.dumps(entry).encode()).decode()}}
    with TestClient(create_app(config)) as client:
        headers = {"Authorization": "Bearer control"}
        assert client.post("/internal/provider/logs", headers=headers, json=envelope(entry)).status_code == 200
        assert client.post("/internal/provider/logs", headers=headers, json=envelope(entry)).json()["duplicate"]
        entry = {"insertId": "native-2", "textPayload": "uncorrelated"}
        client.post("/internal/provider/logs", headers=headers, json=envelope(entry)).raise_for_status()
    found = [log for log in logs(records) if log.body.string_value == "native request"]
    assert len(found) == 1
    assert found[0].severity_number == 13
    assert found[0].trace_id.hex() == "a" * 32 and found[0].span_id.hex() == "b" * 16
    assert found[0].time_unix_nano == int(datetime(2026, 10, 11, 13, 42, 12, tzinfo=timezone.utc).timestamp() * 1e9) + 123456789
    uncorrelated = next(log for log in logs(records) if log.body.string_value == "uncorrelated")
    assert not uncorrelated.trace_id and not uncorrelated.span_id


def test_metric_adapter_maps_native_distribution_and_counters():
    values = convert_metric({"metric": {"type": "run.googleapis.com/request_count"},
        "points": [{"value": {"int64Value": "14"}, "interval": {"endTime": "2026-10-11T13:50:00Z"}}]})
    assert values[0][1] == 14
    distribution = convert_metric({"metric": {"type": "run.googleapis.com/request_latencies"},
        "points": [{"value": {"distributionValue": {"mean": 1532.5, "count": "6"}}}]})
    assert distribution[0][1] == 1532.5
    assert distribution[0][2]["gcp.metric.source"] == "cloud-monitoring-poll"
