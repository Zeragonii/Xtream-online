# syntax=docker/dockerfile:1.7

FROM node:24-alpine AS hlsjs
WORKDIR /vendor
RUN npm init -y >/dev/null 2>&1 \
    && npm install --omit=dev hls.js@1.7.0 \
    && cp node_modules/hls.js/dist/hls.min.js /vendor/hls.min.js

FROM python:3.13-slim

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HLS_DIR=/tmp/xtream-online/hls

LABEL org.opencontainers.image.title="Xtream Online" \
      org.opencontainers.image.description="Local Xtream Codes web player with managed FFmpeg HLS sessions" \
      org.opencontainers.image.version="${APP_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=hlsjs /vendor/hls.min.js /app/app/static/vendor/hls.min.js

RUN mkdir -p /data /tmp/xtream-online/hls \
    && chown -R 10001:10001 /data /tmp/xtream-online /app

USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
