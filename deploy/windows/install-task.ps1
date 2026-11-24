# Run stoplog in the background on Windows: starts at logon, restarts if it crashes,
# no console window. Current user only; no admin needed.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1
# Stop:    Stop-ScheduledTask -TaskName stoplog
# Remove:  Unregister-ScheduledTask -TaskName stoplog -Confirm:$false
# Log:     Get-Content .stoplog\stoplog.log -Tail 30 -Wait
param(
    [string]$AppDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [string]$TaskName = "stoplog"
)

$py = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { throw "Python environment not found at $py. Create it first (see README)." }
$log = Join-Path $AppDir ".stoplog\stoplog.log"

$action = New-ScheduledTaskAction -Execute $py -Argument "-m stoplog --log-file `"$log`" watch" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "stoplog: photos of line stop sheets to a Google Sheet" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "stoplog is running in the background. Log: $log"
