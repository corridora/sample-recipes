from contextlib import asynccontextmanager
from dataclasses import replace
import asyncio
import base64
import hmac
import hashlib
import html
import json
from pathlib import Path
import time
from urllib.parse import urlencode, urlparse

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from opentelemetry import context, propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, ConfigDict, StrictInt

from .config import Settings
from .provider import decode_push, forward_log, poll_metrics, claim_checkpoint, finish_checkpoint
from .repository import CloudRepository, LocalRepository
from .scenarios import Scenarios
from .telemetry import Telemetry, ids

ROOT = Path(__file__).resolve().parent.parent


def create_app(settings=None, repository=None, telemetry=None, clock=None):
    settings = settings or Settings.from_env()
    settings.validate()
    repo = repository or (CloudRepository(settings) if settings.mode == "cloud" else LocalRepository(settings.state_path))
    otel = telemetry or Telemetry(settings)
    worker_otel = Telemetry(replace(settings, service="sample-recipes-enrichment"))
    adapter_otel = Telemetry(replace(settings, service="sample-recipes-gcp-adapter"))
    scenarios = Scenarios(settings, repo, otel, clock)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(otel.close)
        await asyncio.to_thread(worker_otel.close)
        await asyncio.to_thread(adapter_otel.close)
        repo.close()

    app = FastAPI(title="The Recipe Table", lifespan=lifespan)
    app.state.settings, app.state.repo, app.state.otel, app.state.scenarios = settings, repo, otel, scenarios
    app.state.adapter_otel = adapter_otel

    @app.middleware("http")
    async def instrument(request, call_next):
        if not request.url.path.startswith(("/api/", "/internal/")):
            return await call_next(request)
        started = time.perf_counter()
        incoming = propagate.extract(dict(request.headers))
        path = request.url.path
        try:
            with otel.tracer.start_as_current_span(request.method + " " + path, context=incoming, kind=SpanKind.SERVER,
                    attributes={"http.request.method": request.method, "url.path": path}) as span:
                try:
                    response = await call_next(request)
                    span.set_attribute("http.response.status_code", response.status_code)
                    if response.status_code >= 500:
                        span.set_status(Status(StatusCode.ERROR))
                    response.headers["Cache-Control"] = "no-store"
                    response.headers["X-Trace-ID"] = ids()["trace_id"]
                    otel.requests.add(1, {"http.request.method": request.method, "http.response.status_code": response.status_code})
                    otel.duration.record(time.perf_counter() - started, {"http.request.method": request.method})
                    otel.event("Request completed", **{"http.request.method": request.method, "url.path": path,
                        "http.response.status_code": response.status_code})
                except Exception as error:
                    span.record_exception(error)
                    span.set_status(Status(StatusCode.ERROR))
                    otel.event("Request failed", **{"error.type": type(error).__name__, "url.path": path})
                    raise
        finally:
            # End the SERVER span before flushing, including exceptional paths.
            await asyncio.to_thread(otel.flush)
        return response

    def control(request: Request):
        expected = settings.control_token
        header = request.headers.get("authorization", "")
        if not expected or not hmac.compare_digest(header, "Bearer " + expected):
            raise HTTPException(401, "Valid dedicated demo control token required")

    def push_auth(request: Request):
        if settings.mode == "local":
            return control(request)
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or not settings.push_identity:
            raise HTTPException(401, "OIDC push identity required")
        class BoundedRequest(GoogleRequest):
            def __call__(self, *args, **kwargs):
                kwargs["timeout"] = 4
                return super().__call__(*args, **kwargs)
        try:
            claims = id_token.verify_oauth2_token(header[7:], BoundedRequest(), audience=settings.public_url)
            if claims.get("email") != settings.push_identity or not claims.get("email_verified"):
                raise ValueError("Unexpected push service account")
        except Exception:
            raise HTTPException(401, "Invalid OIDC push identity") from None

    @app.get("/healthz")
    def health():
        producers = (otel, worker_otel, adapter_otel)
        return JSONResponse({"status": "ok", "export_enabled": otel.enabled,
                "last_flush_ok": all(p.last_flush_ok for p in producers),
                "flush_failures": sum(p.errors for p in producers), "service_version": settings.version}, headers={"Cache-Control": "no-store"})

    @app.get("/api/trace-check")
    def trace_check():
        return {**ids(), "service": settings.service}

    @app.get("/api/demo/status")
    def demo_status():
        return {**scenarios.status(), "daily_start_utc": settings.daily_start,
                "window_seconds": settings.window_seconds,
                "heartbeat": repo.get("heartbeat")}

    class Scenario(BaseModel):
        model_config = ConfigDict(extra="forbid")
        phase: str
        duration_seconds: StrictInt

    @app.post("/api/demo/scenario", dependencies=[Depends(control)])
    def scenario(body: Scenario):
        try:
            return scenarios.activate(body.phase, body.duration_seconds)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    class Heartbeat(BaseModel):
        model_config = ConfigDict(extra="forbid")
        trace_id: str
        source: str = "playwright"

    @app.post("/api/demo/heartbeat", dependencies=[Depends(control)])
    def heartbeat(body: Heartbeat):
        from datetime import datetime, timezone
        if body.source != "playwright" or len(body.trace_id) != 32:
            raise HTTPException(422, "Playwright journey trace_id required")
        record = {"utc_date": datetime.now(timezone.utc).date().isoformat(), "trace_id": body.trace_id,
                  "source": body.source, "completed_at": time.time()}
        repo.set("heartbeat", record)
        otel.event("Browser incident journey completed", **{"event.name": "demo.heartbeat", "demo.trace_id": body.trace_id})
        return record

    @app.get("/api/recipes")
    def search(q: str = ""):
        if len(q) > 100:
            raise HTTPException(422, "Query too long")
        with otel.tracer.start_as_current_span("Firestore recipe search", kind=SpanKind.CLIENT,
                attributes={"db.system.name": "firestore" if settings.mode == "cloud" else "sqlite"}):
            return {"items": repo.search(q)}

    def get_recipe(recipe_id):
        recipe = repo.recipe(recipe_id)
        if not recipe:
            raise HTTPException(404, "Recipe not found")
        return recipe

    def review_summary():
        if not settings.review_url:
            with otel.tracer.start_as_current_span("Local review summary fixture", kind=SpanKind.CLIENT,
                    attributes={"demo.dependency.fixture": True}):
                return {"product_id": "demo-product", "rating": 4.5, "count": 12, "source": "local-fixture"}
        with otel.tracer.start_as_current_span("AWS review summary", kind=SpanKind.CLIENT,
                attributes={"server.address": urlparse(settings.review_url).hostname, "cloud.remote.provider": "aws"}) as span:
            headers = {}
            propagate.inject(headers)
            if settings.downstream_token:
                headers["Authorization"] = "Bearer " + settings.downstream_token
            try:
                with httpx.Client(timeout=2.5, follow_redirects=False) as client:
                    base = settings.review_url.rstrip("/")
                    url = base if urlparse(base).path == "/internal/review-summary" else base + "/internal/review-summary"
                    reply = client.get(url,
                                       params={"product_id": "demo-product"}, headers=headers)
                    reply.raise_for_status()
                    data = reply.json()
                    if not isinstance(data.get("rating"), (int, float)) or not isinstance(data.get("count"), int):
                        raise ValueError("Invalid review summary contract")
                    return {"product_id": "demo-product", "rating": data["rating"], "count": data["count"], "source": "aws"}
            except (httpx.HTTPError, ValueError) as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR))
                otel.event("Review dependency fallback", **{"error.type": type(error).__name__})
                return {"product_id": "demo-product", "rating": 4.5, "count": 12, "source": "bounded-fallback"}

    @app.get("/api/recipes/{recipe_id}")
    def detail(recipe_id: str):
        started = time.perf_counter()
        recipe = get_recipe(recipe_id)
        state = scenarios.status()
        bad = recipe_id == "demo-recipe" and state["phase"] == "regression"
        trace.get_current_span().set_attributes({"demo.scenario": state["scenario"], "demo.release": state["release"],
                                               "demo.regression": bad})
        if bad:
            ingredients = []
            for name in recipe["ingredients"]:
                with otel.tracer.start_as_current_span("Firestore ingredient get", kind=SpanKind.CLIENT,
                        attributes={"db.system.name": "firestore", "demo.simulated": True, "demo.read.mode": "sequential"}):
                    ingredients.append(repo.ingredient(name))
                    time.sleep(0.25)  # Exactly six bounded reads, +1.5 s simulated network latency.
                    otel.reads.add(1, {"mode": "sequential"})
        else:
            with otel.tracer.start_as_current_span("Firestore ingredient batch get", kind=SpanKind.CLIENT,
                    attributes={"db.system.name": "firestore", "demo.read.mode": "batch"}):
                ingredients = repo.ingredients(recipe["ingredients"])
                otel.reads.add(1, {"mode": "batch"})
        return {**recipe, "ingredients_detail": ingredients, "reviews": review_summary(),
                "enrichment": repo.get("enrich-" + recipe_id, {"status": "not-started"}),
                "read_mode": "sequential" if bad else "batch", "duration_ms": round((time.perf_counter() - started) * 1000),
                "scenario": state, **ids()}

    @app.get("/api/recipes/{recipe_id}/image")
    def image(recipe_id: str):
        recipe = get_recipe(recipe_id)
        with otel.tracer.start_as_current_span("Cloud Storage fixture image", kind=SpanKind.CLIENT,
                attributes={"rpc.system": "gcp.storage", "cloud.resource_id": settings.bucket or "local-fixtures"}):
            return Response(repo.image(recipe), media_type="image/svg+xml")

    def consume_job(payload, attributes, attempt=1):
        incoming = propagate.extract(attributes)
        with worker_otel.tracer.start_as_current_span("PubSub recipe enrichment", context=incoming, kind=SpanKind.CONSUMER,
                attributes={"messaging.system": "gcp_pubsub", "messaging.operation.type": "process",
                    "messaging.destination.name": settings.topic or "local", "messaging.message.delivery_count": attempt,
                    "demo.release": attributes.get("demo_release", "")}) as span:
            state = scenarios.status()  # Reevaluate expiry for every delivery.
            recipe = get_recipe(payload["recipe_id"])
            with worker_otel.tracer.start_as_current_span("Firestore enrichment write", kind=SpanKind.CLIENT):
                result = {"status": "ready", "summary": f'{len(recipe["ingredients"])} ingredients · vegetarian fixture',
                          "worker_trace_id": ids()["trace_id"], "attempt": attempt, "phase": state["phase"]}
                repo.set("enrich-" + recipe["id"], result)
            worker_otel.jobs.add(1, {"result": "ready"})
            worker_otel.event("Enrichment ready", **{"messaging.system": "gcp_pubsub", "recipe.id": recipe["id"]})
        if not worker_otel.flush():
            raise RuntimeError("Worker telemetry export failed; retry delivery")
        return result

    @app.post("/api/recipes/{recipe_id}/enrich")
    def enrich(recipe_id: str, tasks: BackgroundTasks):
        get_recipe(recipe_id)
        state = scenarios.status()
        repo.set("enrich-" + recipe_id, {"status": "pending"})
        with otel.tracer.start_as_current_span("PubSub enrichment publish", kind=SpanKind.PRODUCER,
                attributes={"messaging.system": "gcp_pubsub", "messaging.destination.name": settings.topic or "local"}):
            attributes = {"demo_release": state["release"], "expires_at": str(state["expires_at"] or 0)}
            propagate.inject(attributes)
            payload = {"recipe_id": recipe_id}
            if settings.mode == "cloud":
                from google.cloud import pubsub_v1
                publisher = pubsub_v1.PublisherClient()
                publisher.publish(publisher.topic_path(settings.project, settings.topic), json.dumps(payload).encode(), **attributes).result(timeout=4)
                publisher.stop()
            else:
                tasks.add_task(consume_job, payload, attributes)
        return {"status": "pending"}

    @app.get("/api/recipes/{recipe_id}/enrichment")
    def enrichment(recipe_id: str):
        get_recipe(recipe_id)
        return repo.get("enrich-" + recipe_id, {"status": "not-started"})

    @app.post("/internal/enrich", dependencies=[Depends(push_auth)])
    async def push(request: Request):
        try:
            payload, attributes, attempt = decode_push(await request.json())
            if payload.get("recipe_id") not in ("demo-recipe", "control-recipe", "citrus-bowl"):
                raise ValueError("Only seeded jobs accepted")
            result = await asyncio.to_thread(consume_job, payload, attributes, attempt)
            return result
        except (ValueError, KeyError, TypeError) as error:
            otel.event("Poison enrichment rejected", **{"event.name": "demo.poison", "error.type": type(error).__name__})
            # ACK malformed permanent errors instead of an infinite retry.
            return {"status": "poison-rejected"}

    @app.post("/internal/provider/logs", dependencies=[Depends(push_auth)])
    async def logs(request: Request):
        payload, _, _ = decode_push(await request.json())
        key = hashlib.sha256((payload.get("logName", "") + "\0" + payload.get("insertId", "")).encode()).hexdigest() if payload.get("insertId") else ""
        token = ""
        if key:
            state, token = await asyncio.to_thread(claim_checkpoint, repo, "provider-log-checkpoint", key)
            if state == "duplicate":
                return {"accepted": True, "duplicate": True}
            if state == "busy":
                raise HTTPException(503, "Native log delivery in flight; retry delivery")
        success = False
        try:
            forward_log(payload, adapter_otel)
            if not await asyncio.to_thread(adapter_otel.flush):
                raise HTTPException(503, "Native telemetry export failed; retry delivery")
            success = True
        finally:
            if key:
                await asyncio.to_thread(finish_checkpoint, repo, "provider-log-checkpoint", key, token, success)
        return {"accepted": True}

    @app.post("/internal/seed", dependencies=[Depends(push_auth)])
    def seed():
        if settings.mode == "cloud":
            repo.seed()
        return {"seeded": True}

    @app.post("/internal/cleanup", dependencies=[Depends(push_auth)])
    def cleanup():
        if settings.mode == "cloud":
            repo.cleanup()
        return {"cleaned": True}

    @app.post("/internal/tick", dependencies=[Depends(push_auth)])
    def tick():
        state = scenarios.status()
        # Handler-derived UTC windows work even if this dispatcher is delayed.
        heartbeat = repo.get("heartbeat")
        otel.event("Daily scenario dispatcher", **{"event.name": "demo.dispatch", "demo.phase": state["phase"],
            "demo.browser_heartbeat.present": bool(heartbeat)})
        if settings.provider_forwarding:
            poll_metrics(settings, adapter_otel, repo)
        return {"scenario": state, "heartbeat": heartbeat,
                "browser_runner": "GitHub scheduled Playwright / external runner required"}

    dist = ROOT / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        if not (dist / "index.html").exists():
            raise HTTPException(503, "Build the frontend with npm ci && npm run build")
        tag = ""
        if settings.rum_key:
            src = settings.rum_script + ("&" if "?" in settings.rum_script else "?") + urlencode({"ckid": settings.rum_key})
            attributes = {"src": src, "data-endpoint": settings.endpoint, "data-service": "sample-recipes-web",
                "data-service-namespace": settings.namespace, "data-env": settings.environment,
                "data-version": settings.version, "data-trace-sample-rate": "1", "data-long-tasks": "true",
                "data-trace-origins": settings.public_url}
            tag = "<script " + " ".join(f'{k}="{html.escape(str(v), quote=True)}"' for k, v in attributes.items()) + "></script>"
        body = (dist / "index.html").read_text().replace("<!-- CORRIDORA_RUM -->", tag)
        rum_origin = urlparse(settings.rum_script)
        ingest_origin = urlparse(settings.endpoint)
        origins = [f"{p.scheme}://{p.netloc}" for p in (rum_origin, ingest_origin)]
        return HTMLResponse(body, headers={"Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' " + origins[0] + "; connect-src 'self' " + origins[1] + "; img-src 'self' data:; style-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"})

    return app
