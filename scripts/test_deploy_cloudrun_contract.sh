#!/usr/bin/env bash
# Hermetic static contract checks for scripts/deploy_cloudrun.sh.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy_cloudrun.sh"
DOCKERFILE="$SCRIPT_DIR/../Dockerfile"
START_SCRIPT="$SCRIPT_DIR/start_cloudrun.sh"

assert_contains() {
  local pattern="$1"
  local message="$2"
  if ! grep -Eq -- "$pattern" "$DEPLOY_SCRIPT"; then
    echo "FAIL: $message" >&2
    exit 1
  fi
}

assert_not_contains() {
  local pattern="$1"
  local message="$2"
  if grep -Eq -- "$pattern" "$DEPLOY_SCRIPT"; then
    echo "FAIL: $message" >&2
    exit 1
  fi
}

# PROJECT_ID is the single resolved project input, with the legacy project env
# retained only as a compatibility fallback and no historical default.
assert_contains '^PROJECT_ID="\$\{PROJECT_ID:-\$\{GOOGLE_CLOUD_PROJECT:-\}\}"$' \
  'PROJECT_ID must be the single resolved project input without a hard-coded default'
assert_contains '\[\[ -z "\$PROJECT_ID" \]\]' \
  'missing project configuration must be rejected before any gcloud mutation'
assert_contains 'PROJECT_ID is required' \
  'missing project configuration must produce an actionable error'
assert_contains '\[\[ ! "\$PROJECT_ID" =~ \^\[a-z\]\[a-z0-9-\]\{4,28\}\[a-z0-9\]\$ \]\]' \
  'PROJECT_ID must be validated before any gcloud mutation'
assert_contains 'exit 2' \
  'an invalid PROJECT_ID must fail before deployment'
assert_contains 'gcloud config set project "\$PROJECT_ID"' \
  'gcloud project selection must use PROJECT_ID'
assert_contains 'IMAGE="\$REGION-docker\.pkg\.dev/\$PROJECT_ID/\$REPO/' \
  'the image target must use PROJECT_ID'
assert_contains '--project "\$PROJECT_ID"' \
  'the Cloud Run deploy target must use PROJECT_ID'
assert_contains 'GOOGLE_CLOUD_PROJECT=\$PROJECT_ID' \
  'runtime GOOGLE_CLOUD_PROJECT must use PROJECT_ID'
assert_not_contains 'GOOGLE_CLOUD_PROJECT=[A-Za-z0-9][A-Za-z0-9-]*' \
  'runtime GOOGLE_CLOUD_PROJECT must not contain a divergent literal project'
assert_not_contains 'intelligent-arc-488111-s0|artful-sky-501413-i4' \
  'historical hard-coded project IDs must not remain'

if output="$(/usr/bin/env -u PROJECT_ID -u GOOGLE_CLOUD_PROJECT PATH=/dev/null /bin/bash "$DEPLOY_SCRIPT" 2>&1)"; then
  echo 'FAIL: missing project configuration unexpectedly continued' >&2
  exit 1
else
  exit_code=$?
fi
if [[ "$exit_code" -ne 2 ]]; then
  echo "FAIL: missing project configuration exited with $exit_code, expected 2" >&2
  exit 1
fi
if [[ "$output" != *'PROJECT_ID is required'* ]]; then
  echo 'FAIL: missing project configuration error was not actionable' >&2
  exit 1
fi
echo 'PASS: missing project configuration rejected before gcloud invocation'

# --- Stage B-III: fail-closed paid-production budget envelope contract ---
# budget_store is fail-closed: paid dispatch is enabled ONLY when BOTH a daily
# AND a total ceiling are present. The deploy env must therefore set both caps,
# sourced from the single clearly-marked owner-approved envelope block so that
# raising the ceiling stays a deliberate single-place decision (no drift).
assert_contains 'OWNER-APPROVED PAID-PRODUCTION BUDGET ENVELOPE' \
  'the budget envelope must be a clearly-marked single place to change'
assert_contains '^FYF_DAILY_BUDGET_CAP_USD="' \
  'the owner-approved envelope block must define the daily ceiling'
assert_contains '^FYF_TOTAL_BUDGET_CAP_USD="' \
  'the owner-approved envelope block must define the total ceiling'
assert_contains 'FYF_DAILY_BUDGET_CAP_USD=\$FYF_DAILY_BUDGET_CAP_USD' \
  'the deploy env must set the daily cap from the single envelope block'
assert_contains 'FYF_TOTAL_BUDGET_CAP_USD=\$FYF_TOTAL_BUDGET_CAP_USD' \
  'the deploy env must set the total cap (fail-closed paid production requires BOTH caps)'
assert_not_contains 'FYF_DAILY_BUDGET_CAP_USD=[0-9]' \
  'the deploy --set-env-vars must not hard-code a divergent daily cap literal'
assert_not_contains 'FYF_TOTAL_BUDGET_CAP_USD=[0-9]' \
  'the deploy --set-env-vars must not hard-code a divergent total cap literal'
echo 'PASS: deploy_cloudrun.sh fail-closed budget envelope contract (daily + total caps present and single-sourced)'

# --- Durable completed-job storage contract ---
# Only the jobs subtree is mounted. Queue, lock, and project transaction files
# stay on the container filesystem because they require stronger POSIX semantics
# than Cloud Storage FUSE provides.
assert_contains '^FYF_OUTPUT_BUCKET="\$\{FYF_OUTPUT_BUCKET:-\$\{PROJECT_ID\}-fyf-output-\$\{REGION\}\}"$' \
  'the output bucket must be a deterministic, overrideable deployment input'
assert_contains 'gcloud storage buckets describe "gs://\$FYF_OUTPUT_BUCKET"' \
  'deployment must reuse an existing durable output bucket'
assert_contains 'gcloud storage buckets create "gs://\$FYF_OUTPUT_BUCKET"' \
  'deployment must create the durable output bucket when absent'
assert_contains 'roles/storage.objectUser' \
  'the runtime service account must receive object access scoped to the output bucket'
assert_contains '--add-volume "name=fyf-jobs,type=cloud-storage,bucket=\$FYF_OUTPUT_BUCKET"' \
  'Cloud Run must attach the output bucket as a Cloud Storage volume'
assert_contains '--add-volume-mount "volume=fyf-jobs,mount-path=/app/output/jobs"' \
  'only the completed-job tree may be mounted into the runtime'
assert_not_contains 'mount-path=/app/output([,\"]|$)' \
  'the full output tree must not be mounted because queue and lock files need POSIX semantics'
echo 'PASS: deploy_cloudrun.sh durable completed-job storage contract'

# The Next proxy must not accept traffic before uvicorn is ready. Cloud Run can
# route the first request as soon as :8080 listens, which previously exposed a
# brief but real 500/ECONNREFUSED window after every cold start.
if ! grep -Eq 'curl .*127\.0\.0\.1:8000/health' "$START_SCRIPT"; then
  echo 'FAIL: Cloud Run startup must wait for backend health before starting Next' >&2
  exit 1
fi
if ! grep -Eq 'curl .*--connect-timeout [0-9]+ .*--max-time [0-9]+ .*127\.0\.0\.1:8000/health' "$START_SCRIPT"; then
  echo 'FAIL: backend readiness probe must have bounded connect and response timeouts' >&2
  exit 1
fi
if ! awk '/curl .*127\.0\.0\.1:8000\/health/{ready=NR} /node server\.js/{frontend=NR} END{exit !(ready && frontend && ready < frontend)}' "$START_SCRIPT"; then
  echo 'FAIL: backend readiness wait must occur before node server.js' >&2
  exit 1
fi
echo 'PASS: Cloud Run startup waits for backend health before exposing the proxy'

assert_contains '^EXISTING_RUNTIME_SERVICE_ACCOUNT=' \
  'deployment must inspect the existing Cloud Run runtime identity'
assert_contains 'gcloud run services list' \
  'deployment must enumerate services without hiding lookup failures'
assert_contains '--filter="metadata\.name=\$SERVICE"' \
  'existing identity lookup must select only the configured service'
assert_not_contains 'gcloud run services describe .*2>/dev/null \|\| true' \
  'runtime identity lookup failures must not silently fall back to a different service account'
if ! awk '/gcloud services enable .*run\.googleapis\.com/{enabled=NR} /gcloud run services list/{lookup=NR} END{exit !(enabled && lookup && enabled < lookup)}' "$DEPLOY_SCRIPT"; then
  echo 'FAIL: Cloud Run API must be enabled before the existing-service identity lookup' >&2
  exit 1
fi
assert_contains '^RUNTIME_SERVICE_ACCOUNT="\$\{FYF_RUNTIME_SERVICE_ACCOUNT:-\$\{EXISTING_RUNTIME_SERVICE_ACCOUNT:-\$\{SA_NUM\}-compute@developer\.gserviceaccount\.com\}\}"$' \
  'runtime identity must be overrideable, reuse the existing service identity, and fall back only for a new service'
assert_contains 'serviceAccount:\$\{RUNTIME_SERVICE_ACCOUNT\}' \
  'IAM bindings must target the selected runtime service account'
assert_contains '--service-account "\$RUNTIME_SERVICE_ACCOUNT"' \
  'Cloud Run must deploy with the same identity that received bucket and secret access'
assert_not_contains 'roles/secretmanager\.secretAccessor" --quiet >/dev/null \|\| true' \
  'secret-access IAM failures must stop deployment instead of producing a broken revision'
if ! grep -Eq 'STARTUP_DEADLINE=\$\(\(SECONDS \+ 30\)\)' "$START_SCRIPT"; then
  echo 'FAIL: readiness must use a wall-clock 30-second deadline' >&2
  exit 1
fi

if ! grep -Eq '^ENV BUILD_STANDALONE=true$' "$DOCKERFILE"; then
  echo 'FAIL: Docker build must enable Next standalone output before npm run build' >&2
  exit 1
fi
echo 'PASS: Dockerfile enables Next standalone output'

echo "PASS: deploy_cloudrun.sh project configuration contract"
