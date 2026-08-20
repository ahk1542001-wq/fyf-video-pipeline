#!/usr/bin/env bash
set -e

echo "Starting FYF Video Pipeline on Replit..."

# Replit is the public/demo boundary; use the Google route unless explicitly
# overridden so a deployment never silently depends on a private partner key.
export FYF_RUNTIME_MODE="${FYF_RUNTIME_MODE:-hackathon}"

# Start FastAPI backend on 8000 in background
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Start Next.js frontend on 3001
cd frontend
npm install
npm run dev -- --port 3001 --hostname 0.0.0.0
