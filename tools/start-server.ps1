# Thin wrapper. The real start path is tools\action_start.py.
#
# This script used to start the stack itself, and it was left behind when the
# hub moved: it hardcoded $srv = 'C:\AzerothCore\server' (a directory that no
# longer exists) and gated on a 'MySQL84' Windows service, while this hub runs
# mysqld.exe out of <hub>\mysql\bin with no service at all. It could
# therefore only ever fail, and it failed with a message - "MySQL84 service
# missing" - that pointed at the wrong thing entirely.
#
# It is also realm-blind. There is more than one realm profile now, and the
# active one is data in realms\profiles.json; starting "the server" without
# reading that launches whichever binaries this script happened to name.
#
# action_start.py reads the profile and starts that realm's own servers from
# that realm's own directory, and skips dependencies that are already up.
param([switch]$NoOllama)

if ($NoOllama) {
  Write-Host "-NoOllama is ignored: action_start.py skips Ollama when it is already serving." -ForegroundColor Yellow
}

Write-Host "=== Azeroth Realm launcher (via tools\action_start.py) ===" -ForegroundColor Cyan
& python (Join-Path $PSScriptRoot 'action_start.py')
exit $LASTEXITCODE
