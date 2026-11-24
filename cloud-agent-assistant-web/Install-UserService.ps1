[CmdletBinding()]
param([int]$Port = 8766)
$ErrorActionPreference = 'Stop'
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Port must be 1024-65535.' }
$webTaskName = 'cloud-agent-assistant-web'
$webExistingTask = Get-ScheduledTask -TaskName $webTaskName -ErrorAction SilentlyContinue
if ($webExistingTask -and $webExistingTask.Description -notlike 'Cloud Agent Assistant Web*') { throw 'A different scheduled task already uses this name.' }
$webUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$webPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$webLauncher = Join-Path $PSScriptRoot 'Start-CloudAgentWeb.ps1'
$webAction = New-ScheduledTaskAction -Execute $webPowerShell -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $webLauncher + '" -Foreground -Port ' + $Port) -WorkingDirectory $PSScriptRoot
$webTrigger = New-ScheduledTaskTrigger -AtLogOn -User $webUser
$webPrincipal = New-ScheduledTaskPrincipal -UserId $webUser -LogonType Interactive -RunLevel Limited
$webSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $webTaskName -Action $webAction -Trigger $webTrigger -Principal $webPrincipal -Settings $webSettings -Description 'Cloud Agent Assistant Web user service. Runs the OCI browser agent under the signed-in user; no Windows password is stored.' -Force | Out-Null
Start-ScheduledTask -TaskName $webTaskName
Write-Host "Installed background user service. Browser: http://127.0.0.1:$Port"
