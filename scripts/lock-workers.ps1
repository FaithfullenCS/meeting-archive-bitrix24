[CmdletBinding()]
param([string]$Python = ".venv\Scripts\python.exe")

$ErrorActionPreference = "Stop"
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Push-Location -LiteralPath $taskRoot
try {
    $taskUv = (& $Python -c "import uv; print(uv.find_uv_bin())").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot locate the pinned uv runtime." }
    foreach ($profile in @("cpu", "cuda", "cuda126")) {
        $indexName = if ($profile -eq "cuda") { "cu128" } elseif ($profile -eq "cuda126") { "cu126" } else { "cpu" }
        $arguments = @("pip", "compile", "meeting_archive/resources/worker-$profile.in",
            "--python-version", "3.12", "--python-platform", "x86_64-pc-windows-msvc",
            "--generate-hashes", "--emit-index-url", "--emit-index-annotation",
            "--index", "https://download.pytorch.org/whl/$indexName",
            "--index-strategy", "unsafe-best-match", "--no-header",
            "--output-file", "meeting_archive/resources/worker-$profile.lock",
            "--cache-dir", ".work/uv-lock-cache", "--quiet")
        & $taskUv @arguments
        if ($LASTEXITCODE -ne 0) { throw "Could not resolve the $profile Windows worker." }
    }
} finally {
    Pop-Location
}
