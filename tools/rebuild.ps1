# Configure + build + install the hub server with every module in src\modules.
# The old build tree cached absolute paths to C:\AzerothCore, so it cannot be moved -
# it has to be regenerated here. That folds neatly into the module rebuild.
#
# Installing overwrites worldserver.exe, which Windows locks while the world is
# running. Split the run when someone is online: -BuildOnly compiles without
# touching server\, then -InstallOnly drops the binaries in once the world is down.
param(
  [switch]$BuildOnly,     # configure + compile, leave server\ alone
  [switch]$InstallOnly,   # skip compiling, install what is already built
  # Force configure even when no new mod-* folder appeared. Needed whenever a
  # module gains or loses a FILE or SUBDIRECTORY - upstream merges and cherry-
  # picks do this routinely. AutoCollect.cmake globs sources at configure time
  # and nothing depends on that glob, so new files are silently left out of the
  # build and deleted ones stay in the .vcxproj and break it. See
  # TROUBLESHOOTING.md "A new source file or subdirectory inside an existing
  # module never compiles". Budget for a full module rebuild: regenerating
  # modules.vcxproj marks every source in that target out of date.
  [switch]$Reconfigure,
  [int]$Cores = 0,        # logical CPUs the compile may use; 0 = the default below
  # Scheduling priority for the compile. BelowNormal keeps the box usable while
  # someone is playing; Normal is roughly a third faster when nobody is. Do not
  # go above Normal - AboveNormal starves the tooling that watches the build.
  [ValidateSet('Idle','BelowNormal','Normal')]
  [string]$Priority = 'BelowNormal'
)
$ErrorActionPreference = 'Continue'
$HUB   = if ($env:AZCTL_HOME) { $env:AZCTL_HOME } else { Split-Path -Parent $PSScriptRoot }
$log   = "$HUB\logs\rebuild.log"
$cmake = 'C:\Program Files\CMake\bin\cmake.exe'
New-Item -ItemType Directory -Force -Path "$HUB\logs" | Out-Null
function Say($m){ $l="[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'),$m; Write-Host $l; Add-Content $log $l -Encoding utf8 }
Set-Content $log "=== rebuild in $HUB ===" -Encoding utf8

# AzerothCore's deps/boost/CMakeLists.txt does:
#   if(DEFINED ENV{Boost_ROOT}) set(Boost_ROOT $ENV{Boost_ROOT})
# which would silently beat -DBOOST_ROOT and point at the old C:\local copy.
$env:Boost_ROOT = "$HUB\deps\boost"
$env:BOOST_ROOT = "$HUB\deps\boost"
Say "Boost_ROOT pinned to $env:Boost_ROOT"

# AzerothCore compiles with a bare /MP, so cl.exe fans out over every logical
# CPU and starves whatever else is on the box - including a world server and a
# game client someone is actually playing on. Affinity and priority are
# inherited by child processes, so setting them on THIS shell is enough:
# cmake -> MSBuild -> every cl.exe lands inside the mask.
#
# Do it here rather than as /MP<n> in cmake/compiler/msvc/settings.cmake.
# That is a compile flag: changing it invalidates every object file and turns a
# ten minute incremental build into a multi-hour full rebuild.
$totalCpus = [Environment]::ProcessorCount
# 8 is James's standing figure, set 2026-09-04 after a 12-core run made the box
# unpleasant to use. It is a flat number rather than "all but four" on purpose:
# the point is a ceiling that stays put, not one that scales with the hardware.
if ($Cores -le 0)         { $Cores = [Math]::Min(8, $totalCpus) }
if ($Cores -gt $totalCpus){ $Cores = $totalCpus }
$mask = [int64]([Math]::Pow(2, $Cores) - 1)
$self = Get-Process -Id $PID
try {
  $self.ProcessorAffinity = [IntPtr]$mask
  $self.PriorityClass     = $Priority
  Say ("Compile pinned to {0} of {1} logical CPUs (affinity 0x{2:X}), priority {3}" -f $Cores,$totalCpus,$mask,$Priority)
  Say "  -Cores is capped at 8 by James's instruction - ask him before going higher"
  Say "  -Priority <Idle|BelowNormal|Normal> is free to change; Normal if nobody is playing"
} catch {
  Say "  WARN could not set affinity/priority: $($_.Exception.Message)"
}

Say "Modules present:"
Get-ChildItem "$HUB\src\modules" -Directory -Filter 'mod-*' | ForEach-Object { Say "  $($_.Name)" }

$cache = "$HUB\build\CMakeCache.txt"
$freshTree = -not (Test-Path $cache)
$needConfigure = $freshTree

if ($freshTree) {
  if (Test-Path "$HUB\build") { Say "Removing stale build tree ..."; Remove-Item "$HUB\build" -Recurse -Force -EA SilentlyContinue }
  New-Item -ItemType Directory -Force -Path "$HUB\build" | Out-Null
} else {
  # A module folder CMake has never seen stays invisible until configure runs again:
  # GetModuleSourceList globs modules/ at configure time, and no existing target
  # depends on that glob, so ZERO_CHECK never fires on its own. Reconfiguring in
  # place keeps every compiled object, so this costs the new files and a link.
  $onDisk = @(Get-ChildItem "$HUB\src\modules" -Directory -Filter 'mod-*' | ForEach-Object { $_.Name.ToUpper() })
  $known  = @(Select-String -Path $cache -Pattern '^MODULE_(MOD-[A-Z0-9-]+):STRING=' -EA SilentlyContinue |
              ForEach-Object { $_.Matches[0].Groups[1].Value })
  $newMods = @($onDisk | Where-Object { $known -notcontains $_ })
  if ($Reconfigure) {
    Say "-Reconfigure: forcing configure (a module file/directory list changed)."
    Say "  Expect mod-playerbots to rebuild in full - modules.vcxproj is regenerated."
    $needConfigure = $true
  } elseif ($newMods.Count -gt 0) {
    Say ("New module(s) absent from the CMake cache: " + ($newMods -join ', '))
    Say "Re-running configure in place - object cache survives, so this is not a full rebuild."
    $needConfigure = $true
  } else {
    Say "Already configured - skipping configure (delete build\ to force a fresh one)"
  }
}

# ---- module ledger ---------------------------------------------------------
# realms\vanilla\modules.json is what the launcher's module page writes. "out"
# there sets build=disabled HERE rather than moving a directory out of
# src\modules, which is the operation that trips the configure race
# documented in TROUBLESHOOTING.md ("CMake Error at modules/CMakeLists.txt:270").
# Nothing is deleted: the source, its git repo and its realm/custom branch stay.
#
# The variable name is MODULE_ + the UPPERCASED DIRECTORY, so the HYPHENS SURVIVE
# (MODULE_MOD-AOE-LOOT, never MODULE_MOD_AOE_LOOT - the underscore spelling
# configures cleanly and does nothing). It is generated in control/modules.py and
# only read here; never type one by hand.
#
# Every module is passed explicitly, enabled ones as =static, so a module toggled
# back IN is restored rather than staying disabled in the cache forever.
$modFlags = @()
$ledger = "$HUB\realms\vanilla\modules.json"
if (Test-Path $ledger) {
  try {
    $led = Get-Content $ledger -Raw | ConvertFrom-Json
    $drift = @()
    foreach ($m in $led.modules) {
      $want = 'static'
      if ($m.build -eq 'disabled') { $want = 'disabled' }
      $modFlags += "-D$($m.cmakeVar)=$want"
      if (Test-Path $cache) {
        $pat = "^" + [regex]::Escape($m.cmakeVar) + ":STRING=(.*)$"
        $hit = Select-String -Path $cache -Pattern $pat -EA SilentlyContinue | Select-Object -First 1
        if ($hit) {
          $have = $hit.Matches[0].Groups[1].Value
          # 'default' is what -DMODULES=static resolves to, so it is not drift.
          if (-not (($have -eq $want) -or ($have -eq 'default' -and $want -eq 'static'))) {
            $drift += "$($m.dir): cache=$have ledger=$want"
          }
        }
      }
    }
    $outMods = @($led.modules | Where-Object { $_.build -eq 'disabled' } | ForEach-Object { $_.dir })
    if ($outMods.Count -gt 0) { Say ("Module ledger: {0} built OUT - {1}" -f $outMods.Count, ($outMods -join ', ')) }
    else { Say "Module ledger: every module built in" }
    if ($drift.Count -gt 0) {
      Say "Ledger differs from the CMake cache - forcing configure:"
      $drift | ForEach-Object { Say "  $_" }
      $needConfigure = $true
    }
  } catch {
    Say "WARN could not read $ledger ($($_.Exception.Message)) - building every module in"
    $modFlags = @()
  }
} else {
  Say "No module ledger at $ledger - building every module in"
}

if ($needConfigure -and -not $InstallOnly) {
Say "Configuring ..."
& $cmake -S "$HUB/src" -B "$HUB/build" -G "Visual Studio 17 2022" -A x64 `
  "-DCMAKE_INSTALL_PREFIX=$HUB/server" `
  "-DBOOST_ROOT=$HUB/deps/boost" `
  "-DMYSQL_INCLUDE_DIR=$HUB/mysql/include" `
  "-DMYSQL_LIBRARY=$HUB/mysql/lib/libmysql.lib" `
  "-DOPENSSL_ROOT_DIR=$HUB/deps/openssl" `
  -DWITH_WARNINGS=0 -DTOOLS_BUILD=none -DAPPS_BUILD=all -DSCRIPTS=static -DMODULES=static `
  @modFlags `
  2>&1 | Tee-Object -Append -FilePath $log | Out-Null
Say "configure exit: $LASTEXITCODE"

$cfgErr = Select-String -Path $log -Pattern 'CMake Error' -EA SilentlyContinue
if ($cfgErr){ $cfgErr | Select-Object -First 8 | ForEach-Object { Say "  $($_.Line.Trim())" }
  Add-Content $log 'SENTINEL_REBUILD_FAILED' -Encoding utf8; exit 1 }

foreach($k in 'Found Boost','Found OpenSSL','Found MySQL','Modules configuration'){
  Select-String -Path $log -Pattern $k -EA SilentlyContinue | Select-Object -First 1 |
    ForEach-Object { Say ("  " + $_.Line.Trim()) }
}
}

$rc = 0
if ($InstallOnly) {
  Say "-InstallOnly: skipping compile, installing what build\ already holds"
} else {
Say "Building (RelWithDebInfo, /MP fans out across cores) ..."
# No extra MSBuild switches: PowerShell parses a bare -verbosity:minimal as
# parameter-binding syntax and splits it into "-verbosity:" + "minimal",
# which MSBuild rejects (MSB1016). cmake's own defaults are fine.
& $cmake --build "$HUB/build" --config RelWithDebInfo --parallel 1 `
  2>&1 | Tee-Object -Append -FilePath $log | Out-Null
$rc = $LASTEXITCODE
Say "build exit: $rc"

$errs = Select-String -Path $log -Pattern 'error [A-Z]+\d+|: error ' -EA SilentlyContinue
if ($errs){ Say "--- first 20 errors ---"; $errs | Select-Object -First 20 | ForEach-Object { Say "  $($_.Line.Trim())" } }
}

if ($rc -ne 0) {
  Add-Content $log 'SENTINEL_REBUILD_FAILED' -Encoding utf8
} elseif ($BuildOnly) {
  Say "=== BUILD OK - nothing installed (-BuildOnly) ==="
  Say "Stop the world, then run this script again with -InstallOnly"
  Add-Content $log 'SENTINEL_BUILD_COMPLETE' -Encoding utf8
} else {
  Say "Installing to $HUB\server ..."
  & $cmake --install "$HUB/build" --config RelWithDebInfo 2>&1 | Tee-Object -Append -FilePath $log | Out-Null
  Say "install exit: $LASTEXITCODE"

  # Runtime DLLs must sit beside the exes; sourced from the hub now, not Program Files.
  foreach($f in @("$HUB\mysql\lib\libmysql.dll",
                  "$HUB\deps\openssl\bin\libcrypto-3-x64.dll",
                  "$HUB\deps\openssl\bin\libssl-3-x64.dll",
                  "$HUB\deps\openssl\bin\legacy.dll")){
    $leaf = Split-Path $f -Leaf
    if(-not (Test-Path $f)){ Say "  WARN missing $f"; continue }
    # Copy-Item raises a NON-TERMINATING error, and $ErrorActionPreference is
    # 'Continue', so the old one-liner printed "DLL -> x" even when the copy had
    # failed. authserver holds all four of these open, so an -InstallOnly run
    # with auth still up hits that every time. Say what actually happened.
    Copy-Item $f "$HUB\server" -Force -ErrorAction SilentlyContinue -ErrorVariable cpErr
    if(-not $cpErr){ Say "  DLL -> $leaf"; continue }
    $dest = Join-Path "$HUB\server" $leaf
    if((Test-Path $dest) -and ((Get-FileHash $dest).Hash -eq (Get-FileHash $f).Hash)){
      Say "  DLL == $leaf (locked by a running server, but already current)"
    } else {
      Say "  DLL FAILED $leaf - locked AND out of date."
      Say "     Stop authserver too, then re-run with -InstallOnly."
    }
  }
  # New modules ship .conf.dist files that must land in configs/modules.
  New-Item -ItemType Directory -Force -Path "$HUB\server\configs\modules" | Out-Null
  Get-ChildItem "$HUB\src\modules\*\conf\*.conf.dist" | ForEach-Object {
    Copy-Item $_.FullName "$HUB\server\configs\modules" -Force
    $live = Join-Path "$HUB\server\configs\modules" ($_.BaseName)   # strips .dist
    if (-not (Test-Path $live)) { Copy-Item $_.FullName $live -Force; Say "  new conf -> $(Split-Path $live -Leaf)" }
  }
  Say "=== REBUILD OK ==="
  Add-Content $log 'SENTINEL_REBUILD_COMPLETE' -Encoding utf8
}
