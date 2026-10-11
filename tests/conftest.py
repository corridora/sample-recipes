import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import pytest


@pytest.fixture
def capture():
    records = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            records.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://localhost:" + str(server.server_port), records
    server.shutdown()
    server.server_close()


def spans(records):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    return [span for path, _, body in records if path == "/v1/traces"
        for resource in ExportTraceServiceRequest.FromString(body).resource_spans
        for scope in resource.scope_spans for span in scope.spans]


def logs(records):
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
    return [log for path, _, body in records if path == "/v1/logs"
        for resource in ExportLogsServiceRequest.FromString(body).resource_logs
        for scope in resource.scope_logs for log in scope.log_records]
