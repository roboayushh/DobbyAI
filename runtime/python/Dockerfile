# DobbyAI harness Python runtime (PRD 3 section 8).
# The base is pinned by multi-arch index digest; the resulting local image is
# pinned by its content-addressed image ID in config/runtime.lock.json.
ARG BASE_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
FROM ${BASE_IMAGE}

ARG TOOL_LIBRARY_SHA256=unset
ARG WORKER_VERSION=1.0.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY runtime/python/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps --require-hashes -r /tmp/requirements.lock \
    && rm -f /tmp/requirements.lock \
    && groupadd -g 65532 sandbox \
    && useradd -u 65532 -g 65532 -M -d /tmp -s /usr/sbin/nologin sandbox \
    && mkdir -p /workspace /context /output /deps /opt/harness \
    && chmod 0755 /workspace /context /output /deps

# Reviewed worker and tool library. Read-only for every user.
COPY src/harness/worker/ /opt/harness/
RUN find /opt/harness -name '__pycache__' -prune -exec rm -rf {} + \
    && chmod -R a-w,a+rX /opt/harness

LABEL org.dobby.harness="1" \
      org.dobby.runtime="python" \
      org.dobby.runtime_version="3.12" \
      org.dobby.worker_version="${WORKER_VERSION}" \
      org.dobby.tool_library_sha256="${TOOL_LIBRARY_SHA256}"

USER 65532:65532
WORKDIR /workspace
CMD ["python", "-I", "-B", "-c", "print('dobby runtime')"]
