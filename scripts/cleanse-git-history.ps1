<#
Remediation script to create a mirror backup and remove `.env` from all commits using git-filter-repo.

WARNING: This script will modify a mirrored repository on disk. It will NOT force-push anything to origin
unless you explicitly pass -Push and confirm. You MUST rotate any exposed credentials (Discord bot token,
Hugging Face tokens, etc.) before pushing the cleaned history to a remote.

Usage (PowerShell):
  # Dry-run (recommended):
  .\cleanse-git-history.ps1

  # Run and (optionally) push cleaned mirror (will still require confirmation):
  .\cleanse-git-history.ps1 -Push -RemoteUrl "https://github.com/youruser/DISCORD-BOT.git"

This script assumes your repository path contains spaces and uses absolute Windows paths.
#>

param(
    # Accept a loose value for Push (object) and normalize below so accidental empty-string
    # or stray positional args won't cause a type conversion error when binding.
    [object]$Push = $null,
    [string]$RemoteUrl = ''
)

# Normalize $Push into a strict boolean $PushRequested for later checks
$PushRequested = $false
if ($Push -is [System.Management.Automation.SwitchParameter]) {
    $PushRequested = $Push.IsPresent
} elseif ($null -ne $Push -and $Push -ne '') {
    try { $PushRequested = [bool]$Push } catch { $PushRequested = $true }
}

function Write-Header($msg){ Write-Host "`n=== $msg`n" -ForegroundColor Cyan }

$ErrorActionPreference = 'Stop'

Write-Header "Preflight checks"

# Configure these paths for your environment if needed
$RepoPath = 'F:/STARK-whiteout survival bot/DISCORD BOT'
$MirrorName = 'DISCORD-BOT-mirror-backup.git'

Write-Host "Repository path: $RepoPath"
Write-Host "Mirror directory name: $MirrorName"

Write-Header "1) Ensure git-filter-repo is installed (python package)"
try{
    python -m git_filter_repo --version > $null 2>&1
    Write-Host "git-filter-repo is available via python -m git_filter_repo" -ForegroundColor Green
} catch {
    Write-Host "git-filter-repo not found. Installing via pip..." -ForegroundColor Yellow
    python -m pip install --upgrade --user git-filter-repo
}

Write-Header "2) Add safe.directory entries so git clone --mirror works on Windows with spaces"
git config --global --add safe.directory "$RepoPath"
git config --global --add safe.directory "$RepoPath/.git"
Write-Host "Added safe.directory entries (no-op if they already existed)."

Write-Header "3) Create a bare mirror backup (this contains full unfiltered history)"
$Parent = Split-Path $RepoPath -Parent
Set-Location -Path $Parent

if (Test-Path -Path (Join-Path $Parent $MirrorName)) {
    Write-Host "Mirror directory already exists: $MirrorName" -ForegroundColor Yellow
    Write-Host "If you want to recreate it, remove or rename that directory first and re-run this script." -ForegroundColor Yellow
} else {
    Write-Host "Running: git clone --mirror \"$RepoPath\" \"$MirrorName\""
    git clone --mirror "$RepoPath" "$MirrorName"
}

Write-Header "4) Enter the mirror repo"
Set-Location -Path (Join-Path $Parent $MirrorName)

Write-Header "5) Run git-filter-repo to remove .env from ALL commits"
Write-Host "Using: python -m git_filter_repo --invert-paths --path .env"
# use --path (singular) for one path; --paths-from-file if you have many
python -m git_filter_repo --invert-paths --path .env

Write-Header "6) Maintenance: expire reflogs and run aggressive gc"
git reflog expire --expire=now --all
git gc --prune=now --aggressive

Write-Header "7) Verify offending blob(s) are removed"
# If you know the exact blob id, put it here. Example blob from GitHub scan:
$OffendingBlob = '1b94730e90b026ed4e30be32e83bb913e479d3dc'
Write-Host "Searching history for blob id: $OffendingBlob"
$found = git rev-list --objects --all | Select-String $OffendingBlob -SimpleMatch
if ($found) {
    Write-Host "WARNING: Offending blob still exists in mirror (see matches):" -ForegroundColor Red
    $found
    Write-Host "If the blob still exists, do NOT push the mirror to remote. Inspect the repo and rerun git-filter-repo with additional paths." -ForegroundColor Red
} else {
    Write-Host "Offending blob not found in mirror history." -ForegroundColor Green
}

Write-Header "8) Next steps & safe push (manual confirmation required)"
Write-Host "IMPORTANT: You MUST rotate/revoke exposed credentials (Discord token, Hugging Face tokens) before pushing cleaned history to GitHub."
Write-Host "If you still need to rotate secrets, stop now and rotate them in provider dashboards."

if (-not $PushRequested) {
    Write-Host "The script stopped before pushing. To push the cleaned mirror run this script with -Push -RemoteUrl '<remote>'" -ForegroundColor Yellow
    Write-Host "Example: .\cleanse-git-history.ps1 -Push -RemoteUrl \"https://github.com/youruser/DISCORD-BOT.git\"" -ForegroundColor Cyan
    exit 0
}
if ($PushRequested -and (-not $RemoteUrl)) {
    Write-Host "-Push was specified but -RemoteUrl is empty. Please pass the remote URL." -ForegroundColor Red
    exit 1
}

Write-Host "You asked to push cleaned mirror to: $RemoteUrl" -ForegroundColor Yellow
$confirm = Read-Host 'Type YES to continue and force-push the cleaned mirror to the remote (this rewrites remote history)'
if ($confirm -ne 'YES') {
    Write-Host "Push cancelled by user." -ForegroundColor Yellow
    exit 0
}

Write-Host "Pushing cleaned mirror to remote (force mirror push)..." -ForegroundColor Cyan
git push --force --mirror "$RemoteUrl"

Write-Header "Done"
Write-Host "If the remote rejects the push due to repository rules, follow the GitHub messages and consider opening a GitHub Support ticket."
Write-Host "After successful push, remind collaborators to reclone the repository." -ForegroundColor Green
