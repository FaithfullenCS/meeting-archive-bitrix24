# The deployment may lock this tag to its verified repository digest via BASE_IMAGE.
ARG BASE_IMAGE=python:3.12.15-slim-bookworm
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY oauth_relay/requirements.lock /tmp/relay-requirements.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/relay-requirements.lock
COPY oauth_relay /app/oauth_relay
USER 65532:65532
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD ["python", "-m", "oauth_relay.healthcheck"]
CMD ["python", "-m", "oauth_relay"]
