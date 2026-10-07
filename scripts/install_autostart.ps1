# Registers a per-user Task Scheduler task "UniAgent" that starts the agent hidden (pythonw) at logon.
# A 10-minute repetition relaunches it if it ever stops; the app's own lock prevents duplicates.
# Runs only while you're logged in, with normal (non-admin) rights, and stores no password.
# Undo with scripts\uninstall_autostart.ps1
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Not found: $pythonw (create the venv first)" }

$user = "$env:USERDOMAIN\$env:USERNAME"
$taskName = "UniAgent"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m uni_agent" -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "Uni Agent: Telegram study assistant (Moodle deadlines, lecture/lab summaries, quizzes)." | Out-Null

Write-Output "Registered scheduled task '$taskName' for $user."
Write-Output "Start it now with:  Start-ScheduledTask -TaskName $taskName"
