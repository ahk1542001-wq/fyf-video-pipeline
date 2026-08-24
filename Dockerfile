# FYF Video Pipeline — Google Cloud Run container
# Single service: Next.js standalone (public :8080) rewrites /api/* to
# uvicorn (:8000, internal). Remotion renders inside this same container.
#
# Build:  gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/fyf/fyf-pipeline .
# Deploy: gcloud run deploy fyf-pipeline --image ... --region ... \
#           --cpu 2 --memory 4Gi --execution-environment gen2 \
#           --set-env-vars FYF_RUNTIME_MODE=hackathon,FYF_SEGMENT_RENDER_ENABLED=1,...

FROM node:20-slim AS web-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
# NEXT_PUBLIC_* vars are baked at build time.
ARG NEXT_PUBLIC_FYF_RUNTIME_MODE=hackathon
ENV NEXT_PUBLIC_FYF_RUNTIME_MODE=$NEXT_PUBLIC_FYF_RUNTIME_MODE
RUN npm run build

WORKDIR /build/remotion
COPY remotion/package.json remotion/package-lock.json* ./
RUN npm ci --no-audit --no-fund

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Node 20 (remotion CLI + next server) + media/browser libs for Remotion.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg ffmpeg \
      fonts-noto-core \
      libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
      libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
      libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Python application code (repo-root modules included).
COPY backend ./backend
COPY writer_agent_vertex.py vertex_model_routing.py video_contract.py visual_evidence_vertex.py ./
COPY voice_service ./voice_service

# Frontend standalone output + static assets + public dir.
COPY --from=web-builder /build/frontend/.next/standalone ./frontend
COPY --from=web-builder /build/frontend/.next/static ./frontend/.next/static
COPY --from=web-builder /build/frontend/public ./frontend/public

# Remotion project with node_modules (REMOTION_BIN lives here).
COPY --from=web-builder /build/remotion/node_modules ./remotion/node_modules
COPY remotion ./remotion
# Pre-download the headless browser so renders never fetch at runtime.
RUN cd remotion && npx remotion browser ensure

ENV PORT=8080 \
    FYF_BACKEND_URL=http://127.0.0.1:8000 \
    REMOTION_BIN=/app/remotion/node_modules/.bin/remotion

EXPOSE 8080

COPY scripts/start_cloudrun.sh /app/start_cloudrun.sh
RUN chmod +x /app/start_cloudrun.sh
CMD ["/app/start_cloudrun.sh"]
