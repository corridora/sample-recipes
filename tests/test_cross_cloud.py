from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


@pytest.mark.parametrize("exact_endpoint", [False, True])
def test_controlled_aws_leaf_receives_child_context_only_dependency_auth(tmp_path, capture, exact_endpoint):
    endpoint, _ = capture
    received = []
    class Leaf(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"product_id": "demo-product", "rating": 4.1, "count": 9}).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Leaf)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://localhost:{server.server_port}" + ("/internal/review-summary" if exact_endpoint else "")
    config = Settings(state_path=str(tmp_path / "db"), control_token="control-never-forward",
        ingest_key="backend-never-forward", endpoint=endpoint, review_url=url, downstream_token="leaf-only")
    try:
        with TestClient(create_app(config)) as client:
            result = client.get("/api/recipes/demo-recipe", headers={"traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"})
            assert result.json()["reviews"]["rating"] == 4.1
            assert result.json()["reviews"]["source"] == "aws"
        path, headers = received[0]
        headers = {k.lower(): v for k, v in headers.items()}
        assert path == "/internal/review-summary?product_id=demo-product"
        assert headers["traceparent"].split("-")[1] == "a" * 32
        assert headers["traceparent"].split("-")[2] != "b" * 16
        assert headers["authorization"] == "Bearer leaf-only"
        assert "x-corridora-key" not in headers
        assert "backend-never-forward" not in json.dumps(headers)
        assert "control-never-forward" not in json.dumps(headers)
    finally:
        server.shutdown()
        server.server_close()
