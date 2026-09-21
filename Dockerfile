# ==============================================================
#  Gramma v3 — production image (multi-stage)
# ==============================================================

# ── Stage 1: build the Mini-App (React + Vite) ───────────────
FROM node:20-slim AS frontend
WORKDIR /web
COPY miniapp/package.json miniapp/package-lock.json* ./
RUN npm install
COPY miniapp/ ./
RUN npm run build:backend

# ── Stage 2: Python runtime ──────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Application code + built Mini-App SPA (served by FastAPI on /)
COPY app ./app
COPY scripts ./scripts
COPY run.py .
COPY --from=frontend /web/dist ./app/webapp/static

RUN useradd --create-home --uid 10001 gramma
USER gramma

EXPOSE 8000

CMD ["python", "run.py", "--no-scheduler"]
