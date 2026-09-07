#!/usr/bin/env bash
# Hermetic static contract checks for scripts/deploy_cloudrun.sh.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SCRIPT="$SCRIPT_DIR/deploy_cloudrun.sh"

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

echo "PASS: deploy_cloudrun.sh project configuration contract"
