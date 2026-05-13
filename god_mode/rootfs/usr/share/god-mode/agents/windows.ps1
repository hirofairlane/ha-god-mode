# =====================================================================
#  windows.ps1 - GOD Mode agent for Windows 10/11
#
#  Requires PowerShell 5.1+ (preinstalled). Runs over OpenSSH-Server
#  which must be installed on the target:
#    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
#    Start-Service sshd; Set-Service -Name sshd -StartupType Automatic
#
#  Emits one line of JSON, same shape as the linux/macos/busybox agents.
#  Install path: C:\ProgramData\god-mode\god-agent.ps1
#  Invoked from OpenSSH-Server with: powershell -NoProfile -File C:\ProgramData\god-mode\god-agent.ps1
# =====================================================================
$ErrorActionPreference = 'SilentlyContinue'

$host_name = $env:COMPUTERNAME
$kernel    = "$([System.Environment]::OSVersion.Version) Windows"
$ts        = [int][double]::Parse((Get-Date -UFormat %s))

# Uptime
$uptime_s = [int]((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalSeconds

# Load 1m (Windows has no loadavg; use CPU queue length as proxy)
$queue = (Get-Counter '\System\Processor Queue Length' -SampleInterval 1 -MaxSamples 1).CounterSamples.CookedValue
$load1 = [math]::Round($queue, 2)

# CPU %
$cpu = (Get-Counter '\Processor(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 1).CounterSamples.CookedValue
$cpu_pct = [math]::Round($cpu, 1)

# Memory
$os = Get-CimInstance Win32_OperatingSystem
$mem_total_mb = [math]::Round($os.TotalVisibleMemorySize / 1024)
$mem_free_mb  = [math]::Round($os.FreePhysicalMemory / 1024)
$mem_used_mb  = $mem_total_mb - $mem_free_mb
$mem_pct      = if ($mem_total_mb -gt 0) { [math]::Round($mem_used_mb * 100.0 / $mem_total_mb, 1) } else { 0 }

# Disk
$drives = Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3"
$disk_root = 0
$disk_max  = 0
foreach ($d in $drives) {
    if ($d.Size -gt 0) {
        $used_pct = [math]::Round(($d.Size - $d.FreeSpace) * 100.0 / $d.Size)
        if ($d.DeviceID -eq 'C:') { $disk_root = $used_pct }
        if ($used_pct -gt $disk_max) { $disk_max = $used_pct }
    }
}

# Temperature (OpenHardwareMonitor or LibreHardwareMonitor required; fallback to MSAcpi_ThermalZoneTemperature)
$temp_max = 0
try {
    $tz = Get-CimInstance -Namespace "root/wmi" -Class "MSAcpi_ThermalZoneTemperature"
    if ($tz) {
        $kelvin_tenths = ($tz.CurrentTemperature | Measure-Object -Maximum).Maximum
        $temp_max = [int](($kelvin_tenths / 10.0) - 273.15)
    }
} catch { }

# Updates pending via WindowsUpdate COM
$updates = 0
try {
    $session  = New-Object -ComObject Microsoft.Update.Session
    $searcher = $session.CreateUpdateSearcher()
    $result   = $searcher.Search("IsInstalled=0 and Type='Software'")
    $updates  = $result.Updates.Count
} catch { }

$htype = "bare"
$pm = Get-CimInstance Win32_ComputerSystem
if ($pm.Model -match 'Virtual') { $htype = "vm" }

# Swap (pagefile)
$pagefile = Get-CimInstance Win32_PageFileUsage
$swap_total_mb = 0
$swap_used_mb  = 0
foreach ($p in $pagefile) {
    $swap_total_mb += [int]$p.AllocatedBaseSize
    $swap_used_mb  += [int]$p.CurrentUsage
}
$swap_pct = if ($swap_total_mb -gt 0) { [math]::Round($swap_used_mb * 100.0 / $swap_total_mb, 1) } else { 0 }

# SMART via PhysicalDisk + StorageReliabilityCounter
$smart_health        = "unknown"
$smart_disks_count   = 0
$smart_failed_disks  = 0
$smart_temp_max      = 0
$smart_reallocated_max = 0
try {
    $disks = Get-PhysicalDisk
    if ($disks) {
        $smart_health = "PASSED"
        foreach ($d in $disks) {
            $smart_disks_count++
            if ($d.HealthStatus -ne 'Healthy') {
                $smart_failed_disks++
                $smart_health = "FAILED"
            }
            try {
                $rel = Get-StorageReliabilityCounter -PhysicalDisk $d -ErrorAction Stop
                if ($rel.Temperature -gt $smart_temp_max) { $smart_temp_max = [int]$rel.Temperature }
                if ($rel.ReadErrorsCorrected -gt $smart_reallocated_max) {
                    $smart_reallocated_max = [int]$rel.ReadErrorsCorrected
                }
            } catch { }
        }
    }
} catch { }

# Output single-line JSON
$json = @{
    host                  = $host_name
    htype                 = $htype
    kernel                = $kernel
    ts                    = $ts
    uptime_s              = $uptime_s
    load_1m               = $load1
    cpu_pct               = $cpu_pct
    mem_pct               = $mem_pct
    mem_total_mb          = $mem_total_mb
    mem_used_mb           = $mem_used_mb
    swap_pct              = $swap_pct
    swap_total_mb         = $swap_total_mb
    disk_root_pct         = $disk_root
    disk_max_pct          = $disk_max
    temp_max_c            = $temp_max
    smart_health          = $smart_health
    smart_disks_count     = $smart_disks_count
    smart_failed_disks    = $smart_failed_disks
    smart_temp_max        = $smart_temp_max
    smart_reallocated_max = $smart_reallocated_max
    updates_pending       = $updates
} | ConvertTo-Json -Compress
Write-Output $json
