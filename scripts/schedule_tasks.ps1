# Register the TradeAI daily jobs in Windows Task Scheduler.
#
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_tasks.ps1          # register
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_tasks.ps1 -List    # show
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_tasks.ps1 -Remove  # unregister
#
# Jobs (Mon-Fri):
#   09:15  premarket.py --send   premarket briefing -> Feishu
#   15:35  daily_update.py       closing snapshot bars + full-market discovery
#   18:00  daily_review.py       daily review (with AI narrative)
#
# Each run appends stdout/stderr to data\logs\<job>.log.
param(
    [switch]$List,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\python.exe"
$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$jobs = @(
    @{ Name = "TradeAI-Premarket";   Time = "09:15"; Script = "scripts\premarket.py";    Args = "--send" },
    @{ Name = "TradeAI-DailyUpdate"; Time = "15:35"; Script = "scripts\daily_update.py"; Args = "" },
    @{ Name = "TradeAI-DailyReview"; Time = "18:00"; Script = "scripts\daily_review.py"; Args = "" }
)

function Get-TaskState($name) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) { return "not-registered" }
    return $task.State
}

if ($List) {
    foreach ($job in $jobs) {
        $info = Get-ScheduledTaskInfo -TaskName $job.Name -ErrorAction SilentlyContinue
        $last = if ($info) { $info.LastRunTime } else { "-" }
        $result = if ($info) { $info.LastTaskResult } else { "-" }
        "{0,-20} {1,-6} {2,-16} last={3} result={4}" -f $job.Name, $job.Time, (Get-TaskState $job.Name), $last, $result
    }
    exit 0
}

if ($Remove) {
    foreach ($job in $jobs) {
        if (Get-ScheduledTask -TaskName $job.Name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $job.Name -Confirm:$false
            "removed $($job.Name)"
        }
    }
    exit 0
}

foreach ($job in $jobs) {
    $scriptPath = Join-Path $root $job.Script
    $logPath = Join-Path $logDir ("{0}.log" -f $job.Name.ToLower())
    if (-not (Test-Path $scriptPath)) { throw "missing script: $scriptPath" }
    if (-not (Test-Path $python)) { throw "missing python: $python" }

    # cmd.exe keeps the redirection simple and gives us one log per job.
    $arguments = '/c ""{0}" "{1}" {2} >> "{3}" 2>&1"' -f $python, $scriptPath, $job.Args, $logPath
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $job.Time
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 45) -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $job.Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    "registered {0} (Mon-Fri {1})" -f $job.Name, $job.Time
}

""
"Notes: tasks run only while the user is logged on; keep the PC awake at these times."
"Premarket needs FEISHU_WEBHOOK in .env; review needs DEEPSEEK_API_KEY. Logs: data\logs\."
