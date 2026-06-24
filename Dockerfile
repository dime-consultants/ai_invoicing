# ── Stage 1: build dependencies ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# System deps needed to compile some packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libmagic1 \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.txt \
    && pip install --prefix=/install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt


# ── Stage 2: runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

# Build-time arg so each image is traceable to the commit that produced it —
# helps confirm a "successful" deploy actually shipped new code, not a cached layer.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision="${GIT_SHA}"

WORKDIR /app

# Runtime system deps only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libmagic1 \
    tesseract-ocr \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Create non-root user
RUN groupadd -g 1001 django && useradd -u 1001 -g django -s /bin/bash -m django

# Copy project
COPY --chown=django:django . .

# Create dirs for static/media/outputs and fix ownership.
#
# /app/outputs/converted is where tools/handlers.py writes generated
# xlsx/csv reports (extract_ura_receipts, extract_safaricom_bill,
# clean_acon_export, reconcile_ura_vs_acon, generate_report). It's mounted
# as a separate named volume (outputs_files) shared between the `app` and
# `worker` services so the Celery worker can write extraction output that
# the web process later reads back when persisting ChatMessageAttachments.
#
# Creating it here — owned by django:django — BEFORE the volume is first
# attached means Docker preserves this ownership on the mount point even
# when the named volume is brand new, instead of defaulting to root:root
# (which is what caused the original PermissionError: the volume was
# created fresh and nothing had ever chowned it for the django user).
RUN mkdir -p /app/staticfiles /app/media /app/outputs/converted \
    && chown -R django:django /app

# Copy entrypoint script
COPY --chown=django:django docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

USER django

EXPOSE 8000

# Healthcheck — simple TCP check on port 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/api/health/ || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]