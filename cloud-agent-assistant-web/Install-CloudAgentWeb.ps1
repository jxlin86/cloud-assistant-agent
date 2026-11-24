[CmdletBinding()]
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$webConfig = Join-Path $PSScriptRoot 'agent.toml'
if (-not (Test-Path -LiteralPath $webConfig)) {
    $webExample = Join-Path $PSScriptRoot 'agent.example.toml'
    if (Test-Path -LiteralPath $webExample) { Copy-Item -LiteralPath $webExample -Destination $webConfig }
}
& $Python -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12 or newer is required'"
if ($LASTEXITCODE -ne 0) { throw 'Choose Python 3.12 or newer with -Python.' }
$webPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $webPython)) {
    & $Python -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 or newer is required.' }
}
$coreWheel = Join-Path $PSScriptRoot 'vendor\fde_oci_agent-0.1.4-py3-none-any.whl'
& $webPython -m pip install --disable-pip-version-check --cache-dir (Join-Path $PSScriptRoot '.state\pip-cache') -r (Join-Path $PSScriptRoot 'requirements.lock.txt') $coreWheel
if ($LASTEXITCODE -ne 0) { throw 'Installation failed. Review the message above.' }
& $webPython (Join-Path $PSScriptRoot 'install_app.py')
if ($LASTEXITCODE -ne 0) { throw 'Application installation/verification failed.' }
Write-Host 'Installed cloud-agent-assistant-web. Run .\Start-CloudAgentWeb.ps1'
