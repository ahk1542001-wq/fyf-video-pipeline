#!/usr/bin/env bash
# Cloud Run entrypoint: uvicorn (internal :8000) + Next standalone (:PORT).
set -e
cd /app

export FYF_RUNTIME_MODE="${FYF_RUNTIME_MODE:-hackathon}"
export NEXT_PUBLIC_FYF_RUNTIME_MODE="${NEXT_PUBLIC_FYF_RUNTIME_MODE:-$FYF_RUNTIME_MODE}"
export FYF_SEGMENT_RENDER_ENABLED="${FYF_SEGMENT_RENDER_ENABLED:-1}"
export NEXT_TELEMETRY_DISABLED=1

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

cd frontend
export PORT="${PORT:-8080}"
export HOSTNAME=0.0.0.0
node server.js &
FRONTEND_PID=$!

trap 'kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true' EXIT INT TERM
wait -n $BACKEND_PID $FRONTEND_PID
