#!/bin/bash
# =====================================================================
#  macos.sh - GOD Mode agent for macOS (Big Sur 11+ recommended)
#
#  Uses BSD-style tools: sysctl, top, vm_stat, df, sysctl machdep,
#  ioreg for temperature on Apple Silicon, smc on Intel Macs.
#
#  Target path: /usr/local/bin/god-agent (if user is admin) or
#    $HOME/.local/bin/god-agent (regular user).
# =====================================================================
set -u

LC_ALL=C
export LC_ALL

host=$(hostname -s 2>/dev/null || hostname)
kernel=$(uname -r 2>/dev/null)
ts=$(date +%s 2>/dev/null)

# Uptime via sysctl
boot_ts=$(sysctl -n kern.boottime 2>/dev/null | awk -F'[ ,=]' '/sec/{print $4}')
if [ -n "${boot_ts:-}" ]; then
    uptime_s=$((ts - boot_ts))
else
    uptime_s=0
fi

# Load 1m
load1=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')
[ -z "${load1:-}" ] && load1=0

# CPU %: take 1-second top sample
cpu_line=$(top -l 2 -n 0 -s 1 2>/dev/null | grep -i '^CPU usage' | tail -1)
# Example: "CPU usage: 5.12% user, 3.06% sys, 91.81% idle"
cpu_user=$(echo "$cpu_line" | sed -E 's/.* ([0-9.]+)% user.*/\1/')
cpu_sys=$(echo  "$cpu_line" | sed -E 's/.* ([0-9.]+)% sys.*/\1/')
if [ -n "${cpu_user:-}" ] && [ -n "${cpu_sys:-}" ]; then
    cpu_pct=$(awk -v u="$cpu_user" -v s="$cpu_sys" 'BEGIN{printf "%.1f", u+s}')
else
    cpu_pct="0.0"
fi

# Memory: vm_stat + sysctl
page_size=$(sysctl -n hw.pagesize 2>/dev/null || echo 16384)
total_bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
vm_stat_out=$(vm_stat 2>/dev/null)
pages_free=$(echo "$vm_stat_out"     | awk '/Pages free:/{gsub("\\.","",$3); print $3}')
pages_active=$(echo "$vm_stat_out"   | awk '/Pages active:/{gsub("\\.","",$3); print $3}')
pages_inactive=$(echo "$vm_stat_out" | awk '/Pages inactive:/{gsub("\\.","",$3); print $3}')
pages_wired=$(echo "$vm_stat_out"    | awk '/Pages wired down:/{gsub("\\.","",$4); print $4}')
pages_speculative=$(echo "$vm_stat_out" | awk '/Pages speculative:/{gsub("\\.","",$3); print $3}')
pages_purgeable=$(echo "$vm_stat_out" | awk '/Pages purgeable:/{gsub("\\.","",$3); print $3}')
used_pages=$((${pages_active:-0} + ${pages_wired:-0}))
used_bytes=$((used_pages * page_size))
mem_total_mb=$((total_bytes / 1024 / 1024))
mem_used_mb=$((used_bytes / 1024 / 1024))
if [ "${mem_total_mb:-0}" -gt 0 ]; then
    mem_pct=$(awk -v u="$mem_used_mb" -v t="$mem_total_mb" 'BEGIN{printf "%.1f", u*100.0/t}')
else
    mem_pct=0
fi

# Disk root
disk_root=$(df -P / 2>/dev/null | awk 'NR==2{gsub("%","",$5); print $5}')
[ -z "${disk_root:-}" ] && disk_root=0
# Disk max across all real filesystems (skip devfs, autofs, tmpfs)
disk_max=$(df -P 2>/dev/null | awk 'NR>1 && $1 ~ /\/dev\// && $1 !~ /devfs/ {gsub("%","",$5); if($5+0>m)m=$5+0} END{print m+0}')
[ -z "${disk_max:-}" ] && disk_max=0

# Temperature - tricky on macOS. Apple Silicon has no easy SMC.
# Intel: try osx-cpu-temp if installed.
temp_max=0
if command -v osx-cpu-temp >/dev/null 2>&1; then
    t=$(osx-cpu-temp 2>/dev/null | sed -E 's/[^0-9.]//g')
    [ -n "$t" ] && temp_max=$(printf "%.0f" "$t" 2>/dev/null || echo 0)
fi
# Try powermetrics (requires sudo on AS, ignore if denied)
if [ "$temp_max" = "0" ] && command -v powermetrics >/dev/null 2>&1; then
    t=$(sudo -n powermetrics -n 1 -s smc 2>/dev/null | awk '/CPU die temperature/{print $4; exit}')
    [ -n "$t" ] && temp_max=$(printf "%.0f" "$t" 2>/dev/null || echo 0)
fi

# Updates pending (brew + macOS softwareupdate)
updates=0
if command -v brew >/dev/null 2>&1; then
    outdated=$(brew outdated --quiet 2>/dev/null | wc -l | tr -d ' ')
    updates=$((updates + ${outdated:-0}))
fi
if command -v softwareupdate >/dev/null 2>&1; then
    sw=$(softwareupdate -l 2>/dev/null | grep -c '^[[:space:]]*\* ')
    updates=$((updates + ${sw:-0}))
fi

# Host type
htype="bare"
sysctl -n machdep.cpu.brand_string 2>/dev/null | grep -qi 'apple' && htype="apple-silicon"
sysctl -n machdep.cpu.brand_string 2>/dev/null | grep -qi 'intel'  && htype="intel-mac"

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
