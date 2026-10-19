# Image builder for non-Linux hosts (used by `tools/babua image`).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 e2fsprogs fakeroot ca-certificates \
 && rm -rf /var/lib/apt/lists/*
