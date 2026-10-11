#!/usr/bin/env python3
"""Interactive and CI-friendly orchestration; never logs secret-bearing argv."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".sample"
SECRET_KEYS = {"CORRIDORA_INGEST_KEY", "CORRIDORA_RUM_KEY", "DEMO_CONTROL_TOKEN", "DOWNSTREAM_AUTH_TOKEN"}
DEFAULTS = {
    "SAMPLE_MODE": "local", "PORT": "4103", "CORRIDORA_TARGET": "own",
    "CORRIDORA_OTLP_ENDPOINT": "https://ingest.corridora.com", "CORRIDORA_INGEST_KEY": "", "CORRIDORA_RUM_KEY": "",
    "CORRIDORA_RUM_SCRIPT_URL": "https://rum.corridora.com/corridora-rum.js",
    "OTEL_SERVICE_NAME": "sample-recipes-api", "OTEL_SERVICE_NAMESPACE": "corridora-samples.recipes",
    "DEPLOYMENT_ENVIRONMENT": "development", "RESOURCE_PREFIX": "sample-recipes",
    "DEMO_DAILY_START_UTC": "13:40", "DEMO_WINDOW_SECONDS": "600", "DEMO_CONTROL_TOKEN": "",
    "ENABLE_PROVIDER_FORWARDING": "false", "ENABLE_SYNTHETICS": "false",
    "REVIEW_SUMMARY_URL": "", "DOWNSTREAM_AUTH_TOKEN": "", "TRACKER_SIMULATOR_URL": "",
    "GCP_PROJECT": "", "GCP_REGION": "us-central1", "FIRESTORE_DATABASE": "(default)",
    "GCS_BUCKET": "", "PUBSUB_TOPIC": "", "REUSE_BUCKET": "false", "FIRESTORE_CREATE_IF_MISSING": "false",
    "GCP_SERVICE_ACCOUNT": "", "PUSH_IDENTITY": "", "PUBLIC_URL": "http://localhost:4103",
}


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    # Atomic replace, never follow a user-controlled destination symlink.
    fd, temporary = tempfile.mkstemp(prefix="write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_version():
    result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=ROOT, text=True, capture_output=True)
    commit = result.stdout.strip() if result.returncode == 0 else None
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, text=True, capture_output=True)
    if commit and not dirty.stdout.strip():
        return commit
    digest = hashlib.sha256()
    for directory in (ROOT / "app", ROOT / "frontend" / "src"):
        for path in sorted(directory.rglob("*")):
            if path.is_file() and "__pycache__" not in str(path):
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
    return ((commit[:12] + "-dirty-") if commit else "source-") + digest.hexdigest()[:16]


def load_config(path=None):
    config = dict(DEFAULTS)
    files = [STATE / "config.json", STATE / "secrets.json"]
    if path:
        files.append(Path(path))
    for file in files:
        if file.exists():
            config.update(json.loads(file.read_text()))
    config.update({key: value for key, value in os.environ.items() if key in DEFAULTS or key == "SERVICE_VERSION"})
    return config


def validate_config(config):
    if config["SAMPLE_MODE"] not in ("local", "cloud") or config["CORRIDORA_TARGET"] not in ("own", "demo"):
        raise ValueError("Choose local/cloud mode and own/demo target")
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,22}", config["RESOURCE_PREFIX"]):
        raise ValueError("RESOURCE_PREFIX must be 3–23 lowercase letters/digits/hyphens")
    for key in ("ENABLE_PROVIDER_FORWARDING", "ENABLE_SYNTHETICS", "REUSE_BUCKET", "FIRESTORE_CREATE_IF_MISSING"):
        if config[key] not in ("true", "false"):
            raise ValueError(f"{key} must be true or false")
    hour, minute = map(int, config["DEMO_DAILY_START_UTC"].split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60 and 1 <= int(config["DEMO_WINDOW_SECONDS"]) <= 600 and 1024 <= int(config["PORT"]) <= 65535):
        raise ValueError("Invalid port or daily UTC window")
    from urllib.parse import urlparse
    for key in ("CORRIDORA_OTLP_ENDPOINT", "CORRIDORA_RUM_SCRIPT_URL", "PUBLIC_URL", "REVIEW_SUMMARY_URL"):
        if not config[key]:
            continue
        url = urlparse(config[key])
        if url.username or url.password or url.fragment or not url.hostname:
            raise ValueError(f"Invalid {key}")
        if url.scheme != "https" and not (config["SAMPLE_MODE"] == "local" and url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")):
            raise ValueError(f"{key} must use HTTPS (local localhost HTTP allowed)")
    required = ["DEMO_CONTROL_TOKEN"]
    if config["SAMPLE_MODE"] == "cloud" or config["CORRIDORA_TARGET"] == "demo":
        required.extend(["CORRIDORA_INGEST_KEY", "CORRIDORA_RUM_KEY"])
    if config["SAMPLE_MODE"] == "cloud":
        required.extend(["GCP_PROJECT", "GCS_BUCKET", "PUBSUB_TOPIC", "GCP_SERVICE_ACCOUNT", "PUSH_IDENTITY"])
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError("Missing required values: " + ", ".join(missing))
    if config["CORRIDORA_TARGET"] == "demo" and config["DEPLOYMENT_ENVIRONMENT"] != "demo":
        raise ValueError("Demo must use environment demo and operator-provisioned keys")


def configure(args):
    config = load_config(args.config)
    interactive = not args.non_interactive and sys.stdin.isatty()
    prompts = [
        ("SAMPLE_MODE", "Run locally or deploy to GCP (local/cloud)"),
        ("CORRIDORA_TARGET", "Corridora destination (own/demo; Demo requires operator keys)"),
        ("CORRIDORA_OTLP_ENDPOINT", "Corridora OTLP ingest base URL"),
        ("CORRIDORA_RUM_SCRIPT_URL", "Corridora RUM script URL"),
        ("CORRIDORA_RUM_KEY", "Browser RUM key (blank disables RUM in local mode)"),
        ("CORRIDORA_INGEST_KEY", "Backend ingest key (blank disables export in local mode)"),
        ("OTEL_SERVICE_NAME", "API service name"), ("OTEL_SERVICE_NAMESPACE", "Service namespace"),
        ("DEPLOYMENT_ENVIRONMENT", "Environment"), ("RESOURCE_PREFIX", "Resource prefix"),
        ("PORT", "Local port"), ("DEMO_DAILY_START_UTC", "Daily regression start (HH:MM UTC)"),
        ("DEMO_WINDOW_SECONDS", "Daily window seconds (1..600)"),
        ("ENABLE_PROVIDER_FORWARDING", "Enable bounded provider logs/metrics forwarding (true/false)"),
        ("ENABLE_SYNTHETICS", "Configure scheduled browser runner separately (true/false)"),
        ("REVIEW_SUMMARY_URL", "AWS reviews base URL (blank uses fixture)"),
        ("DOWNSTREAM_AUTH_TOKEN", "Dedicated downstream bearer token (optional)"),
    ]
    for key, label in prompts:
        if key == "DEPLOYMENT_ENVIRONMENT" and config["CORRIDORA_TARGET"] == "demo":
            config[key] = "demo"
        if interactive:
            old = config[key]
            suffix = " [saved]" if key in SECRET_KEYS and old else (f" [{old}]" if old else "")
            answer = getpass.getpass(label + suffix + ": ") if key in SECRET_KEYS else input(label + suffix + ": ")
            config[key] = answer or old
    config["DEMO_CONTROL_TOKEN"] = config["DEMO_CONTROL_TOKEN"] or secrets.token_urlsafe(32)
    if config["SAMPLE_MODE"] == "cloud":
        for key, label in [("GCP_PROJECT", "Existing billing-enabled GCP project"), ("GCP_REGION", "GCP region"),
                           ("FIRESTORE_DATABASE", "Firestore database (shared default retained on teardown)"),
                           ("FIRESTORE_CREATE_IF_MISSING", "Create missing Firestore Native database (true/false)"),
                           ("GCS_BUCKET", "Storage bucket (globally unique)"),
                           ("REUSE_BUCKET", "Reuse existing selected bucket (true/false; retained on teardown)")]:
            if key == "GCS_BUCKET" and not config[key]:
                config[key] = config["GCP_PROJECT"] + "-" + config["RESOURCE_PREFIX"]
            if interactive:
                config[key] = input(f"{label} [{config[key]}]: ") or config[key]
        project, prefix = config["GCP_PROJECT"], config["RESOURCE_PREFIX"]
        config["PUBSUB_TOPIC"] = config["PUBSUB_TOPIC"] or prefix + "-enrichment"
        config["GCP_SERVICE_ACCOUNT"] = config["GCP_SERVICE_ACCOUNT"] or f"{prefix}-app@{project}.iam.gserviceaccount.com"
        config["PUSH_IDENTITY"] = config["PUSH_IDENTITY"] or f"{prefix}-push@{project}.iam.gserviceaccount.com"
        # The first service URL is discovered on deploy; it is never typed as a key.
        if config["PUBLIC_URL"].startswith("http://"):
            config["PUBLIC_URL"] = "https://pending.invalid"
        if interactive:
            for key, label in [("PUBSUB_TOPIC", "Dedicated enrichment topic"),
                               ("GCP_SERVICE_ACCOUNT", "New dedicated API service identity"),
                               ("PUSH_IDENTITY", "New dedicated Pub/Sub + Scheduler identity")]:
                config[key] = input(f"{label} [{config[key]}]: ") or config[key]
    else:
        config["PUBLIC_URL"] = f'http://localhost:{config["PORT"]}'
    config["SERVICE_VERSION"] = source_version()
    validate_config(config)
    save_config(config)
    print("Saved protected .sample/config.json and .sample/secrets.json. No keys were printed.")
    return config


def save_config(config):
    write_private(STATE / "config.json", {k: v for k, v in config.items() if k not in SECRET_KEYS})
    write_private(STATE / "secrets.json", {k: v for k, v in config.items() if k in SECRET_KEYS})


def execute(argv, *, input_data=None, capture=False, env=None):
    result = subprocess.run(argv, cwd=ROOT, env=env, input=input_data, text=True, capture_output=True)
    if result.returncode:
        # Provider errors can echo secret version data/HTTP config: redact known values.
        detail = result.stderr[-3000:]
        for key, value in load_config().items():
            if key in SECRET_KEYS and value:
                detail = detail.replace(value, "[REDACTED]")
        raise RuntimeError(f"Command failed ({Path(argv[0]).name}): {detail}")
    if not capture and result.stdout:
        print(result.stdout[-3000:])
    return result.stdout


def bootstrap():
    if not (ROOT / ".venv" / "bin" / "python").exists():
        execute([sys.executable, "-m", "venv", ".venv"])
    execute([str(ROOT / ".venv/bin/python"), "-m", "pip", "install", "-r", "requirements.lock"])
    execute(["npm", "ci", "--no-audit", "--no-fund"])
    execute(["npm", "run", "build"])


def app_env(config):
    return {**os.environ, **{k: str(v) for k, v in config.items()}, "STATE_PATH": str(STATE / "recipes.db")}


def request_api(config, path, payload=None):
    request = urllib.request.Request(config["PUBLIC_URL"].rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + config["DEMO_CONTROL_TOKEN"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def verify(config):
    validate_config(config)
    health = request_api(config, "/healthz")
    print("App health:", json.dumps(health))
    if not (ROOT / "node_modules/@playwright/test").exists():
        execute(["npm", "ci", "--no-audit", "--no-fund"])
    if not os.environ.get("CHROMIUM_PATH"):
        execute(["npx", "playwright", "install", "chromium"])
    execute(["npm", "run", "test:e2e"], env={**app_env(config), "BASE_URL": config["PUBLIC_URL"]})
    print("Browser journey passed: healthy, regression, control, queued enrichment, recovery. " + config["PUBLIC_URL"])
    print("Inspect emitted trace IDs in Corridora. Stored browser CLIENT/server SERVER correlation and provider signals require UI/query verification; HTTP 200 alone proves neither.")


def main():
    parser = argparse.ArgumentParser(description="Recipe sample: configure, deploy, run, verify, scenario, destroy. Bare invocation starts the wizard.")
    parser.add_argument("command", nargs="?", choices=["configure", "deploy", "run", "verify", "scenario", "destroy"])
    parser.add_argument("--config", help="Protected JSON input configuration")
    parser.add_argument("--non-interactive", action="store_true", help="Read env/config; never prompt")
    parser.add_argument("--dry-run", "--plan", dest="dry_run", action="store_true", help="Print deployment/teardown plan without login or mutations")
    parser.add_argument("--yes", action="store_true", help="Apply displayed cloud plan")
    parser.add_argument("--phase", choices=["healthy", "regression", "recover"], default="regression")
    parser.add_argument("--duration", type=int, default=120)
    args = parser.parse_args()
    if args.command == "configure" or args.command is None:
        config = configure(args)
    else:
        config = load_config(args.config)
    command = args.command
    if command == "configure":
        return
    if command is None:
        if config["SAMPLE_MODE"] == "local":
            bootstrap()
            log = open(STATE / "server.log", "a")
            process = subprocess.Popen([str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", config["PORT"]], cwd=ROOT, env=app_env(config), stdout=log, stderr=log, start_new_session=True)
            write_private(STATE / "process.json", {"pid": process.pid})
            import time
            for _ in range(60):
                try:
                    request_api(config, "/healthz")
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.5)
            command = "verify"
        else:
            command = "deploy"
    if command in ("deploy", "destroy") and config["SAMPLE_MODE"] == "cloud":
        sys.path.insert(0, str(ROOT / "scripts"))
        from gcp import deployment
        deployment(config, args, destroy=command == "destroy")
        if args.command is None and not args.dry_run:
            verify(load_config())
    elif command == "deploy":
        if args.dry_run:
            print("Local plan: isolated Python venv, npm ci, frontend build; ./setup run starts API on " + config["PUBLIC_URL"])
        else:
            validate_config(config)
            bootstrap()
    elif command == "run":
        validate_config(config)
        if config["SAMPLE_MODE"] == "cloud":
            print(json.dumps(request_api(config, "/healthz")))
            print(config["PUBLIC_URL"])
        else:
            python = ROOT / ".venv/bin/python"
            if not python.exists() or not (ROOT / "frontend/dist/index.html").exists():
                bootstrap()
            os.execve(str(python), [str(python), "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", config["PORT"]], app_env(config))
    elif command == "verify":
        verify(config)
    elif command == "scenario":
        validate_config(config)
        print(json.dumps(request_api(config, "/api/demo/scenario", {"phase": args.phase, "duration_seconds": args.duration}), indent=2))
    elif command == "destroy":
        if args.dry_run:
            print("Stop only wizard PID; retain saved configuration. No cloud resources in local mode.")
        elif (STATE / "process.json").exists():
            import signal
            pid = json.loads((STATE / "process.json").read_text())["pid"]
            identity = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
            if str(ROOT / ".venv/bin/python") in identity and "uvicorn" in identity and "app.main:create_app" in identity:
                os.kill(pid, signal.SIGTERM)
            (STATE / "process.json").unlink()


if __name__ == "__main__":
    os.chdir(ROOT)
    try:
        main()
    except (ValueError, RuntimeError, OSError, urllib.error.URLError) as error:
        print("Setup failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
