# Align the bot world with your current progression tier.
#
# Why this exists: bots are deliberately EXCLUDED from Individual Progression
# (IndividualProgression.BotAccountsRegex = "^RNDBOT.*"), so they ignore your gates.
# Keeping them in-tier means six settings across two config files, and the two
# level caps must agree or the lower one silently wins.
#
#   .\set-progression-phase.ps1 vanilla     (level 60, Azeroth only, no Death Knights)
#   .\set-progression-phase.ps1 tbc         (level 70, + Outland,   no Death Knights)
#   .\set-progression-phase.ps1 wotlk       (level 80, + Northrend, Death Knights on)
#
# Add -Restart to bounce worldserver so it takes effect immediately.
param(
  [Parameter(Mandatory=$true)][ValidateSet('vanilla','tbc','wotlk')][string]$Phase,
  [switch]$Restart
)
$ErrorActionPreference = 'Continue'

$HUB    = if ($env:AZCTL_HOME) { $env:AZCTL_HOME } else { Split-Path -Parent $PSScriptRoot }
$pbConf = Join-Path $HUB 'server\configs\modules\playerbots.conf'
$ipConf = Join-Path $HUB 'server\configs\modules\individualProgression.conf'
$srv    = Join-Path $HUB 'server'
$tools  = $PSScriptRoot

# maps: 0 = Eastern Kingdoms, 1 = Kalimdor, 530 = Outland, 571 = Northrend
#
# Shattrath / Dalaran are separate from Maps: ProbTeleToBankers sends 25% of
# teleports to a weighted capital city, and those weights bypass the map list.
# Zero them out per tier or bots keep popping into expansion cities.
$plan = switch ($Phase) {
  'vanilla' { @{ Level = 60; Maps = '0,1';         DisableDK = 1; Shattrath = 0; Dalaran = 0 } }
  'tbc'     { @{ Level = 70; Maps = '0,1,530';     DisableDK = 1; Shattrath = 1; Dalaran = 0 } }
  'wotlk'   { @{ Level = 80; Maps = '0,1,530,571'; DisableDK = 0; Shattrath = 1; Dalaran = 1 } }
}

function Set-Conf($file, $key, $value) {
  if (-not (Test-Path $file)) { Write-Host "  MISSING FILE: $file" -ForegroundColor Red; return }
  $pattern = '^\s*' + [regex]::Escape($key) + '\s*='
  $hit = $false
  $out = foreach ($l in (Get-Content $file)) {
    if (-not $hit -and $l -match $pattern) { $hit = $true; "$key = $value" } else { $l }
  }
  Set-Content -Path $file -Value $out -Encoding ascii
  if ($hit) { Write-Host ("  {0,-52} = {1}" -f $key, $value) }
  else      { Write-Host ("  {0,-52} = {1}  (KEY NOT FOUND)" -f $key, $value) -ForegroundColor Yellow }
}

Write-Host "=== Progression phase -> $Phase (level cap $($plan.Level)) ===" -ForegroundColor Cyan

Write-Host "playerbots.conf:"
Set-Conf $pbConf 'AiPlayerbot.RandomBotMaxLevel'        $plan.Level
Set-Conf $pbConf 'AiPlayerbot.RandomBotMaps'            $plan.Maps
Set-Conf $pbConf 'AiPlayerbot.DisableDeathKnightLogin'  $plan.DisableDK
Set-Conf $pbConf 'AiPlayerbot.TeleToShattrathCityWeight' $plan.Shattrath
Set-Conf $pbConf 'AiPlayerbot.TeleToDalaranWeight'       $plan.Dalaran

Write-Host "individualProgression.conf:"
# Must match RandomBotMaxLevel; if these disagree the lower cap wins and the
# mismatch is invisible in the logs.
Set-Conf $ipConf 'IndividualProgression.BotAccountsMaxLevel' $plan.Level

if ($Restart) {
  # Graceful only.  Stop-Process -Force on worldserver loses everything since the
  # last periodic save, and starting the exe directly bypasses realms.start_realm,
  # which is what knows WHICH realm's config to boot on this hub.
  Write-Host "Restarting worldserver gracefully ..." -ForegroundColor Yellow
  & python -B (Join-Path $tools 'action_restart.py')
  if ($LASTEXITCODE -ne 0) {
    Write-Host "  action_restart.py exited $LASTEXITCODE - check the realm by hand." -ForegroundColor Red
  }
} else {
  Write-Host "Not restarted. Bounce worldserver for these to take effect." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Note: bots already sitting in a now-excluded zone rotate out over time"
Write-Host "rather than teleporting instantly. Existing Death Knight bots simply stop logging in."
Write-Host ""
Write-Host "Raising the cap does NOT relevel existing bots. Randomize() re-gears a bot at"
Write-Host "its current level and IncreaseLevel() is commented out in this fork, so bots"
Write-Host "already at the old cap climb only by earning XP once BotAccountsMaxLevel lets"
Write-Host "them. A full reroll needs DeleteRandomBotAccounts=1 for ONE boot (then back to 0)."
