#!/usr/bin/env bash
# =====================================================================
#  god-agent.sh - emite JSON con metricas de host (Linux universal)
#  Path destino: /usr/local/bin/god-agent (chmod 755)
#  Llamado por LXC 104 (jarvis collector) via SSH cada 60s.
#
#  Output: una linea JSON con campos estandarizados.
#  No requiere sudo (todo de /proc, /sys, df, free, sensors si esta).
# =====================================================================
set -euo pipefail

LC_ALL=C export LC_ALL

host=$(hostname -s 2>/dev/null || hostname)
kernel=$(uname -r)
ts=$(date +%s)

# Uptime
uptime_s=$(awk '{print int($1)}' /proc/uptime)

# Load avg (1m)
load1=$(awk '{print $1}' /proc/loadavg)

# CPU % usage (snapshot, requiere doble lectura ~200ms aparte)
read_cpu() {
  awk '/^cpu /{u=$2+$4; t=$2+$3+$4+$5+$6+$7+$8; print u, t}' /proc/stat
}
read u1 t1 < <(read_cpu)
sleep 0.2
read u2 t2 < <(read_cpu)
if [ "$t2" -ne "$t1" ]; then
  cpu_pct=$(awk -v u1=$u1 -v u2=$u2 -v t1=$t1 -v t2=$t2 'BEGIN{printf "%.1f", (u2-u1)*100.0/(t2-t1)}')
else
  cpu_pct=0
fi

# Memoria
mem_total_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
mem_avail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
mem_used_kb=$((mem_total_kb - mem_avail_kb))
mem_pct=$(awk -v u=$mem_used_kb -v t=$mem_total_kb 'BEGIN{printf "%.1f", u*100.0/t}')

# Disco root
disk_root=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')

# Disco "mayor" (% mas alto entre filesystems reales)
disk_max=$(df -P --type=ext4 --type=xfs --type=btrfs --type=zfs 2>/dev/null | awk 'NR>1{gsub("%","",$5); if($5+0>m)m=$5+0} END{print m+0}')

# Temperatura: hwmon thermal zone si disponible
# Wildcard glob no expande -> path literal -> [ -r ] falla -> continue
# Algunos zones existen pero `cat` da "No data available" -> 2>/dev/null
temp_max=0
shopt -s nullglob 2>/dev/null || true
for tz in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$tz" ] || continue
  raw=$(cat "$tz" 2>/dev/null || echo 0)
  [ -z "$raw" ] && raw=0
  c=$(( raw / 1000 ))
  if [ "$c" -gt "$temp_max" ] && [ "$c" -lt 200 ]; then
    temp_max=$c
  fi
done

# Si esta `sensors` (lm-sensors), buscar valor mas alto Package/Tdie/Tctl
if command -v sensors >/dev/null 2>&1; then
  alt=$( { sensors -u 2>/dev/null || true; } | awk -F': *' 'BEGIN{m=0} /_input:/ {if($2+0>m && $2+0<200) m=$2+0} END{printf "%.0f", m+0}' || true )
  alt=${alt:-0}
  if [ "$alt" -gt "$temp_max" ] 2>/dev/null; then
    temp_max=$alt
  fi
fi

# Updates pendientes (apt / dnf / pacman). Usamos subshells con
# || true para evitar que set -e/pipefail mate el script si el gestor
# devuelve exit 1 cuando NO hay updates (caso pacman -Qu).
updates=0
if command -v apt-get >/dev/null 2>&1 && [ -d /var/lib/apt/lists ]; then
  updates=$( { apt-get -s upgrade 2>/dev/null || true; } | grep -c '^Inst' || true )
elif command -v pacman >/dev/null 2>&1; then
  updates=$( { pacman -Qu 2>/dev/null || true; } | wc -l )
elif command -v dnf >/dev/null 2>&1; then
  updates=$( { dnf check-update 2>/dev/null || true; } | grep -c '^[a-zA-Z]' || true )
fi
updates=${updates:-0}

# Swap
swap_total_kb=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo 2>/dev/null)
swap_free_kb=$(awk '/^SwapFree:/{print $2}' /proc/meminfo 2>/dev/null)
swap_total_kb=${swap_total_kb:-0}
swap_free_kb=${swap_free_kb:-0}
swap_total_mb=$((swap_total_kb / 1024))
if [ "$swap_total_kb" -gt 0 ]; then
    swap_used_kb=$((swap_total_kb - swap_free_kb))
    swap_pct=$(awk -v u=$swap_used_kb -v t=$swap_total_kb 'BEGIN{printf "%.1f", u*100.0/t}')
else
    swap_pct="0.0"
fi

# SMART data (best effort, requiere smartmontools + root)
smart_disks_count=0
smart_failed_disks=0
smart_temp_max=0
smart_reallocated_max=0
smart_health="unknown"
if command -v smartctl >/dev/null 2>&1; then
    smart_health="PASSED"
    # Iterar discos físicos (/dev/sd?, /dev/nvme?n?, etc)
    disks=$( { ls /dev/sd[a-z] /dev/nvme[0-9]n[0-9] 2>/dev/null || true; } | head -10)
    for d in $disks; do
        smart_disks_count=$((smart_disks_count + 1))
        h=$( { smartctl -H "$d" 2>/dev/null || true; } | awk '/SMART overall-health|SMART Health Status/{print $NF}' | head -1)
        if [ -n "$h" ] && [ "$h" != "PASSED" ] && [ "$h" != "OK" ]; then
            smart_failed_disks=$((smart_failed_disks + 1))
            smart_health="FAILED"
        fi
        # Temperatura (atributo 194 SATA, "Temperature_Celsius") o nvme "Temperature:"
        t=$( { smartctl -A "$d" 2>/dev/null || true; } | awk '/^194 |Temperature_Celsius|Temperature:/{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+$/ && $i+0 > 10 && $i+0 < 120) {print $i; exit}}' | head -1)
        if [ -n "$t" ] && [ "$t" -gt "$smart_temp_max" ] 2>/dev/null; then
            smart_temp_max=$t
        fi
        # Reallocated sector count (atributo 5 raw value)
        r=$( { smartctl -A "$d" 2>/dev/null || true; } | awk '/^  5 |Reallocated_Sector_Ct/{print $NF; exit}')
        r=${r:-0}
        if [ "$r" -gt "$smart_reallocated_max" ] 2>/dev/null; then
            smart_reallocated_max=$r
        fi
    done
fi

# Tipo de host (heuristico)
htype="unknown"
if [ -f /.dockerenv ]; then
  htype="docker"
elif [ -f /proc/1/environ ] && grep -qa "container=lxc" /proc/1/environ 2>/dev/null; then
  htype="lxc"
elif [ -d /proc/vz ]; then
  htype="openvz"
elif grep -q "^flags.*hypervisor" /proc/cpuinfo 2>/dev/null; then
  htype="vm"
else
  htype="bare"
fi

# Output JSON una linea
printf '{'
printf '"host":"%s",' "$host"
printf '"htype":"%s",' "$htype"
printf '"kernel":"%s",' "$kernel"
printf '"ts":%d,' "$ts"
printf '"uptime_s":%d,' "$uptime_s"
printf '"load_1m":%s,' "$load1"
printf '"cpu_pct":%s,' "$cpu_pct"
printf '"mem_pct":%s,' "$mem_pct"
printf '"mem_total_mb":%d,' "$((mem_total_kb / 1024))"
printf '"mem_used_mb":%d,' "$((mem_used_kb / 1024))"
printf '"swap_pct":%s,' "$swap_pct"
printf '"swap_total_mb":%d,' "$swap_total_mb"
printf '"disk_root_pct":%d,' "$disk_root"
printf '"disk_max_pct":%d,' "$disk_max"
printf '"temp_max_c":%d,' "$temp_max"
printf '"smart_health":"%s",' "$smart_health"
printf '"smart_disks_count":%d,' "$smart_disks_count"
printf '"smart_failed_disks":%d,' "$smart_failed_disks"
printf '"smart_temp_max":%d,' "$smart_temp_max"
printf '"smart_reallocated_max":%d,' "$smart_reallocated_max"
printf '"updates_pending":%d' "$updates"
printf '}\n'
