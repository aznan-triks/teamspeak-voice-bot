# Stage 1: the Rust bridge (TeamSpeak client + Opus decoding).
FROM rust:1-bookworm AS bridge
RUN apt-get update && apt-get install -y --no-install-recommends libopus-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY bridge/ .
RUN cargo build --release --locked

# Stage 2: the Python orchestrator (speech-to-text, archive, plugins). Same Debian base as stage 1 (glibc).
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home bot && mkdir /data && chown bot /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=bridge /src/target/release/ts-bridge /usr/local/bin/ts-bridge
COPY tsbot/ tsbot/
ENV PYTHONUNBUFFERED=1 TSBOT_CONFIG=/app/config.toml HF_HOME=/data/models
USER bot
VOLUME /data
CMD ["python", "-m", "tsbot"]
