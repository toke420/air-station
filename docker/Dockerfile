# One small Debian 13 image for Icecast and the AirStation API.
# Liquidsoap, MediaMTX and Caddy use their official upstream images.
FROM debian:trixie-slim

ARG ICECAST_VERSION=2.4.4-*

RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      "icecast2=${ICECAST_VERSION}" python3 curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin air \
 && dpkg-query -W > /usr/local/share/air-package-versions.txt

COPY app /app/app
COPY config/icecast.xml /app/config/icecast.xml

ENV PYTHONPATH=/app/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /app
