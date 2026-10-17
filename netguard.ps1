param(
  [Parameter(Position=0)]
  [ValidateSet("Run","Install","Uninstall","Stop","Status","Start")]
  [string]$Action = "Run"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Python = if (Get-Command python3 -ErrorAction SilentlyContinue) { "python3" } elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "python3" }
$TaskName = "ClassroomNetguard"
$LogPath = Join-Path $ScriptDir "netguard.log"

# Якщо поряд лежить зібраний PyInstaller виконуваний файл netguard.exe,
# використовуємо його замість запуску netguard.py через python.
$Frozen = Join-Path $ScriptDir "netguard.exe"
if (Test-Path $Frozen) {
  $NetguardExe = $Frozen
  $NetguardArgs = ""
} else {
  $NetguardExe = $Python
  $NetguardArgs = "`"$ScriptDir\netguard.py`""
}

function Test-Admin {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object Security.Principal.WindowsPrincipal($identity)
  return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Run-Netguard {
  Write-Host "Запуск netguard у поточному вікні..."
  Push-Location $ScriptDir
  try {
    if ($NetguardArgs) {
      & $NetguardExe $ScriptDir\netguard.py 2>&1
    } else {
      & $NetguardExe 2>&1
    }
  } finally {
    Pop-Location
  }
}

function Start-Background {
  if (Get-Process | Where-Object { $_.ProcessName -like "*netguard*" }) {
    Write-Host "netguard уже працює."
    return
  }
  Push-Location $ScriptDir
  try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $NetguardExe
    $psi.Arguments = $NetguardArgs
    $psi.WorkingDirectory = $ScriptDir
    $psi.WindowStyle = "Hidden"
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError = $false
    [System.Diagnostics.Process]::Start($psi) | Out-Null
    Write-Host "netguard запущено в фоні."
  } finally {
    Pop-Location
  }
}

function Stop-Background {
  Get-Process | Where-Object { $_.ProcessName -like "*netguard*" } | Stop-Process -Force -ErrorAction SilentlyContinue
  Write-Host "Зупинено (якщо було запущено)."
}

function Get-Status {
  if (Get-Process | Where-Object { $_.ProcessName -like "*netguard*" }) {
    Write-Host "netguard працює."
  } else {
    Write-Host "netguard не працює."
  }
}

function Install-Autostart {
  if (-not (Test-Admin)) {
    Write-Error "Потрібні права адміністратора. Запустіть PowerShell як адміністратор."
    exit 1
  }
  # Встановлюємо заплановане завдання: при вході з найвищими правами
  $action = New-ScheduledTaskAction -Execute $NetguardExe -Argument $NetguardArgs -WorkingDirectory $ScriptDir
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0 -Minutes 0)
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Classroom netguard (block/unblock internet)" | Out-Null
  Write-Host "Автозапуск netguard встановлено як Scheduled Task '$TaskName' (SYSTEM, Highest privileges)."
  Write-Host "Відкриття журналу: type $LogPath у cmd/PowerShell (якщо логування додаватиметься)."
}

function Uninstall-Autostart {
  if (-not (Test-Admin)) {
    Write-Error "Потрібні права адміністратора. Запустіть PowerShell як адміністратор."
    exit 1
  }
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Автозапуск netguard видалено."
}

switch ($Action) {
  "Run" { Run-Netguard }
  "Start" { Start-Background }
  "Stop" { Stop-Background }
  "Status" { Get-Status }
  "Install" { Install-Autostart }
  "Uninstall" { Uninstall-Autostart }
  default { Write-Error "Невідома дія: $Action" }
}
