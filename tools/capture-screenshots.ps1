<#
.SYNOPSIS
  Captures one PNG per launcher tab for the README, from a running hub.

.DESCRIPTION
  Drives headless Edge (or Chrome) against the panel's own HTTP listener and
  screenshots each view.  Two details make this reproducible rather than a
  best-effort scrape:

    * the tab is chosen by URL, not by clicking.  launcher.html reads
      ?realm=&view=&tool=, so a headless browser with no input driver can still
      land on any tab.  Without that the only reachable view is 'play'.
    * --user-data-dir points at a throwaway profile.  Headless Edge started
      against your everyday profile silently attaches to the already-running
      browser and exits without writing a file.

  The hub must be running (tray icon, or `python control\control.py`).  Nothing
  is written to the hub itself.

.PARAMETER Hub
  Base URL of the panel.  Default http://127.0.0.1:8750

.PARAMETER OutDir
  Where the PNGs land.  Default <repo>\docs\img

.PARAMETER Page
  Path to the launcher page.  Default /launcher

.PARAMETER Realm
  Realm slug to show.  Default is whichever realm the hub has active.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\capture-screenshots.ps1
#>
[CmdletBinding()]
param(
  [string] $Hub    = 'http://127.0.0.1:8750',
  [string] $OutDir = '',
  [string] $Page   = '/launcher',
  [string] $Realm  = '',
  [int]    $Width  = 1600,
  [int]    $Height = 1000,
  [int]    $WaitMs = 9000
)

# $PSScriptRoot is only reliably populated once the body is running, so the
# default lands here rather than in the param block above.
$ROOT = if ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot }
        else { Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
if (-not $OutDir) { $OutDir = Join-Path $ROOT 'docs\img' }

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- the browser
$candidates = @(
  "$env:ProgramFiles(x86)\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
)
$browser = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $browser) { throw "No Edge or Chrome found. Install one, or edit `$candidates." }

# ------------------------------------------------------------------ the hub
try {
  $null = Invoke-WebRequest -Uri "$Hub/api/status" -TimeoutSec 5 -UseBasicParsing
} catch {
  throw "The hub is not answering on $Hub. Start it (tray icon, or python control\control.py) and retry."
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$profileDir = Join-Path $env:TEMP ("azctl-shot-" + [guid]::NewGuid().ToString('N'))

# Ordered so the README can walk the file list top to bottom.
$shots = @(
  @{ file = '01-play.png';      view = 'play';      tool = ''        },
  @{ file = '02-server.png';    view = 'server';    tool = ''        },
  @{ file = '03-world.png';     view = 'world';     tool = ''        },
  @{ file = '04-addons.png';    view = 'addons';    tool = ''        },
  @{ file = '05-tools-bots.png';view = 'tools';     tool = 'bots'    },
  @{ file = '06-tools-raid.png';view = 'tools';     tool = 'raid'    },
  @{ file = '07-reference.png'; view = 'reference'; tool = ''        },
  @{ file = '08-changelog.png'; view = 'changelog'; tool = ''        },
  @{ file = '09-settings.png';  view = 'settings';  tool = ''        }
)

$made = @()
foreach ($s in $shots) {
  $q = "view=$($s.view)"
  if ($s.tool)  { $q += "&tool=$($s.tool)" }
  if ($Realm)   { $q += "&realm=$Realm" }
  $url = "$Hub$Page`?$q"
  $out = Join-Path $OutDir $s.file

  Write-Host ("  {0,-22} {1}" -f $s.file, $url)
  $args = @(
    '--headless=new'
    '--disable-gpu'
    '--hide-scrollbars'
    '--force-device-scale-factor=1'
    "--user-data-dir=$profileDir"
    "--window-size=$Width,$Height"
    "--virtual-time-budget=$WaitMs"
    "--screenshot=$out"
    $url
  )
  $p = Start-Process -FilePath $browser -ArgumentList $args -Wait -PassThru -WindowStyle Hidden
  if (Test-Path $out) {
    $kb = [math]::Round((Get-Item $out).Length / 1KB)
    Write-Host ("     ok  {0} KB" -f $kb) -ForegroundColor Green
    $made += $out
  } else {
    Write-Host ("     FAILED (exit {0})" -f $p.ExitCode) -ForegroundColor Yellow
  }
}

Remove-Item -Recurse -Force $profileDir -ErrorAction SilentlyContinue

Write-Host ""
Write-Host ("{0} of {1} captured into {2}" -f $made.Count, $shots.Count, $OutDir)
Write-Host "Review every image before committing - the Settings and Tools tabs can show" -ForegroundColor Yellow
Write-Host "realm names, character names and account details from your own install." -ForegroundColor Yellow
