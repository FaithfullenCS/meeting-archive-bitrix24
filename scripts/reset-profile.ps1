#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ProfileHome = $(if ($env:MEETING_ARCHIVE_HOME) { $env:MEETING_ARCHIVE_HOME } else { Join-Path $env:LOCALAPPDATA 'MeetingArchive' }),
    [ValidateSet('Ask', 'Profile', 'All')][string]$Mode = 'Ask',
    [switch]$ConfirmReset,
    [switch]$Preview
)
$ErrorActionPreference = 'Stop'
trap { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }

function Full-Path([string]$Value) {
    if ($Value -notmatch '^[A-Za-z]:[\\/]' -or $Value.StartsWith('\\')) { throw 'Нужен абсолютный локальный путь.' }
    return [IO.Path]::GetFullPath($Value).TrimEnd('\')
}
function Assert-PlainPath([string]$Value) {
    $cursor = $Value
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Путь содержит ссылку: $cursor. Удаление остановлено." }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}
function Assert-Closed {
    # The launcher's named mutex identifies this profile; a recycled PID does not.
    if ($script:profileGuard) { return }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $digest = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($profileRoot)) } finally { $hasher.Dispose() }
    $suffix = ([BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant().Substring(0,24)
    $createdNew = $false
    $guard = [Threading.Mutex]::new($false, ('Local\MeetingArchive-' + $suffix), [ref]$createdNew)
    if (-not $createdNew) {
        $guard.Dispose()
        throw 'Meeting Archive ещё работает с этим профилем. Завершите приложение через значок в трее -> Выход и повторите uninstall.'
    }
    $script:profileGuard = $guard
}
function Assert-Tree([string]$Value) {
    Assert-PlainPath $Value
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Value)
    while ($pending.Count) {
        $current = $pending.Pop()
        if (-not (Get-Item -LiteralPath $current -Force).PSIsContainer) { continue }
        foreach ($child in Get-ChildItem -LiteralPath $current -Force) {
            # Leaf links can be unlinked; never recurse through a directory link.
            if ($child.PSIsContainer) {
                if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Внешняя ссылка в папке: $($child.FullName). Удаление остановлено." }
                $pending.Push($child.FullName)
            }
        }
    }
}

$profileRoot = Full-Path $ProfileHome
$defaultProfile = Full-Path (Join-Path $env:LOCALAPPDATA 'MeetingArchive')
if ($profileRoot -eq [IO.Path]::GetPathRoot($profileRoot).TrimEnd('\')) { throw 'Нельзя сбрасывать корень диска.' }
Assert-PlainPath $profileRoot
Assert-Closed
if (-not (Test-Path -LiteralPath $profileRoot)) { Write-Host 'Профиль уже отсутствует. Совещания не изменены.'; exit 0 }

if ($Mode -eq 'Ask') {
    Write-Host 'Что удалить?'
    Write-Host '1 - Только профиль, ключи, каталог и очередь. Совещания и модели сохранить.'
    Write-Host '2 - Все данные приложения: профиль, сохранённые совещания, заметки и собственные модели.'
    Write-Host '3 - Отмена.'
    $choice = Read-Host 'Выберите 1, 2 или 3 (Enter = 1)'
    if ($choice -eq '' -or $choice -eq '1') { $Mode = 'Profile' }
    elseif ($choice -eq '2') { $Mode = 'All' }
    elseif ($choice -eq '3') { Write-Host 'Отменено.'; exit 0 }
    else { throw 'Неизвестный выбор. Ничего не удалено.' }
}

$targets = [Collections.Generic.List[string]]::new()
$archiveRoot = $null
$settingsPath = Join-Path $profileRoot 'settings.json'
if (Test-Path -LiteralPath $settingsPath) {
    Assert-PlainPath $settingsPath
    $settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.archive_root) { $archiveRoot = Full-Path $settings.archive_root }
}
foreach ($name in @('settings.json', 'secrets.dpapi', 'archive.sqlite', 'archive.sqlite-wal', 'archive.sqlite-shm', 'archive.sqlite-journal', 'runtime.json', 'uploads')) {
    $target = Full-Path (Join-Path $profileRoot $name)
    if (-not $target.StartsWith($profileRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Небезопасный путь профиля.' }
    if (Test-Path -LiteralPath $target) { $targets.Add($target) }
}
foreach ($temporary in Get-ChildItem -LiteralPath $profileRoot -File -Filter '.write-*' -Force) { $targets.Add($temporary.FullName) }
$meetingCount = 0
if ($Mode -eq 'All') {
    if (-not $archiveRoot) { throw 'Не удалось определить папку совещаний. Выберите сброс только профиля или верните settings.json.' }
    if ($archiveRoot -eq $profileRoot -or $archiveRoot.StartsWith($profileRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $profileRoot.StartsWith($archiveRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Архив пересекается с профилем. Удаление остановлено.' }
    Assert-PlainPath $archiveRoot
    if (Test-Path -LiteralPath $archiveRoot) {
        $pending = [Collections.Generic.Stack[string]]::new()
        $pending.Push($archiveRoot)
        while ($pending.Count) {
            $current = $pending.Pop()
            Assert-PlainPath $current
            $manifestPath = Join-Path $current 'meeting.json'
            if (Test-Path -LiteralPath $manifestPath) {
                Assert-PlainPath $manifestPath
                $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($manifest.schemaVersion -eq 1 -and $manifest.source -in @('bitrix', 'import') -and $manifest.PSObject.Properties.Name -contains 'callId') {
                    if ($current -eq $archiveRoot -or -not $current.StartsWith($archiveRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Небезопасная папка совещания.' }
                    $targets.Add($current)
                    $meetingCount++
                    continue
                }
            }
            foreach ($child in Get-ChildItem -LiteralPath $current -Directory -Force) {
                Assert-PlainPath $child.FullName
                $pending.Push($child.FullName)
            }
        }
    }
    $modulePath = Join-Path $profileRoot 'module'
    $ownedMarker = Join-Path $modulePath '.meeting-archive-owned'
    if (Test-Path -LiteralPath $ownedMarker) { Assert-PlainPath $ownedMarker; $targets.Add($modulePath) }
}
# Validate the complete plan before the first deletion. Archive/source folders are never read from manifest paths.
foreach ($target in $targets) { Assert-Tree $target }
Write-Host "Профиль: $profileRoot"
Write-Host "Режим: $Mode; папок совещаний: $meetingCount"
foreach ($target in $targets) { Write-Host "  $target" }
Write-Host 'Оригиналы импортированных файлов и ресурсы других программ сохраняются.'
if ($Preview) { Write-Host 'Предпросмотр: ничего не удалено.'; exit 0 }
Write-Host 'Удаление необратимо. Для восстановления нужны резервная копия и повторный вход.'
if (-not $ConfirmReset -and (Read-Host 'Для подтверждения введите DELETE') -cne 'DELETE') { Write-Host 'Отменено.'; exit 0 }
Assert-Closed
foreach ($target in $targets) { Assert-Tree $target }
# The per-user startup registration is shared only by the default profile.
if ($profileRoot -eq $defaultProfile) {
    $runKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Run', $true)
    if ($runKey) { try { $runKey.DeleteValue('MeetingArchive', $false) } finally { $runKey.Dispose() } }
}
foreach ($target in $targets) {
    Remove-Item -LiteralPath $target -Recurse -Force
    if (Test-Path -LiteralPath $target) { throw "Не удалось удалить $target" }
}
Write-Host 'Сброс выполнен. Следующий запуск создаст новый профиль. Подключите Bitrix24 и сохраните настройки заново.'
if ($Mode -eq 'Profile') { Write-Host 'Совещания и модели сохранены на диске. Старый каталог и очередь сброшены; файлы можно открыть через Проводник.' }
