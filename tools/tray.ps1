# Azeroth Realm tray icon - single control point for the whole stack.
#
# Uses a native WinForms NotifyIcon so there is nothing to install. Launched by
# "Azeroth Tray.vbs", which runs it with window style 0 so no console ever appears.
#
# Long actions (a saving shutdown takes minutes while 1000 bots drain) run in a
# detached hidden PowerShell so the menu never freezes.
$ErrorActionPreference = 'SilentlyContinue'

# Single-instance lock. Without this, every click of the shortcut adds another tray
# icon (they stack up silently because each instance is windowless). The mutex is
# held for the life of the process and released automatically when it exits.
$createdNew = $false
$script:TrayMutex = New-Object System.Threading.Mutex($true, 'Local\AzerothControlTray', [ref]$createdNew)
if (-not $createdNew) {
    # Another tray already owns the icon - leave it alone and exit quietly.
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# The hub is wherever this script lives, one level up - so a clone anywhere
# works, and AZCTL_HOME still wins for a hub on another drive.
$HUB     = if ($env:AZCTL_HOME) { $env:AZCTL_HOME } else { Split-Path -Parent $PSScriptRoot }
$CONTROL = Join-Path $HUB 'control'
$WOW     = Join-Path $HUB 'client\Wow.exe'
$PANEL   = 'http://127.0.0.1:8750/'
$PANELPORT = 8750
$LAUNCHER = $PANEL + 'launcher'      # the realm picker / switcher front end
$LAUNCHER_EXE = Join-Path $HUB 'app\AzerothControl\AzerothControl.exe'
$LOG     = Join-Path $HUB 'logs\tray.log'

function Log($m){ Add-Content $LOG ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $m) }

$PY  = 'C:\Users\Ammo\AppData\Local\Programs\Python\Python314\python.exe'
$PYW = 'C:\Users\Ammo\AppData\Local\Programs\Python\Python314\pythonw.exe'
if(-not (Test-Path $PY )){ $PY  = 'python' }
if(-not (Test-Path $PYW)){ $PYW = $PY }

function RunAction($scriptName){
  # Dedicated action scripts rather than inlined python -c: no nested quoting to get
  # wrong, and every path stays inside PowerShell single quotes.
  $p = Join-Path $HUB ('tools\' + $scriptName)
  Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
    '-NoProfile','-ExecutionPolicy','Bypass','-Command',
    "& '$PY' '$p' 2>&1 | Add-Content '$LOG'"
  )
}

function PanelUp(){
  try { $null = Invoke-RestMethod ($PANEL + 'api/status') -TimeoutSec 3; return $true } catch { return $false }
}
function PanelHas($route){
  try {
    $null = Invoke-WebRequest ($PANEL.TrimEnd('/') + $route) -TimeoutSec 4 -UseBasicParsing
    return $true
  } catch { return $false }
}
function PanelProc(){
  Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    # Match the script name however the panel was started: `pythonw C:\...\control\control.py`
    # from this tray, or `py -3 control.py` / `python control.py` from inside control\.
    # The old '*control?control.py*' pattern missed the second form, so a panel started
    # that way was invisible here and a second one could bind 8750 beside it.
    Where-Object { $_.CommandLine -match '(^|[\\/ "])control\.py(\s|"|$)' }
}
function PanelStale(){
  # A panel process serves whatever it imported at startup, so one left running
  # since before control\*.py was last edited is serving code that no longer
  # exists on disk.  Comparing timestamps catches that generally, where checking
  # for one known route only ever catches the change that route was added for.
  $p = PanelProc | Select-Object -First 1
  if(-not $p){ return $false }
  $newest = Get-ChildItem (Join-Path $CONTROL '*.py') -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if(-not $newest){ return $false }
  return ($newest.LastWriteTime -gt $p.CreationDate)
}
function PanelPortProc(){
  # Whoever actually HOLDS the port, however it was started: `pythonw control.py`
  # from this tray, `python control.py` from a shell, or the packaged window hosting
  # the panel itself. A restart that only kills command-line matches leaves a
  # window-hosted panel on the port and reports success having changed nothing.
  try {
    Get-NetTCPConnection -LocalPort $PANELPORT -State Listen -ErrorAction Stop |
      Select-Object -ExpandProperty OwningProcess -Unique |
      ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }
  } catch { return @() }
}

function RestartPanel(){
  # Stop every panel this box can see - the port's owner AND any other control.py,
  # because Python's HTTPServer sets SO_REUSEADDR and Windows will happily let a
  # second process bind the port beside the first; requests then land on either.
  $procs = @()
  $procs += PanelPortProc
  $procs += (PanelProc | ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
  $procs = @($procs | Where-Object { $_ } | Sort-Object Id -Unique)

  if($procs.Count -eq 0){
    Log 'hub restart: nothing running, starting one'
    EnsurePanel
    if(PanelUp){
      $ni.ShowBalloonTip(4000,'Azeroth Control','Control panel started.',
        [System.Windows.Forms.ToolTipIcon]::Info)
    } else {
      $ni.ShowBalloonTip(6000,'Azeroth Control','The control panel did not come up - see logs\tray.log.',
        [System.Windows.Forms.ToolTipIcon]::Warning)
    }
    return
  }

  $names = ($procs | ForEach-Object { '{0} (pid {1})' -f $_.ProcessName, $_.Id }) -join ', '
  $ans = [System.Windows.Forms.MessageBox]::Show(
    "Restart the control panel?`n`nStopping: $names`n`nThe realm, MySQL and everyone's " +
    "characters keep running untouched. A launcher window open on the panel will " +
    "need reopening.",
    'Azeroth Control', [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Question)
  if($ans -ne [System.Windows.Forms.DialogResult]::Yes){ return }

  Log ("hub restart: stopping " + $names)
  foreach($p in $procs){
    try { Stop-Process -Id $p.Id -Force -ErrorAction Stop } catch { Log ("hub restart: " + $_.Exception.Message) }
  }

  # The port outlives the process for a moment; starting into a held port is the
  # failure that looks like "it did not restart properly".
  for($i=0; $i -lt 25; $i++){
    if(@(PanelPortProc).Count -eq 0){ break }
    Start-Sleep -Milliseconds 400
  }
  if(@(PanelPortProc).Count -gt 0){
    Log 'hub restart: port still held, not starting a second panel'
    $ni.ShowBalloonTip(7000,'Azeroth Control',
      "Something is still holding port $PANELPORT, so no new panel was started - see logs\tray.log.",
      [System.Windows.Forms.ToolTipIcon]::Warning)
    return
  }

  Start-Process -FilePath $PYW -ArgumentList (Join-Path $CONTROL 'control.py') `
                -WorkingDirectory $CONTROL -WindowStyle Hidden
  for($i=0; $i -lt 25; $i++){ Start-Sleep -Milliseconds 600; if(PanelUp){ break } }
  if(PanelUp){
    Log 'hub restart: panel is back up'
    $ni.ShowBalloonTip(4000,'Azeroth Control','Control panel restarted.',
      [System.Windows.Forms.ToolTipIcon]::Info)
  } else {
    Log 'hub restart: panel did not answer after starting'
    $ni.ShowBalloonTip(7000,'Azeroth Control',
      'The control panel did not answer after restarting - see logs\tray.log.',
      [System.Windows.Forms.ToolTipIcon]::Warning)
  }
}

function EnsurePanel(){
  if(PanelUp){ return }
  # pythonw.exe has no console at all, so the panel leaves nothing on screen.
  Start-Process -FilePath $PYW -ArgumentList (Join-Path $CONTROL 'control.py') `
                -WorkingDirectory $CONTROL -WindowStyle Hidden
  for($i=0; $i -lt 10; $i++){ Start-Sleep -Milliseconds 600; if(PanelUp){ break } }
}

function ActiveRealm(){
  # Read realms\profiles.json directly rather than asking the panel: the tray has
  # to be able to name the active realm even with nothing running, and this is the
  # same file the launcher and switch-realm.py write.
  try {
    $doc = Get-Content (Join-Path $HUB 'realms\profiles.json') -Raw -ErrorAction Stop | ConvertFrom-Json
    $r = $doc.realms | Where-Object { $_.slug -eq $doc.activeRealm } | Select-Object -First 1
    if($r){ return $r.name }
    return $doc.activeRealm
  } catch { return $null }
}

function ClearStalePanel(){
  # Returns $true unless the user declined. The window attaches to whatever is
  # already listening on 8750, so an out-of-date panel has to go first or the
  # launcher opens onto an API that predates it.
  if(-not (PanelUp)){ return $true }
  if(-not ((PanelStale) -or -not (PanelHas '/launcher'))){ return $true }
  $ans = [System.Windows.Forms.MessageBox]::Show(
    "The control panel running in the background is older than the panel code " +
    "on disk, so the launcher would open onto the old version." +
    "`n`nRestart it? This restarts the web panel only - the realm and everyone's " +
    "characters keep running untouched.",
    'Azeroth Realm', [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Question)
  if($ans -ne [System.Windows.Forms.DialogResult]::Yes){ return $false }
  Log 'launcher: panel is stale, stopping it'
  PanelProc | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  Start-Sleep -Milliseconds 800
  return $true
}

function OpenLauncher(){
  if(-not (ClearStalePanel)){ return }

  if(Test-Path $LAUNCHER_EXE){
    # Deliberately no EnsurePanel here: with nothing on 8750 the exe hosts the
    # panel itself, which keeps it to one process that ends with the window.
    # A second launch raises the window that is already open.
    Log 'launcher: opening AzerothControl.exe'
    Start-Process -FilePath $LAUNCHER_EXE -WorkingDirectory (Split-Path $LAUNCHER_EXE)
    return
  }

  # No window build present - fall back to the browser rather than doing nothing.
  Log 'launcher: exe not found, falling back to the browser'
  EnsurePanel
  if(-not (PanelHas '/launcher')){
    $ni.ShowBalloonTip(6000,'Azeroth Realm',
      'The panel has no launcher in it - see logs\tray.log.',
      [System.Windows.Forms.ToolTipIcon]::Warning)
    return
  }
  Start-Process $LAUNCHER
}

# --- icon: borrow the game's own, falling back to a stock one ---
$icon = $null
if(Test-Path $WOW){ try { $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($WOW) } catch {} }
if(-not $icon){ $icon = [System.Drawing.SystemIcons]::Application }

$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Icon = $icon
$ni.Text = 'Azeroth Realm'
$ni.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$miStatus = New-Object System.Windows.Forms.ToolStripMenuItem
$miStatus.Text = 'Checking...'
$miStatus.Enabled = $false
[void]$menu.Items.Add($miStatus)
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

function AddItem($text, $action){
  $mi = New-Object System.Windows.Forms.ToolStripMenuItem
  $mi.Text = $text
  $mi.Add_Click($action)
  [void]$menu.Items.Add($mi)
  return $mi
}

[void](AddItem 'Open Launcher' { OpenLauncher })
[void](AddItem 'Open Launcher (browser)' {
  # The web view and the browser share one panel, so this is a fallback for when
  # WebView2 misbehaves rather than a second, separate launcher.
  EnsurePanel
  Start-Process $LAUNCHER
})
[void](AddItem 'Open Control Panel' {
  EnsurePanel
  Start-Process $PANEL
})
[void](AddItem 'Play WoW' {
  if(Test-Path $WOW){ Start-Process -FilePath $WOW -WorkingDirectory (Split-Path $WOW) }
})
[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

[void](AddItem 'Start everything' {
  $realm = ActiveRealm
  if(-not $realm){ $realm = 'the active realm' }
  $ni.ShowBalloonTip(4000,'Azeroth Realm',"Starting $realm - MySQL, auth and world...",
    [System.Windows.Forms.ToolTipIcon]::Info)
  Log 'menu: start everything'
  RunAction 'action_start.py'
})

[void](AddItem 'Restart world (saves first)' {
  $realm = ActiveRealm
  if(-not $realm){ $realm = 'the active realm' }
  $ni.ShowBalloonTip(5000,'Azeroth Realm',
    "Saving characters, then restarting the world for $realm. This can take a few minutes.",
    [System.Windows.Forms.ToolTipIcon]::Info)
  Log 'menu: restart world'
  RunAction 'action_restart.py'
})

[void](AddItem 'Stop everything (saves first)' {
  $realm = ActiveRealm
  if(-not $realm){ $realm = 'the running realm' }
  $r = [System.Windows.Forms.MessageBox]::Show(
    "Shut $realm down?`n`nCharacters are saved first - this is a clean shutdown, not a kill.",
    'Azeroth Realm', [System.Windows.Forms.MessageBoxButtons]::YesNo,
    [System.Windows.Forms.MessageBoxIcon]::Question)
  if($r -ne [System.Windows.Forms.DialogResult]::Yes){ return }
  $ni.ShowBalloonTip(6000,'Azeroth Realm',
    'Saving all characters and shutting down. Watch the tray tooltip for progress.',
    [System.Windows.Forms.ToolTipIcon]::Info)
  Log 'menu: stop everything'
  # action_stop.py stops the ACTIVE realm's servers, then MySQL via a clean
  # mysqladmin shutdown, and Ollama too - never a force kill.
  RunAction 'action_stop.py'
})

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

[void](AddItem 'Restart hub (control panel)' {
  Log 'menu: restart hub'
  RestartPanel
})

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
[void](AddItem 'Exit tray (server keeps running)' {
  $ni.Visible = $false
  $ni.Dispose()
  [System.Windows.Forms.Application]::Exit()
})

# Refresh the status line each time the menu opens - cheap, and always current.
$menu.Add_Opening({
  $up = @()
  foreach($n in 'mysqld','authserver','worldserver'){
    if(Get-Process -Name $n -EA SilentlyContinue){ $up += $n }
  }
  $oll = $false
  try { $null = Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2; $oll = $true } catch {}
  if($oll){ $up += 'ollama' }

  if($up.Count -eq 0){ $txt = 'Everything stopped' }
  elseif($up -contains 'worldserver'){ $txt = "Running ({0}/4 up)" -f $up.Count }
  else { $txt = "Partially up ({0}/4)" -f $up.Count }
  $realm = ActiveRealm
  if($realm){ $txt = "$realm - $txt" }
  $miStatus.Text = $txt
  # NotifyIcon.Text caps at 63 chars; a realm name pushes it over, so trim.
  $tip = "Azeroth Realm - $txt"
  if($tip.Length -gt 63){ $tip = $tip.Substring(0,63) }
  $ni.Text = $tip
})

$ni.ContextMenuStrip = $menu
# Double-click opens the launcher, not the control panel: picking or switching a
# realm is the common case, and the panel is one menu item away.
$ni.Add_MouseDoubleClick({ OpenLauncher })

Log 'tray started'
$ni.ShowBalloonTip(3000,'Azeroth Realm','Tray running. Double-click for the launcher, right-click for controls.',
  [System.Windows.Forms.ToolTipIcon]::Info)

# ApplicationContext with no form: keeps the message loop alive with nothing on screen.
[System.Windows.Forms.Application]::Run((New-Object System.Windows.Forms.ApplicationContext))
$ni.Visible = $false
$ni.Dispose()
