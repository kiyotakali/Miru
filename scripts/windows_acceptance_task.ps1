param(
    [ValidateSet(
        "Launch", "Stop", "Status", "UiStatus", "Minimize", "Restore",
        "Close", "Hotkey", "Screenshot", "Click"
    )]
    [string]$Action = "Status",
    [int]$DebugPort = 9222,
    [int]$X = 0,
    [int]$Y = 0,
    [int]$SecondX = 0,
    [int]$SecondY = 0
)

$ErrorActionPreference = "Stop"
$taskName = "MiruWindowsAcceptance"
$launcherPath = Join-Path $env:TEMP "miru-windows-acceptance.cmd"
$miruExe = Join-Path $env:LOCALAPPDATA "Programs\Miru\Miru.exe"

function Stop-MiruAcceptance {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Remove-Item -LiteralPath $launcherPath -Force -ErrorAction SilentlyContinue
}

function New-MiruInteractivePrincipal {
    return New-ScheduledTaskPrincipal `
        -UserId ("{0}\{1}" -f $env:COMPUTERNAME, $env:USERNAME) `
        -LogonType Interactive `
        -RunLevel Limited
}

if ($Action -eq "Stop") {
    Stop-MiruAcceptance
    Get-Process -Name "Miru", "miru-pet" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Output "Miru Windows acceptance runtime stopped."
    exit 0
}

if ($Action -in @("UiStatus", "Minimize", "Restore", "Close", "Hotkey", "Screenshot", "Click")) {
    $probeTaskName = "MiruWindowsUiProbe"
    $probeResult = Join-Path $env:TEMP "miru-windows-ui-result.json"
    $probeScreenshot = Join-Path $env:TEMP "miru-windows-ui.png"
    $python = "C:\MiruDev\.venv-windows\Scripts\pythonw.exe"
    $probe = "C:\MiruDev\scripts\windows_ui_probe.py"
    if (-not (Test-Path -LiteralPath $python)) { throw "Windows test Python is missing" }
    if (-not (Test-Path -LiteralPath $probe)) { throw "Windows UI probe is missing" }

    $existingProbe = Get-ScheduledTask -TaskName $probeTaskName -ErrorAction SilentlyContinue
    if ($existingProbe) {
        Stop-ScheduledTask -TaskName $probeTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $probeTaskName -Confirm:$false
    }
    Remove-Item -LiteralPath $probeResult -Force -ErrorAction SilentlyContinue
    if ($Action -in @("Screenshot", "Click")) {
        Remove-Item -LiteralPath $probeScreenshot -Force -ErrorAction SilentlyContinue
    }

    $probeActionName = switch ($Action) {
        "UiStatus" { "status" }
        "Minimize" { "minimize" }
        "Restore" { "restore" }
        "Close" { "close" }
        "Hotkey" { "hotkey" }
        "Screenshot" { "screenshot" }
        "Click" { "click" }
    }
    $arguments = '"{0}" --action {1} --result "{2}"' -f `
        $probe, $probeActionName, $probeResult
    if ($Action -in @("Screenshot", "Click")) {
        $arguments += ' --screenshot-path "{0}"' -f $probeScreenshot
    }
    if ($Action -eq "Click") {
        $arguments += ' --x {0} --y {1}' -f $X, $Y
        if ($SecondX -ne 0 -or $SecondY -ne 0) {
            $arguments += ' --second-x {0} --second-y {1}' -f $SecondX, $SecondY
        }
    }

    $actionSpec = New-ScheduledTaskAction -Execute $python -Argument $arguments
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask `
        -TaskName $probeTaskName `
        -Action $actionSpec `
        -Principal (New-MiruInteractivePrincipal) `
        -Settings $settings `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $probeTaskName

    $deadline = (Get-Date).AddSeconds(20)
    while (-not (Test-Path -LiteralPath $probeResult)) {
        if ((Get-Date) -ge $deadline) { throw "Windows UI probe timed out" }
        Start-Sleep -Milliseconds 200
    }
    Get-Content -LiteralPath $probeResult -Raw
    Unregister-ScheduledTask -TaskName $probeTaskName -Confirm:$false
    exit 0
}

if ($Action -eq "Launch") {
    if (-not (Test-Path -LiteralPath $miruExe)) {
        throw "Installed Miru.exe was not found at $miruExe"
    }

    Stop-MiruAcceptance
    @(
        "@echo off",
        "set MIRU_WEBVIEW_DEBUG_PORT=$DebugPort",
        ('"{0}"' -f $miruExe)
    ) | Set-Content -LiteralPath $launcherPath -Encoding Ascii

    $actionSpec = New-ScheduledTaskAction -Execute $launcherPath
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $actionSpec `
        -Principal (New-MiruInteractivePrincipal) `
        -Settings $settings `
        -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 2
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$processes = @(Get-Process -Name "Miru", "miru-pet" -ErrorAction SilentlyContinue |
    Select-Object ProcessName, Id, SessionId, StartTime)
$listening = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in @(5001, $DebugPort) } |
    Select-Object LocalAddress, LocalPort, OwningProcess)

[pscustomobject]@{
    Task = if ($task) { $task.State.ToString() } else { "Missing" }
    Processes = $processes
    Listening = $listening
} | ConvertTo-Json -Depth 4
