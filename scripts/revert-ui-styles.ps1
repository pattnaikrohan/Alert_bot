# Put the UI back exactly as it was before the "modern / classic styles" change of 2 Nov 2026.
# Restores the portal source, the built portal and the bot's Python code from the snapshot, then tells you to restart.
#
#   powershell -ExecutionPolicy Bypass -File scripts\revert-ui-styles.ps1
#
# The snapshot stays in .backups\ui-before-styles-2026-11-02, so you can revert more than once.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$snap = Join-Path $root ".backups\ui-before-styles-2026-11-02"
if (-not (Test-Path $snap)) { throw "Snapshot not found: $snap" }

foreach ($pair in @(
    @("portal-src", "portal\src"),
    @("portal-dist", "portal\dist"),
    @("portal-public", "portal\public"),
    @("alertbot", "src\alertbot"),
    @("tests", "tests")
)) {
    $from = Join-Path $snap $pair[0]
    $to = Join-Path $root $pair[1]
    Write-Host "Restoring $($pair[1]) ..."
    if (Test-Path $to) { Remove-Item -Recurse -Force $to }
    Copy-Item -Recurse $from $to
}

Write-Host ""
Write-Host "Reverted. Restart the web server (uv run --all-extras alertbot serve) and refresh the browser."
