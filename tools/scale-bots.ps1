# Scale bots and parallelise their AI across map threads, then measure the result.
#
# Why MapUpdate.Threads is the lever: mod-playerbots runs per-bot AI from
# PLAYERHOOK_ON_AFTER_UPDATE -> Player::Update -> Map::Update, i.e. the map threads.
# With MapUpdate.Threads=1 every bot on every map is processed on one core.
# Only RandomPlayerbotMgr::UpdateAI (bot rotation/teleport scheduling) is world-thread-bound.
param(
  [int]$Bots = 1000,
  [int]$MapThreads = 6
)
$ErrorActionPreference = 'Continue'
$log    = 'C:\AzerothCore\downloads\scale-bots.log'
$wConf  = 'C:\AzerothCore\server\configs\worldserver.conf'
$pbConf = 'C:\AzerothCore\server\configs\modules\playerbots.conf'
$srv    = 'C:\AzerothCore\server'
$srvLog = 'C:\AzerothCore\server\logs\Server.log'
$mysql  = 'C:\Program Files\MySQL\MySQL Server 8.4\bin\mysql.exe'

function Say($m) {
  $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m
  Write-Host $line; Add-Content -Path $log -Value $line -Encoding utf8
}
Set-Content -Path $log -Value "=== scale bots to $Bots / MapUpdate.Threads=$MapThreads ===" -Encoding utf8

function Set-Conf($file, $key, $value) {
  $pattern = '^\s*' + [regex]::Escape($key) + '\s*='
  $hit = $false
  $out = foreach ($l in (Get-Content $file)) {
    if (-not $hit -and $l -match $pattern) { $hit = $true; "$key = $value" } else { $l }
  }
  Set-Content -Path $file -Value $out -Encoding ascii
  Say ("  {0,-40} = {1}{2}" -f $key, $value, $(if(-not $hit){'  (KEY NOT FOUND)'}))
}

function Read-LogSafe($p) {
  if (-not (Test-Path $p)) { return '' }
  try { $fs=[IO.File]::Open($p,'Open','Read','ReadWrite'); $sr=New-Object IO.StreamReader($fs)
        $t=$sr.ReadToEnd(); $sr.Close(); $fs.Close(); return $t } catch { return '' }
}

Say "Applying config"
Set-Conf $wConf  'MapUpdate.Threads'          $MapThreads
Set-Conf $pbConf 'AiPlayerbot.MinRandomBots'  $Bots
Set-Conf $pbConf 'AiPlayerbot.MaxRandomBots'  $Bots

Say "Restarting worldserver (detached so it survives this script)"
Get-Process -Name worldserver -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 8
Remove-Item $srvLog -ErrorAction SilentlyContinue
if (-not (Get-Process -Name authserver -ErrorAction SilentlyContinue)) {
  Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "`"$srv\authserver.exe`""; CurrentDirectory = $srv } | Out-Null
}
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
  CommandLine = "`"$srv\worldserver.exe`""; CurrentDirectory = $srv }
Say "worldserver PID $($r.ProcessId) (create rc=$($r.ReturnValue))"

# Going 700 -> 1000 characters means provisioning new accounts/chars at startup.
$deadline = (Get-Date).AddMinutes(45)
while ((Get-Date) -lt $deadline) {
  if ((Read-LogSafe $srvLog) -match 'ready\.\.\.') { Say "worldserver ready"; break }
  Start-Sleep -Seconds 20
}

Say "Letting bots log in (5 min) ..."
Start-Sleep -Seconds 300

# --- Measure -------------------------------------------------------------
$p = Get-Process -Name worldserver -ErrorAction SilentlyContinue
if (-not $p) { Say "FATAL: worldserver not running"; Add-Content $log "SENTINEL_SCALE_FAILED"; exit 1 }

$t1 = @{}; foreach ($t in $p.Threads) { $t1[$t.Id] = $t.TotalProcessorTime }
$cpu1 = $p.TotalProcessorTime
Start-Sleep -Seconds 30
$p.Refresh()
$cpuTotalPct = (($p.TotalProcessorTime - $cpu1).TotalSeconds / 30) * 100

$rows = foreach ($t in $p.Threads) {
  if ($t1.ContainsKey($t.Id)) {
    $d = ($t.TotalProcessorTime - $t1[$t.Id]).TotalSeconds
    [pscustomobject]@{ Tid=$t.Id; CorePct=[math]::Round(($d/30)*100,1) }
  }
}
$busy = @($rows | Where-Object { $_.CorePct -gt 5 })

Say "--- per-thread CPU (100 = one saturated core) ---"
foreach ($r2 in ($rows | Sort-Object CorePct -Descending | Select-Object -First 10)) {
  Say ("   tid {0,-8} {1,6}%" -f $r2.Tid, $r2.CorePct)
}
Say ("Busy threads (>5%)   : {0}" -f $busy.Count)
Say ("Peak single thread   : {0}%" -f (($rows | Measure-Object CorePct -Maximum).Maximum))
Say ("Total CPU            : {0:N0}% of one core  ({1:N1}% of 12 logical)" -f $cpuTotalPct, ($cpuTotalPct/12))
Say ("worldserver RAM      : {0:N0} MB" -f ($p.WorkingSet64/1MB))
$os = Get-CimInstance Win32_OperatingSystem
Say ("System RAM free      : {0:N1} GB" -f ($os.FreePhysicalMemory/1MB))

# Bot counts straight from the DB (avoid PS stderr redirection quirks).
$onl = (& $mysql -h127.0.0.1 -uacore -pacore --batch --skip-column-names `
        -e "SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;")
$tot = (& $mysql -h127.0.0.1 -uacore -pacore --batch --skip-column-names `
        -e "SELECT COUNT(*) FROM acore_characters.characters;")
Say "Bots online / total  : $onl / $tot"

Say "=== DONE ==="
Add-Content -Path $log -Value "SENTINEL_SCALE_COMPLETE" -Encoding utf8
