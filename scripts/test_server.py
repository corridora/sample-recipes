"""Local-only OTLP sink and app; uses fake keys and never contacts Corridora."""
import base64
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
CAPTURE = ROOT / ".sample/browser-capture.jsonl"
CAPTURE.parent.mkdir(exist_ok=True)
CAPTURE.write_text("")
lock = threading.Lock()


class Capture(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "http://localhost:4103")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-CORRIDORA-KEY,X-Corridora-Ingest-Source")
        self.send_header("Access-Control-Allow-Methods", "POST,OPTIONS")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with lock, CAPTURE.open("a") as stream:
            stream.write(json.dumps({"path": self.path, "content_type": self.headers.get("Content-Type"),
                "source": self.headers.get("X-Corridora-Ingest-Source", "backend"), "body": base64.b64encode(body).decode()}) + "\n")
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 4313), Capture)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ.update({"SAMPLE_MODE": "local", "PORT": "4103", "CORRIDORA_TARGET": "own", "DEPLOYMENT_ENVIRONMENT": "test",
    "CORRIDORA_OTLP_ENDPOINT": "http://localhost:4313", "CORRIDORA_INGEST_KEY": "fake-backend-key",
    "CORRIDORA_RUM_KEY": "fake-rum-key" if os.environ.get("RUM_BUNDLE_PATH") else "",
    "DEMO_CONTROL_TOKEN": "local-test-control", "STATE_PATH": str(ROOT / ".sample/browser.db"),
    "REVIEW_SUMMARY_URL": "", "PUBLIC_URL": "http://localhost:4103", "SERVICE_VERSION": "test-build"})
uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=4103, access_log=False)
