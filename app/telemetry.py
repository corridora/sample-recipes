"""Real OTel SDK providers; no automatic exporter HTTP instrumentation."""
import sys
import threading
import time
from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter


class Telemetry:
    def __init__(self, settings):
        resource = Resource.create({
            "service.name": settings.service, "service.namespace": settings.namespace,
            "service.version": settings.version,
            "deployment.environment.name": settings.environment,
            "cloud.provider": "gcp" if settings.mode == "cloud" else "local",
            "cloud.platform": "gcp_cloud_run" if settings.mode == "cloud" else "local",
            "cloud.account.id": settings.project or "local", "cloud.region": settings.region,
            "cloud.resource_id": settings.public_url,
        })
        self.traces = TracerProvider(resource=resource)
        self.logs = LoggerProvider(resource=resource)
        readers = []
        self.enabled = bool(settings.ingest_key)
        self.errors = 0
        self.last_flush_ok = True
        self.flush_lock = threading.Lock()
        self.export_failed = {}
        if self.enabled:
            common = {"headers": {"X-CORRIDORA-KEY": settings.ingest_key}, "timeout": 1.5}
            base = settings.endpoint.rstrip("/")
            self.traces.add_span_processor(BatchSpanProcessor(
                self.observe(OTLPSpanExporter(endpoint=base + "/v1/traces", **common), "traces"),
                max_queue_size=256, max_export_batch_size=64, schedule_delay_millis=5000))
            self.logs.add_log_record_processor(BatchLogRecordProcessor(
                self.observe(OTLPLogExporter(endpoint=base + "/v1/logs", **common), "logs"),
                max_queue_size=256, max_export_batch_size=64, schedule_delay_millis=5000))
            readers = [PeriodicExportingMetricReader(
                self.observe(OTLPMetricExporter(endpoint=base + "/v1/metrics", **common), "metrics"),
                export_interval_millis=60000, export_timeout_millis=1500)]
        self.metrics = MeterProvider(resource=resource, metric_readers=readers)
        self.tracer = self.traces.get_tracer("sample-recipes", "1.0.0")
        meter = self.metrics.get_meter("sample-recipes")
        self.requests = meter.create_counter("sample.http.requests", unit="{request}")
        self.duration = meter.create_histogram("sample.http.duration", unit="s")
        self.reads = meter.create_counter("sample.recipe.reads", unit="{read}")
        self.jobs = meter.create_counter("sample.enrichment.jobs", unit="{job}")
        self.native_requests = meter.create_histogram("gcp.run.request_count", unit="{request}")
        self.native_latency = meter.create_histogram("gcp.run.request_latencies", unit="ms")
        self.logger = self.logs.get_logger("sample-recipes", "1.0.0")

    def event(self, message, **attributes):
        self.logger.emit(body=message, attributes=attributes, severity_number=SeverityNumber.INFO, severity_text="INFO")

    def observe(self, exporter, signal):
        original = exporter.export
        def export(*args, **kwargs):
            try:
                result = original(*args, **kwargs)
                failed = getattr(result, "name", "SUCCESS") != "SUCCESS"
                self.export_failed[signal] = failed
                if failed:
                    self.errors += 1
                    print(f"OTLP {signal} export failed", file=sys.stderr)
                return result
            except Exception:
                self.export_failed[signal] = True
                self.errors += 1
                raise
        exporter.export = export
        return exporter

    def event_at(self, message, timestamp, **attributes):
        severity = {"DEBUG": SeverityNumber.DEBUG, "INFO": SeverityNumber.INFO,
                    "NOTICE": SeverityNumber.INFO, "WARNING": SeverityNumber.WARN,
                    "ERROR": SeverityNumber.ERROR, "CRITICAL": SeverityNumber.FATAL,
                    "ALERT": SeverityNumber.FATAL, "EMERGENCY": SeverityNumber.FATAL}.get(
                        attributes.get("gcp.severity", "INFO"), SeverityNumber.INFO)
        self.logger.emit(body=message, timestamp=timestamp, attributes=attributes,
                         severity_number=severity, severity_text=attributes.get("gcp.severity", "INFO"))

    def flush(self, budget_ms=5000):
        # Sequential finite budgets. Request CPU remains allocated until response.
        with self.flush_lock:
            deadline = time.monotonic() + budget_ms / 1000
            outcomes = []
            for provider in (self.traces, self.logs, self.metrics):
                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                try:
                    outcomes.append(provider.force_flush(timeout_millis=remaining) is not False)
                except Exception as error:
                    # Never attach a telemetry handler to exporter diagnostics.
                    print(f"telemetry flush failed: {type(error).__name__}", file=sys.stderr)
                    outcomes.append(False)
            self.last_flush_ok = all(outcomes) and not any(self.export_failed.values())
            if not self.last_flush_ok:
                self.errors += 1
            return self.last_flush_ok

    def close(self):
        self.flush()
        for provider in (self.traces, self.logs, self.metrics):
            provider.shutdown()


def ids():
    ctx = trace.get_current_span().get_span_context()
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
