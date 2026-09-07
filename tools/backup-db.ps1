<#
    backup-db.ps1 - nightly mysqldump of the four realm databases.

    Measured on this realm: 371 MB of SQL compresses to 96 MB in about 25
    seconds. Five days retained is roughly 480 MB. Breakdown per database:

        acore_world        78 MB gz   (reference data + your panel edits)
        acore_playerbots   18 MB gz   (bot state, mostly travelnode paths)
        acore_characters  2.8 MB gz   (the irreplaceable one)
        acore_auth         27 KB gz

    The dump is streamed straight from mysqldump's stdout into a GZipStream, so
    the uncompressed SQL never touches the disk. That is 96 MB written per night
    instead of 467 MB - the whole point, on an SSD.

    --single-transaction takes a consistent InnoDB snapshot without locking
    anything, so this is safe to run against a live realm with bots in it.

    Each database is written to a .part file and only renamed into place after
    mysqldump exits 0 AND the archive decompresses cleanly all the way to
    mysqldump's own "Dump completed on" trailer. A truncated or corrupt archive
    therefore never replaces a good one, and old backups are pruned only after a
    complete new set has landed.

    Usage:
        powershell -File backup-db.ps1              run a backup now
        powershell -File backup-db.ps1 -Install     register the 05:00 daily task
        powershell -File backup-db.ps1 -Uninstall   remove the task
        powershell -File backup-db.ps1 -Restore <path-to.sql.gz>

    -Restore only expands the archive and prints the import command; it will not
    overwrite a live database on its own.
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [string]$Restore,
    [int]$KeepDays = 5
)

$ErrorActionPreference = 'Stop'
$HUB        = if ($env:AZCTL_HOME) { $env:AZCTL_HOME } else { Split-Path -Parent $PSScriptRoot }
$DUMP       = "$HUB\mysql\bin\mysqldump.exe"
$DEST       = "$HUB\backups"
$LOG        = "$HUB\logs\backup.log"
$DATABASES  = @('acore_auth', 'acore_characters', 'acore_world', 'acore_playerbots')
$TASK       = 'Azeroth Control Nightly Backup'

function Say($msg) {
    $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Write-Output $line
    New-Item -ItemType Directory -Force -Path (Split-Path $LOG) | Out-Null
    Add-Content -Path $LOG -Value $line -Encoding utf8
}

# ---------------------------------------------------------------- install ----
if ($Install) {
    $ps  = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $act = New-ScheduledTaskAction -Execute $ps `
             -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $trg = New-ScheduledTaskTrigger -Daily -At 5:00AM
    # Hidden, plus a hidden PowerShell window: this realm's rule is that nothing
    # ever flashes a console. StartWhenAvailable catches up a run the machine
    # slept through instead of silently skipping the night.
    $set = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable `
             -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
             -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
             -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TASK -Action $act -Trigger $trg -Settings $set `
        -Description 'Dumps the AzerothCore databases to <hub>\backups, gzipped, keeping 5 days.' `
        -Force | Out-Null
    Say "scheduled task '$TASK' registered - daily at 05:00"
    exit 0
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TASK -Confirm:$false
    Say "scheduled task '$TASK' removed"
    exit 0
}

# ---------------------------------------------------------------- restore ----
if ($Restore) {
    if (-not (Test-Path $Restore)) { throw "no such file: $Restore" }
    $out = $Restore -replace '\.gz$', ''
    $in  = [System.IO.File]::OpenRead($Restore)
    $gz  = New-Object System.IO.Compression.GZipStream($in, [System.IO.Compression.CompressionMode]::Decompress)
    $fs  = [System.IO.File]::Create($out)
    $gz.CopyTo($fs)
    $fs.Dispose(); $gz.Dispose(); $in.Dispose()
    Say ("expanded -> {0} ({1:N1} MB)" -f $out, ((Get-Item $out).Length / 1MB))
    Say "Stop worldserver, then import it with:"
    Say "  $HUB\mysql\bin\mysql.exe -h127.0.0.1 -uacore -pacore <database> < `"$out`""
    exit 0
}

# ----------------------------------------------------------------- backup ----
Add-Type -AssemblyName System.IO.Compression | Out-Null
New-Item -ItemType Directory -Force -Path $DEST | Out-Null

if (-not (Test-Path $DUMP)) { Say "FAILED: mysqldump not found at $DUMP"; exit 1 }

$stamp   = Get-Date -Format 'yyyy-MM-dd'
$started = Get-Date
$made    = @()
$failed  = @()

foreach ($db in $DATABASES) {
    $final = Join-Path $DEST "$db-$stamp.sql.gz"
    $part  = "$final.part"

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $DUMP
    $psi.Arguments              = "-h127.0.0.1 -uacore -pacore --single-transaction --quick " +
                                  "--no-tablespaces --routines --triggers " +
                                  "--default-character-set=utf8mb4 $db"
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true

    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        # stderr must be drained concurrently or a chatty dump deadlocks on a
        # full pipe buffer while we sit in CopyTo waiting for stdout.
        $errTask = $p.StandardError.ReadToEndAsync()

        $fs = [System.IO.File]::Create($part)
        $gz = New-Object System.IO.Compression.GZipStream($fs, [System.IO.Compression.CompressionLevel]::Optimal)
        $p.StandardOutput.BaseStream.CopyTo($gz)
        $gz.Dispose(); $fs.Dispose()

        $p.WaitForExit()
        $stderr = ($errTask.Result -split "`r?`n" |
                   Where-Object { $_ -and $_ -notmatch 'Using a password on the command line' }) -join '; '

        if ($p.ExitCode -ne 0) { throw "mysqldump exit $($p.ExitCode): $stderr" }
    }
    catch {
        Say "$db FAILED: $($_.Exception.Message)"
        Remove-Item $part -Force -ErrorAction SilentlyContinue
        $failed += $db
        continue
    }

    # Verify by decompressing the whole archive and confirming mysqldump's own
    # completion trailer is the last thing in it. Costs CPU and zero writes, and
    # is the only thing that distinguishes a good archive from a truncated one.
    try {
        $in   = [System.IO.File]::OpenRead($part)
        $dz   = New-Object System.IO.Compression.GZipStream($in, [System.IO.Compression.CompressionMode]::Decompress)
        $buf  = New-Object byte[] 65536
        $tail = ''
        $n    = 0
        while (($read = $dz.Read($buf, 0, $buf.Length)) -gt 0) {
            $n += $read
            if ($read -ge 512) { $tail = [Text.Encoding]::ASCII.GetString($buf, $read - 512, 512) }
            else { $tail = $tail + [Text.Encoding]::ASCII.GetString($buf, 0, $read) }
        }
        $dz.Dispose(); $in.Dispose()
        if ($tail -notmatch 'Dump completed on') { throw "no completion trailer after $n bytes" }
    }
    catch {
        Say "$db FAILED verification: $($_.Exception.Message)"
        Remove-Item $part -Force -ErrorAction SilentlyContinue
        $failed += $db
        continue
    }

    Move-Item $part $final -Force
    $mb = (Get-Item $final).Length / 1MB
    Say ("{0,-18} {1,7:N1} MB" -f $db, $mb)
    $made += $db
}

# --------------------------------------------------------------- retention ---
# Only prune once tonight's set is complete. A failed run must never be the
# reason the last good backup disappears.
if ($failed.Count -eq 0) {
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    $old    = @(Get-ChildItem $DEST -Filter '*.sql.gz' | Where-Object { $_.LastWriteTime -lt $cutoff })
    if ($old.Count) {
        $freed = ($old | Measure-Object Length -Sum).Sum
        $old | Remove-Item -Force
        Say ("pruned {0} file(s) older than {1} days, {2:N0} MB reclaimed" -f $old.Count, $KeepDays, ($freed / 1MB))
    }
} elseif ($made.Count -gt 0) {
    Say "partial set ($($failed -join ', ') failed) - keeping every old backup"
}

$held = @(Get-ChildItem $DEST -Filter '*.sql.gz')
Say ("done in {0:N0}s - {1} of {2} databases, {3} archive(s) held, {4:N0} MB total" -f `
     ((Get-Date) - $started).TotalSeconds, $made.Count, $DATABASES.Count, $held.Count,
     (($held | Measure-Object Length -Sum).Sum / 1MB))

# Keep the log from becoming its own disk problem.
if ((Test-Path $LOG) -and (Get-Item $LOG).Length -gt 256KB) {
    Set-Content $LOG -Value (Get-Content $LOG -Tail 400) -Encoding utf8
}

if ($failed.Count) { exit 1 } else { exit 0 }
