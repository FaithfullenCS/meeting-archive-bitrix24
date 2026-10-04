[CmdletBinding()]
param([string]$Python = ".venv\Scripts\python.exe")
$ErrorActionPreference = "Stop"
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Push-Location -LiteralPath $taskRoot
try {
    $taskUv = (& $Python -c "import uv; print(uv.find_uv_bin())").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot locate the pinned uv runtime." }
    foreach ($profile in @("base", "dev")) {
        $destination = if ($profile -eq "dev") { "requirements-dev.lock" } else { "requirements.lock" }
        $arguments = @("pip", "compile", "pyproject.toml", "--python-version", "3.12",
            "--python-platform", "x86_64-pc-windows-msvc", "--generate-hashes", "--no-header",
            "--output-file", $destination, "--cache-dir", ".work/uv-lock-cache", "--quiet")
        if ($profile -eq "dev") { $arguments += @("--extra", "dev") }
        & $taskUv @arguments
        if ($LASTEXITCODE -ne 0) { throw "Could not resolve the $profile application dependencies." }
    }
} finally {
    Pop-Location
}
