<#
.SYNOPSIS
  IngatMaintain monitoring agent. Sends a health report to the IngatMaintain server.

.DESCRIPTION
  Run every few minutes by the "IngatMaintain Agent" scheduled task (see install.ps1).
  It only makes outgoing HTTPS requests to the server; nothing listens on this computer
  and the server cannot run commands here.

  Reports: hostname, OS, uptime, IP addresses, memory, fixed-disk space, the status of
  the services the server asks about, the expiry date of the certificate files the server
  asks about (public certificate only - private keys are never read), and the installed
  version of the tracked software.

  Compatible with Windows PowerShell 5.1 and PowerShell 7.
#>
param(
    [string]$ConfigDir = $(if ($env:ProgramData) { Join-Path $env:ProgramData 'IngatMaintain' } else { '/etc/ingatmaintain' })
)

$ErrorActionPreference = 'Stop'
$AgentVersion = '1.1.0'
$ConfigFile = Join-Path $ConfigDir 'agent.json'
$LogFile = Join-Path $ConfigDir 'agent.log'
$OnWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or ($IsWindows -eq $true)

# Windows PowerShell 5.1 may default to TLS 1.0.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

function Write-Log([string]$Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss} {1}' -f (Get-Date), $Message
    # Keep the log small: last ~200 lines.
    $lines = @()
    if (Test-Path $LogFile) { $lines = @(Get-Content $LogFile -Tail 199) }
    $lines += $line
    Set-Content -Path $LogFile -Value $lines -Encoding UTF8
}

function Round-GB($bytes) { [math]::Round([double]$bytes / 1GB, 2) }

function Get-Disks {
    $disks = @()
    foreach ($d in [System.IO.DriveInfo]::GetDrives()) {
        try {
            if ($d.DriveType -ne 'Fixed' -or -not $d.IsReady) { continue }
            if (-not $OnWindows -and $d.Name -notmatch '^/(home|var|opt|srv)?$') { continue }
            $disks += @{ name = $d.Name.TrimEnd('\'); total_gb = (Round-GB $d.TotalSize); free_gb = (Round-GB $d.AvailableFreeSpace) }
        } catch { }
    }
    , $disks
}

function Get-SystemInfo {
    $info = @{ os = ''; uptime_hours = $null; memory = @{ total_gb = $null; free_gb = $null } }
    if ($OnWindows) {
        $os = Get-CimInstance Win32_OperatingSystem
        $info.os = "$($os.Caption) $($os.Version)".Trim()
        $info.uptime_hours = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1)
        $info.memory = @{ total_gb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2); free_gb = [math]::Round($os.FreePhysicalMemory / 1MB, 2) }
    } else {
        $info.os = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
        if (Test-Path /proc/uptime) {
            $info.uptime_hours = [math]::Round([double]((Get-Content /proc/uptime -Raw).Split(' ')[0]) / 3600, 1)
        }
        if (Test-Path /proc/meminfo) {
            $mem = @{}
            foreach ($l in Get-Content /proc/meminfo) { if ($l -match '^(\w+):\s+(\d+)') { $mem[$Matches[1]] = [double]$Matches[2] } }
            $info.memory = @{ total_gb = [math]::Round($mem['MemTotal'] / 1MB, 2); free_gb = [math]::Round($mem['MemAvailable'] / 1MB, 2) }
        }
    }
    $info
}

function Get-IPs {
    $ips = @()
    foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($nic.OperationalStatus -ne 'Up' -or $nic.NetworkInterfaceType -eq 'Loopback') { continue }
        foreach ($a in $nic.GetIPProperties().UnicastAddresses) {
            if ($a.Address.AddressFamily -eq 'InterNetwork') { $ips += $a.Address.ToString() }
        }
    }
    , $ips
}

function Get-ServiceStates($names) {
    $result = @()
    foreach ($name in $names) {
        $status = 'NotFound'
        if ($OnWindows) {
            $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
            if ($svc) { $status = $svc.Status.ToString() }
        } elseif (Get-Command systemctl -ErrorAction SilentlyContinue) {
            $state = (& systemctl is-active $name 2>$null | Select-Object -First 1)
            if ($state -and $state -ne 'unknown' -and $state -ne 'inactive') { $status = $state }
            elseif ($state -eq 'inactive') { $status = 'Stopped' }
        }
        $result += @{ name = $name; status = $status }
    }
    , $result
}

function Get-CertInfo($paths) {
    $result = @()
    foreach ($path in $paths) {
        $entry = @{ path = $path; subject = ''; not_after = ''; error = '' }
        try {
            if (-not (Test-Path -LiteralPath $path)) { throw 'file not found' }
            $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2 -ArgumentList (Resolve-Path -LiteralPath $path).Path
            $entry.subject = $cert.Subject
            $entry.not_after = $cert.NotAfter.ToString('yyyy-MM-dd')
        } catch {
            $entry.error = $_.Exception.Message
        }
        $result += $entry
    }
    , $result
}

function Get-SoftwareVersions($names) {
    $found = @{}
    if (-not $OnWindows -or -not $names) { return $found }
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    $installed = @(Get-ItemProperty -Path $keys -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -and $_.DisplayVersion })
    foreach ($name in $names) {
        $match = $installed | Where-Object { $_.DisplayName -like "*$name*" } | Select-Object -First 1
        if ($match) { $found[$name] = [string]$match.DisplayVersion }
    }
    $found
}

# Drive health from Windows' own storage stack (Windows 8 / Server 2012 and later).
# Counters a drive doesn't support come back as $null (or 0 for temperature) and are left out.
function Get-StorageHealth {
    $drives = @()
    if (-not $OnWindows -or -not (Get-Command Get-PhysicalDisk -ErrorAction SilentlyContinue)) { return , $drives }
    foreach ($pd in @(Get-PhysicalDisk -ErrorAction SilentlyContinue)) {
        $rc = $null
        try { $rc = $pd | Get-StorageReliabilityCounter -ErrorAction Stop } catch { }
        $health = switch ("$($pd.HealthStatus)") {
            'Healthy' { 'OK' } 'Warning' { 'Warning' } 'Unhealthy' { 'Failing' } default { 'Unknown' }
        }
        $bus = "$($pd.BusType)"
        $type = if ($bus -eq 'NVMe') { 'NVMe' } elseif ($bus -eq 'SD' -or $bus -eq 'MMC') { 'SD card' } `
                elseif ($bus -eq 'USB') { 'USB' } elseif ("$($pd.MediaType)" -eq 'HDD') { 'HDD' } else { 'SSD' }
        $wear = $null; $temp = $null; $hours = $null; $errors = $null
        if ($rc) {
            if ($null -ne $rc.Wear) { $wear = [int]$rc.Wear }
            if ($rc.Temperature) { $temp = [int]$rc.Temperature }
            if ($null -ne $rc.PowerOnHours) { $hours = [int64]$rc.PowerOnHours }
            if ($null -ne $rc.ReadErrorsUncorrected -or $null -ne $rc.WriteErrorsUncorrected) {
                $errors = [int64]$rc.ReadErrorsUncorrected + [int64]$rc.WriteErrorsUncorrected
            }
        }
        $notes = ''
        if ("$($pd.OperationalStatus)" -ne 'OK') { $notes = "Operational status: $($pd.OperationalStatus)" }
        if (-not $rc) { $notes = ($notes + ' No reliability counters from this drive/controller.').Trim() }
        $drives += @{
            name = "Disk $($pd.DeviceId)"; model = "$($pd.FriendlyName)".Trim(); type = $type; health = $health
            wear_pct = $wear; temp_c = $temp; power_on_hours = $hours; errors = $errors; eol = ''; notes = $notes
        }
    }
    , $drives
}

# Disk / file system errors in the System event log over the last 24 hours - the earliest sign
# of a failing drive or card, including ones that report no health data.
function Get-StorageEvents {
    $result = @{ count = 0; sample = ''; window_hours = 24 }
    if (-not $OnWindows) { return $result }
    $since = (Get-Date).AddHours(-24)
    $events = @()
    foreach ($provider in 'disk', 'Ntfs', 'Microsoft-Windows-Ntfs', 'stornvme', 'storahci', 'sdbus', 'volmgr') {
        try {
            $events += @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = $provider; Level = 1, 2, 3; StartTime = $since } -MaxEvents 100 -ErrorAction Stop)
        } catch { }  # "no events found" and unknown providers both land here
    }
    if ($events.Count -gt 0) {
        $latest = $events | Sort-Object TimeCreated -Descending | Select-Object -First 1
        $message = ("$($latest.Message)" -split "`r?`n")[0]
        $result.count = $events.Count
        $result.sample = "$($latest.ProviderName) event $($latest.Id): $message"
    }
    $result
}

# A clean string array from JSON: drops nulls/blanks (PowerShell 5.1 turns a missing list into @($null)).
function Get-List($value) {
    , @(@($value) | Where-Object { $_ -and "$_".Trim() } | ForEach-Object { "$_".Trim() })
}

function Send-Json([string]$Url, $Body, [string]$Token) {
    $headers = @{}
    if ($Token) { $headers['Authorization'] = "Bearer $Token" }
    $json = $Body | ConvertTo-Json -Depth 6 -Compress
    Invoke-RestMethod -Method Post -Uri $Url -Headers $headers -ContentType 'application/json; charset=utf-8' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 30 -UseBasicParsing
}

try {
    if (-not (Test-Path $ConfigFile)) { throw "No $ConfigFile - run install.ps1 first." }
    $config = Get-Content $ConfigFile -Raw | ConvertFrom-Json

    $sys = Get-SystemInfo
    $report = @{
        agent_version = $AgentVersion
        hostname      = [System.Net.Dns]::GetHostName()
        os            = $sys.os
        uptime_hours  = $sys.uptime_hours
        memory        = $sys.memory
        ips           = (Get-IPs)
        disks         = (Get-Disks)
        services      = (Get-ServiceStates (Get-List $config.services))
        certs         = (Get-CertInfo (Get-List $config.cert_paths))
        software      = (Get-SoftwareVersions (Get-List $config.software))
        storage       = (Get-StorageHealth)
        storage_events = (Get-StorageEvents)
        readonly_mounts = @()
    }

    $response = Send-Json -Url "$($config.server)/api/agent/checkin" -Body $report -Token $config.token

    # Remember what the server wants collected next time.
    foreach ($field in 'services', 'cert_paths', 'software') {
        $config | Add-Member -NotePropertyName $field -NotePropertyValue (Get-List $response.config.$field) -Force
    }
    $config | ConvertTo-Json -Depth 4 | Set-Content -Path $ConfigFile -Encoding UTF8
    Write-Log 'Check-in OK'
} catch {
    Write-Log "Check-in failed: $($_.Exception.Message)"
    exit 1
}
