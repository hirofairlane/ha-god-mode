#!/usr/bin/env bash
# =====================================================================
#  god-bootstrap.sh - oneshot init for the GOD Mode add-on
#    1. SSH keypair in /data/.ssh (with backup in /homeassistant/god_mode/.ssh
#       so it survives addon reinstall / supervisor repair)
#    2. Ansible inventory.yml grouped by category, with optional
#       hierarchical groups for hosts that declare a `parent`
#    3. ansible.cfg pointing to /data/.ssh
#
#  No `set -e`: we want the script to keep going on best-effort writes
#  (eg. chmod on a read-only mount) and log progress instead. Hard
#  errors are raised explicitly via `bashio::log.error` + `exit 1` in
#  the few places where continuing is meaningless.
# =====================================================================
# No `set -e` / `set -u` here on purpose. Tests like `[ -n "" ]` returning
# exit 1 inside helper functions otherwise propagate through pipes/groups
# and abort the whole script with no useful log. We make the script
# tolerant and emit `exit 0` explicitly at the end.

DATA="/data"
SSH_DIR="${DATA}/.ssh"
INV_DIR="${DATA}/ansible"
INV_FILE="${INV_DIR}/inventory.yml"
CFG_FILE="${INV_DIR}/ansible.cfg"
META_DIR="${DATA}/metadata"
METRICS_DIR="${DATA}/metrics"
PVE_CHILDREN_DIR="${DATA}/pve_children"

# Long-lived backup outside /data (survives addon reinstall/repair).
# /homeassistant is HA Core config root in HA OS 17+.
HA_BACKUP_DIR="/homeassistant/god_mode/.ssh"

mkdir -p "${SSH_DIR}" "${INV_DIR}" "${META_DIR}" "${METRICS_DIR}" "${PVE_CHILDREN_DIR}"
chmod 700 "${SSH_DIR}"

# 1) Keypair
#    Priority: existing /data key > backup in /homeassistant > generate new.
if [ ! -f "${SSH_DIR}/id_ed25519" ]; then
    if [ -f "${HA_BACKUP_DIR}/id_ed25519" ] && [ -f "${HA_BACKUP_DIR}/id_ed25519.pub" ]; then
        bashio::log.info "Restoring GOD Mode SSH keypair from ${HA_BACKUP_DIR}"
        cp "${HA_BACKUP_DIR}/id_ed25519"     "${SSH_DIR}/id_ed25519"
        cp "${HA_BACKUP_DIR}/id_ed25519.pub" "${SSH_DIR}/id_ed25519.pub"
        chmod 600 "${SSH_DIR}/id_ed25519"
        chmod 644 "${SSH_DIR}/id_ed25519.pub"
    else
        bashio::log.warning "==============================================================="
        bashio::log.warning "GENERATING A NEW SSH KEYPAIR."
        bashio::log.warning "Any host whose authorized_keys already trusts a previous"
        bashio::log.warning "GOD Mode pubkey will STOP being reachable until you re-enroll."
        bashio::log.warning "==============================================================="
        ssh-keygen -t ed25519 -N "" -f "${SSH_DIR}/id_ed25519" -C "god-mode@$(hostname)" >/dev/null
        chmod 600 "${SSH_DIR}/id_ed25519"
        chmod 644 "${SSH_DIR}/id_ed25519.pub"
    fi
fi

# Always sync to long-lived backup so future reinstalls find it.
# Best-effort: HA Core config dir may be read-only at boot, in which
# case we silently skip the backup and continue.
if mkdir -p "${HA_BACKUP_DIR}" 2>/dev/null; then
    chmod 700 "${HA_BACKUP_DIR}" 2>/dev/null || true
    cp -f "${SSH_DIR}/id_ed25519"     "${HA_BACKUP_DIR}/id_ed25519"     2>/dev/null || true
    cp -f "${SSH_DIR}/id_ed25519.pub" "${HA_BACKUP_DIR}/id_ed25519.pub" 2>/dev/null || true
    chmod 600 "${HA_BACKUP_DIR}/id_ed25519"     2>/dev/null || true
    chmod 644 "${HA_BACKUP_DIR}/id_ed25519.pub" 2>/dev/null || true
else
    bashio::log.warning "Could not write SSH backup to ${HA_BACKUP_DIR} (read-only?). Key still in /data/.ssh."
fi

# 2) Persisted known_hosts
touch "${SSH_DIR}/known_hosts"
chmod 644 "${SSH_DIR}/known_hosts"

# 3) Pubkey banner
PUB=$(cat "${SSH_DIR}/id_ed25519.pub")
bashio::log.info "==============================================================="
bashio::log.info "GOD Mode public SSH key:"
bashio::log.info "  ${PUB}"
bashio::log.info ""
bashio::log.info "Add it to ~/.ssh/authorized_keys on every host you want to"
bashio::log.info "monitor. From the add-on web UI you can copy/paste it directly."
bashio::log.info "Backup of this key lives in: ${HA_BACKUP_DIR}"
bashio::log.info "==============================================================="

if [ -d /config ]; then
    cp "${SSH_DIR}/id_ed25519.pub" /config/god_mode_pubkey.txt 2>/dev/null || true
fi

# 4) Build inventory.yml grouped by category, plus optional pve_node parent groups
HOSTS_JSON=$(bashio::config "hosts | tojson" 2>/dev/null || echo "[]")
SSH_USER_DEFAULT=$(bashio::config "ssh_user" 2>/dev/null || echo "root")
if [ -z "${HOSTS_JSON}" ] || [ "${HOSTS_JSON}" = "null" ]; then
    HOSTS_JSON="[]"
fi
bashio::log.info "Bootstrap: parsed $(echo "${HOSTS_JSON}" | jq 'length') hosts from config"

CATEGORIES=$(echo "${HOSTS_JSON}" | jq -r '[.[] | .category // "uncategorized"] | unique | .[]' 2>/dev/null || echo "")
PVE_NODES=$(echo "${HOSTS_JSON}" | jq -r '[.[] | select(.category == "pve_node") | .name] | .[]' 2>/dev/null || echo "")

emit_host_block() {
    local entry="$1"
    local name addr user port chip parent
    name=$(echo   "${entry}" | jq -r '.name')
    addr=$(echo   "${entry}" | jq -r '.addr')
    user=$(echo   "${entry}" | jq -r --arg d "${SSH_USER_DEFAULT}" '.user // $d')
    port=$(echo   "${entry}" | jq -r '.port // 22')
    chip=$(echo   "${entry}" | jq -r '.chip // ""')
    parent=$(echo "${entry}" | jq -r '.parent // ""')
    echo "        ${name}:"
    echo "          ansible_host: ${addr}"
    echo "          ansible_user: ${user}"
    echo "          ansible_port: ${port}"
    if [ -n "${chip}" ];   then echo "          god_chip: ${chip}";     fi
    if [ -n "${parent}" ]; then echo "          god_parent: ${parent}"; fi
    return 0
}

{
    echo "# AUTO-GENERATED by /usr/bin/god-bootstrap.sh on add-on start"
    echo "# Edits will be lost. Modify hosts list in add-on Configuration UI."
    echo ""
    echo "all:"
    echo "  vars:"
    echo "    ansible_ssh_private_key_file: ${SSH_DIR}/id_ed25519"
    echo "    ansible_ssh_common_args: '-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${SSH_DIR}/known_hosts -o ConnectTimeout=8'"
    echo "    ansible_python_interpreter: auto_silent"
    echo "  children:"
    # Category groups
    for cat in ${CATEGORIES}; do
        echo "    ${cat}:"
        echo "      hosts:"
        echo "${HOSTS_JSON}" | jq -c --arg cat "${cat}" '.[] | select((.category // "uncategorized") == $cat)' | while IFS= read -r entry; do
            emit_host_block "${entry}"
        done
    done
    # Parent groups (pve_node → its children declared with `parent: <node>`)
    for node in ${PVE_NODES}; do
        children_json=$(echo "${HOSTS_JSON}" | jq -c --arg node "${node}" '[.[] | select(.parent == $node)]')
        count=$(echo "${children_json}" | jq 'length')
        [ "${count}" -gt 0 ] || continue
        echo "    pve_children_${node}:"
        echo "      hosts:"
        echo "${children_json}" | jq -c '.[]' | while IFS= read -r entry; do
            emit_host_block "${entry}"
        done
    done
} > "${INV_FILE}"

# 5) ansible.cfg
cat > "${CFG_FILE}" <<ANSCFG
[defaults]
inventory = ${INV_FILE}
host_key_checking = False
forks = 16
gathering = explicit
remote_tmp = /tmp/.ansible-${USER:-root}/tmp
[ssh_connection]
pipelining = True
ANSCFG

# 6) Categories + chips + parents map for the collector
echo "${HOSTS_JSON}" | jq 'map({(.name): (.category // "uncategorized")}) | add' > "${INV_DIR}/categories.json"
echo "${HOSTS_JSON}" | jq 'map(select(.chip)   | {(.name): .chip})   | add // {}' > "${INV_DIR}/chips.json"
echo "${HOSTS_JSON}" | jq 'map(select(.parent) | {(.name): .parent}) | add // {}' > "${INV_DIR}/parents.json"

bashio::log.info "Bootstrap complete. Inventory: ${INV_FILE}"
bashio::log.info "Hosts configured: $(echo "${HOSTS_JSON}" | jq 'length')"
bashio::log.info "Categories: $(echo "${CATEGORIES}" | tr '\n' ' ')"
bashio::log.info "PVE nodes:  $(echo "${PVE_NODES}"  | tr '\n' ' ')"
exit 0
