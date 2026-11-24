[CmdletBinding()]
param([int]$Port = 8766, [switch]$Foreground)
$ErrorActionPreference = 'Stop'
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Port must be 1024-65535.' }
$webPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $webPython)) { throw 'Run Install-CloudAgentWeb.ps1 first.' }
$webConfig = Join-Path $PSScriptRoot 'agent.toml'
$webUrl = "http://127.0.0.1:$Port"
$existing = $null
try { $existing = Invoke-RestMethod -Uri "$webUrl/health" -TimeoutSec 2 } catch { }
if ($existing) {
    if ($existing.service -eq 'cloud-agent-assistant-web') { Write-Host "Already running: $webUrl"; return }
    throw "Port $Port is already in use by another service."
}
$webState = Join-Path $PSScriptRoot '.state'
New-Item -ItemType Directory -Path $webState -Force | Out-Null
$webArguments = @('-X', 'utf8', '-u', '-m', 'cloud_agent_assistant_web', '--config', $webConfig, '--port', "$Port")
if ($Foreground) {
    & $webPython @webArguments >> (Join-Path $webState 'service.out.log') 2>> (Join-Path $webState 'service.err.log')
    if ($LASTEXITCODE -ne 0) { throw "Web service exited with code $LASTEXITCODE." }
    return
}
# Start-Process needs explicit Windows quoting for paths with spaces.
$webArgumentLine = '-X utf8 -u -m cloud_agent_assistant_web --config "' + $webConfig + '" --port ' + $Port
$webProcess = Start-Process -FilePath $webPython -ArgumentList $webArgumentLine -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $webState 'service.out.log') -RedirectStandardError (Join-Path $webState 'service.err.log')
for ($webAttempt = 0; $webAttempt -lt 40; $webAttempt++) {
    Start-Sleep -Milliseconds 500
    if ($webProcess.HasExited) { throw "Service could not start. See $webState\service.err.log" }
    try {
        $webHealth = Invoke-RestMethod -Uri "$webUrl/health" -TimeoutSec 2
        if ($webHealth.service -eq 'cloud-agent-assistant-web') { Write-Host "Running: $webUrl (PID $($webHealth.pid))"; return }
    } catch { }
}
throw "Startup did not complete. See $webState\service.err.log"
