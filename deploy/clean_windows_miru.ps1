param(
    [switch]$Yes,
    [switch]$RemoveApplication
)

$ErrorActionPreference = "Stop"
if (-not $Yes) {
    throw "Re-run with -Yes to delete Miru's local Windows account and cache."
}

Get-Process Miru, miru-pet -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

$StateRoot = Join-Path $env:LOCALAPPDATA "Miru"
if (Test-Path $StateRoot) {
    Remove-Item -Recurse -Force $StateRoot
}

if ($RemoveApplication) {
    $AppRoot = Join-Path $env:LOCALAPPDATA "Programs\Miru"
    $Uninstaller = Join-Path $AppRoot "unins000.exe"
    if (Test-Path $Uninstaller) {
        $Process = Start-Process -FilePath $Uninstaller `
            -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" `
            -Wait -PassThru
        if ($Process.ExitCode -ne 0) {
            throw "Miru uninstaller failed (exit $($Process.ExitCode))."
        }
        Start-Sleep -Milliseconds 500
    }
    if (Test-Path $AppRoot) {
        Remove-Item -Recurse -Force $AppRoot
    }
}

if (Test-Path $StateRoot) {
    throw "Miru state still exists: $StateRoot"
}
Write-Output "Miru Windows local state is clean."
