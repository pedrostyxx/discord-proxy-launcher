# Compila o app (sem console, pede elevação UAC ao abrir) em dist\discord-proxy-launcher.zip,
# com a pasta do app: discord-proxy-launcher.exe + discord-proxy-launcher.pkg + _internal.
# Uso:  powershell -ExecutionPolicy Bypass -File build.ps1 [-NoDedicated]
#
# O servidor dedicado (dconnect.json, fora do Git porque tem o token do app) é
# embutido no app. Sem ele o build para, a não ser que -NoDedicated seja dado:
# um app sem o servidor dedicado só usaria as listas públicas.
#
# Antivírus e o Chrome (medido no VirusTotal, 1.1.2):
# - .exe único com o bootloader pronto do PyInstaller: 3/71 (CrowdStrike, Bkav, SecureAge);
# - bootloader compilado aqui, pacote anexado ao .exe: 2/71 (CrowdStrike, SecureAge);
# - bootloader compilado aqui e o pacote (PKG) num arquivo .pkg ao lado do .exe: 0/71.
# O que os detectores por aprendizado de máquina pegam é o bloco grande de dados
# compactados anexado ao fim do .exe. Por isso: bootloader compilado aqui (exige
# o Visual Studio Build Tools com o workload "C++", VCTools), EXE(append_pkg=False)
# e nada de arquivo único (que precisa do pacote anexado).
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
    $extra = @("--add-data", "$PSScriptRoot\dconnect.json;.")
} else {
    throw "dconnect.json nao encontrado: o app sairia sem o servidor dedicado. Use -NoDedicated para compilar assim mesmo."
}

# --- spec com o pacote fora do .exe (append_pkg=False) -----------------------
$work = Join-Path $PSScriptRoot "build"
New-Item -ItemType Directory -Force $work | Out-Null
$specArgs = @(
    "--windowed", "--uac-admin", "--noupx", "--onedir",
    "--name", "discord-proxy-launcher",
    "--icon", "$PSScriptRoot\assets\icon.ico",
    "--add-data", "$PSScriptRoot\assets\icon.ico;assets",
    "--version-file", "$PSScriptRoot\version_info.txt",
    "--specpath", $work
) + $extra
& (Join-Path $PSScriptRoot ".venv\Scripts\pyi-makespec.exe") @specArgs "$PSScriptRoot\main.py" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pyi-makespec falhou (codigo $LASTEXITCODE)" }
$spec = Join-Path $work "discord-proxy-launcher.spec"
$text = Get-Content $spec -Raw
if ($text -notmatch "exclude_binaries=True,") { throw "spec inesperado: exclude_binaries nao encontrado" }
$text = $text -replace "exclude_binaries=True,", "exclude_binaries=True,`r`n    append_pkg=False,"
[System.IO.File]::WriteAllText($spec, $text)

$dist = Join-Path $PSScriptRoot "dist"
& $python -m PyInstaller --noconfirm --clean --log-level WARN --distpath $dist --workpath $work $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falhou (codigo $LASTEXITCODE)" }

$folder = Join-Path $dist "discord-proxy-launcher"
foreach ($name in "discord-proxy-launcher.exe", "discord-proxy-launcher.pkg") {
    if (-not (Test-Path (Join-Path $folder $name))) { throw "faltou $name na pasta do app" }
}
$single = Join-Path $dist "discord-proxy-launcher.exe"
if (Test-Path $single) { Remove-Item $single }     # sobra de builds antigos (arquivo único)
$zip = Join-Path $dist "discord-proxy-launcher.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $folder -DestinationPath $zip -CompressionLevel Optimal

$item = Get-Item $zip
"{0}  ({1:N1} MB)" -f $item.FullName, ($item.Length / 1MB)
