param([Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference = 'Stop'
$c = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($c.target)
$staged = [IO.Path]::GetFullPath($c.staged)
$backup = Join-Path (Split-Path -Parent $Config) ('backup-' + [guid]::NewGuid().ToString('N'))
$changed = $false
$started = $null
function Safe-File([string]$root, [string]$name) {
    if ($name -match '(^/|\\|:|(^|/)\.\.?(/|$)|(^|/)(Данные|models|profile|\.git)(/|$))') { throw 'Unsafe update path' }
    $full = [IO.Path]::GetFullPath((Join-Path $root $name))
    if (-not $full.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path escaped update directory' }
    $part = $full
    while ($part -and $part.Length -ge $root.Length) {
        if ((Test-Path -LiteralPath $part) -and ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse points are forbidden' }
        $part = Split-Path -Parent $part
    }
    return $full
}
function Copy-Program([string]$source, [string]$destination, [string]$name) {
    $src = Safe-File $source $name
    $dst = Safe-File $destination $name
    New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
    Copy-Item -LiteralPath $src -Destination $dst -Force
}
function Start-Archive {
    $arguments = '--home "' + $c.home + '"'
    if ($c.headless) { $arguments += ' --no-browser --no-tray' }
    return Start-Process -FilePath (Join-Path $target 'MeetingArchive.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
}
try {
    $owner = Get-Process -Id $c.pid -ErrorAction SilentlyContinue
    if ($owner -and -not $owner.WaitForExit(60000)) { throw 'Application did not stop; no files replaced' }
    $new = Get-Content -LiteralPath (Join-Path $staged 'update-manifest.json') -Raw | ConvertFrom-Json
    if ($new.version -ne $c.version) { throw 'Unexpected update version' }
    foreach ($name in $c.new_files) {
        $file = Safe-File $staged $name
        $expected = $new.files.PSObject.Properties[$name].Value
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $actual = ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($file)))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
        if ($actual -ne $expected) { throw 'Staged file checksum mismatch' }
    }
    $names = @(@($c.old_files) + @($c.new_files) + @('update-manifest.json') | Sort-Object -Unique)
    foreach ($name in $names) { $null = Safe-File $target $name }
    New-Item -ItemType Directory -Path $backup | Out-Null
    foreach ($name in @($c.old_files) + @('update-manifest.json')) { Copy-Program $target $backup $name }
    $changed = $true
    foreach ($name in @($c.new_files) + @('update-manifest.json')) { Copy-Program $staged $target $name }
    foreach ($name in $c.old_files) {
        if ($name -notin $c.new_files) { Remove-Item -LiteralPath (Safe-File $target $name) -Force }
    }
    $started = Start-Archive
    $deadline = (Get-Date).AddSeconds(45)
    $healthy = $false
    $startupError = ''
    do {
        Start-Sleep -Milliseconds 500
        try {
            $runtime = Get-Content -LiteralPath (Join-Path $c.home 'runtime.json') -Raw | ConvertFrom-Json
            if ($runtime.pid -eq $started.Id -and $runtime.version -eq $c.version) {
                # Readiness does not need a UI cookie or a launch capability.
                # Use IPv4 loopback directly and bypass system proxy discovery.
                $probe = [Net.HttpWebRequest]::Create('http://127.0.0.1:8765/api/bootstrap')
                $probe.Proxy = $null
                $probe.Timeout = 2000
                try { $reply = $probe.GetResponse(); $reply.Close() }
                catch [Net.WebException] {
                    if ($_.Exception.Response) {
                        $healthy = [int]$_.Exception.Response.StatusCode -eq 401
                        $_.Exception.Response.Close()
                    } else { throw }
                }
            }
        } catch { $startupError = $_.Exception.Message }
    } until ($healthy -or $started.HasExited -or (Get-Date) -gt $deadline)
    if (-not $healthy) { throw ('New application did not pass startup verification. ' + $startupError + ' Process=' + $started.Id + ' Runtime=' + $runtime.pid + ' Version=' + $runtime.version) }
    @{state='installed';version=$c.version;backup=$backup} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Config) 'receipt.json') -Encoding UTF8
} catch {
    $failure = $_.Exception.Message
    $canRestore = $true
    if ($started -and -not $started.HasExited) {
        try {
            $runtime = Get-Content -LiteralPath (Join-Path $c.home 'runtime.json') -Raw | ConvertFrom-Json
            if ($runtime.pid -ne $started.Id) { throw 'Unexpected application process' }
            $session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            Invoke-WebRequest -Uri $runtime.url -WebSession $session -UseBasicParsing -TimeoutSec 3 | Out-Null
            $boot = Invoke-RestMethod -Uri 'http://localhost:8765/api/bootstrap' -WebSession $session -TimeoutSec 3
            Invoke-WebRequest -Uri 'http://localhost:8765/api/shutdown' -Method POST -WebSession $session -Headers @{'x-csrf-token'=$boot.csrf} -UseBasicParsing -TimeoutSec 3 | Out-Null
            $canRestore = $started.WaitForExit(30000)
        } catch { $canRestore = $false }
    }
    if ($changed -and $canRestore) {
        foreach ($name in $c.new_files) {
            if ($name -notin $c.old_files) { Remove-Item -LiteralPath (Safe-File $target $name) -Force -ErrorAction SilentlyContinue }
        }
        foreach ($name in @($c.old_files) + @('update-manifest.json')) { Copy-Program $backup $target $name }
        $null = Start-Archive
    }
    if (-not $changed -and -not (Get-Process -Id $c.pid -ErrorAction SilentlyContinue)) { $null = Start-Archive }
    @{state= $(if ($changed -and $canRestore) {'rolled_back'} else {'error'}); error=$failure; backup=$backup} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Config) 'receipt.json') -Encoding UTF8
    exit 1
}
