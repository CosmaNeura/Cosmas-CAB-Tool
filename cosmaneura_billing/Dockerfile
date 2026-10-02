# syntax=docker/dockerfile:1

FROM node:22-alpine AS frontend-builder

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS python-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY cosmaneura_billing/ ./cosmaneura_billing/
RUN python -m pip wheel --wheel-dir /wheels ".[api,inference,postgres,messaging,telemetry,core_bridge]"


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    COSMANEURA_FRONTEND_DIST=/app/static \
    COSMANEURA_MIGRATIONS_DIR=/app/migrations \
    COSMANEURA_FEEDBACK_PATH=/var/lib/cosmaneura/coder_feedback.jsonl \
    COSMANEURA_DIRECT_CONTRACT_LIST=/var/lib/cosmaneura/direct_contract_patients.csv \
    XDG_CACHE_HOME=/var/lib/cosmaneura/cache

RUN groupadd --system --gid 10001 cosmaneura \
    && useradd --system --uid 10001 --gid cosmaneura --create-home \
        --home-dir /home/cosmaneura --shell /usr/sbin/nologin cosmaneura \
    && mkdir -p /app/static /var/lib/cosmaneura/cache \
    && chown -R 10001:10001 /app /var/lib/cosmaneura /home/cosmaneura

COPY --from=python-builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels \
        "cosmaneura-billing[api,inference,postgres,messaging,telemetry,core_bridge]" \
    && python -I -c "from azure.communication.email import EmailClient" \
    && python -I -c "from azure.identity import ManagedIdentityCredential; from openai import AzureOpenAI" \
    && python -I -c "from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter; from opentelemetry.sdk.metrics import MeterProvider" \
    && python -I -c "from cosmaneura_billing.orchestrator import BillingOrchestrator; o=BillingOrchestrator(); assert o.ncci.loaded and o.coverage.loaded" \
    && rm -rf /wheels

COPY --from=frontend-builder --chown=10001:10001 /build/dist/ /app/static/
COPY --chown=10001:10001 migrations/ /app/migrations/

WORKDIR /app
USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "cosmaneura_billing.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
