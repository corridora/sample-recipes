"""Actual GCP deployment, with explicit reusable Firestore/bucket boundaries.

All mutations use gcloud argv, an owner inventory, and protected files/stdin.
No cloud credentials are fetched during a dry run.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from setup import STATE, ROOT, execute, save_config, source_version, validate_config, write_private

APIS = ["run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
        "firestore.googleapis.com", "storage.googleapis.com", "pubsub.googleapis.com",
        "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "logging.googleapis.com",
        "monitoring.googleapis.com", "iam.googleapis.com"]


def plan(config):
    p = config["RESOURCE_PREFIX"]
    return {"project": config["GCP_PROJECT"], "region": config["GCP_REGION"],
        "cost": "$0–2/mo shared GCP allowance; billing alerts are not caps; Cloud Build also metered",
        "limits": "request billing, min=0/max=1, 512Mi/1CPU, concurrency=8, 30s request timeout, tiny fixtures",
        "resources": {"run": p, "artifact_registry": p, "secret": p + "-config",
            "api_identity": config["GCP_SERVICE_ACCOUNT"], "push_identity": config["PUSH_IDENTITY"],
            "bucket": config["GCS_BUCKET"], "firestore": config["FIRESTORE_DATABASE"],
            "firestore_collection": p, "pubsub": config["PUBSUB_TOPIC"], "dlq": p + "-dead-letter",
            "scheduler": p + "-dispatcher", "optional_logging_sink": p + "-platform-logs"},
        "shared_retained": "GCP project/billing/APIs/Firestore database; selected existing bucket",
        "browser_schedule": "GitHub Actions every 15 min after UTC window until verified heartbeat; externally provision repository secrets"}


class GCP:
    def __init__(self, config):
        self.config = config
        self.project, self.region = config["GCP_PROJECT"], config["GCP_REGION"]
        self.path = STATE / "resources.json"
        self.inventory = json.loads(self.path.read_text()) if self.path.exists() else {
            "project": self.project, "prefix": config["RESOURCE_PREFIX"], "owner": secrets.token_hex(8), "resources": []}
        if (self.inventory["project"], self.inventory["prefix"]) != (self.project, config["RESOURCE_PREFIX"]):
            raise ValueError("Inventory belongs to another project/prefix; use a separate checkout")
        identity_keys = ["GCP_REGION", "GCP_SERVICE_ACCOUNT", "PUSH_IDENTITY", "PUBSUB_TOPIC", "GCS_BUCKET", "FIRESTORE_DATABASE"]
        desired = {key: config[key] for key in identity_keys}
        prior = self.inventory.get("deployment_contract")
        if prior and prior != desired:
            changed = [key for key in identity_keys if prior.get(key) != desired[key]]
            raise ValueError("Immutable deployed resource settings changed: " + ", ".join(changed) +
                ". Restore saved settings before teardown, or use a fresh checkout/prefix; no cloud changes applied.")
        if not prior and self.inventory["resources"]:
            raise ValueError("Legacy inventory lacks immutable deployment settings; reconcile it explicitly before applying changes")
        self.inventory["deployment_contract"] = desired

    def command(self, args, **kwargs):
        return execute(["gcloud", *args, "--project", self.project, "--quiet"], **kwargs)

    def present(self, describe):
        result = subprocess.run(["gcloud", *describe, "--project", self.project, "--quiet", "--format=json"], cwd=ROOT, text=True, capture_output=True)
        if result.returncode == 0:
            return True
        errors = result.stderr.lower()
        if any(marker in errors for marker in ("not_found", "not found", "does not exist", "no such object", "not exist")):
            return False
        raise RuntimeError("Resource discovery failed; refusing to treat permission/network errors as missing: " + result.stderr[-1000:])

    def ensure(self, kind, name, describe, create, delete, *, reuse=False, keep=False):
        record = next((r for r in self.inventory["resources"] if r["kind"] == kind and r["name"] == name), None)
        exists = self.present(describe)
        if exists:
            if record and record.get("state") == "creating":
                # A preceding create can finish while the local process is interrupted.
                # Names were checked absent and written before creation; retain exact ownership.
                if record["owned"]:
                    self.verify_owner(record)
                record["state"] = "ready"
                write_private(self.path, self.inventory)
            elif record and record["owned"]:
                self.verify_owner(record)
            elif not record:
                if not reuse:
                    raise ValueError(f"Existing {kind} {name} has no ownership inventory. Choose a fresh prefix; only explicit bucket/Firestore reuse is allowed.")
                record = {"kind": kind, "name": name, "owned": False, "state": "ready", "delete": []}
                self.inventory["resources"].append(record)
                write_private(self.path, self.inventory)
            return
        if record and not record["owned"] and not record.get("created_here"):
            raise ValueError(f"Previously reused {kind} vanished; select a new resource explicitly")
        if not record:
            record = {"kind": kind, "name": name, "owned": not keep, "created_here": True, "state": "creating", "delete": delete}
            self.inventory["resources"].append(record)
        record["state"] = "creating"
        write_private(self.path, self.inventory)
        self.command(create)
        record["state"] = "ready"
        write_private(self.path, self.inventory)

    def deploy(self):
        c, p = self.config, self.config["RESOURCE_PREFIX"]
        label = "sample-owner=" + self.inventory["owner"]
        region = ["--region", self.region]
        self.command(["services", "enable", *APIS])
        if not self.present(["firestore", "databases", "describe", "--database", c["FIRESTORE_DATABASE"]]) and c["FIRESTORE_CREATE_IF_MISSING"] != "true":
            raise ValueError("Firestore database missing; explicitly enable FIRESTORE_CREATE_IF_MISSING to create it")
        self.ensure("firestore", c["FIRESTORE_DATABASE"], ["firestore", "databases", "describe", "--database", c["FIRESTORE_DATABASE"]],
            ["firestore", "databases", "create", "--database", c["FIRESTORE_DATABASE"], "--location", self.region, "--type", "firestore-native"], [], reuse=True, keep=True)
        # Creating the shared database must be an explicit selection.
        self.ensure("bucket", c["GCS_BUCKET"], ["storage", "buckets", "describe", "gs://" + c["GCS_BUCKET"]],
            ["storage", "buckets", "create", "gs://" + c["GCS_BUCKET"], "--location", self.region, "--uniform-bucket-level-access", "--public-access-prevention", "--soft-delete-duration=0"],
            ["storage", "rm", "--recursive", "gs://" + c["GCS_BUCKET"]], reuse=c["REUSE_BUCKET"] == "true")
        bucket_record = next(r for r in self.inventory["resources"] if r["kind"] == "bucket")
        if bucket_record["owned"]:
            self.command(["storage", "buckets", "update", "gs://" + c["GCS_BUCKET"], "--update-labels", label])
        for identity in (c["GCP_SERVICE_ACCOUNT"], c["PUSH_IDENTITY"]):
            account = identity.split("@", 1)[0]
            if identity != account + "@" + self.project + ".iam.gserviceaccount.com":
                raise ValueError("Dedicated service identities must belong to the selected project")
            self.ensure("service-account", identity, ["iam", "service-accounts", "describe", identity],
                ["iam", "service-accounts", "create", account, "--display-name", p, "--description", label],
                ["iam", "service-accounts", "delete", identity])
        for topic in (c["PUBSUB_TOPIC"], p + "-dead-letter", p + "-platform-logs"):
            if topic.endswith("-platform-logs") and c["ENABLE_PROVIDER_FORWARDING"] != "true":
                continue
            self.ensure("topic", topic, ["pubsub", "topics", "describe", topic],
                ["pubsub", "topics", "create", topic, "--labels", label, "--message-retention-duration", "86400s"],
                ["pubsub", "topics", "delete", topic])
        secret = p + "-config"
        self.ensure("secret", secret, ["secrets", "describe", secret],
            ["secrets", "create", secret, "--replication-policy", "automatic", "--labels", label], ["secrets", "delete", secret])
        self.command(["secrets", "versions", "add", secret, "--data-file=-"], input_data=json.dumps({k: c[k] for k in ("CORRIDORA_INGEST_KEY", "CORRIDORA_RUM_KEY", "DEMO_CONTROL_TOKEN", "DOWNSTREAM_AUTH_TOKEN")}))
        versions = json.loads(self.command(["secrets", "versions", "list", secret, "--filter=state:ENABLED", "--sort-by=~createTime", "--format=json"], capture=True))
        for version in versions[1:]:
            self.command(["secrets", "versions", "destroy", version["name"].rsplit("/", 1)[-1], "--secret", secret])
        self.ensure("registry", p, ["artifacts", "repositories", "describe", p, "--location", self.region],
            ["artifacts", "repositories", "create", p, "--location", self.region, "--repository-format=docker", "--labels", label],
            ["artifacts", "repositories", "delete", p, "--location", self.region])
        with tempfile.TemporaryDirectory(dir=STATE) as tmp:
            policy = Path(tmp) / "cleanup.json"
            write_private(policy, [{"name": "discard-old", "action": {"type": "Delete"}, "condition": {"olderThan": "604800s"}},
                                   {"name": "keep-two", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}}])
            self.command(["artifacts", "repositories", "set-cleanup-policies", p, "--location", self.region, "--policy", str(policy), "--no-dry-run"])
        app_member = "serviceAccount:" + c["GCP_SERVICE_ACCOUNT"]
        for role in (["roles/datastore.user"] + (["roles/monitoring.viewer"] if c["ENABLE_PROVIDER_FORWARDING"] == "true" else [])):
            self.command(["projects", "add-iam-policy-binding", self.project, "--member", app_member, "--role", role, "--condition=None"], capture=True)
        self.command(["storage", "buckets", "add-iam-policy-binding", "gs://" + c["GCS_BUCKET"], "--member", app_member, "--role=roles/storage.objectUser"], capture=True)
        self.command(["pubsub", "topics", "add-iam-policy-binding", c["PUBSUB_TOPIC"], "--member", app_member, "--role=roles/pubsub.publisher"], capture=True)
        self.command(["secrets", "add-iam-policy-binding", secret, "--member", app_member, "--role=roles/secretmanager.secretAccessor"], capture=True)
        c["SERVICE_VERSION"] = source_version()
        image = f'{self.region}-docker.pkg.dev/{self.project}/{p}/recipes:{c["SERVICE_VERSION"][:40]}'
        self.command(["builds", "submit", ".", "--tag", image])
        # One inexpensive service hosts UI/API, push consumer and native adapters.
        # Public access for UI; application routes verify Google OIDC push identity.
        env = {k: str(v) for k, v in c.items() if k not in ("CORRIDORA_INGEST_KEY", "CORRIDORA_RUM_KEY", "DEMO_CONTROL_TOKEN", "DOWNSTREAM_AUTH_TOKEN", "PORT")}
        env["CORRIDORA_CONFIG_FILE"] = "/var/secrets/sample/config"
        with tempfile.TemporaryDirectory(dir=STATE) as tmp:
            file = Path(tmp) / "env.json"
            write_private(file, env)
            record = next((r for r in self.inventory["resources"] if r["kind"] == "run"), None)
            if not record and self.present(["run", "services", "describe", p, *region]):
                raise ValueError("Existing Cloud Run service has no ownership inventory")
            if record and self.present(["run", "services", "describe", p, *region]):
                self.verify_owner(record)
            if not record:
                self.inventory["resources"].append({"kind": "run", "name": p, "owned": True, "state": "creating", "delete": ["run", "services", "delete", p, *region]})
                write_private(self.path, self.inventory)
            self.command(["run", "deploy", p, "--image", image, *region, "--service-account", c["GCP_SERVICE_ACCOUNT"],
                "--allow-unauthenticated", "--min-instances=0", "--max-instances=1", "--memory=512Mi", "--cpu=1",
                "--cpu-throttling", "--concurrency=8", "--timeout=30s", "--labels", label,
                "--env-vars-file", str(file), "--update-secrets", f"/var/secrets/sample/config={secret}:latest"])
        url = self.command(["run", "services", "describe", p, *region, "--format=value(status.url)"], capture=True).strip()
        c["PUBLIC_URL"] = url
        # The discovered origin controls OIDC audience and the browser propagation allowlist.
        self.command(["run", "services", "update", p, *region, "--update-env-vars", "PUBLIC_URL=" + url])
        save_config(c)
        self.inventory["url"] = url
        for record in self.inventory["resources"]:
            if record["kind"] == "run":
                record["state"] = "ready"
        write_private(self.path, self.inventory)
        self.command(["run", "services", "add-iam-policy-binding", p, *region, "--member", "serviceAccount:" + c["PUSH_IDENTITY"], "--role=roles/run.invoker"], capture=True)
        number = self.command(["projects", "describe", self.project, "--format=value(projectNumber)"], capture=True).strip()
        service_agent = "serviceAccount:service-" + number + "@gcp-sa-pubsub.iam.gserviceaccount.com"
        self.command(["iam", "service-accounts", "add-iam-policy-binding", c["PUSH_IDENTITY"], "--member", service_agent, "--role=roles/iam.serviceAccountTokenCreator"], capture=True)
        self.command(["pubsub", "topics", "add-iam-policy-binding", p + "-dead-letter", "--member", service_agent, "--role=roles/pubsub.publisher"], capture=True)
        sub = p + "-enrichment-push"
        self.ensure("subscription", sub, ["pubsub", "subscriptions", "describe", sub],
            ["pubsub", "subscriptions", "create", sub, "--topic", c["PUBSUB_TOPIC"], "--push-endpoint", url + "/internal/enrich",
                "--push-auth-service-account", c["PUSH_IDENTITY"], "--push-auth-token-audience", url,
                "--ack-deadline=30", "--min-retry-delay=10s", "--max-retry-delay=20s", "--message-retention-duration=600s",
                "--dead-letter-topic", p + "-dead-letter", "--max-delivery-attempts=5", "--labels", label],
            ["pubsub", "subscriptions", "delete", sub])
        self.command(["pubsub", "subscriptions", "add-iam-policy-binding", sub, "--member", service_agent, "--role=roles/pubsub.subscriber"], capture=True)
        self.ensure("subscription", p + "-dead-letter-retain", ["pubsub", "subscriptions", "describe", p + "-dead-letter-retain"],
            ["pubsub", "subscriptions", "create", p + "-dead-letter-retain", "--topic", p + "-dead-letter", "--message-retention-duration=86400s", "--labels", label],
            ["pubsub", "subscriptions", "delete", p + "-dead-letter-retain"])
        job = p + "-dispatcher"
        create_job = ["scheduler", "jobs", "create", "http", job, "--location", self.region, "--schedule=*/15 * * * *", "--time-zone=UTC", "--uri", url + "/internal/tick", "--http-method=POST", "--oidc-service-account-email", c["PUSH_IDENTITY"], "--oidc-token-audience", url, "--attempt-deadline=30s", "--max-retry-attempts=1", "--description", "sample-owner=" + self.inventory["owner"]]
        self.ensure("scheduler", job, ["scheduler", "jobs", "describe", job, "--location", self.region], create_job, ["scheduler", "jobs", "delete", job, "--location", self.region])
        if c["ENABLE_PROVIDER_FORWARDING"] == "true":
            self.provider_logging(url, service_agent)
        # Seed as push identity using deployer's serviceAccountTokenCreator permission.
        token = self.command(["auth", "print-identity-token", "--impersonate-service-account", c["PUSH_IDENTITY"], "--audiences", url, "--include-email"], capture=True).strip()
        import urllib.request
        request = urllib.request.Request(url + "/internal/seed", data=b"{}", headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            print("Seeded:", response.status)
        print("Deployed:", url)
        print("Run ./setup verify; configure repository APP_URL and DEMO_CONTROL_TOKEN to activate the daily browser watchdog.")

    def provider_logging(self, url, service_agent):
        c, p = self.config, self.config["RESOURCE_PREFIX"]
        sink = p + "-platform-logs"
        # Exclude adapter/worker/dispatcher requests, avoiding a log-forwarding loop.
        log_filter = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{p}" AND logName="projects/{self.project}/logs/run.googleapis.com%2Frequests" AND NOT httpRequest.requestUrl:"/internal/" AND sample(insertId,0.1)'
        self.ensure("sink", sink, ["logging", "sinks", "describe", sink],
            ["logging", "sinks", "create", sink, f"pubsub.googleapis.com/projects/{self.project}/topics/{sink}", "--log-filter", log_filter, "--description", "Bounded native request logs; " + self.inventory["owner"]],
            ["logging", "sinks", "delete", sink])
        writer = self.command(["logging", "sinks", "describe", sink, "--format=value(writerIdentity)"], capture=True).strip()
        self.command(["pubsub", "topics", "add-iam-policy-binding", sink, "--member", writer, "--role=roles/pubsub.publisher"], capture=True)
        self.ensure("subscription", sink + "-push", ["pubsub", "subscriptions", "describe", sink + "-push"],
            ["pubsub", "subscriptions", "create", sink + "-push", "--topic", sink, "--push-endpoint", url + "/internal/provider/logs",
                "--push-auth-service-account", c["PUSH_IDENTITY"], "--push-auth-token-audience", url,
                "--message-retention-duration=600s", "--ack-deadline=30", "--min-retry-delay=10s", "--max-retry-delay=20s",
                "--dead-letter-topic", p + "-dead-letter", "--max-delivery-attempts=5", "--labels", "sample-owner=" + self.inventory["owner"]],
            ["pubsub", "subscriptions", "delete", sink + "-push"])
        self.command(["pubsub", "subscriptions", "add-iam-policy-binding", sink + "-push", "--member", service_agent, "--role=roles/pubsub.subscriber"], capture=True)

    def destroy(self):
        # Never delete an entire Firestore database, project, shared APIs or reused bucket.
        c, p = self.config, self.config["RESOURCE_PREFIX"]
        for record in self.inventory["resources"]:
            if record["owned"] and record["state"] == "ready":
                self.verify_owner(record)
        if any(r["kind"] == "run" and r["state"] == "ready" for r in self.inventory["resources"]):
            token = self.command(["auth", "print-identity-token", "--impersonate-service-account", c["PUSH_IDENTITY"], "--audiences", c["PUBLIC_URL"], "--include-email"], capture=True).strip()
            import urllib.request
            request = urllib.request.Request(c["PUBLIC_URL"] + "/internal/cleanup", data=b"{}", headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                print("Removed owned seed collection/fixture prefix:", response.status)
        for record in reversed(self.inventory["resources"]):
            if not record["owned"] or record["state"] == "deleted":
                continue
            if record["state"] != "ready":
                raise ValueError("Resource creation was interrupted; rerun deploy to reconcile before destroy")
            self.verify_owner(record)
            self.command(record["delete"])
            record["state"] = "deleted"
            write_private(self.path, self.inventory)
        # Dedicated SA bindings no longer grant access after account deletion.
        print("Removed inventoried resources; retained shared Firestore database/project and reused bucket.")
        print("Owned Firestore collection and fixture objects removed; the database itself is retained.")

    def verify_owner(self, record):
        c, name, kind = self.config, record["name"], record["kind"]
        lookup = {
            "run": ["run", "services", "describe", name, "--region", self.region],
            "service-account": ["iam", "service-accounts", "describe", name],
            "topic": ["pubsub", "topics", "describe", name],
            "subscription": ["pubsub", "subscriptions", "describe", name],
            "secret": ["secrets", "describe", name],
            "registry": ["artifacts", "repositories", "describe", name, "--location", self.region],
            "bucket": ["storage", "buckets", "describe", "gs://" + name],
            "scheduler": ["scheduler", "jobs", "describe", name, "--location", self.region],
            "sink": ["logging", "sinks", "describe", name],
        }
        remote = json.loads(self.command([*lookup[kind], "--format=json"], capture=True))
        labels = remote.get("labels", remote.get("metadata", {}).get("labels", {}))
        description = remote.get("description", "")
        owner = self.inventory["owner"]
        if labels.get("sample-owner") != owner and ("sample-owner=" + owner) not in description and (kind != "sink" or owner not in description):
            raise ValueError(f"Ownership marker changed for {kind} {name}; refusing deletion")


def deployment(config, args, destroy=False):
    print(json.dumps(plan(config), indent=2))
    if args.dry_run:
        print("Dry run: no cloud login, API calls or resource changes.")
        return
    validate_config(config)
    active = execute(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], capture=True).strip()
    if not active:
        raise ValueError("Sign in with gcloud auth login; setup never asks for account passwords")
    print("Using active gcloud account:", active)
    if not args.yes:
        if args.non_interactive or input("Apply this plan to the selected billed project? Type deploy or destroy: ") != ("destroy" if destroy else "deploy"):
            raise ValueError("No changes applied. Use --yes to apply this displayed plan in CI.")
    gcp = GCP(config)
    if destroy:
        if not gcp.path.exists():
            raise ValueError("No inventory; refusing to infer ownership from resource names")
        gcp.destroy()
    else:
        gcp.deploy()
