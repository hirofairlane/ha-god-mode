#!/usr/bin/with-contenv bashio
# =====================================================================
#  02-deploy-packages.sh - Auto-installs Lovelace YAML packages and the
#  GOD Mode dashboard into /config when option `install_packages: true`.
#
#  Strategy: copy from /usr/share/god-mode/lovelace/{packages,dashboards}
#  to /config/{packages,dashboards}, only overwriting when content
#  differs (sha256). User edits to god_*.yaml in /config persist
#  unless the add-on is updated.
#
#  Also patches /config/configuration.yaml to register the dashboard.
# =====================================================================
set -e

if ! bashio::config.true "install_packages"; then
    bashio::log.info "install_packages=false, skipping /config sync"
    exit 0
fi

if [ ! -d /config ]; then
    bashio::log.warning "/config not mounted (config map missing) - skipping"
    exit 0
fi

SRC_PKG="/usr/share/god-mode/lovelace/packages"
SRC_DASH="/usr/share/god-mode/lovelace/dashboards"
DST_PKG="/config/packages"
DST_DASH="/config/dashboards"

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
CFG=/config/configuration.yaml
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

# --- Inject required secrets if not present ---
SECRETS=/config/secrets.yaml
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

bashio::log.info "Lovelace YAML deploy complete"
