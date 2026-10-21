param(
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
    [Parameter(Mandatory = $true)][string]$PythonExecutable,
    [string]$TaskName = 'SPiRE Recorder'
)
$ErrorActionPreference = 'Stop'
$runtimePath = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
$pythonPath = (Resolve-Path -LiteralPath $PythonExecutable).Path
$pythonWindowless = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonWindowless)) {
    throw 'A deployed Python runtime with pythonw.exe is required.'
}
& $pythonPath -B -c 'import runtime_layer.recorder_cli' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw 'Install the SPiRE package in the selected Python runtime before registering the task.'
}
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    throw 'Task already exists. Stop and explicitly remove it before replacing the pinned runtime.'
}
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonWindowless `
    -Argument '-B -m runtime_layer.recorder_cli supervise --live' -WorkingDirectory $runtimePath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'SPiRE read-only recorder: hourly runs, near-sample gate, graceful stop and reconnect.'
$task.Settings.Priority = 7
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
