#!/usr/bin/env bash
# =====================================================================
#  god-deploy-packages.sh (oneshot)
#
#  From v0.4.0 the rich monitoring UI lives in the addon's own ingress
#  webui. This script's job is reduced to:
#    1. Regenerate the minimal HA packages (alerts + agg counters +
#       per-host status/cpu/mem/temp/disk sensors)
#    2. Copy them to /homeassistant/packages/
#    3. Ensure homeassistant.packages: !include_dir_named packages
#    4. Stub Telegram chat_id placeholder in secrets.yaml
#    5. Upgrade cleanup: remove the legacy Lovelace dashboard +
#       lovelace.dashboards.god-mode registration left behind by 0.3.x
#
#  HA OS 17+: /homeassistant is HA Core's config dir
#  (requires `homeassistant_config:rw` in config.yaml).
# =====================================================================
set -e

if ! bashio::config.true "install_packages"; then
    bashio::log.info "install_packages=false, skipping deploy"
    exit 0
fi

# Pick the right HA Core config path
if [ -d /homeassistant ]; then
    HA_CFG=/homeassistant
elif [ -d /config ]; then
    HA_CFG=/config
else
    bashio::log.warning "Neither /homeassistant nor /config mounted - skipping"
    exit 0
fi
bashio::log.info "HA Core config root: ${HA_CFG}"

# Regenerate god_hosts_linux.yaml from /data/options.json
if command -v python3 >/dev/null 2>&1 && [ -x /usr/bin/god-generate-yaml.py ]; then
    bashio::log.info "Regenerating HA packages from hosts list"
    /usr/bin/god-generate-yaml.py || bashio::log.warning "Generator failed"
fi

SRC_PKG="/usr/share/god-mode/lovelace/packages"
DST_PKG="${HA_CFG}/packages"
mkdir -p "${DST_PKG}"

copy_if_changed() {
    local src=$1 dst=$2
    if [ ! -f "${dst}" ] || ! cmp -s "${src}" "${dst}"; then
        cp -f "${src}" "${dst}"
        bashio::log.info "Updated: ${dst}"
    fi
}

for f in "${SRC_PKG}"/god_*.yaml; do
    [ -f "${f}" ] || continue
    copy_if_changed "${f}" "${DST_PKG}/$(basename "${f}")"
done

# --- v0.4.0 upgrade cleanup: legacy dashboard YAML + obsolete packages ---
for legacy in \
    "${HA_CFG}/dashboards/god_mode.yaml" \
    "${DST_PKG}/god_proxmox.yaml" \
    "${DST_PKG}/god_telegram.yaml"; do
    if [ -f "${legacy}" ]; then
        bashio::log.info "Removing legacy file (no longer shipped in v0.4.0): ${legacy}"
        rm -f "${legacy}"
    fi
done

# --- Ensure `packages: !include_dir_named packages` is set ---
CFG="${HA_CFG}/configuration.yaml"
if [ -f "${CFG}" ] && ! grep -qE "^[[:space:]]+packages:[[:space:]]+!include_dir_named[[:space:]]+packages" "${CFG}"; then
    bashio::log.info "Adding 'packages: !include_dir_named packages' to homeassistant block"
    if grep -qE "^homeassistant:[[:space:]]*$" "${CFG}"; then
        awk '
            BEGIN { added=0 }
            /^homeassistant:[[:space:]]*$/ && !added {
                print
                print "  packages: !include_dir_named packages"
                added=1
                next
            }
            { print }
        ' "${CFG}" > "${CFG}.tmp" && mv "${CFG}.tmp" "${CFG}"
    else
        {
            echo ""
            echo "homeassistant:"
            echo "  packages: !include_dir_named packages"
        } >> "${CFG}"
    fi
fi

# --- v0.4.0 upgrade cleanup: remove legacy lovelace.dashboards.god-mode block ---
if [ -f "${CFG}" ] && grep -qE "^[[:space:]]+god-mode:[[:space:]]*$" "${CFG}"; then
    bashio::log.info "Removing legacy lovelace.dashboards.god-mode registration"
    python3 - "${CFG}" <<'PY' || true
import re, sys, pathlib
p = pathlib.Path(sys.argv[1])
text = p.read_text()
# Strip the god-mode entry under lovelace.dashboards (entry + up to 6 indented lines)
new = re.sub(
    r"(?m)^[ \t]+god-mode:[ \t]*\n(?:[ \t]+[^\n]*\n){0,6}",
    "",
    text,
)
if new != text:
    p.write_text(new)
PY
fi

# --- Stub god_telegram_chat_id placeholder if missing (used by god_alerts.yaml) ---
SECRETS="${HA_CFG}/secrets.yaml"
if [ -f "${SECRETS}" ] && ! grep -q "^god_telegram_chat_id:" "${SECRETS}"; then
    printf "god_telegram_chat_id: 0  # placeholder, set after creating Telegram group\n" >> "${SECRETS}"
fi

# --- Trigger HA YAML reload via Supervisor API (best effort) ---
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    bashio::log.info "Triggering core config check + reload"
    curl -fsS -X POST \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        http://supervisor/core/api/services/homeassistant/reload_all \
        >/dev/null 2>&1 \
        && bashio::log.info "Reload all OK" \
        || bashio::log.warning "reload_all failed (HA may not be up yet)"
fi

bashio::log.info "HA-side deploy complete — minimal sensors + alerts only. UI lives in addon webui."
