<#
    Switch-Realm.ps1 - pick which realm profile owns the shared ports.

    Every profile binds 3724 / 8085 / 7878 on 127.0.0.1, so exactly one realm runs
    at a time and the client realmlist never has to change. Switching stops the
    running realm gracefully (CTRL_BREAK -> World::StopNow, which SAVES every
    character) before starting the next one.

    This is the no-desktop front end. The tray icon's *Open Launcher* leads to the
    same thing with a progress bar; all three run one engine, control\realms.py,
    which reuses control.py's process control rather than reimplementing it - a
    subtly wrong stop loses unsaved progress.

    Usage:
        Switch-Realm.ps1                 list profiles and what is running
        Switch-Realm.ps1 -To <slug>      switch to that realm (slug from -List)
        Switch-Realm.ps1 -Stop           gracefully stop the running realm
#>
param(
    [string]$To,
    [switch]$Stop
)
$ErrorActionPreference = 'Stop'

$py = @(
    'C:\Users\Ammo\AppData\Local\Programs\Python\Python314\python.exe',
    (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $py) { Write-Host "python not found" -ForegroundColor Red; exit 1 }

$script = Join-Path $PSScriptRoot 'switch-realm.py'

if ($Stop)      { & $py $script --stop }
elseif ($To)    { & $py $script --to $To }
else            { & $py $script --list }

exit $LASTEXITCODE
