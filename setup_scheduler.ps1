# AlpacaTrader — Task Scheduler Setup
# RIGHT-CLICK this file and select "Run with PowerShell" (as Administrator)

$taskName = "AlpacaTrader15min"
$batFile  = "E:\Dell Inspiron\P\Claude Code\AlpacaTrader\run_15min_bot.bat"

# Remove existing task if present
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batFile`""
$trigger  = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
              -RepetitionInterval (New-TimeSpan -Minutes 15) `
              -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
              -StartWhenAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
              -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest

Register-ScheduledTask `
  -TaskName  $taskName `
  -Action    $action `
  -Trigger   $trigger `
  -Settings  $settings `
  -Principal $principal `
  -Force

Write-Host ""
Write-Host "Task '$taskName' registered successfully." -ForegroundColor Green
Write-Host "Runs every 15 minutes. Check Task Scheduler to confirm." -ForegroundColor Cyan
Write-Host ""
Pause
