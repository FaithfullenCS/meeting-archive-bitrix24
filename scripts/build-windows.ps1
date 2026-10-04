[CmdletBinding()]
param([string]$Python = ".venv\Scripts\python.exe")
$ErrorActionPreference = "Stop"
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Push-Location -LiteralPath $taskRoot
try {
    & $Python -X utf8 scripts/build_windows.py
    if ($LASTEXITCODE -ne 0) { throw "Windows build failed." }
} finally {
    Pop-Location
}
