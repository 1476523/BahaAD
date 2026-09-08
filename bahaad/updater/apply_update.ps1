# BahaAD self-update helper. Spec: docs/requirements/updater.md
#
# Launched by applier.py launch_apply_and_restart() as an independent background process
# that outlives the main app. Shipped as-is inside main.dist/ (never compiled) because
# its correctness is the precondition for the whole self-update mechanism working at all.
#
# Comments and strings are ASCII-only ON PURPOSE: this runs under Windows PowerShell 5.1
# (powershell.exe), which reads a BOM-less .ps1 in the system ANSI codepage. On a
# non-Latin Windows locale (e.g. Traditional Chinese / cp950) any non-ASCII byte here
# corrupts the token stream and the script fails to parse -- the self-update then dies
# silently. Keep this file 7-bit ASCII.
#
# Wait-Process: Windows will not let us overwrite a running .exe, so the main process
# must have fully exited before we copy over the install directory.
param(
    [int]$MainPid,
    [string]$StagingDir,
    [string]$InstallDir,
    [string]$ExePath
)

$ErrorActionPreference = 'Continue'
try { Start-Transcript -Path (Join-Path $env:TEMP 'bahaad_apply_update.log') -Force | Out-Null } catch {}

if ($MainPid -gt 0) { Wait-Process -Id $MainPid -Timeout 120 -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

# Merge the staged files over the install directory. robocopy (built into Windows) merges
# into existing folders and overwrites changed files; "Copy-Item -Recurse" nests the
# source folder inside the destination instead of merging. robocopy exit codes 0-7 are
# success, 8+ is a real failure (e.g. a file still locked because the old process has not
# fully exited).
robocopy $StagingDir $InstallDir /E /NFL /NDL /NJH /NJS /NP /R:3 /W:2 | Out-Null
$rc = $LASTEXITCODE
if ($rc -ge 8) {
    # Keep the staged files so the user can retry from the update banner (the app still
    # has _pending_update set and will re-lock / show the banner on restart).
    Write-Output "robocopy failed with exit code $rc -- keeping staging dir for retry"
} else {
    Remove-Item -Path $StagingDir -Recurse -Force -ErrorAction SilentlyContinue
}
try { Stop-Transcript | Out-Null } catch {}

# Only relaunch if the old process really exited -- otherwise the single-instance mutex
# would just pop "BahaAD is already running".
$stillRunning = ($MainPid -gt 0) -and [bool](Get-Process -Id $MainPid -ErrorAction SilentlyContinue)
if (-not $stillRunning) { Start-Process -FilePath $ExePath }
