# Instala o plugin FortSmart no QGIS (todos os perfis).
param(
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Source = Join-Path $ProjectRoot "qgis_plugin\fortsmart_gs3_import"

if (-not (Test-Path $Source)) {
    throw "Pasta do plugin nao encontrada: $Source"
}

$profilesRoot = Join-Path $env:APPDATA "QGIS\QGIS3\profiles"
if (-not (Test-Path $profilesRoot)) {
    New-Item -ItemType Directory -Path $profilesRoot -Force | Out-Null
}

$profiles = @()
if ($Profile) {
    $profiles = @($Profile)
} else {
    $profiles = Get-ChildItem $profilesRoot -Directory | Where-Object { $_.Name -ne "profiles.ini" } | ForEach-Object { $_.Name }
    if (-not $profiles) { $profiles = @("default") }
}

Write-Host "FortSmart Precision Ag - instalacao QGIS"
Write-Host "Origem: $Source"
Write-Host "Perfis: $($profiles -join ', ')"
Write-Host ""

foreach ($prof in $profiles) {
    $Target = Join-Path $profilesRoot "$prof\python\plugins\fortsmart_gs3_import"
    New-Item -ItemType Directory -Path (Split-Path $Target -Parent) -Force | Out-Null
    if (Test-Path $Target) {
        Remove-Item -Recurse -Force $Target
    }
    Copy-Item -Recurse -Force $Source $Target

    # Remove UTF-8 BOM (QGIS marca o plugin como quebrado se metadata.txt tiver BOM)
    Get-ChildItem $Target -Recurse -Include *.py,*.txt,*.svg,*.md -File -ErrorAction SilentlyContinue | ForEach-Object {
        $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            [System.IO.File]::WriteAllBytes($_.FullName, $bytes[3..($bytes.Length - 1)])
        }
    }
    Get-ChildItem $Target -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    Write-Host "[OK] $Target"

    # Ativar plugin no QGIS3.ini
    $iniPath = Join-Path $profilesRoot "$prof\QGIS\QGIS3.ini"
    if (Test-Path $iniPath) {
        $content = Get-Content $iniPath -Raw
        if ($content -notmatch "\[PythonPlugins\]") {
            Add-Content $iniPath "`n[PythonPlugins]`nfortsmart_gs3_import=true"
        } elseif ($content -match "fortsmart_gs3_import\s*=\s*false") {
            (Get-Content $iniPath) -replace "fortsmart_gs3_import\s*=\s*false", "fortsmart_gs3_import=true" | Set-Content $iniPath
        } elseif ($content -notmatch "fortsmart_gs3_import") {
            $lines = Get-Content $iniPath
            $idx = ($lines | Select-String -Pattern "^\[PythonPlugins\]" | Select-Object -First 1).LineNumber
            if ($idx) {
                $before = $lines[0..($idx)]
                $after = $lines[($idx + 1)..($lines.Length - 1)]
                ($before + "fortsmart_gs3_import=true" + $after) | Set-Content $iniPath
            }
        }
        Write-Host "     Plugin ativado em QGIS3.ini"
    }
}

Write-Host ""
Write-Host "Instalado com sucesso."
Write-Host ""
Write-Host "IMPORTANTE:"
Write-Host "  1. Feche o QGIS completamente (se estiver aberto)"
Write-Host "  2. Abra o QGIS de novo"
Write-Host "  3. Complementos - Instalados - procure: FortSmart Precision Ag"
Write-Host "  4. Barra de ferramentas: Importar FortSmart / Exportar FortSmart"
Write-Host ""
Write-Host "NAO confundir com 'FortSmart Precision Planner' (outro plugin)."
