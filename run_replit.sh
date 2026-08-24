#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Starting FYF Video Pipeline on Replit..."

# Replit is the public/demo boundary; use the Google route unless explicitly
# overridden so a deployment never silently depends on a private partner key.
export FYF_RUNTIME_MODE="${FYF_RUNTIME_MODE:-hackathon}"
export NEXT_PUBLIC_FYF_RUNTIME_MODE="${NEXT_PUBLIC_FYF_RUNTIME_MODE:-$FYF_RUNTIME_MODE}"
# Segmented render keeps per-segment checkpoints and avoids long single
# chromium runs that get OOM-killed on small deployment containers.
export FYF_SEGMENT_RENDER_ENABLED="${FYF_SEGMENT_RENDER_ENABLED:-1}"

start_backend() {
  # The deployment build creates this environment. Prefer it so the public
  # container never tries to resolve/install Python packages during startup.
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
  elif command -v uv >/dev/null 2>&1 && [[ -f uv.lock ]]; then
    uv run --frozen --python "${FYF_PYTHON_VERSION:-3.11}" python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
  else
    python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
  fi
}

start_backend &
BACKEND_PID=$!

start_frontend() {
  cd frontend
  if [[ -f .next/standalone/server.js ]]; then
    export PORT=3001
    export HOSTNAME=0.0.0.0
    node .next/standalone/server.js
    return
  fi

  if [[ ! -x node_modules/.bin/next ]]; then
    npm ci --no-audit --no-fund
  fi
  if [[ ! -f .next/BUILD_ID ]]; then
    npm run build
  fi
  npm run start -- --hostname 0.0.0.0 --port 3001
}

start_frontend &
FRONTEND_PID=$!

cleanup() {
  kill "$BACKEND_PID" 2>/dev/null || true
  kill "$FRONTEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
  wait "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait "$FRONTEND_PID"
