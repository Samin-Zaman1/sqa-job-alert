# Registers a Windows scheduled task that runs the SQA job check every 30 minutes (or -IntervalMinutes).
# -FirstRun "21:00" starts the repetition at that time of day (today, or tomorrow if it has passed),
# e.g. -IntervalMinutes 720 -FirstRun "09:00" checks at 9 AM and 9 PM.
# Remove it with:  Unregister-ScheduledTask -TaskName "SQA Job Alert" -Confirm:$false
param([int]$IntervalMinutes = 30, [string]$FirstRun = "")

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path (Split-Path -Parent (py -c "import sys; print(sys.executable)")) "pythonw.exe"

if ($FirstRun) {
    $start = [datetime]::Today.Add([timespan]::Parse($FirstRun))
    while ($start -lt (Get-Date)) { $start = $start.AddMinutes($IntervalMinutes) }
} else {
    $start = (Get-Date).AddMinutes(1)
}

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$dir\sqa_jobs.py`"" -WorkingDirectory $dir
$trigger  = New-ScheduledTaskTrigger -Once -At $start `
              -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
              -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
              -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "SQA Job Alert" -Action $action -Trigger $trigger `
  -Settings $settings -Description "Checks Bdjobs and LinkedIn for new SQA jobs in Bangladesh" -Force |
  Select-Object TaskName, State
