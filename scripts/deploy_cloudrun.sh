#!/usr/bin/env bash
# One-shot Cloud Run build + deploy for the FYF pipeline.
# Prereqs: gcloud auth login; billing enabled; APIs enabled (script does this).
set -euo pipefail

# PROJECT_ID is canonical; retain GOOGLE_CLOUD_PROJECT as a legacy fallback.
PROJECT_ID="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: PROJECT_ID is required (set PROJECT_ID or GOOGLE_CLOUD_PROJECT before deployment)." >&2
  exit 2
fi
if [[ ! "$PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  echo "ERROR: PROJECT_ID must be a valid Google Cloud project ID (6-30 lowercase letters, digits, or hyphens)." >&2
  exit 2
fi
REGION="${GOOGLE_CLOUD_REGION:-asia-southeast1}"
REPO="fyf"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/fyf-pipeline:latest"
SERVICE="fyf-pipeline"
FYF_OUTPUT_BUCKET="${FYF_OUTPUT_BUCKET:-${PROJECT_ID}-fyf-output-${REGION}}"

# ===========================================================================
# OWNER-APPROVED PAID-PRODUCTION BUDGET ENVELOPE  <-- single place to change.
# ---------------------------------------------------------------------------
# Stage B-I budget_store is FAIL-CLOSED: paid provider dispatch is enabled ONLY
# when BOTH a daily AND a total ceiling are present and valid. Deploying with
# just FYF_DAILY_BUDGET_CAP_USD would DISABLE all paid production (every paid
# request 429s with reason "paid_production_disabled"). Both caps below are the
# owner-approved hackathon envelope of $3. Raising either value is a DELIBERATE
# OWNER DECISION: edit ONLY this block (the deploy --set-env-vars below sources
# these two variables verbatim, so there is no second literal to drift).
# ===========================================================================
FYF_DAILY_BUDGET_CAP_USD="3"
FYF_TOTAL_BUDGET_CAP_USD="3"

gcloud config set project "$PROJECT_ID"

echo "== enable APIs =="
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com storage.googleapis.com

SA_NUM=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
EXISTING_RUNTIME_SERVICE_ACCOUNT=$(
  gcloud run services list \
    --project "$PROJECT_ID" \
    --region "$REGION" \
    --filter="metadata.name=$SERVICE" \
    --format='value(spec.template.spec.serviceAccountName)'
)
RUNTIME_SERVICE_ACCOUNT="${FYF_RUNTIME_SERVICE_ACCOUNT:-${EXISTING_RUNTIME_SERVICE_ACCOUNT:-${SA_NUM}-compute@developer.gserviceaccount.com}}"

echo "== artifact registry =="
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" 2>/dev/null || echo "repo exists"

echo "== durable completed-job storage =="
if ! gcloud storage buckets describe "gs://$FYF_OUTPUT_BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$FYF_OUTPUT_BUCKET" \
    --project "$PROJECT_ID" \
    --location "$REGION" \
    --uniform-bucket-level-access
fi

echo "== secrets from .env.clickhouse =="
if [[ -f .env.clickhouse ]]; then
  set -a; source .env.clickhouse; set +a
  for KEY in CLICKHOUSE_HOST CLICKHOUSE_PORT CLICKHOUSE_USER CLICKHOUSE_PASSWORD CLICKHOUSE_DATABASE CLICKHOUSE_SECURE; do
    printf '%s' "${!KEY}" | gcloud secrets create "$KEY" --data-file=- 2>/dev/null || \
      printf '%s' "${!KEY}" | gcloud secrets versions add "$KEY" --data-file=-
  done
else
  echo "WARNING: .env.clickhouse missing — deploying without ClickHouse envs"
fi

SECRETS_FLAGS=()
[[ -f .env.clickhouse ]] && SECRETS_FLAGS=(
  --set-secrets "CLICKHOUSE_HOST=CLICKHOUSE_HOST:latest,CLICKHOUSE_PORT=CLICKHOUSE_PORT:latest,CLICKHOUSE_USER=CLICKHOUSE_USER:latest,CLICKHOUSE_PASSWORD=CLICKHOUSE_PASSWORD:latest,CLICKHOUSE_DATABASE=CLICKHOUSE_DATABASE:latest,CLICKHOUSE_SECURE=CLICKHOUSE_SECURE:latest"
)

echo "== build =="
gcloud builds submit --tag "$IMAGE" .

echo "== allow runtime SA to read secrets =="
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$FYF_OUTPUT_BUCKET" \
  --member="serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role="roles/storage.objectUser" --quiet >/dev/null

echo "== deploy =="
gcloud run deploy "$SERVICE" \
  --project "$PROJECT_ID" \
  --image "$IMAGE" \
  --region "$REGION" \
  --port 8080 \
  --cpu 2 --memory 4Gi \
  --execution-environment gen2 --no-cpu-throttling \
  --min-instances 0 --max-instances 1 \
  --timeout 3600 \
  --allow-unauthenticated \
  --service-account "$RUNTIME_SERVICE_ACCOUNT" \
  --add-volume "name=fyf-jobs,type=cloud-storage,bucket=$FYF_OUTPUT_BUCKET" \
  --add-volume-mount "volume=fyf-jobs,mount-path=/app/output/jobs" \
  --set-env-vars "FYF_RUNTIME_MODE=hackathon,NEXT_PUBLIC_FYF_RUNTIME_MODE=hackathon,FYF_SEGMENT_RENDER_ENABLED=1,FYF_PUBLIC_DEPLOYMENT=true,FYF_BACKEND_URL=http://127.0.0.1:8000,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,FYF_PUBLIC_GENERATION_ENABLED=true,FYF_DAILY_BUDGET_CAP_USD=$FYF_DAILY_BUDGET_CAP_USD,FYF_TOTAL_BUDGET_CAP_USD=$FYF_TOTAL_BUDGET_CAP_USD,FYF_LOCK_METADATA_MODE=per_segment" \
  "${SECRETS_FLAGS[@]}"

echo "== DONE =="
gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)'
