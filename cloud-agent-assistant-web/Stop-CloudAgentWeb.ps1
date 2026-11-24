[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$webTask = Get-ScheduledTask -TaskName 'cloud-agent-assistant-web' -ErrorAction SilentlyContinue
if ($webTask -and $webTask.Description -like 'Cloud Agent Assistant Web*') { Stop-ScheduledTask -InputObject $webTask }
$webRuntime = Join-Path $PSScriptRoot '.state\runtime.json'
if (-not (Test-Path -LiteralPath $webRuntime)) { Write-Host 'Service is not running.'; return }
$webDetails = Get-Content -LiteralPath $webRuntime -Raw | ConvertFrom-Json
$webProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$webDetails.pid)" -ErrorAction SilentlyContinue
if ($webProcess) {
    if ($webProcess.CommandLine -notlike '*-m cloud_agent_assistant_web*' -or $webProcess.CommandLine -notlike "*$PSScriptRoot*") {
        throw 'The recorded PID belongs to a different process. Nothing was stopped.'
    }
    Stop-Process -Id ([int]$webDetails.pid)
}
Write-Host 'Cloud Agent Assistant Web stopped. Submitted OCI work may still be running.'
