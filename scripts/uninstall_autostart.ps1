# Stops and removes the "UniAgent" scheduled task. Leaves all files and data untouched.
$taskName = "UniAgent"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "Removed scheduled task '$taskName'."
} else {
    Write-Output "No scheduled task '$taskName' found."
}
# pythonw processes started by the task keep running until stopped; end any Uni Agent still running:
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*-m uni_agent*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Output "Stopped Uni Agent (PID $($_.ProcessId))." }
