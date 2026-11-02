# Read-only Fubon market data sidecar.
#
# fubon_neo ships native wheels only for macOS arm64 and Linux x86_64, so this image is
# always linux/amd64 (Apple Silicon hosts run it through Rosetta). The wheel is not
# committed: download it from Fubon and place it in fubon_sidecar/vendor/ (see README).
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM --platform=linux/amd64 ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Taipei

WORKDIR /app

COPY vendor/ /tmp/vendor/
COPY pyproject.toml ./
COPY fubon_sidecar/ ./fubon_sidecar/

# The ".[sdk]" extra pins fugle-marketdata==2.4.1 in the same resolver run as the wheel:
# fugle-marketdata>=2.5.0 rejects the "/v1.0" base URL hard-coded by fubon_neo 2.2.8 and
# the realtime client fails with TypeError.
RUN set -eu; \
    wheel="$(find /tmp/vendor -maxdepth 1 -name 'fubon_neo-*x86_64*.whl' | sort | tail -n 1)"; \
    if [ -z "$wheel" ]; then \
      echo "fubon_neo Linux x86_64 wheel not found in fubon_sidecar/vendor/; see fubon_sidecar/README.md" >&2; \
      exit 1; \
    fi; \
    pip install --no-cache-dir "$wheel" ".[sdk]"; \
    rm -rf /tmp/vendor; \
    useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin sidecar; \
    mkdir -p /app/log; \
    chown -R sidecar:sidecar /app

USER sidecar

EXPOSE 8010

CMD ["python", "-m", "fubon_sidecar"]
