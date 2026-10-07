#Requires -Version 5.1
[CmdletBinding()]
param(
 [string]$ProfileHome = $(if ($env:MEETING_ARCHIVE_HOME) { $env:MEETING_ARCHIVE_HOME } else { Join-Path $env:LOCALAPPDATA 'MeetingArchive' }),
 [ValidateSet('Ask','Profile','All','Custom')][string]$Mode='Ask',
 [string]$ApplicationRoot = '',
 [switch]$RemoveProfile, [switch]$RemoveMeetings, [switch]$RemoveChats, [switch]$RemoveModels, [switch]$RemoveApplication,
 [switch]$ConfirmReset, [switch]$Preview, [switch]$CloseRunning, [switch]$Gui, [switch]$UiSmoke,
 [string]$ReportPath=''
)
$ErrorActionPreference='Stop'
trap { Write-Host $_.Exception.Message -ForegroundColor Red; exit 1 }

function Full-Path([string]$Value) {
 if ($Value -notmatch '^[A-Za-z]:[\\/]' -or $Value.StartsWith('\\')) { throw 'Нужен абсолютный локальный путь.' }
 $result=[IO.Path]::GetFullPath($Value).TrimEnd('\')
 if ($result -eq [IO.Path]::GetPathRoot($result).TrimEnd('\')) { throw 'Корень диска нельзя использовать для удаления.' }
 return $result
}
function Assert-PlainPath([string]$Value) {
 $cursor=$Value
 while ($cursor) {
  if (Test-Path -LiteralPath $cursor) {
   if (((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Путь содержит ссылку: $cursor" }
  }
  $parent=[IO.Path]::GetDirectoryName($cursor)
  if ($parent -eq $cursor) { break }; $cursor=$parent
 }
}
function Tree-Items([string]$Value) {
 Assert-PlainPath $Value
 if (-not (Test-Path -LiteralPath $Value)) { return }
 $pending=[Collections.Generic.Stack[string]]::new(); $pending.Push($Value)
 while ($pending.Count) {
  $current=$pending.Pop(); Assert-PlainPath $current
  $item=Get-Item -LiteralPath $current -Force
  $item
  if ($item.PSIsContainer) { foreach ($child in Get-ChildItem -LiteralPath $current -Force) { $pending.Push($child.FullName) } }
 }
}
function Read-Json([string]$Value) { Assert-PlainPath $Value; return Get-Content -LiteralPath $Value -Raw -Encoding UTF8 | ConvertFrom-Json }
function File-Sha256([string]$Value) {
 $stream=[IO.File]::OpenRead($Value); $hasher=[Security.Cryptography.SHA256]::Create()
 try { return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-','') } finally { $stream.Dispose(); $hasher.Dispose() }
}
function Profile-MutexName {
 $hasher=[Security.Cryptography.SHA256]::Create()
 try { $bytes=$hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($script:profileRoot)) } finally { $hasher.Dispose() }
 return 'Local\MeetingArchive-'+([BitConverter]::ToString($bytes)).Replace('-','').ToLowerInvariant().Substring(0,24)
}
function Profile-Running {
 if ($script:profileGuard) { return $false }
 $created=$false; $guard=[Threading.Mutex]::new($false,(Profile-MutexName),[ref]$created)
 if (-not $created) { $guard.Dispose(); return $true }
 $script:profileGuard=$guard; return $false
}
function Close-Application {
 if (-not (Profile-Running)) { return }
 $runtime=Read-Json (Join-Path $script:profileRoot 'runtime.json')
 $uri=[Uri]$runtime.url
 if ($uri.Scheme -ne 'http' -or $uri.Host -notin @('localhost','127.0.0.1') -or $uri.Port -ne 8765 -or $uri.AbsolutePath -ne '/' -or $uri.Query -notmatch '^\?launch=[A-Za-z0-9_-]+$') { throw 'Невозможно проверить локальный адрес приложения. Завершите его через трей.' }
 # Never kill a PID from runtime.json; the mutex and authenticated local server are authoritative.
 try {
  $jar=New-Object Net.CookieContainer
  function Local-Request([Uri]$address,[string]$method,[string]$csrfToken='') {
   $request=[Net.HttpWebRequest]::Create($address)
   $request.Proxy=$null; $request.AllowAutoRedirect=$false; $request.Timeout=5000; $request.ReadWriteTimeout=5000
   $request.CookieContainer=$jar; $request.Method=$method
   if ($csrfToken) { $request.Headers['X-CSRF-Token']=$csrfToken; $request.ContentLength=0 }
   $response=$request.GetResponse()
   try { $reader=[IO.StreamReader]::new($response.GetResponseStream()); try { return $reader.ReadToEnd() } finally { $reader.Dispose() } } finally { $response.Dispose() }
  }
  Local-Request $uri 'GET' | Out-Null
  $bootstrap=Local-Request ([Uri]($uri.GetLeftPart([UriPartial]::Authority)+'/api/bootstrap')) 'GET' | ConvertFrom-Json
  Local-Request ([Uri]($uri.GetLeftPart([UriPartial]::Authority)+'/api/shutdown')) 'POST' $bootstrap.csrf | Out-Null
 } catch { throw 'Приложение не подтвердило завершение. Выберите «Выход» в трее и повторите. Принудительная остановка не выполняется.' }
 $deadline=[DateTime]::UtcNow.AddSeconds(60)
 while (Profile-Running) {
  if ([DateTime]::UtcNow -gt $deadline) { throw 'Приложение ещё завершает запись. Дождитесь выхода из трея и повторите.' }
  Start-Sleep -Milliseconds 200
  if ($Gui) { [Windows.Forms.Application]::DoEvents() }
 }
}
function Assert-Profile {
 if (-not (Test-Path -LiteralPath $script:profileRoot)) { return }
 Assert-PlainPath $script:profileRoot
 $marker=Join-Path $script:profileRoot '.meeting-archive-profile.json'
 if (Test-Path -LiteralPath $marker) {
  $value=Read-Json $marker
  if ($value.product -ne 'MeetingArchive' -or $value.schemaVersion -ne 1) { throw 'Папка не принадлежит профилю Meeting Archive.' }
 } elseif ($script:profileRoot -ne $script:defaultProfile) {
  # Backwards compatibility for isolated older profiles: require the app schema.
  $settingsFile=Join-Path $script:profileRoot 'settings.json'
  if (-not (Test-Path -LiteralPath $settingsFile)) { throw 'У отдельного профиля нет маркера принадлежности.' }
  $value=Read-Json $settingsFile
  if (-not $value.archive_root -or -not ($value.PSObject.Properties.Name -contains 'chat_archive_root')) { throw 'Не подтверждена принадлежность отдельного профиля. Ничего не удалено.' }
 }
}
function Build-Plan($Components) {
 Assert-Profile
 $entries=[Collections.Generic.List[object]]::new(); $kept=[Collections.Generic.List[string]]::new()
 $settings=$null; $settingsFile=Join-Path $script:profileRoot 'settings.json'
 if (Test-Path -LiteralPath $settingsFile) { $settings=Read-Json $settingsFile }
 function Add-Target([string]$path,[string]$component) {
  $path=Full-Path $path
  if (Test-Path -LiteralPath $path) { $entries.Add([pscustomobject]@{path=$path;component=$component}) }
 }
 if ($Components.profile -and (Test-Path -LiteralPath $script:profileRoot)) {
  foreach ($name in @('settings.json','secrets.dpapi','archive.sqlite','archive.sqlite-wal','archive.sqlite-shm','archive.sqlite-journal','runtime.json','.meeting-archive-profile.json','uploads','exports','webview-data','browser-window','updates','logs')) { Add-Target (Join-Path $script:profileRoot $name) 'profile' }
  foreach ($item in Get-ChildItem -LiteralPath $script:profileRoot -File -Filter '.write-*' -Force) { Add-Target $item.FullName 'profile' }
 }
 $meetingRoot=$null; $chatRoot=$null
 if ($settings -and $settings.archive_root) { $meetingRoot=[string]$settings.archive_root; if($Components.meetings){$meetingRoot=Full-Path $meetingRoot} }
 if ($settings -and $settings.chat_archive_root) { $chatRoot=[string]$settings.chat_archive_root; if($Components.chats){$chatRoot=Full-Path $chatRoot} }
 if ($Components.meetings -or $Components.chats) {
  foreach ($root in @($meetingRoot,$chatRoot)) {
   if ($root -and ($root -eq $script:profileRoot -or $root.StartsWith($script:profileRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $script:profileRoot.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase))) { throw 'Архив пересекается с профилем. Удаление остановлено.' }
  }
  if ($meetingRoot -and $chatRoot -and ($meetingRoot -eq $chatRoot -or $meetingRoot.StartsWith($chatRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $chatRoot.StartsWith($meetingRoot+'\',[StringComparison]::OrdinalIgnoreCase))) { throw 'Папки двух архивов пересекаются. Удаление остановлено.' }
 }
 if ($Components.meetings) {
  if (-not $meetingRoot) { throw 'Не удалось определить папку совещаний из settings.json.' }
  foreach ($item in @(Tree-Items $meetingRoot)) {
   if ($item.PSIsContainer) {
    $manifestPath=Join-Path $item.FullName 'meeting.json'
    if (Test-Path -LiteralPath $manifestPath) {
     $m=Read-Json $manifestPath
     if ($m.schemaVersion -eq 1 -and $m.source -in @('bitrix','import') -and $m.PSObject.Properties.Name -contains 'callId' -and $item.FullName -ne $meetingRoot) { Add-Target $item.FullName 'meetings' }
    }
   }
  }
 } elseif ($meetingRoot) { $kept.Add('Совещания: '+$meetingRoot) }
 if ($Components.chats) {
  if (-not $chatRoot) { throw 'Не удалось определить папку чатов из settings.json.' }
  if (Test-Path -LiteralPath $chatRoot) {
   $archiveMarker=Join-Path $chatRoot 'archive.json'
   if (-not (Test-Path -LiteralPath $archiveMarker) -or (Read-Json $archiveMarker).kind -ne 'chat-archive') { throw 'Не подтверждена принадлежность папки архива чатов.' }
   foreach ($item in @(Tree-Items $chatRoot)) {
    if (-not $item.PSIsContainer) { continue }
    $manifestPath=Join-Path $item.FullName 'chat.json'
    if (Test-Path -LiteralPath $manifestPath) {
     $m=Read-Json $manifestPath
     $relative=$item.FullName.Substring($chatRoot.Length+1).Replace('\','/')
     $expected=$m.portal+'/user-'+$m.user_id+'/chats/'
     $layout=($relative -eq ($expected+'chat-'+$m.id) -or $relative -eq ($expected+'tasks/chat-'+$m.id) -or $relative -eq ($expected+'conversations/chat-'+$m.id))
     if ($m.schemaVersion -eq 1 -and [long]$m.id -gt 0 -and [long]$m.user_id -gt 0 -and $m.portal -and $layout) { Add-Target $item.FullName 'chats' }
    }
    $accountPath=Join-Path $item.FullName 'account.json'
    if (Test-Path -LiteralPath $accountPath) {
     $m=Read-Json $accountPath
     if ($m.schemaVersion -eq 1 -and [long]$m.user_id -gt 0 -and $item.Name -eq ('user-'+$m.user_id) -and $item.Parent.Name -eq $m.portal) {
      foreach ($name in @('account.json','events.jsonl','collections/tasks.json','collections/conversations.json','_notebook')) { Add-Target (Join-Path $item.FullName $name) 'chats' }
     }
    }
   }
   Add-Target $archiveMarker 'chats'
  }
 } elseif ($chatRoot) { $kept.Add('Чаты: '+$chatRoot) }
 $module=Join-Path $script:profileRoot 'module'
 if ($Components.models -and (Test-Path -LiteralPath (Join-Path $module '.meeting-archive-owned'))) { Add-Target $module 'models' }
 elseif (Test-Path -LiteralPath $module) { $kept.Add('Модели: '+$module) }
 if ($Components.application) {
  Assert-PlainPath $script:applicationPath
  $manifestPath=Join-Path $script:applicationPath 'update-manifest.json'
  if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Нет update-manifest.json. Удаление файлов программы невозможно проверить.' }
  $manifest=Read-Json $manifestPath
  if (-not $manifest.files.'MeetingArchive.exe') { throw 'Манифест не относится к Meeting Archive.' }
  $primaryExe=Join-Path $script:applicationPath 'MeetingArchive.exe'
  if ((Test-Path -LiteralPath $primaryExe) -and (File-Sha256 $primaryExe) -ine $manifest.files.'MeetingArchive.exe') { throw 'Главный EXE отличается от манифеста. Нельзя безопасно удалить его библиотеки.' }
  foreach ($property in $manifest.files.PSObject.Properties) {
   $name=$property.Name
   if ($name -notmatch '^(MeetingArchive\.exe|uninstall\.cmd|README\.md|build-info\.json|_internal/.+|docs/.+|scripts/(reset-profile|uninstall-ui)\.ps1|skills/meeting-archive-files/.+)$' -or $name.Split('/') -contains '..' -or $name.Contains('\') -or $property.Value -notmatch '^[a-fA-F0-9]{64}$') { throw 'Неверный путь в манифесте программы.' }
   $target=Full-Path (Join-Path $script:applicationPath $name)
   if (-not $target.StartsWith($script:applicationPath+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Путь выходит за пределы программы.' }
   if (Test-Path -LiteralPath $target) {
    Assert-PlainPath $target
    if ((File-Sha256 $target) -ieq $property.Value) { Add-Target $target 'application' }
    else { $kept.Add('Изменённый файл программы: '+$target) }
   }
  }
  Add-Target $manifestPath 'application'
 }
 # Remove overlapping child targets, never the root of an archive/profile/program.
 $unique=@($entries | Sort-Object path -Unique)
 $entries=@($unique | Where-Object { $current=$_.path; -not @($unique | Where-Object { $current -ne $_.path -and $current.StartsWith($_.path+'\',[StringComparison]::OrdinalIgnoreCase) }).Count })
 $fingerprints=[Collections.Generic.List[string]]::new(); [long]$bytes=0; [int]$files=0
 foreach ($entry in $entries) {
  foreach ($item in @(Tree-Items $entry.path)) {
   $size=0; if (-not $item.PSIsContainer) { $size=$item.Length; $bytes+=$size; $files++ }
   $fingerprints.Add($item.FullName+'|'+$size+'|'+$item.LastWriteTimeUtc.Ticks)
  }
 }
 $signature=($fingerprints | Sort-Object) -join "`n"
 $hasher=[Security.Cryptography.SHA256]::Create()
 try { $token=([BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($signature)))).Replace('-','') } finally { $hasher.Dispose() }
 return [pscustomobject]@{entries=$entries;components=$Components;token=$token;files=$files;bytes=$bytes;kept=@($kept)}
}
function Remove-Registrations {
 # Custom/synthetic homes never touch shared HKCU registrations.
 if ($script:profileRoot -ne $script:defaultProfile) { return }
 $exe=Join-Path $script:applicationPath 'MeetingArchive.exe'
 $exePattern='^(?:"'+[Regex]::Escape($exe)+'"|'+[Regex]::Escape($exe)+')(?:\s|$)'
 $paths=@('Software\Microsoft\Windows\CurrentVersion\Run','Software\Classes\meetingarchive\shell\open\command','Software\Classes\AppUserModelId\MeetingArchive.Desktop')
 $run=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($paths[0],$true)
 if ($run) { try { $value=[string]$run.GetValue('MeetingArchive',''); if ($value -imatch $exePattern) { $run.DeleteValue('MeetingArchive',$false) } } finally { $run.Dispose() } }
 $command=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($paths[1])
 $owns=$false
 if ($command) { try { $owns=([string]$command.GetValue('','')) -imatch $exePattern } finally { $command.Dispose() } }
 if ($owns) {
  [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree('Software\Classes\meetingarchive',$false)
  $identity=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($paths[2])
  $icon=''; if ($identity) { try { $icon=[string]$identity.GetValue('IconUri','') } finally { $identity.Dispose() } }
  if ($icon -ieq $exe) {
   [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($paths[2],$false)
   [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree('Software\Classes\CLSID\{28E0343B-EAE4-478D-AFCF-CE1C91D461BC}',$false)
  }
  $shortcut=Join-Path $env:APPDATA 'Microsoft/Windows/Start Menu/Programs/Meeting Archive.lnk'
  if (Test-Path -LiteralPath $shortcut) {
   $shell=New-Object -ComObject WScript.Shell; $link=$shell.CreateShortcut($shortcut)
   if ($link.TargetPath -ieq $exe) { Remove-Item -LiteralPath $shortcut -Force }
  }
 }
}
function Execute-Plan($Plan) {
 if (Profile-Running) { throw 'Приложение работает. Завершите его через трей или кнопку «Завершить приложение».' }
 if ($Plan.components.application) {
  $targetExe=Join-Path $script:applicationPath 'MeetingArchive.exe'
  foreach ($activeProcess in @(Get-Process -Name MeetingArchive -ErrorAction SilentlyContinue)) {
   try { $activePath=$activeProcess.Path } catch { throw 'Не удалось проверить работающий процесс Meeting Archive. Завершите его перед удалением.' }
   if (-not $activePath -or $activePath -ieq $targetExe) { throw 'Эта программа ещё используется другим профилем. Завершите все её экземпляры через трей.' }
  }
 }
 $fresh=Build-Plan $Plan.components
 if ($fresh.token -cne $Plan.token) { throw 'Состав файлов изменился. Проверьте план заново.' }
 # All paths have been inspected end-to-end before the first mutation.
 $removed=[Collections.Generic.List[string]]::new(); $errors=[Collections.Generic.List[string]]::new()
 if ($Plan.components.profile -or $Plan.components.application) { Remove-Registrations }
 foreach ($entry in $fresh.entries) {
  try {
   @(Tree-Items $entry.path) | Out-Null
   Remove-Item -LiteralPath $entry.path -Recurse -Force
   if (Test-Path -LiteralPath $entry.path) { throw 'Путь остался после удаления.' }
   $removed.Add($entry.path)
  } catch { $errors.Add($entry.path+': '+$_.Exception.Message) }
 }
 if ($Plan.components.application) {
  foreach ($path in @($removed)) {
   $parent=[IO.Path]::GetDirectoryName($path)
   while ($parent -and $parent -ne $script:applicationPath -and $parent.StartsWith($script:applicationPath+'\',[StringComparison]::OrdinalIgnoreCase)) {
    if (-not (Test-Path -LiteralPath $parent) -or @(Get-ChildItem -LiteralPath $parent -Force).Count) { break }; Assert-PlainPath $parent; Remove-Item -LiteralPath $parent; $parent=[IO.Path]::GetDirectoryName($parent)
   }
  }
 }
 $report=[pscustomobject]@{schemaVersion=1;product='MeetingArchive';completed=($errors.Count -eq 0);files=$Plan.files;bytes=$Plan.bytes;removed=@($removed);preserved=$Plan.kept;errors=@($errors)}
 if ($ReportPath) { $reportFile=Full-Path $ReportPath; Assert-PlainPath $reportFile; $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $reportFile -Encoding UTF8 }
 return $report
}
$script:profileRoot=Full-Path $ProfileHome
$script:defaultProfile=Full-Path (Join-Path $env:LOCALAPPDATA 'MeetingArchive')
if (-not $ApplicationRoot) { $ApplicationRoot=Split-Path $PSScriptRoot -Parent }
$script:applicationPath=Full-Path $ApplicationRoot
$script:profileGuard=$null
Assert-Profile
if ($UiSmoke) { $Preview=$true }
if ($Gui) { . (Join-Path $PSScriptRoot 'uninstall-ui.ps1'); Show-Uninstaller; exit 0 }
if ($Mode -eq 'Ask') {
 Write-Host '1 - Сбросить профиль; сохранить программу, чаты, совещания и модели.'
 Write-Host '2 - Удалить профиль и все распознанные архивы с собственными моделями.'
 Write-Host '3 - Отмена.'
 $choice=Read-Host 'Выберите 1, 2 или 3 (Enter = 1)'
 if ($choice -in @('','1')) { $Mode='Profile' } elseif ($choice -eq '2') { $Mode='All' } elseif ($choice -eq '3') { exit 0 } else { throw 'Неизвестный выбор. Ничего не удалено.' }
}
$components=@{profile=($Mode -in @('Profile','All') -or $RemoveProfile);meetings=($Mode -eq 'All' -or $RemoveMeetings);chats=($Mode -eq 'All' -or $RemoveChats);models=($Mode -eq 'All' -or $RemoveModels);application=[bool]$RemoveApplication}
if ($Preview) { $plan=Build-Plan $components }
else { if ($CloseRunning) { Close-Application }; if (Profile-Running) { throw 'Приложение работает. Выберите «Выход» в трее.' }; $plan=Build-Plan $components }
Write-Host "Файлов: $($plan.files); объём: $([math]::Round($plan.bytes/1MB,2)) МБ"
foreach ($entry in $plan.entries) { Write-Host ('  '+$entry.component+' : '+$entry.path) }
foreach ($kept in $plan.kept) { Write-Host ('Сохраняется: '+$kept) }
Write-Host 'Оригиналы входящих записей, изменённые/посторонние файлы и ресурсы других программ сохраняются.'
if ($Preview) { Write-Host 'Предпросмотр: ничего не удалено.'; exit 0 }
if (-not $ConfirmReset -and (Read-Host 'Удаление необратимо. Для подтверждения введите DELETE') -cne 'DELETE') { Write-Host 'Отменено.'; exit 0 }
$report=Execute-Plan $plan
if (-not $report.completed) { foreach ($failureMessage in $report.errors) { Write-Host $failureMessage -ForegroundColor Red }; exit 1 }
Write-Host 'Выбранные компоненты удалены. Сохранённые архивы и модели остаются по прежним путям.'
