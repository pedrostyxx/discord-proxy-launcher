# Compila o app (sem console, pede elevação UAC ao abrir) em dois formatos:
#   dist\discord-proxy-launcher.zip   pasta do app (discord-proxy-launcher.exe + _internal) — o download principal
#   dist\discord-proxy-launcher.exe   arquivo único, para quem ainda usa o link antigo
# Uso:  powershell -ExecutionPolicy Bypass -File build.ps1 [-NoDedicated]
#
# O servidor dedicado (dconnect.json, fora do Git porque tem o token do app) é
# embutido no app. Sem ele o build para, a não ser que -NoDedicated seja dado:
# um app sem o servidor dedicado só usaria as listas públicas.
#
# Antivírus e o Chrome: o bootloader pronto do PyInstaller (o mesmo dentro de
# milhares de .exe, inclusive de malware) é o que mais gera falsos positivos.
# Por isso o PyInstaller do .venv é instalado com o bootloader compilado aqui,
# o que exige o Visual Studio Build Tools com o workload "C++" (VCTools). O
# formato em pasta é o que menos dispara detecções genéricas: nada é extraído
# para a pasta temporária ao abrir.
param([switch]$NoDedicated)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    python -m venv .venv
    & $python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip falhou (codigo $LASTEXITCODE)" }
}

# --- PyInstaller com bootloader compilado localmente -------------------------
$pyiVersion = (& $python -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
$marker = Join-Path $PSScriptRoot ".venv\bootloader-local.txt"
if (-not (Test-Path $marker) -or (Get-Content $marker -Raw).Trim() -ne $pyiVersion) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    $vs = $null
    if (Test-Path $vswhere) {
        $vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    }
    if (-not $vs) {
        throw "Visual Studio Build Tools (C++) nao encontrado: e preciso para compilar o bootloader. Instale com: winget install Microsoft.VisualStudio.2022.BuildTools --override `"--quiet --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended`""
    }
    $vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
    "Compilando o bootloader do PyInstaller $pyiVersion (uma vez por versao)..."
    cmd /c "`"$vcvars`" >nul && set PYINSTALLER_COMPILE_BOOTLOADER=1 && `"$python`" -m pip install --disable-pip-version-check --force-reinstall --no-deps --no-cache-dir --no-binary pyinstaller pyinstaller==$pyiVersion"
    if ($LASTEXITCODE -ne 0) { throw "a compilacao do bootloader falhou (codigo $LASTEXITCODE)" }
    Set-Content -Path $marker -Value $pyiVersion -Encoding ascii
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
    throw "dconnect.json nao encontrado: o app sairia sem o servidor dedicado. Use -NoDedicated para compilar assim mesmo."
}

$common = @(
    "--noconfirm", "--clean", "--log-level", "WARN",
    "--windowed", "--uac-admin", "--noupx",
    "--name", "discord-proxy-launcher",
    "--icon", "assets\icon.ico",
    "--add-data", "assets\icon.ico;assets",
    "--version-file", "version_info.txt"
) + $extra

foreach ($mode in "--onedir", "--onefile") {
    & $python -m PyInstaller @common $mode main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller $mode falhou (codigo $LASTEXITCODE)" }
}

$folder = Join-Path $PSScriptRoot "dist\discord-proxy-launcher"
$zip = Join-Path $PSScriptRoot "dist\discord-proxy-launcher.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $folder -DestinationPath $zip -CompressionLevel Optimal

foreach ($item in (Get-Item $zip), (Get-Item (Join-Path $PSScriptRoot "dist\discord-proxy-launcher.exe"))) {
    "{0}  ({1:N1} MB)" -f $item.FullName, ($item.Length / 1MB)
}
