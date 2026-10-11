"""GCP native signal conversion. Native trace IDs are preserved, never invented."""
import base64
from datetime import datetime, timezone, timedelta
import json
import re
import hashlib
import secrets
import time
from opentelemetry import context, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags


def decode_push(envelope):
    message = envelope["message"]
    raw = base64.b64decode(message["data"], validate=True)
    if len(raw) > 65536:
        raise ValueError("Push payload exceeds sample limit")
    return json.loads(raw), message.get("attributes", {}), int(envelope.get("deliveryAttempt", 1))


def convert_log(entry):
    resource = entry.get("resource", {})
    attributes = {
        "event.name": "gcp.platform.log", "cloud.provider": "gcp",
        "gcp.log.name": entry.get("logName", ""),
        "gcp.log.insert_id": entry.get("insertId", ""),
        "gcp.severity": entry.get("severity", "DEFAULT"),
        "gcp.resource.type": resource.get("type", ""),
        "gcp.log.timestamp": entry.get("timestamp", ""),
    }
    for key in ("project_id", "location", "service_name", "revision_name"):
        if key in resource.get("labels", {}):
            attributes["gcp.resource." + key] = resource["labels"][key]
    http = entry.get("httpRequest", {})
    for key in ("status", "requestMethod", "latency"):
        if key in http:
            attributes["gcp.http." + key] = http[key]
    # Avoid forwarding arbitrary user data/query strings and secrets from jsonPayload.
    body = entry.get("textPayload", "Cloud Run platform request")[:2048]
    trace_id = entry.get("trace", "").rsplit("/", 1)[-1]
    span_id = entry.get("spanId", "")
    ctx = context.Context()  # Never inherit the adapter HTTP request's trace.
    if len(trace_id) == 32 and len(span_id) == 16:
        try:
            span = SpanContext(int(trace_id, 16), int(span_id, 16), True,
                               TraceFlags(1 if entry.get("traceSampled") else 0))
            if span.is_valid:
                ctx = trace.set_span_in_context(NonRecordingSpan(span))
        except ValueError:
            pass
    return body, attributes, ctx


def forward_log(entry, telemetry):
    body, attributes, ctx = convert_log(entry)
    token = context.attach(ctx)
    try:
        try:
            raw = entry.get("timestamp", "")
            fractional = re.search(r"\.(\d{1,9})(?:Z|[+-]\d\d:\d\d)$", raw)
            seconds = int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            timestamp = seconds * 1_000_000_000 + int(fractional[1].ljust(9, "0")) if fractional else seconds * 1_000_000_000
        except ValueError:
            timestamp = None
        telemetry.event_at(body, timestamp, **attributes)
    finally:
        context.detach(token)


def convert_metric(series):
    metric_type = series.get("metric", {}).get("type", "")
    points = []
    for point in series.get("points", [])[:4]:
        value = point.get("value", {})
        if "int64Value" in value:
            number = float(value["int64Value"])
        elif "doubleValue" in value:
            number = float(value["doubleValue"])
        elif "distributionValue" in value:
            number = float(value["distributionValue"].get("mean", 0))
        else:
            continue
        points.append((metric_type, number, {"gcp.metric.type": metric_type,
            "gcp.interval.end": point.get("interval", {}).get("endTime", ""),
            "gcp.interval.start": point.get("interval", {}).get("startTime", ""),
            "gcp.resource.labels": json.dumps(series.get("resource", {}).get("labels", {}), sort_keys=True),
            "gcp.metric.labels": json.dumps(series.get("metric", {}).get("labels", {}), sort_keys=True),
            "gcp.metric.source": "cloud-monitoring-poll"}))
    return points


def claim_checkpoint(repo, bucket, key, now=None):
    """Atomic leased reservation; a concurrent delivery must retry until completion."""
    now = time.time() if now is None else now
    token = secrets.token_hex(12)
    def update(prior):
        prior = {k: v for k, v in prior.items() if v.get("expires_at", 0) > now}
        item = prior.get(key)
        if item and item["phase"] == "complete":
            return prior, ("duplicate", "")
        if item and item["lease_until"] > now:
            return prior, ("busy", "")
        # Bound retained completions, never evict an active lease.
        if len(prior) >= 128 and key not in prior:
            complete = sorted((v["expires_at"], k) for k, v in prior.items() if v["phase"] == "complete")
            if not complete:
                return prior, ("busy", "")
            del prior[complete[0][1]]
        prior[key] = {"phase": "pending", "token": token, "lease_until": now + 60, "expires_at": now + 86400}
        return prior, ("claimed", token)
    return repo.mutate(bucket, update)


def finish_checkpoint(repo, bucket, key, token, success):
    def update(prior):
        value = prior.get(key)
        if value and value.get("token") == token:
            if success:
                value["phase"] = "complete"
            else:
                del prior[key]
        return prior, None
    repo.mutate(bucket, update)


def observe_metric_points(series, instrument, telemetry, repo, claims=None):
    claims = [] if claims is None else claims
    for metric_type, number, attributes in convert_metric(series):
        point_key = hashlib.sha256(json.dumps([metric_type, attributes], sort_keys=True).encode()).hexdigest()
        state, token = claim_checkpoint(repo, "provider-metric-checkpoint", point_key)
        if state == "busy":
            raise RuntimeError("Native metric point already in flight; retry poll")
        if state == "duplicate":
            continue
        claims.append((point_key, token))
        instrument.record(number, attributes)
    return claims


def poll_metrics(settings, telemetry, repo):
    from google.auth import default
    from google.auth.transport.requests import AuthorizedSession
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/monitoring.read"])
    session = AuthorizedSession(credentials)
    now = datetime.now(timezone.utc)
    claims = []
    success = False
    try:
        for metric_type, instrument in [
            ("run.googleapis.com/request_count", telemetry.native_requests),
            ("run.googleapis.com/request_latencies", telemetry.native_latency),
        ]:
            response = session.get(
                f"https://monitoring.googleapis.com/v3/projects/{settings.project}/timeSeries",
                params={"filter": f'metric.type="{metric_type}" AND resource.labels.service_name="{settings.prefix}"',
                        "interval.startTime": (now - timedelta(minutes=20)).isoformat(),
                        "interval.endTime": now.isoformat(), "pageSize": 8, "view": "FULL"}, timeout=4)
            response.raise_for_status()
            for series in response.json().get("timeSeries", [])[:8]:
                observe_metric_points(series, instrument, telemetry, repo, claims=claims)
        telemetry.event("GCP Monitoring bounded poll complete", **{"event.name": "gcp.monitoring.poll", "gcp.points.new": len(claims)})
        if not telemetry.flush():
            raise RuntimeError("Native metric export failed; retry poll")
        success = True
    finally:
        for key, token in claims:
            finish_checkpoint(repo, "provider-metric-checkpoint", key, token, success)
