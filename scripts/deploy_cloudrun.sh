#!/usr/bin/env bash
# One-shot Cloud Run build + deploy for the FYF pipeline.
# Prereqs: gcloud auth login; billing enabled; APIs enabled (script does this).
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:-intelligent-arc-488111-s0}"
REGION="${GOOGLE_CLOUD_REGION:-asia-southeast1}"
REPO="fyf"
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/fyf-pipeline:latest"
SERVICE="fyf-pipeline"

gcloud config set project "$PROJECT"

echo "== enable APIs =="
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com

echo "== artifact registry =="
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" 2>/dev/null || echo "repo exists"

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
SA_NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA_NUM}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --quiet >/dev/null || true

echo "== deploy =="
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" \
  --region "$REGION" \
  --port 8080 \
  --cpu 2 --memory 4Gi \
  --execution-environment gen2 \
  --min-instances 0 --max-instances 1 \
  --timeout 3600 \
  --allow-unauthenticated \
  --set-env-vars "FYF_RUNTIME_MODE=hackathon,NEXT_PUBLIC_FYF_RUNTIME_MODE=hackathon,FYF_SEGMENT_RENDER_ENABLED=1,FYF_PUBLIC_DEPLOYMENT=true,FYF_BACKEND_URL=http://127.0.0.1:8000" \
  "${SECRETS_FLAGS[@]}"

echo "== DONE =="
gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)'
