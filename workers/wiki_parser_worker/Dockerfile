# syntax=docker/dockerfile:1.7

# linux/amd64 manifest for Docker Official Image python:3.12.13-slim-bookworm.
ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:72d3d75f2639ab82b34b29390ad3d6e0827c775befee94edda8e9976818f488d

FROM ${PYTHON_IMAGE} AS dependency-builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY requirements-linux-x86_64.lock /build/requirements-linux-x86_64.lock
RUN python -m pip install \
    --require-hashes \
    --target=/opt/runtime \
    -r /build/requirements-linux-x86_64.lock
COPY runtime-manifest.json /build/runtime-manifest.json
COPY scripts/fetch_runtime_models.py /build/fetch_runtime_models.py
COPY scripts/fetch_runtime_sources.py /build/fetch_runtime_sources.py
RUN python /build/fetch_runtime_sources.py \
    --manifest /build/runtime-manifest.json \
    --output /opt/upstream-sources
RUN PYTHONPATH=/opt/runtime python /build/fetch_runtime_models.py \
    --manifest /build/runtime-manifest.json \
    --output /opt/models
RUN chmod -R a-w /opt/runtime /opt/models /opt/upstream-sources

FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="pi Wiki Parser Worker" \
      org.opencontainers.image.version="0.0.28" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="/usr/share/pi-wiki-parser-worker/source"
ENV HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 \
    HOME=/nonexistent \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/usr/share/pi-wiki-parser-worker/source/src:/opt/runtime \
    PYTHONUNBUFFERED=1 \
    TRANSFORMERS_OFFLINE=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependency-builder /opt/runtime /opt/runtime
COPY --from=dependency-builder /opt/models /opt/models
COPY --from=dependency-builder /opt/upstream-sources /usr/share/pi-wiki-parser-worker/upstream-sources
COPY . /usr/share/pi-wiki-parser-worker/source
RUN mkdir -p /exchange/jobs /tmp \
    && chown -R 65532:65532 /exchange /tmp \
    && chmod -R a-w /usr/share/pi-wiki-parser-worker \
    && chmod 0755 /exchange /exchange/jobs /tmp
USER 65532:65532
WORKDIR /nonexistent
VOLUME ["/exchange"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15m --retries=3 \
    CMD ["python", "-m", "wiki_parser_worker", "probe", "--queue-root", "/exchange"]
ENTRYPOINT ["python", "-m", "wiki_parser_worker"]
CMD ["serve", "--queue-root", "/exchange", "--config", "/usr/share/pi-wiki-parser-worker/source/config/routing-quality-v1.json", "--model-root", "/opt/models"]
