# GCP deployment details

Published 2026-10-11 · Updated 2026-10-11

The programmatic infrastructure definition is `scripts/gcp.py`. It uses gcloud
argv arrays, protected environment/cleanup-policy files and secret version input
over stdin. It does not interpolate shell commands or print credentials.

Use an existing billed project. The deployer must enable the listed APIs, create
the selected resources, manage their IAM policies, deploy Cloud Run/Cloud Build,
and impersonate the new dedicated push identity for seed/cleanup. Grant
`roles/iam.serviceAccountTokenCreator` to the deployer **on that identity only**
when your project does not already permit it. The script grants the managed
Pub/Sub agent that identity-scoped role for authenticated pushes. The Cloud Build
execution identity needs Artifact Registry write permission. These permissions
depend on project creation time and organization policy; the script does not
change billing, organization policies or shared build identities.

The application identity receives project `roles/datastore.user` (Firestore's
standard role is not collection scoped), and optional `roles/monitoring.viewer`.
Storage object access is restricted to the selected bucket, Pub/Sub publish to
the enrichment topic, and Secret Manager access to the dedicated bundle. The
selected reused bucket must be appropriate for that bucket-level access. Public
UI/API IAM and private OIDC-verified push/control routes share one Cloud Run
container to limit the recurring footprint.

Pub/Sub push uses audience equal to the discovered Cloud Run origin. Enrichment
messages carry W3C `traceparent`/`tracestate` and scenario metadata in attributes.
The consumer reevaluates scenario expiry at delivery time. The sample only accepts
three seeded IDs and acknowledges malformed permanent jobs with poison evidence;
transient provider failures are NACKed through failed HTTP delivery. Retry delays
are 10–20 seconds, ack deadline 30 seconds, retention 10 minutes and configured
dead-letter threshold five attempts (GCP's minimum). Pub/Sub redelivery/dead-letter
attempt counts are best effort rather than an exact retry counter. The retained
DLQ subscription keeps messages for one day; use scoped gcloud Pub/Sub commands
to inspect/replay after resolving the cause.
[Pub/Sub create flags](https://docs.cloud.google.com/sdk/gcloud/reference/pubsub/subscriptions/create).

Application OTLP export is three HTTP protobuf pipelines, not a Google-native
export destination. The optional native Logging topic/adapter and Monitoring
read path are additional pipelines. Logging routing and native Monitoring
availability can lag. The app's bounded flush avoids depending on idle CPU in
request-billed Cloud Run.
[Cloud Run CPU allocation](https://docs.cloud.google.com/run/docs/configuring/billing-settings),
[OTLP endpoint configuration](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/).

Keep the ownership inventory. Resource discovery distinguishes NotFound from
permission/network errors and refuses to adopt preexisting dedicated names.
Explicit reuse is supported for the selected Firestore database/bucket only.
Rerun deploy after partial failure; it retains completed resources and reconciles
an interrupted create. Resource identities (region, runtime/push identity, topic,
bucket and Firestore database) are frozen in the deployment inventory. Changing
them fails before cloud mutations; restore the saved settings before teardown,
or use a fresh checkout and resource prefix for a new deployment. Source,
application settings and secret rotation can still be redeployed. Before teardown,
remote labels/descriptions must match
the inventory owner. If an operator removes an ownership marker or replaces a
resource, deletion fails safely and needs operator reconciliation.

Artifact Registry cleanup runs asynchronously, keeps the latest two images and
deletes older matching images after seven days; cleanup may take about a day.
Secret rotation retains only the latest enabled version. Existing shared buckets
are never deleted. Dataset cleanup is scoped to the dedicated Firestore collection
and `<prefix>/fixtures/` storage path. Newly created fixture buckets disable soft
delete to avoid retaining deleted fictional objects; reused bucket retention
settings are left unchanged. No command deletes the project, shared
Firestore database, enabled APIs, or another sample's resources.
[Registry cleanup policies](https://docs.cloud.google.com/artifact-registry/docs/repositories/cleanup-policy).

The Dockerfile and resource plan are generated/locally validated here. A real cloud
deployment, exact project IAM, remote resource ownership metadata formats, native
push delivery, build execution identity and measured spend must be validated by
an authorized operator. The generator does not claim a deployed service or a
verified hosted daily incident.
