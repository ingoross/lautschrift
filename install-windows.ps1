param([string]$Python, [switch]$SkipModel)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    if ($Python) {
        & $Python -m venv .venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw 'Python 3.12 oder 3.13 (64 Bit) installieren oder -Python C:\Pfad\python.exe angeben.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python-Umgebung konnte nicht erstellt werden.' }
}
& $venvPython -m pip install -r requirements-windows.txt
if ($LASTEXITCODE -ne 0) { throw 'Installation der Python-Pakete fehlgeschlagen.' }
if (-not $SkipModel) {
    & $venvPython scripts\download_model.py
    if ($LASTEXITCODE -ne 0) { throw 'Modell-Download fehlgeschlagen.' }
}
Write-Host 'Installation fertig. Starten mit start-windows.cmd'
