<#
.SYNOPSIS
  Installs the IngatMaintain monitoring agent on this computer.

.DESCRIPTION
  1. Enrolls this computer with the server using a one-time enrollment key.
  2. Saves the device token to C:\ProgramData\IngatMaintain (readable by SYSTEM and Administrators only).
  3. Creates the "IngatMaintain Agent" scheduled task, running agent.ps1 as SYSTEM every few minutes.

  Run from an elevated PowerShell prompt:
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Server https://ingatmaintain.example.com -EnrollKey <key>

  To uninstall:
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -Uninstall

  agent.ps1 is downloaded from the server unless it sits next to this script
  (for sites where you copy both files over on the secure drive).
#>
param(
    [string]$Server,
    [string]$EnrollKey,
    [switch]$Uninstall,
    [string]$ConfigDir = $(if ($env:ProgramData) { Join-Path $env:ProgramData 'IngatMaintain' } else { '/etc/ingatmaintain' })
)

$ErrorActionPreference = 'Stop'
$TaskName = 'IngatMaintain Agent'
$OnWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or ($IsWindows -eq $true)
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

if ($Uninstall) {
    if ($OnWindows) { schtasks.exe /Delete /TN $TaskName /F 2>$null | Out-Null }
    if (Test-Path $ConfigDir) { Remove-Item -Recurse -Force $ConfigDir }
    Write-Host 'IngatMaintain agent removed. Also click "Remove agent" for this computer in IngatMaintain.'
    return
}

if (-not $Server -or -not $EnrollKey) { throw 'Usage: install.ps1 -Server <https://...> -EnrollKey <key>' }
$Server = $Server.TrimEnd('/')
if ($Server -notmatch '^https://' -and $Server -notmatch '^http://(localhost|127\.0\.0\.1)') {
    throw 'The server address must start with https:// so the device token is never sent unencrypted.'
}
if ($OnWindows) {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated (Run as administrator) PowerShell prompt.'
    }
}

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$agentPath = Join-Path $ConfigDir 'agent.ps1'
$localAgent = if ($PSScriptRoot) { Join-Path $PSScriptRoot 'agent.ps1' } else { '' }
if ($localAgent -and (Test-Path $localAgent)) {
    if ((Resolve-Path $localAgent).Path -ne $agentPath) { Copy-Item $localAgent $agentPath -Force }
} else {
    Invoke-WebRequest -Uri "$Server/api/agent/agent.ps1" -OutFile $agentPath -UseBasicParsing
}

$hostname = [System.Net.Dns]::GetHostName()
$body = @{ enroll_key = $EnrollKey; hostname = $hostname; os = [System.Environment]::OSVersion.VersionString } | ConvertTo-Json -Compress
try {
    $result = Invoke-RestMethod -Method Post -Uri "$Server/api/agent/enroll" -ContentType 'application/json' -Body $body -TimeoutSec 30 -UseBasicParsing
} catch {
    throw "Enrollment failed: $($_.Exception.Message). Check the server address and that the key hasn't expired."
}

$config = [ordered]@{
    server     = $Server
    token      = $result.device_token
    services   = @(@($result.config.services) | Where-Object { $_ })
    cert_paths = @(@($result.config.cert_paths) | Where-Object { $_ })
    software   = @(@($result.config.software) | Where-Object { $_ })
}
$configFile = Join-Path $ConfigDir 'agent.json'
$config | ConvertTo-Json -Depth 4 | Set-Content -Path $configFile -Encoding UTF8

if ($OnWindows) {
    # Only SYSTEM and Administrators may read the token.
    icacls.exe $ConfigDir /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' | Out-Null
    $minutes = [int]$result.config.interval_minutes
    if ($minutes -lt 1) { $minutes = 5 }
    $command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$agentPath`""
    schtasks.exe /Create /TN $TaskName /SC MINUTE /MO $minutes /RU SYSTEM /RL HIGHEST /TR $command /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the scheduled task.' }
    schtasks.exe /Run /TN $TaskName | Out-Null
    Write-Host "Enrolled as '$($result.machine)'. The agent reports every $minutes minutes - check the Fleet page in IngatMaintain."
} else {
    Write-Host "Enrolled as '$($result.machine)'. Schedule this to run every few minutes (e.g. cron):"
    Write-Host "  pwsh -NoProfile -File $agentPath -ConfigDir $ConfigDir"
}
