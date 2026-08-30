param(
    [switch]$SkipDependencies,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Resolve-CommandPath([string[]]$Candidates) {
    foreach ($Candidate in $Candidates) {
        $Command = Get-Command $Candidate -ErrorAction SilentlyContinue
        if ($Command) { return $Command.Source }
    }
    return $null
}

$Python = Resolve-CommandPath @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    "py",
    "python"
)
if (-not $Python) { throw "Python 3.12 is required on the Windows build machine." }

$VenvPython = Join-Path $Root ".venv-windows\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    if ((Split-Path -Leaf $Python) -eq "py.exe") {
        & $Python -3.12 -m venv (Join-Path $Root ".venv-windows")
    } else {
        & $Python -m venv (Join-Path $Root ".venv-windows")
    }
    if ($LASTEXITCODE -ne 0) { throw "Creating the Windows Python environment failed (exit $LASTEXITCODE)." }
}

if (-not $SkipDependencies) {
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Updating pip failed (exit $LASTEXITCODE)." }
    & $VenvPython -m pip install -r requirements.txt "pyinstaller>=6.11,<7"
    if ($LASTEXITCODE -ne 0) { throw "Installing Python dependencies failed (exit $LASTEXITCODE)." }
}

$Cargo = Resolve-CommandPath @("cargo")
if (-not $Cargo) {
    $Cargo = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"
}
if (-not (Test-Path $Cargo)) { throw "Rust cargo is required on the Windows build machine." }

& $Cargo test --manifest-path (Join-Path $Root "src-tauri\Cargo.toml")
if ($LASTEXITCODE -ne 0) { throw "Windows pet tests failed (exit $LASTEXITCODE)." }
& $Cargo build --release --manifest-path (Join-Path $Root "src-tauri\Cargo.toml")
if ($LASTEXITCODE -ne 0) { throw "Windows pet build failed (exit $LASTEXITCODE)." }

Remove-Item -Recurse -Force (Join-Path $Root "build\miru_windows") -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Root "dist\Miru") -ErrorAction SilentlyContinue
& $VenvPython -m PyInstaller miru_windows.spec --clean --noconfirm --workpath build\miru_windows
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)." }

$PetSource = Join-Path $Root "src-tauri\target\release\app.exe"
$PetTarget = Join-Path $Root "dist\Miru\miru-pet.exe"
if (-not (Test-Path $PetSource)) { throw "Tauri pet binary was not produced: $PetSource" }
Copy-Item $PetSource $PetTarget -Force

if (-not $SkipInstaller) {
    $IsccCandidates = @(
        "iscc",
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    $Iscc = Resolve-CommandPath $IsccCandidates
    if (-not $Iscc) { throw "Inno Setup 6 is required to build the installer." }
    New-Item -ItemType Directory -Force (Join-Path $Root "dist\windows-installer") | Out-Null
    & $Iscc (Join-Path $Root "packaging\windows\Miru.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)." }
}

Write-Output "Windows build complete."
