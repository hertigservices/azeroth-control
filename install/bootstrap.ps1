<#
.SYNOPSIS
    Build an AzerothCore 3.3.5a server from nothing, on Windows, and wire this
    panel to it.

.DESCRIPTION
    A full run takes several hours, most of it compiling and importing, and it
    WILL be interrupted - by a reboot the installers ask for, by a download that
    fails, or by you closing the laptop. So this is written to be resumed rather
    than restarted: every phase checks whether its work is already done and skips
    itself if so, and you can run a single phase with -Phase.

        .\install\bootstrap.ps1                 # run every phase, skipping done work
        .\install\bootstrap.ps1 -Phase build    # just rebuild
        .\install\bootstrap.ps1 -WhatIf         # say what it would do, change nothing

    It stops at the first failure with the reason, rather than continuing and
    failing later somewhere that does not explain itself. Nothing here is
    destructive: it never drops a database it did not create in this run, and it
    never overwrites an existing realms/profiles.json.

    Version pins come from install/versions.json. Several look like downgrades
    and are not - that file records why for each one. Do not bump them casually.

.NOTES
    Requires an elevated shell for the prereqs phase (installers), and about
    100 GB free: ~30 GB build tree, ~25 GB databases, plus your client.
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    # Which phase to run. 'all' runs them in order.
    [ValidateSet('all', 'prereqs', 'clone', 'build', 'db', 'data', 'panel', 'verify')]
    [string]$Phase = 'all',

    # Where the server gets built and installed. Defaults to this repo's folder,
    # so the panel and the server it drives live together and stay relocatable.
    [string]$Hub,

    # Your existing WoW 3.3.5a (build 12340) client. Required for the 'data'
    # phase, which extracts maps/vmaps/mmaps FROM YOUR OWN CLIENT. None of that
    # data is downloaded and none of it ships here.
    [string]$Client,

    # Clone the mod-playerbots fork instead of upstream AzerothCore. The bots
    # need core changes that are not upstream; see versions.json.
    [switch]$WithPlayerbots,

    # Parallel compiler jobs. 8 at BelowNormal keeps the machine usable and
    # finishes in about the same wall-clock time on a typical desktop.
    [int]$Jobs = 0
)

$ErrorActionPreference = 'Stop'
$script:HERE = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:REPO = Split-Path -Parent $script:HERE
if (-not $Hub) { $Hub = $script:REPO }

$V = Get-Content (Join-Path $script:HERE 'versions.json') -Raw | ConvertFrom-Json
if ($Jobs -le 0) { $Jobs = $V.build.jobs }

# ---------------------------------------------------------------- output helpers

function Say  ($m) { Write-Host $m }
function Head ($m) {
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
    Write-Host "  $m" -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkCyan
}
function Ok   ($m) { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Skip ($m) { Write-Host "  [skip] $m" -ForegroundColor DarkGray }
function Warn ($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Die  ($m) {
    Write-Host ''
    Write-Host "  FAILED: $m" -ForegroundColor Red
    Write-Host ''
    Write-Host '  Nothing after this point ran. Fix the above and run the same' -ForegroundColor DarkGray
    Write-Host '  command again - finished phases will skip themselves.' -ForegroundColor DarkGray
    Write-Host '  Symptom index: docs/TROUBLESHOOTING.md' -ForegroundColor DarkGray
    exit 1
}

function Have ($exe) { return [bool](Get-Command $exe -ErrorAction SilentlyContinue) }

function Run ($exe, $argList, $what, $workdir) {
    if ($PSCmdlet -and -not $PSCmdlet.ShouldProcess($what, $exe)) {
        Say "  would run: $exe $($argList -join ' ')"
        return 0
    }
    $p = @{ FilePath = $exe; ArgumentList = $argList; NoNewWindow = $true; Wait = $true; PassThru = $true }
    if ($workdir) { $p['WorkingDirectory'] = $workdir }
    $proc = Start-Process @p
    return $proc.ExitCode
}

function Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ------------------------------------------------------------- derived locations

$SRC    = Join-Path $Hub 'src'
$BUILD  = Join-Path $Hub 'build'
$SERVER = Join-Path $Hub 'server'
$CONF   = Join-Path $SERVER 'configs'

# =============================================================== PHASE: prereqs

function Phase-Prereqs {
    Head 'Phase 1/7  Prerequisites'

    if (-not (Elevated)) {
        Die 'This phase installs software and needs an elevated shell. Re-run as Administrator, or install the pins in install/versions.json by hand and skip ahead with -Phase clone.'
    }
    if (-not (Have 'winget')) {
        Die 'winget is not available. Install "App Installer" from the Microsoft Store, or install the pins in install/versions.json by hand.'
    }

    # --- git
    if (Have 'git') { Skip "git present ($((git --version) -replace 'git version ',''))" }
    else {
        Say '  installing Git...'
        if ((Run 'winget' @('install','--id','Git.Git','-e','--silent',
                            '--accept-package-agreements','--accept-source-agreements') 'Install Git') -ne 0) {
            Die 'Git install failed.'
        }
        Ok 'Git installed - you may need a NEW shell before git is on PATH'
    }

    # --- CMake, pinned. 4.x cannot configure this project; see versions.json.
    $cmOk = $false
    if (Have 'cmake') {
        $cv = (cmake --version | Select-Object -First 1) -replace '[^0-9\.]', ''
        if ($cv -like '3.31*') { Skip "cmake $cv (pinned)"; $cmOk = $true }
        else { Warn "cmake $cv is installed, but this build needs $($V.cmake.version)." }
    }
    if (-not $cmOk) {
        Say "  installing CMake $($V.cmake.version) ..."
        $a = @('install','--id',$V.cmake.winget,'-e','--version',$V.cmake.version,'--silent',
               '--accept-package-agreements','--accept-source-agreements',
               '--override',$V.cmake.override)
        if ((Run 'winget' $a "Install CMake $($V.cmake.version)") -ne 0) {
            Die "CMake $($V.cmake.version) install failed. $($V.cmake.why_pinned)"
        }
        Ok "CMake $($V.cmake.version) installed"
    }

    # --- OpenSSL, pinned to the LTS line. A higher number is NOT an upgrade here.
    $sslH = 'C:\Program Files\OpenSSL-Win64\include\openssl\ssl.h'
    if (Test-Path $sslH) { Skip 'OpenSSL headers present' }
    else {
        Say "  installing OpenSSL $($V.openssl.version) ($($V.openssl.channel)) ..."
        $a = @('install','--id',$V.openssl.winget,'-e','--silent',
               '--accept-package-agreements','--accept-source-agreements')
        Run 'winget' $a "Install OpenSSL $($V.openssl.version)" | Out-Null
        if (-not (Test-Path $sslH)) {
            Die ("OpenSSL headers still absent at $sslH.`n" +
                 "         Install $($V.openssl.installer) from slproweb by hand.`n" +
                 "         Note: $($V.openssl.why_pinned)")
        }
        Ok 'OpenSSL installed'
    }

    # --- Boost. Prebuilt for v143; building it from source is a long detour.
    $boostDir = $V.boost.install_dir
    if (Test-Path (Join-Path $boostDir 'boost\version.hpp')) { Skip "Boost at $boostDir" }
    else {
        Warn "Boost $($V.boost.version) is not at $boostDir."
        Say  "  This one is a manual download - there is no reliable silent installer."
        Say  "    1. Get $($V.boost.installer)"
        Say  "       from https://sourceforge.net/projects/boost/files/boost-binaries/$($V.boost.version)/"
        Say  "    2. Install it to exactly $boostDir"
        Say  "    3. Run this phase again."
        Die  'Boost missing.'
    }

    # Both spellings, forward slashes. Getting this wrong is the single most
    # common "Boost is installed but CMake cannot find it" cause.
    foreach ($n in @('BOOST_ROOT', 'Boost_ROOT')) {
        $want = $V.boost.env.BOOST_ROOT
        if ([Environment]::GetEnvironmentVariable($n, 'Machine') -ne $want) {
            if ($PSCmdlet.ShouldProcess("$n = $want", 'setx Machine')) {
                [Environment]::SetEnvironmentVariable($n, $want, 'Machine')
                Ok "$n set to $want"
            }
        } else { Skip "$n already set" }
        Set-Item "env:$n" $want          # so THIS shell can configure without a restart
    }

    # --- MySQL
    if (Have 'mysql') { Skip 'mysql client on PATH' }
    else {
        Warn "MySQL $($V.mysql.version) not found on PATH."
        Say  "  Install it (winget install $($V.mysql.winget)), then add its bin\ to PATH."
        Say  "  Before importing anything, put this in my.ini under [mysqld]:"
        Say  "      sql_mode=`"$($V.mysql.my_ini.sql_mode)`""
        Say  "  $($V.mysql.gotchas[0])"
        Die  'MySQL missing.'
    }

    # --- Visual Studio
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationVersion
        if ($vs) { Ok "Visual Studio C++ tools present ($vs)" }
        else { Die "Visual Studio is installed but WITHOUT the `"$($V.visual_studio.workload)`" workload. Add it in the VS Installer." }
    } else {
        Die "Visual Studio $($V.visual_studio.version) not found. Install it with the `"$($V.visual_studio.workload)`" workload. $($V.visual_studio.why_pinned)"
    }

    Ok 'Prerequisites satisfied'
    Warn 'If anything was installed just now, open a NEW shell before the build phase - a Machine-scope variable is invisible to this one.'
}

# ================================================================= PHASE: clone

function Phase-Clone {
    Head 'Phase 2/7  Source'

    if (Test-Path (Join-Path $SRC '.git')) {
        Skip "source already at $SRC"
        return
    }
    if ($WithPlayerbots) {
        $url = $V.sources.core_with_playerbots
        Say "  cloning the playerbots fork: $url"
        Say "  ($($V.sources._note))"
    } else {
        $url = $V.sources.core_upstream
        Say "  cloning upstream AzerothCore: $url"
        Say '  (re-run with -WithPlayerbots if you want bots - it is a different fork,'
        Say '   not a module you can add to this clone later without pain)'
    }

    New-Item -ItemType Directory -Force -Path $Hub | Out-Null
    if ((Run 'git' @('clone','--depth','1',$url,$SRC) "Clone $url") -ne 0) {
        Die 'git clone failed.'
    }
    Ok "cloned to $SRC"

    if ($WithPlayerbots) {
        $modDir = Join-Path $SRC 'modules\mod-playerbots'
        if (-not (Test-Path $modDir)) {
            Run 'git' @('clone','--depth','1',$V.modules.'mod-playerbots',$modDir) 'Clone mod-playerbots' | Out-Null
            Ok 'mod-playerbots cloned into modules/'
        }
    }
}

# ================================================================= PHASE: build

function Phase-Build {
    Head 'Phase 3/7  Build'

    if (-not (Test-Path $SRC)) { Die "No source at $SRC - run -Phase clone first." }
    if (Test-Path (Join-Path $SERVER 'worldserver.exe')) {
        Skip 'worldserver.exe already built (delete it to force a rebuild)'
        return
    }

    New-Item -ItemType Directory -Force -Path $BUILD, $SERVER | Out-Null

    Say "  configuring (CMake -> $BUILD)"
    $cfg = @('-S', $SRC, '-B', $BUILD,
             '-G', 'Visual Studio 17 2022', '-A', 'x64',
             "-DCMAKE_INSTALL_PREFIX=$SERVER",
             '-DTOOLS=1', '-DSCRIPTS=static', '-DWITH_WARNINGS=0')
    if ((Run 'cmake' $cfg 'CMake configure') -ne 0) {
        Die ("CMake configure failed.`n" +
             "         If it says Boost is missing: this shell may predate BOOST_ROOT (open a new one),`n" +
             "         the value may use backslashes (it must use forward slashes), or only one of the`n" +
             "         two spellings may be set. See docs/TROUBLESHOOTING.md.`n" +
             "         If it complains about cmake_minimum_required: you are on CMake 4.x. Use 3.31.")
    }
    Ok 'configured'

    Say "  building with $Jobs jobs at $($V.build.priority) priority"
    Say "  ($($V.build.why))"
    Say '  This is the long one. An hour is normal.'

    if ($PSCmdlet.ShouldProcess('AzerothCore', "cmake --build -j $Jobs")) {
        $proc = Start-Process -FilePath 'cmake' -NoNewWindow -PassThru `
            -ArgumentList @('--build', $BUILD, '--config', 'Release',
                            '--target', 'install', '--parallel', "$Jobs")
        try { $proc.PriorityClass = [Diagnostics.ProcessPriorityClass]::BelowNormal } catch {}
        $proc.WaitForExit()
        if ($proc.ExitCode -ne 0) {
            Die "Build failed (exit $($proc.ExitCode)). Link errors naming symbols rather than files usually mean a Boost/toolset mismatch - the prebuilt Boost is $($V.boost.toolset) and needs VS2022."
        }
    }
    Ok "built and installed to $SERVER"
}

# ==================================================================== PHASE: db

function Phase-Db {
    Head 'Phase 4/7  Databases'

    if (-not (Have 'mysql')) { Die 'mysql client not on PATH.' }

    Say '  MySQL root password (blank if none):'
    $sec = Read-Host -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    $env:MYSQL_PWD = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    # MYSQL_PWD rather than -p on the command line: an argument list is readable
    # by every other process on the machine, an environment variable is not.

    $mode = (mysql --user=root --batch --skip-column-names -e 'SELECT @@sql_mode;' 2>&1)
    if ($LASTEXITCODE -ne 0) { Die "Cannot connect to MySQL as root: $mode" }
    if ($mode -match 'STRICT_TRANS_TABLES' -or $mode -match 'NO_ZERO_DATE') {
        Warn "sql_mode is '$mode'"
        Die ("The world SQL import will fail partway through with a constraint error`n" +
             "         that reads like corrupt data. Put this in my.ini under [mysqld],`n" +
             "         restart MySQL, and run this phase again:`n`n" +
             "             sql_mode=`"$($V.mysql.my_ini.sql_mode)`"")
    }
    Ok "sql_mode is relaxed ('$mode')"

    $existing = (mysql --user=root --batch --skip-column-names -e 'SHOW DATABASES;') -split "`n"
    if ($existing -contains 'acore_world') {
        Skip 'acore_* databases already exist - not touching them'
    } else {
        if ($PSCmdlet.ShouldProcess('acore_auth, acore_world, acore_characters', 'CREATE DATABASE + user')) {
            $sql = @'
CREATE DATABASE IF NOT EXISTS acore_auth       DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE DATABASE IF NOT EXISTS acore_world      DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE DATABASE IF NOT EXISTS acore_characters DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER IF NOT EXISTS 'acore'@'localhost' IDENTIFIED BY 'acore';
GRANT ALL PRIVILEGES ON acore_auth.*       TO 'acore'@'localhost';
GRANT ALL PRIVILEGES ON acore_world.*      TO 'acore'@'localhost';
GRANT ALL PRIVILEGES ON acore_characters.* TO 'acore'@'localhost';
FLUSH PRIVILEGES;
'@
            $sql | mysql --user=root
            if ($LASTEXITCODE -ne 0) { Die 'Database creation failed.' }
            Ok 'databases and acore user created'
            Warn "The acore user's password is the default 'acore'. Change it, and change WorldDatabaseInfo in worldserver.conf to match - the panel reads its credentials from that file, so changing both keeps everything working."
        }
    }

    Say ''
    Say '  The schemas are imported by worldserver itself on first start: it'
    Say '  detects empty databases and applies the base dumps and updates. That'
    Say '  is the supported path, so this script does not second-guess it.'
    Say '  Expect the first start to take a while and to log a lot of SQL.'
    $env:MYSQL_PWD = $null
}

# ================================================================== PHASE: data

function Phase-Data {
    Head 'Phase 5/7  Client data (maps, vmaps, mmaps)'

    $maps = Join-Path $SERVER 'Data\maps'
    if ((Test-Path $maps) -and (Get-ChildItem $maps -File -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        Skip 'Data/maps already populated'
        return
    }
    if (-not $Client) {
        Warn 'No -Client given, so this phase cannot run.'
        Say  '  This data is EXTRACTED FROM YOUR OWN 3.3.5a client. It is not'
        Say  '  downloaded, and it is not distributed with this repo.'
        Say  ''
        Say  '    .\install\bootstrap.ps1 -Phase data -Client "C:\path\to\your\client"'
        Say  ''
        Say  '  Budget a few hours. mmap generation is the slow part by far.'
        return
    }
    if (-not (Test-Path (Join-Path $Client 'Wow.exe'))) {
        Die "No Wow.exe in $Client - that does not look like a 3.3.5a client."
    }

    foreach ($t in @('mapextractor', 'vmap4extractor', 'vmap4assembler', 'mmaps_generator')) {
        $exe = Join-Path $SERVER "$t.exe"
        if (-not (Test-Path $exe)) { Die "$t.exe missing from $SERVER - the build needs -DTOOLS=1." }
    }

    Say "  extracting from $Client (hours, not minutes)"
    Push-Location $Client
    try {
        foreach ($step in @(
            @{ n = 'maps + dbc';    e = 'mapextractor';     a = @() },
            @{ n = 'vmaps';         e = 'vmap4extractor';   a = @() },
            @{ n = 'vmap assembly'; e = 'vmap4assembler';   a = @('Buildings', 'vmaps') },
            @{ n = 'mmaps';         e = 'mmaps_generator';  a = @() })) {
            Say "    $($step.n) ..."
            if ((Run (Join-Path $SERVER "$($step.e).exe") $step.a $step.n $Client) -ne 0) {
                Die "$($step.e) failed."
            }
        }
    } finally { Pop-Location }

    New-Item -ItemType Directory -Force -Path (Join-Path $SERVER 'Data') | Out-Null
    foreach ($d in @('dbc', 'maps', 'vmaps', 'mmaps', 'Cameras')) {
        $from = Join-Path $Client $d
        if (Test-Path $from) {
            Move-Item $from (Join-Path $SERVER 'Data') -Force
            Ok "moved $d into server/Data"
        }
    }
}

# ================================================================= PHASE: panel

function Phase-Panel {
    Head 'Phase 6/7  Panel configuration'

    $prof = Join-Path $script:REPO 'realms\profiles.json'
    if (Test-Path $prof) {
        Skip 'realms/profiles.json exists - leaving it alone'
    } else {
        $conf = Join-Path $CONF 'worldserver.conf'
        if (-not (Test-Path $conf)) {
            $dist = Join-Path $CONF 'worldserver.conf.dist'
            if (Test-Path $dist) {
                Copy-Item $dist $conf
                Ok 'created worldserver.conf from the .dist'
            } else {
                Warn "No worldserver.conf under $CONF yet - build and install first."
            }
        }
        if ((Run 'python' @((Join-Path $script:HERE 'detect.py')) 'detect install') -ne 0) {
            Die 'install/detect.py failed. Run it by hand to see why.'
        }
        Ok 'realms/profiles.json written'
    }
}

# ================================================================ PHASE: verify

function Phase-Verify {
    Head 'Phase 7/7  Verify'
    Run 'python' @((Join-Path $script:HERE 'verify.py'), '--db') 'verify' | Out-Null
    Say ''
    Say '  Start the panel with:'
    Say '      python control\control.py'
    Say '  then open http://127.0.0.1:8750/launcher'
}

# ==================================================================== main

Head 'Azeroth Control - AzerothCore bootstrap'
Say "  hub    : $Hub"
Say "  repo   : $($script:REPO)"
Say "  phase  : $Phase"
Say "  jobs   : $Jobs at $($V.build.priority)"
if ($WhatIfPreference) { Warn 'WhatIf: nothing will actually change.' }

$order = @('prereqs', 'clone', 'build', 'db', 'data', 'panel', 'verify')
$todo = if ($Phase -eq 'all') { $order } else { @($Phase) }

foreach ($p in $todo) {
    switch ($p) {
        'prereqs' { Phase-Prereqs }
        'clone'   { Phase-Clone }
        'build'   { Phase-Build }
        'db'      { Phase-Db }
        'data'    { Phase-Data }
        'panel'   { Phase-Panel }
        'verify'  { Phase-Verify }
    }
}

Write-Host ''
Write-Host '  Done.' -ForegroundColor Green
