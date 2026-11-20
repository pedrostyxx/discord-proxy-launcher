# Compila dist\discord-proxy-launcher.exe (arquivo único, sem console, pede elevação UAC ao abrir).
# Uso:  powershell -ExecutionPolicy Bypass -File build.ps1 [-NoDedicated]
#
# O servidor dedicado (dconnect.json, fora do Git porque tem o token do app) é
# embutido no .exe. Sem ele o build para, a não ser que -NoDedicated seja dado:
# um .exe sem o servidor dedicado só usaria as listas públicas.
param([switch]$NoDedicated)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    python -m venv .venv
    & $python -m pip install --disable-pip-version-check -r requirements.txt
}

& $python tools\make_icon.py

$extra = @()
# Mensagens so em ASCII: o PowerShell 5.1 le este arquivo (sem BOM) na codificacao do Windows.
if ($NoDedicated) {
    "Servidor dedicado: NAO incluido (-NoDedicated)"
} elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot "dconnect.json")) {
    # Valida com o mesmo leitor do app (aceita BOM e UTF-16) e nunca mostra o token.
    & $python -c "import sys; from launcher import dconnect; s = dconnect.load('dconnect.json'); sys.exit(1) if s is None else print('Servidor dedicado: ' + s.host + s.path[:6] + '...')"
    if ($LASTEXITCODE -ne 0) { throw "dconnect.json invalido (veja server/README.md)" }
    $extra = @("--add-data", "dconnect.json;.")
} else {
    throw "dconnect.json nao encontrado: o .exe sairia sem o servidor dedicado. Use -NoDedicated para compilar assim mesmo."
}

& $python -m PyInstaller --noconfirm --clean --log-level WARN `
    --onefile --windowed --uac-admin --noupx `
    --name discord-proxy-launcher `
    --icon assets\icon.ico `
    --add-data "assets\icon.ico;assets" `
    --version-file version_info.txt `
    @extra `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou (codigo $LASTEXITCODE)" }

$exe = Get-Item (Join-Path $PSScriptRoot "dist\discord-proxy-launcher.exe")
"{0}  ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB)
