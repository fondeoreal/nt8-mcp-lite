<#
.SYNOPSIS
    Instalador de NT8-MCP v3: NinjaTrader 8 para cualquier IA, en SOLO LECTURA de ordenes.

.DESCRIPTION
    Copia el proyecto, crea el entorno virtual de Python, instala los AddOns
    NinjaScript, genera un token compartido (mcp_bridge.json) y registra el
    servidor en todos los clientes de IA detectados (Claude Desktop/Code, VS Code,
    Cursor, Windsurf, Antigravity, Continue, Gemini CLI, Codex, LM Studio).
    Se puede volver a ejecutar sin romper una instalacion existente.

.PARAMETER InstallPath
    Donde vive el servidor MCP. Por defecto %LOCALAPPDATA%\NT8-MCP.

.PARAMETER NinjaTraderPath
    Carpeta de usuario de NT8 (la que contiene bin\Custom). Se autodetecta,
    incluso con Documentos redirigido a OneDrive.

.PARAMETER Token
    Token a usar. Si se omite se reutiliza el existente o se genera uno nuevo.

.PARAMETER Port
    Puerto del puente en loopback. Por defecto 36980. (36973 es el ATI de NinjaTrader: prohibido.)

.PARAMETER ForceAddOns
    Copiar los AddOns aunque NinjaTrader este abierto. Por defecto, con NT8 abierto no se tocan:
    usa scripts\deploy_addon.py, que comprueba que no haya posiciones abiertas.

.PARAMETER SkipClients
    No registrar el servidor en ningun cliente de IA.

.PARAMETER SkipClaudeDesktop
    Compatibilidad: equivale a -SkipClients.

.PARAMETER Uninstall
    Desinstalar: quita los AddOns y el registro en Claude Desktop.

.EXAMPLE
    .\install.ps1
.EXAMPLE
    .\install.ps1 -InstallPath D:\Trading\NT8-MCP -Port 41000
.EXAMPLE
    .\install.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string] $InstallPath = (Join-Path $env:LOCALAPPDATA 'NT8-MCP'),
    [string] $NinjaTraderPath,
    [string] $Token,
    [int]    $Port = 36980,
    [switch] $SkipClaudeDesktop,
    [switch] $SkipClients,
    [switch] $ForceAddOns,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
$PayloadPath = Split-Path -Parent $MyInvocation.MyCommand.Definition
# Cada edicion trae su juego de AddOns (Pro: MCPSim + MCPVault; Lite: MCPSimLite). Los de la otra se retiran.
$AllAddOns     = @('MCPBridge.cs', 'MCPSim.cs', 'MCPSimLite.cs', 'MCPVault.cs')
$AddOnFiles    = @($AllAddOns | Where-Object { Test-Path (Join-Path $PayloadPath "ninjatrader\$_") })
$RetiredAddOns = @($AllAddOns | Where-Object { $AddOnFiles -notcontains $_ })

# ---------------------------------------------------------------- utilidades
function Write-Step  { param([string]$m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok    { param([string]$m) Write-Host "    OK  $m" -ForegroundColor Green }
function Write-Info  { param([string]$m) Write-Host "    $m" -ForegroundColor Gray }
function Write-Warn2 { param([string]$m) Write-Host "    !   $m" -ForegroundColor Yellow }
function Fail        { param([string]$m) Write-Host "`nERROR: $m" -ForegroundColor Red; exit 1 }

function Write-Utf8NoBom {
    # PowerShell 5.1 mete BOM con -Encoding utf8, y json.load() de Python
    # revienta con "Unexpected UTF-8 BOM". El JSON se escribe siempre asi.
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Write-Utf8Bom {
    # Los .cs si llevan BOM: asegura que csc lea bien los acentos.
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($true)))
}

function New-Token {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $raw = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''
    return $raw.Substring(0, 28)
}

function Resolve-NinjaTraderPath {
    param([string]$Override)
    if ($Override) {
        if (-not (Test-Path (Join-Path $Override 'bin\Custom'))) {
            Fail "No encuentro 'bin\Custom' dentro de: $Override"
        }
        return $Override
    }
    $candidates = @([Environment]::GetFolderPath('MyDocuments'))
    if ($env:OneDrive)             { $candidates += $env:OneDrive }
    if ($env:OneDriveCommercial)   { $candidates += $env:OneDriveCommercial }
    $candidates += $env:USERPROFILE
    foreach ($base in ($candidates | Where-Object { $_ })) {
        foreach ($docs in @('', 'Documents', 'Documentos')) {
            $p = if ($docs) { Join-Path $base (Join-Path $docs 'NinjaTrader 8') }
                 else       { Join-Path $base 'NinjaTrader 8' }
            if (Test-Path (Join-Path $p 'bin\Custom')) { return $p }
        }
    }
    return $null
}

function Resolve-Python {
    $probes = @(
        [pscustomobject]@{ Exe = 'py';      Pre = @('-3') },
        [pscustomobject]@{ Exe = 'python';  Pre = @() },
        [pscustomobject]@{ Exe = 'python3'; Pre = @() }
    )
    # Sin comillas dobles en el -c: PowerShell 5.1 las elimina al pasar
    # argumentos a un ejecutable nativo y Python recibiria codigo invalido.
    $code = 'import sys; print(sys.version_info[0]*100+sys.version_info[1])'
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        foreach ($p in $probes) {
            if (-not (Get-Command $p.Exe -ErrorAction SilentlyContinue)) { continue }
            $out = & $p.Exe @($p.Pre + @('-c', $code)) 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }  # atrapa el stub de Microsoft Store
            $last = ("$out".Trim() -split "`n")[-1].Trim()
            $n = 0
            if (-not [int]::TryParse($last, [ref]$n)) { continue }
            if ($n -ge 310) {
                $ver = "$([math]::Floor($n / 100)).$($n % 100)"
                return [pscustomobject]@{ Exe = $p.Exe; Pre = $p.Pre; Version = $ver }
            }
        }
    } finally { $ErrorActionPreference = $prev }
    return $null
}

if ($Port -eq 36973) { Fail "El puerto 36973 es el ATI de NinjaTrader. Usa otro (por defecto 36980)." }
if ($SkipClaudeDesktop) { $SkipClients = $true }

# --------------------------------------------------------------------- inicio
Write-Host ""
Write-Host "  NT8-MCP  ::  instalador" -ForegroundColor White
Write-Host "  ------------------------" -ForegroundColor DarkGray

$ntPath = Resolve-NinjaTraderPath -Override $NinjaTraderPath
if (-not $ntPath) {
    Fail @"
No encuentro la carpeta de usuario de NinjaTrader 8.
Esperaba algo como: $([Environment]::GetFolderPath('MyDocuments'))\NinjaTrader 8\bin\Custom
Instala NT8 y abrelo al menos una vez, o pasa la ruta a mano:
    .\install.ps1 -NinjaTraderPath "D:\ruta\NinjaTrader 8"
"@
}
$addOnDir = Join-Path $ntPath 'bin\Custom\AddOns'
Write-Info "NinjaTrader 8 : $ntPath"
Write-Info "Instalacion   : $InstallPath"

# ---------------------------------------------------------------- desinstalar
if ($Uninstall) {
    Write-Step "Desinstalando"
    foreach ($f in $AllAddOns) {
        $t = Join-Path $addOnDir $f
        if (Test-Path $t) { Remove-Item $t -Force; Write-Ok "eliminado $f" }
        else { Write-Info "$f no estaba instalado" }
    }
    $venvPy = Join-Path $InstallPath '.venv\Scripts\python.exe'
    $reg    = Join-Path $InstallPath 'scripts\register_clients.py'
    if (-not $SkipClients -and (Test-Path $venvPy) -and (Test-Path $reg)) {
        & $venvPy $reg --remove
    }
    Write-Host ""
    Write-Warn2 "La carpeta $InstallPath se conserva. Borrala a mano si quieres."
    Write-Warn2 "Reinicia NinjaTrader 8 y Claude Desktop para completar."
    Write-Host ""
    exit 0
}

# ------------------------------------------------------------ 1. comprobaciones
Write-Step "1/7  Comprobando requisitos"

if ($AddOnFiles -notcontains 'MCPBridge.cs' -or
    -not ($AddOnFiles -contains 'MCPSim.cs' -or $AddOnFiles -contains 'MCPSimLite.cs')) {
    Fail "Faltan AddOns en ninjatrader\ junto al instalador. Copia la carpeta entera del proyecto."
}
if (-not (Test-Path (Join-Path $PayloadPath 'src\nt8_mcp\server.py'))) {
    Fail "Falta src\nt8_mcp\server.py junto al instalador. Copia la carpeta entera del proyecto."
}
Write-Ok "ficheros del proyecto presentes"

$py = Resolve-Python
if (-not $py) {
    Fail @"
No encuentro Python 3.10 o superior.
Instalalo desde https://www.python.org/downloads/ marcando 'Add python.exe to PATH',
abre una consola nueva y vuelve a ejecutar este instalador.
"@
}
Write-Ok "Python $($py.Version) ($($py.Exe))"

if (Get-Process NinjaTrader -ErrorAction SilentlyContinue) {
    Write-Warn2 "NinjaTrader 8 esta abierto: tendras que reiniciarlo al terminar."
}

# ------------------------------------------------------------- 2. copiar payload
Write-Step "2/7  Copiando el proyecto"

$samePlace = (Resolve-Path $PayloadPath).Path.TrimEnd('\') -ieq `
             ((New-Item -ItemType Directory -Force $InstallPath).FullName.TrimEnd('\'))
if ($samePlace) {
    Write-Info "ya estas ejecutando desde la carpeta de instalacion; no copio nada"
} else {
    foreach ($item in @('src', 'scripts', 'ninjatrader', 'tests')) {
        $s = Join-Path $PayloadPath $item
        if (Test-Path $s) {
            $d = Join-Path $InstallPath $item
            if (Test-Path $d) { Remove-Item $d -Recurse -Force }
            Copy-Item $s $d -Recurse -Force
        }
    }
    foreach ($item in @('requirements.txt', 'config.example.json', 'README.md', 'PROGRESS.md', 'STRATEGY_ANALYZER.md', 'CLIENTES_IA.md', 'install.ps1', 'Instalar.cmd')) {
        $s = Join-Path $PayloadPath $item
        if (Test-Path $s) { Copy-Item $s (Join-Path $InstallPath $item) -Force }
    }
    Get-ChildItem $InstallPath -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "copiado a $InstallPath"
}

# --------------------------------------------------------------- 3. token/config
Write-Step "3/7  Token y configuracion"

$cfgPath = Join-Path $InstallPath 'config.json'
if (-not $Token) {
    if (Test-Path $cfgPath) {
        $bridgeJson = Join-Path $ntPath 'mcp_bridge.json'
        $srcText = if (Test-Path $bridgeJson) { [System.IO.File]::ReadAllText($bridgeJson) } else { [System.IO.File]::ReadAllText($cfgPath) }
        $existing = [regex]::Match($srcText, '"token"\s*:\s*"([^"]*)"')
        if ($existing.Success -and $existing.Groups[1].Value -and
            $existing.Groups[1].Value -ne 'CAMBIA-ESTE-TOKEN') {
            $Token = $existing.Groups[1].Value
            Write-Info "reutilizo el token de la instalacion existente"
        }
    }
}
if (-not $Token) { $Token = New-Token; Write-Info "token nuevo generado" }

$examplePath = Join-Path $InstallPath 'config.example.json'
if (-not (Test-Path $examplePath)) { Fail "Falta config.example.json en $InstallPath" }

if (Test-Path $cfgPath) {
    Write-Info "config.json ya existe: conservo tus ajustes y solo fijo token y puerto"
    $cfgText = [System.IO.File]::ReadAllText($cfgPath).TrimStart([char]0xFEFF)
} else {
    $cfgText = [System.IO.File]::ReadAllText($examplePath).TrimStart([char]0xFEFF)
}
$cfgText = [regex]::Replace($cfgText, '("token"\s*:\s*")[^"]*(")', "`${1}$Token`${2}")
$cfgText = [regex]::Replace($cfgText, '("port"\s*:\s*)\d+',        "`${1}$Port")
Write-Utf8NoBom -Path $cfgPath -Text $cfgText
Write-Ok "config.json escrito (sin BOM)"
# Fuente unica de verdad: la leen el AddOn v3 y el servidor Python
Write-Utf8NoBom -Path (Join-Path $ntPath 'mcp_bridge.json') -Text "{`r`n  `"token`": `"$Token`",`r`n  `"port`": $Port`r`n}`r`n"
Write-Ok "mcp_bridge.json escrito en $ntPath"

# ----------------------------------------------------------------- 4. AddOns
Write-Step "4/7  Instalando los AddOns en NinjaTrader"

$ntRunning = [bool](Get-Process NinjaTrader -ErrorAction SilentlyContinue)
$installAddOns = (-not $ntRunning) -or $ForceAddOns
if (-not $installAddOns) {
    Write-Warn2 "NinjaTrader esta abierto: NO toco los AddOns (recompilar con posiciones vivas es un riesgo)."
    Write-Warn2 "Cuando estes plano ejecuta:  .venv\Scripts\python.exe scripts\deploy_addon.py"
}
if ($installAddOns) {

New-Item -ItemType Directory -Force $addOnDir | Out-Null

# Las copias de seguridad van FUERA de bin\Custom: NT8 compila esa carpeta en
# recursivo y un backup dentro provoca errores de clase duplicada.
$backupDir = Join-Path $InstallPath ('backups\' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
$backedUp  = $false
foreach ($f in $AllAddOns) {
    $target = Join-Path $addOnDir $f
    if (Test-Path $target) {
        if (-not $backedUp) { New-Item -ItemType Directory -Force $backupDir | Out-Null; $backedUp = $true }
        Copy-Item $target $backupDir -Force
    }
}
if ($backedUp) { Write-Info "copia de los AddOns previos en $backupDir" }

foreach ($f in $AddOnFiles) {
    $src  = Join-Path $InstallPath "ninjatrader\$f"
    $text = [System.IO.File]::ReadAllText($src)

    if ($f -eq 'MCPBridge.cs') {
        $rxToken = '(private\s+const\s+string\s+(?:Default)?Token\s*=\s*")[^"]*(";)'
        $rxPort  = '(private\s+const\s+int\s+(?:Default)?Port\s*=\s*)\d+(\s*;)'
        # Se comprueba que el patron encaje, no que el texto cambie: si el
        # payload ya traia este mismo token, el resultado es identico y valido.
        if (-not [regex]::IsMatch($text, $rxToken)) {
            Fail "No encuentro la linea 'private const string Token = ...' en MCPBridge.cs."
        }
        if (-not [regex]::IsMatch($text, $rxPort)) {
            Fail "No encuentro la linea 'private const int Port = ...' en MCPBridge.cs."
        }
        $text = [regex]::Replace($text, $rxToken, "`${1}$Token`${2}")
        $text = [regex]::Replace($text, $rxPort,  "`${1}$Port`${2}")
        if ($text -notmatch [regex]::Escape($Token)) { Fail "La sustitucion del token en MCPBridge.cs no cuadro." }
    }

    Write-Utf8Bom -Path (Join-Path $addOnDir $f) -Text $text
    Write-Ok "instalado $f"
}
foreach ($f in $RetiredAddOns) {
    $t = Join-Path $addOnDir $f
    if (Test-Path $t) { Remove-Item $t -Force; Write-Ok "retirado $f (de la otra edicion)" }
}

}   # fin de if ($installAddOns)

$stray = Get-ChildItem $addOnDir -Recurse -Filter '*.cs' -ErrorAction SilentlyContinue |
         Where-Object { $_.DirectoryName -ne $addOnDir }
if ($stray) {
    Write-Warn2 "Hay .cs en subcarpetas de AddOns; NT8 los compilara tambien y pueden chocar:"
    $stray | ForEach-Object { Write-Warn2 "  $($_.FullName)" }
}

# ------------------------------------------------------------------ 5. venv
Write-Step "5/7  Entorno virtual de Python"

$venvDir = Join-Path $InstallPath '.venv'
$venvPy  = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    & $py.Exe @($py.Pre + @('-m', 'venv', $venvDir))
    if ($LASTEXITCODE -ne 0) { Fail "No pude crear el entorno virtual en $venvDir" }
    Write-Ok "entorno creado"
} else {
    Write-Info "el entorno ya existia"
}

& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
$reqPath = Join-Path $InstallPath 'requirements.txt'
if (-not (Test-Path $reqPath)) { Write-Utf8NoBom -Path $reqPath -Text "mcp>=1.2.0`r`n" }
& $venvPy -m pip install -r $reqPath --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "Fallo 'pip install -r requirements.txt'. Revisa tu conexion a internet." }
Write-Ok "dependencias instaladas"

# ------------------------------------------------------- 6. autocomprobacion
Write-Step "6/7  Comprobando el servidor MCP"

$env:PYTHONPATH     = Join-Path $InstallPath 'src'
$env:NT8_MCP_CONFIG = $cfgPath
$check = Join-Path $env:TEMP ('nt8mcp_check_' + [guid]::NewGuid().ToString('N') + '.py')
Write-Utf8NoBom -Path $check -Text @'
import asyncio
import nt8_mcp.server as s
print(len(asyncio.run(s.mcp.list_tools())))
'@
try {
    $tools = & $venvPy $check 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "El servidor MCP no arranca:`n$tools" }
    Write-Ok "servidor correcto, $($tools | Select-Object -Last 1) herramientas registradas"
} finally {
    Remove-Item $check -Force -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------ 7. Clientes de IA
Write-Step "7/7  Registrando en los clientes de IA detectados"

if ($SkipClients) {
    Write-Info "omitido (-SkipClients)"
} else {
    & $venvPy (Join-Path $InstallPath 'scripts\register_clients.py')
    if ($LASTEXITCODE -ne 0) { Write-Warn2 "No pude registrar el servidor en algun cliente; ver CLIENTES_IA.md." }
}

# ------------------------------------------------------------------ resumen
Write-Host ""
Write-Host "  Instalacion completada" -ForegroundColor Green
Write-Host "  ---------------------" -ForegroundColor DarkGray
Write-Host "  Carpeta   : $InstallPath"
Write-Host "  AddOns    : $addOnDir"
Write-Host "  Puerto    : 127.0.0.1:$Port"
Write-Host "  Token     : $Token"
Write-Host ""
Write-Host "  Ahora, en este orden:" -ForegroundColor White
if ($installAddOns) {
    Write-Host "    1. En NinjaTrader: Editor NinjaScript -> F5, o reinicia NT8."
    Write-Host "       En Output debe salir: MCPBridge v3.0.0 (solo lectura de ordenes) escuchando en 127.0.0.1:$Port"
} else {
    Write-Host "    1. Cuando estes plano: .venv\Scripts\python.exe scripts\deploy_addon.py  y luego F5 en NT8."
}
Write-Host "    2. Reinicia las apps de IA (Claude Desktop desde la bandeja, VS Code, etc.)."
Write-Host "    3. Verifica con:"
Write-Host "       `"$venvPy`" `"$(Join-Path $InstallPath 'scripts\smoke_test.py')`"" -ForegroundColor Gray
Write-Host "    4. Otras IAs por HTTP: scripts\start_http.cmd  (detalles en CLIENTES_IA.md)"
Write-Host ""
Write-Host "  SOLO LECTURA: ninguna IA puede enviar, modificar ni cancelar ordenes." -ForegroundColor Yellow
Write-Host ""
