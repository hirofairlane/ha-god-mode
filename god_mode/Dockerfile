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
RUN apk add --no-cache \
        openssh-client \
        openssh-keygen \
        curl \
        jq \
        bash \
        sshpass \
        coreutils \
    && pip install --no-cache-dir --break-system-packages \
        ansible-core==2.17.* \
        requests \
        pyyaml \
    && rm -rf /var/cache/apk/* /root/.cache

# Copia el rootfs (s6-rc.d, scripts, playbooks, lovelace YAML)
COPY rootfs /

# Permisos ejecutables
RUN find /etc/s6-overlay/s6-rc.d \( -name "up" -o -name "run" \) -type f -exec chmod +x {} + \
    && chmod +x \
        /usr/bin/god-collector.py \
        /usr/bin/god-webui.py \
        /usr/bin/god-agent.sh \
        /usr/bin/god-bootstrap.sh \
        /usr/bin/god-deploy-packages.sh \
        /usr/bin/god-collector-runner.sh \
        /usr/bin/god-webui-runner.sh

LABEL \
    io.hass.name="GOD Mode" \
    io.hass.description="Centralized homelab monitoring (Linux + Proxmox)" \
    io.hass.arch="amd64|aarch64" \
    io.hass.type="addon" \
    io.hass.version="0.2.0" \
    maintainer="Sergio (hirofairlane)"

EXPOSE 8099 9876
