#!/bin/sh
# =====================================================================
#  busybox.sh - GOD Mode agent for OpenWrt / Alpine / busybox systems
#  Works on /bin/sh + busybox (no bash, no GNU coreutils flags).
#
#  Emits a single line of JSON with host metrics. Path on target:
#    /usr/bin/god-agent (for OpenWrt /usr/local/bin is not on PATH)
# =====================================================================
# DO NOT use `set -e` here: busybox builtins return non-zero in normal
# flows (eg: empty cat). We handle failures explicitly.

LC_ALL=C
export LC_ALL

host=$(uname -n 2>/dev/null)
[ -z "$host" ] && host="unknown"
kernel=$(uname -r 2>/dev/null)
ts=$(date +%s 2>/dev/null)

# Uptime (busybox awk is enough)
uptime_s=$(awk '{print int($1)}' /proc/uptime 2>/dev/null)
[ -z "$uptime_s" ] && uptime_s=0

# Load 1m
load1=$(awk '{print $1}' /proc/loadavg 2>/dev/null)
[ -z "$load1" ] && load1=0

# CPU % (delta from /proc/stat, two snapshots)
read_cpu() {
    awk '/^cpu /{u=$2+$4; t=$2+$3+$4+$5+$6+$7+$8; print u, t}' /proc/stat 2>/dev/null
}
set -- $(read_cpu)
u1=$1 t1=$2
sleep 1
set -- $(read_cpu)
u2=$1 t2=$2
if [ "${t2:-0}" -gt "${t1:-0}" ] 2>/dev/null; then
    cpu_pct=$(awk -v u1="$u1" -v u2="$u2" -v t1="$t1" -v t2="$t2" \
        'BEGIN{printf "%.1f", (u2-u1)*100.0/(t2-t1)}')
else
    cpu_pct="0.0"
fi

# Memory (busybox does have MemAvailable on recent kernels)
mem_total=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)
mem_avail=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)
[ -z "$mem_avail" ] && mem_avail=$(awk '/^MemFree:/{print $2}' /proc/meminfo 2>/dev/null)
mem_total=${mem_total:-1}
mem_avail=${mem_avail:-0}
mem_used=$((mem_total - mem_avail))
mem_pct=$(awk -v u="$mem_used" -v t="$mem_total" 'BEGIN{printf "%.1f", u*100.0/t}')
mem_total_mb=$((mem_total / 1024))
mem_used_mb=$((mem_used / 1024))

# Disk (root only, busybox df doesn't grok --type)
disk_root=$(df -P / 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}')
[ -z "$disk_root" ] && disk_root=0
disk_max=$(df -P 2>/dev/null | awk 'NR>1 && $1 !~ /(tmpfs|devtmpfs|squashfs|overlay)/ {gsub("%","",$5); if($5+0>m)m=$5+0} END{print m+0}')
[ -z "$disk_max" ] && disk_max=0

# Temperature (best effort, OpenWrt thermal_zone normally absent)
temp_max=0
for tz in /sys/class/thermal/thermal_zone*/temp; do
    [ -r "$tz" ] || continue
    raw=$(cat "$tz" 2>/dev/null)
    [ -z "$raw" ] && continue
    c=$((raw / 1000))
    if [ "$c" -gt "$temp_max" ] 2>/dev/null && [ "$c" -lt 200 ] 2>/dev/null; then
        temp_max=$c
    fi
done
# OpenWrt some boards: /sys/class/hwmon/hwmon*/temp1_input
for tz in /sys/class/hwmon/hwmon*/temp*_input; do
    [ -r "$tz" ] || continue
    raw=$(cat "$tz" 2>/dev/null)
    [ -z "$raw" ] && continue
    c=$((raw / 1000))
    if [ "$c" -gt "$temp_max" ] 2>/dev/null && [ "$c" -lt 200 ] 2>/dev/null; then
        temp_max=$c
    fi
done

# Updates (opkg supports list-upgradable; needs prior `opkg update` recently)
updates=0
if command -v opkg >/dev/null 2>&1; then
    updates=$(opkg list-upgradable 2>/dev/null | wc -l)
fi
updates=${updates:-0}

# Host type
htype="bare"
if [ -f /etc/openwrt_release ]; then
    htype="openwrt"
fi

printf '{'
printf '"host":"%s",' "$host"
printf '"htype":"%s",' "$htype"
printf '"kernel":"%s",' "$kernel"
printf '"ts":%s,' "${ts:-0}"
printf '"uptime_s":%s,' "$uptime_s"
printf '"load_1m":%s,' "$load1"
printf '"cpu_pct":%s,' "$cpu_pct"
printf '"mem_pct":%s,' "$mem_pct"
printf '"mem_total_mb":%s,' "$mem_total_mb"
printf '"mem_used_mb":%s,' "$mem_used_mb"
printf '"disk_root_pct":%s,' "$disk_root"
printf '"disk_max_pct":%s,' "$disk_max"
printf '"temp_max_c":%s,' "$temp_max"
printf '"updates_pending":%s' "$updates"
printf '}\n'
