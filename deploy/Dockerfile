# syntax=docker/dockerfile:1
# Build from the repository root:  docker build -f deploy/Dockerfile -t rug-app .
#
# Targets:  app (default, production)   smoke (app + the test harness; never deploy this one)
#
# Behind a TLS-intercepting proxy, pass its CA as a BuildKit secret, never as COPY or ENV:
#   docker build --secret id=ca,src=/path/ca.pem -f deploy/Dockerfile .
# The secret exists only during the RUN steps that mount it and is not part of any layer.

ARG PYTHON_IMAGE=python:3.11-slim
ARG NODE_IMAGE=node:22-slim

# ---- frontend -------------------------------------------------------------------------------
FROM ${NODE_IMAGE} AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca; fi; \
    npm ci --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

# ---- python runtime -------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# Tesseract does the OCR of images inside documents. Leave INSTALL_TESSERACT=1 for real use;
# 0 is only for build hosts that cannot reach the Debian mirrors (documents with images then fail
# to index and are retried, instead of being indexed without their image text).
ARG INSTALL_TESSERACT=1
# Debian's mirrors also serve HTTPS; use it so package downloads are authenticated and encrypted.
RUN --mount=type=secret,id=ca,required=false \
    if [ "$INSTALL_TESSERACT" = "1" ]; then \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    OPTS=""; \
    if [ -f /run/secrets/ca ]; then OPTS="-o Acquire::https::CaInfo=/run/secrets/ca"; fi; \
    if [ -n "${HTTPS_PROXY:-}" ]; then OPTS="$OPTS -o Acquire::https::Proxy=$HTTPS_PROXY"; fi; \
    apt-get $OPTS update -qq \
    && apt-get $OPTS install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*; \
    fi
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin rug
WORKDIR /app
COPY backend/pyproject.toml ./
COPY backend/rug ./rug
COPY backend/eval ./eval
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca SSL_CERT_FILE=/run/secrets/ca; fi; \
    pip install .
COPY --from=frontend /src/frontend/dist /app/static
ENV RUG_STATIC_DIR=/app/static
USER rug
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
# Migrate, then serve. The worker service overrides the command with `rug worker`.
CMD ["sh", "-c", "rug migrate && exec rug serve --host 0.0.0.0 --port 8000"]

# ---- smoke: the test harness (mock directory, fake models) ------------------------------------
FROM app AS smoke
USER root
COPY backend/harness ./harness
USER rug
CMD ["python", "-m", "harness.e2e_server", "--host", "0.0.0.0", "--port", "8000", \
     "--static", "/app/static", "--docs", "/tmp/rug-smoke-docs", "--secure-cookies", \
     "--token-delay", "1.0"]
