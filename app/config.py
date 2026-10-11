"""Only public browser settings are serialized by the API."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib.parse import urlparse


@dataclass
class Settings:
    mode: str = "local"
    port: int = 4103
    target: str = "own"
    endpoint: str = "https://ingest.corridora.com"
    ingest_key: str = ""
    rum_key: str = ""
    rum_script: str = "https://rum.corridora.com/corridora-rum.js"
    service: str = "sample-recipes-api"
    namespace: str = "corridora-samples.recipes"
    environment: str = "development"
    version: str = "local"
    control_token: str = ""
    daily_start: str = "13:40"
    window_seconds: int = 600
    review_url: str = ""
    downstream_token: str = ""
    public_url: str = "http://localhost:4103"
    project: str = ""
    region: str = "us-central1"
    prefix: str = "sample-recipes"
    firestore_database: str = "(default)"
    bucket: str = ""
    topic: str = ""
    push_identity: str = ""
    provider_forwarding: bool = False
    synthetics: bool = False
    state_path: str = ".sample/recipes.db"

    @classmethod
    def from_env(cls):
        values = {}
        path = os.environ.get("CORRIDORA_CONFIG_FILE")
        if path:
            values.update(json.loads(Path(path).read_text()))
        values.update(os.environ)
        keys = {
            "mode": "SAMPLE_MODE", "port": "PORT", "target": "CORRIDORA_TARGET",
            "endpoint": "CORRIDORA_OTLP_ENDPOINT", "ingest_key": "CORRIDORA_INGEST_KEY",
            "rum_key": "CORRIDORA_RUM_KEY", "rum_script": "CORRIDORA_RUM_SCRIPT_URL",
            "service": "OTEL_SERVICE_NAME", "namespace": "OTEL_SERVICE_NAMESPACE",
            "environment": "DEPLOYMENT_ENVIRONMENT", "version": "SERVICE_VERSION",
            "control_token": "DEMO_CONTROL_TOKEN", "daily_start": "DEMO_DAILY_START_UTC",
            "window_seconds": "DEMO_WINDOW_SECONDS", "review_url": "REVIEW_SUMMARY_URL",
            "downstream_token": "DOWNSTREAM_AUTH_TOKEN", "public_url": "PUBLIC_URL",
            "project": "GCP_PROJECT", "region": "GCP_REGION", "prefix": "RESOURCE_PREFIX",
            "bucket": "GCS_BUCKET", "topic": "PUBSUB_TOPIC", "push_identity": "PUSH_IDENTITY",
            "firestore_database": "FIRESTORE_DATABASE", "state_path": "STATE_PATH",
            "provider_forwarding": "ENABLE_PROVIDER_FORWARDING", "synthetics": "ENABLE_SYNTHETICS"
        }
        for field, key in keys.items():
            if key in values:
                values[field] = values[key]
        kwargs = {field: values[field] for field in keys if field in values}
        for key in ("port", "window_seconds"):
            if key in kwargs:
                kwargs[key] = int(kwargs[key])
        for key in ("provider_forwarding", "synthetics"):
            if key in kwargs:
                kwargs[key] = str(kwargs[key]).lower() == "true"
        result = cls(**kwargs)
        result.validate()
        return result

    def validate(self):
        hour, minute = map(int, self.daily_start.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60 and 1 <= self.window_seconds <= 600):
            raise ValueError("Invalid UTC scenario window")
        if self.mode not in ("local", "cloud") or self.target not in ("own", "demo"):
            raise ValueError("SAMPLE_MODE must be local/cloud; CORRIDORA_TARGET own/demo")
        for url in (self.endpoint, self.rum_script, self.public_url, self.review_url):
            if not url:
                continue
            parsed = urlparse(url)
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError("URLs must not contain credentials or fragments")
            if parsed.scheme != "https" and not (self.mode == "local" and parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")):
                raise ValueError("Endpoints must use HTTPS (local localhost HTTP allowed)")
        if self.mode == "cloud" and not all((self.project, self.bucket, self.topic, self.control_token, self.ingest_key, self.rum_key)):
            raise ValueError("Cloud requires project, bucket, topic, control and Corridora credentials")
        if self.target == "demo" and (not self.ingest_key or not self.rum_key or self.environment != "demo"):
            raise ValueError("Demo requires operator-provisioned ingest/RUM keys and environment demo")
