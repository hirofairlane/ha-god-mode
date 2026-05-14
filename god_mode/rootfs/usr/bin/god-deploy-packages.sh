#!/usr/bin/env bash
# =====================================================================
#  deploy-packages (oneshot) - Auto-installs Lovelace YAML packages and
#  the GOD Mode dashboard into HA Core's /homeassistant config dir.
#
#  HA OS 17+ changed addon mappings:
#    - /config     => addon's own config dir (was HA Core's config)
#    - /homeassistant => HA Core's config dir (new path, requires
#                        `homeassistant_config:rw` map in config.yaml)
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

# Regenerate god_hosts_linux.yaml + god_mode.yaml from /data/options.json
if command -v python3 >/dev/null 2>&1 && [ -x /usr/bin/god-generate-yaml.py ]; then
    bashio::log.info "Regenerating Lovelace YAML from hosts list"
    /usr/bin/god-generate-yaml.py || bashio::log.warning "Generator failed (using fallback)"
fi

SRC_PKG="/usr/share/god-mode/lovelace/packages"
SRC_DASH="/usr/share/god-mode/lovelace/dashboards"
DST_PKG="${HA_CFG}/packages"
DST_DASH="${HA_CFG}/dashboards"

mkdir -p "${DST_PKG}" "${DST_DASH}"

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

for f in "${SRC_DASH}"/god_*.yaml; do
    [ -f "${f}" ] || continue
    copy_if_changed "${f}" "${DST_DASH}/$(basename "${f}")"
done

# --- Inject lovelace.dashboards.god-mode in configuration.yaml ---
CFG="${HA_CFG}/configuration.yaml"
if [ -f "${CFG}" ] && ! grep -q "god-mode:" "${CFG}"; then
    bashio::log.info "Patching configuration.yaml to register god-mode dashboard"
    cp "${CFG}" "${CFG}.god-mode.bak"
    awk '
        BEGIN { in_lovelace=0; patched=0 }
        /^lovelace:[[:space:]]*$/ { in_lovelace=1; print; next }
        in_lovelace && /^[^[:space:]]/ && !patched && NF>0 {
            print "  dashboards:"
            print "    god-mode:"
            print "      mode: yaml"
            print "      title: GOD Mode"
            print "      icon: mdi:eye-outline"
            print "      show_in_sidebar: true"
            print "      require_admin: false"
            print "      filename: dashboards/god_mode.yaml"
            print ""
            patched=1
            in_lovelace=0
        }
        { print }
    ' "${CFG}.god-mode.bak" > "${CFG}.tmp" && mv "${CFG}.tmp" "${CFG}"
fi

# --- Ensure homeassistant.packages: !include_dir_named packages is set ---
if [ -f "${CFG}" ] && ! grep -qE "^[[:space:]]+packages:[[:space:]]+!include_dir_named packages" "${CFG}"; then
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

# --- Inject required PVE secret if not present ---
SECRETS="${HA_CFG}/secrets.yaml"
if [ -f "${SECRETS}" ]; then
    if ! grep -q "^god_pve_api_authorization:" "${SECRETS}"; then
        TOKEN_ID=$(bashio::config 'pve.token_id')
        TOKEN_SECRET=$(bashio::config 'pve.token_secret')
        if [ -n "${TOKEN_ID}" ] && [ -n "${TOKEN_SECRET}" ]; then
            printf "\ngod_pve_api_authorization: PVEAPIToken=%s=%s\n" "${TOKEN_ID}" "${TOKEN_SECRET}" >> "${SECRETS}"
            bashio::log.info "Added god_pve_api_authorization to secrets.yaml"
        fi
    fi
fi

# --- Stub god_telegram_chat_id placeholder if missing ---
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
        || bashio::log.warning "reload_all failed (fallback to manual reloads)"
fi

bashio::log.info "Lovelace YAML deploy complete"
