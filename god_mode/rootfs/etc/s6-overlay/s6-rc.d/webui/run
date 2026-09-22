#!/usr/bin/with-contenv bashio
export ANSIBLE_CONFIG=/data/ansible/ansible.cfg
export GOD_VERSION="$(bashio::addon.version 2>/dev/null || echo dev)"
exec /usr/bin/god-webui.py
