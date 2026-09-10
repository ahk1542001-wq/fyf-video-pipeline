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

cleanup() {
  kill "$BACKEND_PID" "${FRONTEND_PID:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Cloud Run considers the container ready when the public :8080 listener is
# available. Do not start that listener until the internal API can answer,
# otherwise the first proxied request sees ECONNREFUSED during cold starts.
BACKEND_READY=0
STARTUP_DEADLINE=$((SECONDS + 30))
while (( SECONDS < STARTUP_DEADLINE )); do
  if curl -fsS --connect-timeout 1 --max-time 2 http://127.0.0.1:8000/health >/dev/null; then
    BACKEND_READY=1
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    wait "$BACKEND_PID"
    exit $?
  fi
  sleep 0.25
done
if [[ "$BACKEND_READY" -ne 1 ]]; then
  echo "Backend did not become ready within 30 seconds" >&2
  exit 1
fi

cd frontend
export PORT="${PORT:-8080}"
export HOSTNAME=0.0.0.0
node server.js &
FRONTEND_PID=$!

wait -n $BACKEND_PID $FRONTEND_PID
