# Operator AI — built, not deployed. Build from the repo root:
#   docker build -f deploy/Dockerfile -t operator-ai .
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.14-slim
# the git commit, shown on /health: docker build --build-arg OPERATOR_COMMIT=$(git rev-parse HEAD) ...
ARG OPERATOR_COMMIT=unknown
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPERATOR_PORT=8850 OPERATOR_DATA_DIR=/app/data \
    OPERATOR_CORPUS_DIR=/app/inputs OPERATOR_COOKIE_SECURE=1 OPERATOR_COMMIT=${OPERATOR_COMMIT}
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr curl sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 operator
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY operator_ai/ operator_ai/
COPY scripts/ scripts/
COPY tests/fixtures/ tests/fixtures/
COPY tests/gold_questions.json tests/not_covered_questions.json tests/audit_tasks.json tests/
COPY --from=web /web/dist web/dist
RUN mkdir -p /app/data /app/inputs /app/backups /app/backup-keys /app/logs && chown -R operator:operator /app
USER operator
EXPOSE 8850
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=4 CMD curl -fsS http://127.0.0.1:8850/health || exit 1
CMD ["python", "-m", "uvicorn", "operator_ai.app:app", "--host", "0.0.0.0", "--port", "8850", "--proxy-headers", "--forwarded-allow-ips", "*"]
