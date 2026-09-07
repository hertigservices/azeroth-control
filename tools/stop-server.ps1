# Graceful shutdown that SAVES all players first.
#
# worldserver registers boost::asio signal_set(SIGINT, SIGTERM) + SIGBREAK -> World::StopNow,
# which runs the normal shutdown path (saves every online character). Stop-Process -Force
# skips all of that and loses everything since the last periodic save - which is exactly how
# a character position gets lost.
#
# Since worldserver runs detached with its own console, we attach to that console and raise
# CTRL_BREAK in it. Group 0 delivers to every process on the console including this one, so
# we disable our own handler first.
param(
  [int]$TimeoutSec = 240,
  [switch]$AuthToo
)
$ErrorActionPreference = 'Continue'

Add-Type -Namespace Win32 -Name Con -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool AttachConsole(uint dwProcessId);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool FreeConsole();
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);
[DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(IntPtr HandlerRoutine, bool Add);
'@

function Stop-Gracefully([System.Diagnostics.Process]$proc, [string]$label) {
  Write-Host "Sending CTRL_BREAK to $label (PID $($proc.Id)) ..."
  [void][Win32.Con]::FreeConsole()
  if (-not [Win32.Con]::AttachConsole([uint32]$proc.Id)) {
    Write-Host "  could not attach to its console" -ForegroundColor Yellow
    [void][Win32.Con]::FreeConsole(); return $false
  }
  # Ignore the event in our own process so we don't take ourselves down with it.
  [void][Win32.Con]::SetConsoleCtrlHandler([IntPtr]::Zero, $true)
  $sent = [Win32.Con]::GenerateConsoleCtrlEvent(1, 0)   # 1 = CTRL_BREAK_EVENT
  [void][Win32.Con]::FreeConsole()
  [void][Win32.Con]::SetConsoleCtrlHandler([IntPtr]::Zero, $false)
  if (-not $sent) { Write-Host "  GenerateConsoleCtrlEvent failed" -ForegroundColor Yellow; return $false }

  Write-Host "  waiting up to ${TimeoutSec}s for it to save and exit ..." -NoNewline
  if ($proc.WaitForExit($TimeoutSec * 1000)) { Write-Host " exited cleanly (code $($proc.ExitCode))"; return $true }
  Write-Host " STILL RUNNING" -ForegroundColor Red
  return $false
}

$world = Get-Process -Name worldserver -ErrorAction SilentlyContinue
if ($world) {
  if (-not (Stop-Gracefully $world 'worldserver')) {
    Write-Host "Graceful stop FAILED. Not force-killing - that would lose unsaved progress." -ForegroundColor Red
    Write-Host "Type 'saveall' then 'server shutdown 1' in the worldserver window, or re-run this script." -ForegroundColor Yellow
    exit 1
  }
} else { Write-Host "worldserver not running" }

if ($AuthToo) {
  $auth = Get-Process -Name authserver -ErrorAction SilentlyContinue
  if ($auth) { [void](Stop-Gracefully $auth 'authserver') } else { Write-Host "authserver not running" }
}

# A clean shutdown clears every characters.online flag; leftovers mean it didn't save.
$mysql = 'C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe'
$stillOnline = (& $mysql -h127.0.0.1 -uacore -pacore --batch --skip-column-names `
  -e "SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;" | Select-Object -Last 1)
Write-Host ""
if ("$stillOnline".Trim() -eq '0') {
  Write-Host "VERIFIED: all characters flushed and marked offline - save completed." -ForegroundColor Green
} else {
  Write-Host "WARNING: $stillOnline characters still flagged online - shutdown may not have saved." -ForegroundColor Yellow
}
