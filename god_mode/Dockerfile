ARG BUILD_FROM=ghcr.io/hassio-addons/base:14.5.4
# hadolint ignore=DL3006
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ANSIBLE_HOST_KEY_CHECKING=False \
    ANSIBLE_FORCE_COLOR=False

# System deps:
#   ansible-core: engine de inventory + playbooks
#   openssh-client: claves + ssh remoto
#   python3 + pip: collector + webui
#   curl + jq: comprobaciones API
#   tini: PID 1 limpio (s6 ya lo lleva, pero por seguridad)
RUN apk add --no-cache \
        ansible-core \
        openssh-client \
        python3 \
        py3-pip \
        py3-yaml \
        py3-requests \
        curl \
        jq \
        bash \
        tini \
        sshpass \
    && rm -rf /var/cache/apk/*

# Copia el rootfs (incluye services s6, scripts, playbooks, lovelace YAML)
COPY rootfs /

# Permisos ejecutables
RUN chmod +x \
        /etc/cont-init.d/*.sh \
        /etc/services.d/*/run \
        /usr/bin/god-collector.py \
        /usr/bin/god-webui.py \
        /usr/bin/god-agent.sh

# Volume persistente del add-on (HA Supervisor lo monta aquí)
# /data está reservado por Supervisor para state persistente entre reinicios

LABEL \
    io.hass.name="GOD Mode" \
    io.hass.description="Centralized homelab monitoring (Linux + Proxmox)" \
    io.hass.arch="amd64|aarch64" \
    io.hass.type="addon" \
    io.hass.version="0.2.0" \
    maintainer="Sergio (hirofairlane)"

EXPOSE 8099 9876
