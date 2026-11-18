# Compila dist\discord-proxy-launcher.exe (arquivo único, sem console, pede elevação UAC ao abrir).
# Uso:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    python -m venv .venv
    & $python -m pip install --disable-pip-version-check -r requirements.txt
}

& $python tools\make_icon.py

& $python -m PyInstaller --noconfirm --clean --log-level WARN `
    --onefile --windowed --uac-admin --noupx `
    --name discord-proxy-launcher `
    --icon assets\icon.ico `
    --add-data "assets\icon.ico;assets" `
    --version-file version_info.txt `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou (código $LASTEXITCODE)" }

$exe = Get-Item (Join-Path $PSScriptRoot "dist\discord-proxy-launcher.exe")
"{0}  ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB)
