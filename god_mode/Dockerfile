ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

# Base oficial HA: trae Python 3.12 + s6-overlay v3 + bashio.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_HOST_KEY_CHECKING=False \
    ANSIBLE_FORCE_COLOR=False

# System deps:
#   ansible-core: engine de inventory + playbooks
#   openssh-client: claves + ssh remoto
#   curl + jq: comprobaciones API + JSON munging en init scripts
#   bash: bashio requiere bash
#   openssl: self-signed CA + leaf cert for the collector's opt-in TLS
#   nodejs/npm: runtime for the Claude Code CLI (opt-in Telegram bridge)
#   ffmpeg: audio transcoding for voice-message transcription
RUN apk add --no-cache \
        openssh-client \
        openssh-keygen \
        curl \
        jq \
        bash \
        sshpass \
        coreutils \
        openssl \
        nodejs \
        npm \
        ffmpeg \
    && pip install --no-cache-dir --break-system-packages \
        ansible-core==2.17.* \
        requests \
        pyyaml \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/cache/apk/* /root/.cache

# whisper.cpp, built from source, for local voice-message transcription.
# Build-only deps are removed right after so they don't bloat the final
# image. The "base" multilingual model (~142MB) is a deliberate middle
# ground between size and Spanish-language accuracy.
RUN apk add --no-cache --virtual .whisper-build-deps build-base git \
    && git clone --depth 1 --branch v1.7.2 https://github.com/ggerganov/whisper.cpp /tmp/whisper.cpp \
    && make -C /tmp/whisper.cpp -j"$(nproc)" main \
    && install -Dm755 /tmp/whisper.cpp/main /usr/bin/whisper-cli \
    && mkdir -p /usr/share/whisper-models \
    && curl -fL -o /usr/share/whisper-models/ggml-base.bin \
        https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin \
    && rm -rf /tmp/whisper.cpp \
    && apk del .whisper-build-deps

# Copia el rootfs (s6-rc.d, scripts, playbooks, lovelace YAML)
COPY rootfs /

# Permisos ejecutables
RUN find /etc/s6-overlay/s6-rc.d \( -name "up" -o -name "run" \) -type f -exec chmod +x {} + \
    && chmod +x \
        /usr/bin/god-collector.py \
        /usr/bin/god-webui.py \
        /usr/bin/god-pve-sync.py \
        /usr/bin/god-agent.sh \
        /usr/bin/god-bootstrap.sh \
        /usr/bin/god-deploy-packages.sh \
        /usr/bin/god-generate-yaml.py \
        /usr/bin/god-collector-runner.sh \
        /usr/bin/god-webui-runner.sh \
        /usr/bin/god-discover.py \
        /usr/bin/god-updates.py \
        /usr/bin/god-audit.py \
        /usr/bin/god_cached_runner.py \
        /usr/bin/god-claude-bridge.py

LABEL \
    io.hass.name="GOD Mode" \
    io.hass.description="Centralized homelab monitoring (Linux + Proxmox)" \
    io.hass.arch="amd64|aarch64" \
    io.hass.type="addon" \
    io.hass.version="0.4.14" \
    maintainer="Sergio (hirofairlane)"

EXPOSE 8099 9876
