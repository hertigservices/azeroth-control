# Keep src\modules up to date with upstream WITHOUT losing this realm's custom work.
#
# The model
# ---------
#   master / main  - pure upstream. Never commit here, never edit files here.
#   realm/custom   - our branch. Every local change lives here as a commit.
#
# An update is therefore a MERGE of upstream into realm/custom, not a checkout.
# Git can then tell the two apart forever, conflicts surface as conflicts instead
# of silently overwriting work, and `git rerere` remembers how each conflict was
# resolved so the same fight is not re-fought on the next update.
#
# Usage
#   .\module-sync.ps1                      # status for every module
#   .\module-sync.ps1 -Action Preview -Module mod-playerbots
#   .\module-sync.ps1 -Action Merge   -Module mod-playerbots
#
# Preview is read-only: it reports which upstream commits are pending and, for
# every file both sides changed, whether the merge would conflict. Run it before
# Merge and you never start a merge blind.
#
# Merge refuses to run unless the tree is clean, the module is on realm/custom,
# and no compile is in flight. It tags the pre-merge state first, so the escape
# hatch is always `git reset --hard <tag>`.
param(
  [ValidateSet('Status','Preview','Merge')]
  [string]$Action = 'Status',
  [string]$Module,
  [switch]$NoFetch          # skip the network round trip, report from cached refs
)
$ErrorActionPreference = 'Continue'
$HUB  = if ($env:AZCTL_HOME) { $env:AZCTL_HOME } else { Split-Path -Parent $PSScriptRoot }
$MODS = Join-Path $HUB 'src\modules'
$CUSTOM = 'realm/custom'

# Windows PowerShell 5.1 turns a native command's stderr into ErrorRecord objects
# that land in the OUTPUT stream, so git's "LF will be replaced by CRLF" warnings
# would otherwise be counted as porcelain lines and break .Trim() on the result.
# Every git call goes through these two helpers, which keep strings only.
function G { param([string]$repo, [Parameter(ValueFromRemainingArguments)]$rest)
  $out = & git -C $repo @rest 2>$null
  @($out | Where-Object { $_ -is [string] })
}
function G1 { param([string]$repo, [Parameter(ValueFromRemainingArguments)]$rest)
  $out = G $repo @rest
  if ($out.Count -eq 0) { return '' }
  return ([string]$out[0]).Trim()
}

# Upstream's default branch is master for most of these and main for others;
# ask the remote rather than guessing per module.
function Upstream-Branch($repo) {
  foreach ($b in 'master','main') {
    $null = & git -C $repo rev-parse --verify -q "origin/$b" 2>$null
    if ($LASTEXITCODE -eq 0) { return $b }
  }
  return $null
}

function Module-Dirs {
  Get-ChildItem $MODS -Directory -Filter 'mod-*' | Where-Object { Test-Path (Join-Path $_.FullName '.git') }
}

function Build-Running {
  [bool](Get-Process -Name cl,MSBuild -ErrorAction SilentlyContinue)
}

function Show-Status {
  "{0,-30} {1,-14} {2,-7} {3,-8} {4}" -f 'MODULE','BRANCH','DIRTY','BEHIND','UPSTREAM HEAD'
  "{0,-30} {1,-14} {2,-7} {3,-8} {4}" -f ('-'*30),('-'*14),('-'*7),('-'*8),('-'*20)
  foreach ($m in Module-Dirs) {
    $r = $m.FullName
    $branch = (& git -C $r rev-parse --abbrev-ref HEAD).Trim()
    $dirty  = @(& git -C $r status --porcelain).Count
    $ub = Upstream-Branch $r
    if (-not $ub) {
      "{0,-30} {1,-14} {2,-7} {3,-8} {4}" -f $m.Name,$branch,$dirty,'-','(no remote - local only)'
      continue
    }
    if (-not $NoFetch) { $null = & git -C $r fetch origin $ub --quiet 2>$null }
    $behind = (& git -C $r rev-list --count "HEAD..origin/$ub").Trim()
    $head   = (& git -C $r log -1 --format='%h %ad %s' --date=short "origin/$ub").Trim()
    if ($head.Length -gt 46) { $head = $head.Substring(0,46) + '...' }
    "{0,-30} {1,-14} {2,-7} {3,-8} {4}" -f $m.Name,$branch,$dirty,$behind,$head
  }
  ""
  "Behind > 0 with local commits on $CUSTOM means: run -Action Preview before touching it."
}

function Show-Preview($name) {
  $r = Join-Path $MODS $name
  if (-not (Test-Path (Join-Path $r '.git'))) { "no git repo: $name"; return }
  $ub = Upstream-Branch $r
  if (-not $ub) { "$name has no remote - nothing to merge from."; return }
  if (-not $NoFetch) { $null = & git -C $r fetch origin $ub --quiet 2>$null }

  $behind = (& git -C $r rev-list --count "HEAD..origin/$ub").Trim()
  "=== $name : $behind upstream commit(s) pending on origin/$ub ==="
  if ($behind -eq '0') { "Already current."; return }
  ""
  & git -C $r log --format='  %h %ad %s' --date=short "HEAD..origin/$ub"
  ""

  # Merge base is where our branch left upstream; files changed on BOTH sides
  # since then are the only ones that can conflict.
  $base = (& git -C $r merge-base HEAD "origin/$ub").Trim()
  $ours   = & git -C $r diff --name-only $base HEAD
  $theirs = & git -C $r diff --name-only $base "origin/$ub"
  $both   = $ours | Where-Object { $theirs -contains $_ }
  "Files changed by us: $($ours.Count)   by upstream: $($theirs.Count)   by both: $($both.Count)"
  if (-not $both) { "No overlap - this merge should be clean."; return }
  ""
  "{0,-52} {1,-10} {2}" -f 'FILE (changed by both)','CONFLICTS','OURS/THEIRS'
  $tmp = Join-Path $env:TEMP "modsync-$PID"
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  $clean = 0; $conf = 0
  foreach ($f in $both) {
    & git -C $r show "${base}:$f"        | Out-File -Encoding utf8 "$tmp\base"   -ErrorAction SilentlyContinue
    & git -C $r show "origin/${ub}:$f"   | Out-File -Encoding utf8 "$tmp\theirs" -ErrorAction SilentlyContinue
    & git -C $r show "HEAD:$f"           | Out-File -Encoding utf8 "$tmp\ours"   -ErrorAction SilentlyContinue
    $null = & git merge-file -p "$tmp\ours" "$tmp\base" "$tmp\theirs" 2>$null
    $n = $LASTEXITCODE
    if ($n -eq 0) { $clean++ } else { $conf++ }
    $o = (& git -C $r diff --numstat $base HEAD -- $f) -split '\s+'
    $t = (& git -C $r diff --numstat $base "origin/$ub" -- $f) -split '\s+'
    $short = $f -replace '^src/',''
    if ($short.Length -gt 50) { $short = '...' + $short.Substring($short.Length-47) }
    "{0,-52} {1,-10} {2}" -f $short, $n, ("+{0}/-{1} vs +{2}/-{3}" -f $o[0],$o[1],$t[0],$t[1])
  }
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
  ""
  "auto-merges clean: $clean    needs hand resolution: $conf"
  ""
  "Then: .\module-sync.ps1 -Action Merge -Module $name"
}

function Do-Merge($name) {
  $r = Join-Path $MODS $name
  if (-not (Test-Path (Join-Path $r '.git'))) { "no git repo: $name"; return }
  $ub = Upstream-Branch $r
  if (-not $ub) { "$name has no remote - nothing to merge from."; return }

  # Guard rails. Each of these has cost someone a day somewhere.
  if (Build-Running) {
    "REFUSING: a compile is running (cl.exe/MSBuild). Merging would change source"
    "files underneath it and produce a binary matching no consistent source state."
    return
  }
  $branch = (& git -C $r rev-parse --abbrev-ref HEAD).Trim()
  if ($branch -ne $CUSTOM) {
    "REFUSING: $name is on '$branch', not '$CUSTOM'."
    "Upstream must be merged INTO our branch, never the other way round."
    return
  }
  $dirty = @(& git -C $r status --porcelain)
  if ($dirty.Count -gt 0) {
    "REFUSING: working tree is dirty ($($dirty.Count) entries). Commit first -"
    "an uncommitted change is exactly what a merge is able to destroy."
    $dirty | Select-Object -First 10 | ForEach-Object { "  $_" }
    return
  }

  if (-not $NoFetch) { $null = & git -C $r fetch origin $ub --quiet 2>$null }
  $behind = (& git -C $r rev-list --count "HEAD..origin/$ub").Trim()
  if ($behind -eq '0') { "$name is already current."; return }

  # Escape hatch before anything is touched.
  $tag = "pre-merge-{0:yyyyMMdd-HHmmss}" -f (Get-Date)
  $null = & git -C $r tag $tag
  "Tagged pre-merge state as $tag  (undo with: git -C $r reset --hard $tag)"

  "Merging origin/$ub into $CUSTOM ($behind commits) ..."
  $out = & git -C $r merge --no-ff "origin/$ub" -m "merge: upstream origin/$ub into $CUSTOM" 2>&1
  $out | ForEach-Object { "  $_" }

  $conflicts = @(& git -C $r diff --name-only --diff-filter=U)
  if ($conflicts.Count -gt 0) {
    ""
    "MERGE STOPPED WITH $($conflicts.Count) CONFLICTED FILE(S) - this is normal, not a failure:"
    $conflicts | ForEach-Object { "  $_" }
    ""
    "Resolve each, then:  git -C $r add <file>  &&  git -C $r commit"
    "Abandon instead with: git -C $r merge --abort"
    "rerere is on, so each resolution is remembered for next time."
  } else {
    ""
    "Merged clean. NOTHING IS LIVE UNTIL YOU REBUILD:"
    "  powershell -File $PSScriptRoot\rebuild.ps1 -BuildOnly"
    "then stop the world and rerun with -InstallOnly."
  }
}

switch ($Action) {
  'Status'  { Show-Status }
  'Preview' { if (-not $Module) { "-Module is required for Preview"; break }; Show-Preview $Module }
  'Merge'   { if (-not $Module) { "-Module is required for Merge";   break }; Do-Merge   $Module }
}
