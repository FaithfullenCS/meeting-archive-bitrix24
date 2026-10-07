# Native selection/preview UI; all removal logic remains in reset-profile.ps1.
function Show-Uninstaller {
 Add-Type -AssemblyName System.Windows.Forms
 Add-Type -AssemblyName System.Drawing
 [Windows.Forms.Application]::EnableVisualStyles()
 $form=New-Object Windows.Forms.Form
 $form.Text='Meeting Archive — удаление и сброс'; $form.Size=[Drawing.Size]::new(800,650)
 $form.MinimumSize=$form.Size; $form.StartPosition='CenterScreen'; $form.BackColor=[Drawing.ColorTranslator]::FromHtml('#f3f5f4')
 $form.Font=[Drawing.Font]::new('Segoe UI',10)
 $title=New-Object Windows.Forms.Label; $title.Text='Удаление Meeting Archive'; $title.Font=[Drawing.Font]::new('Segoe UI',19,[Drawing.FontStyle]::Bold)
 $title.Location=[Drawing.Point]::new(24,18); $title.Size=[Drawing.Size]::new(730,38); $form.Controls.Add($title)
 $description=New-Object Windows.Forms.Label; $description.Text='Выберите компоненты. Архивы и модели по умолчанию сохраняются.'
 $description.Location=[Drawing.Point]::new(26,62); $description.Size=[Drawing.Size]::new(730,26); $form.Controls.Add($description)
 $checks=@{}; $row=100
 foreach ($entry in @(@('application','Программа и её интеграция с Windows',$true),@('profile','Настройки, ключи, каталог, очередь и кэш',$true),@('models','Собственные модели и библиотеки приложения',$false),@('meetings','Сохранённые совещания, тексты и заметки',$false),@('chats','Сохранённые чаты, вложения и заметки',$false))) {
  $check=New-Object Windows.Forms.CheckBox; $check.Text=$entry[1]; $check.Checked=[bool]$entry[2]
  $check.Location=[Drawing.Point]::new(28,$row); $check.Size=[Drawing.Size]::new(730,29)
  $checks[$entry[0]]=$check; $form.Controls.Add($check); $row+=31
 }
 $previewBox=New-Object Windows.Forms.TextBox; $previewBox.Multiline=$true; $previewBox.ReadOnly=$true; $previewBox.ScrollBars='Both'; $previewBox.WordWrap=$false
 $previewBox.Location=[Drawing.Point]::new(26,267); $previewBox.Size=[Drawing.Size]::new(734,240); $previewBox.BackColor=[Drawing.Color]::White
 $form.Controls.Add($previewBox)
 $state=New-Object Windows.Forms.Label; $state.Text='Нажмите «Проверить выбранное», чтобы увидеть пути и объём.'
 $state.Location=[Drawing.Point]::new(26,518); $state.Size=[Drawing.Size]::new(730,30); $form.Controls.Add($state)
 $buttons=@{}
 foreach ($entry in @(@('close','Завершить приложение',26,188),@('preview','Проверить выбранное',222,180),@('remove','Удалить выбранное',410,180),@('cancel','Закрыть',598,160))) {
  $button=New-Object Windows.Forms.Button; $button.Text=$entry[1]; $button.Location=[Drawing.Point]::new($entry[2],556); $button.Size=[Drawing.Size]::new($entry[3],36)
  $button.FlatStyle='Flat'; $button.BackColor=[Drawing.Color]::White; $form.Controls.Add($button); $buttons[$entry[0]]=$button
 }
 $buttons.remove.BackColor=[Drawing.ColorTranslator]::FromHtml('#a44040'); $buttons.remove.ForeColor=[Drawing.Color]::White; $buttons.remove.Enabled=$false
 $buttons.preview.BackColor=[Drawing.ColorTranslator]::FromHtml('#2b6d58'); $buttons.preview.ForeColor=[Drawing.Color]::White
 $form.Tag=@{plan=$null}
 foreach ($check in $checks.Values) { $check.Add_CheckedChanged({ $form.Tag.plan=$null; $buttons.remove.Enabled=$false; $state.Text='Выбор изменился — проверьте план заново.' }) }
 $buttons.cancel.Add_Click({$form.Close()})
 $buttons.close.Add_Click({
  $buttons.close.Enabled=$false
  try { $state.Text='Ждём штатного завершения записи…'; [Windows.Forms.Application]::DoEvents(); Close-Application; $state.Text='Приложение завершено. Теперь проверьте план удаления.' }
  catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message,'Meeting Archive','OK','Warning') | Out-Null }
  finally { $buttons.close.Enabled=$true; $form.Tag.plan=$null; $buttons.remove.Enabled=$false }
 })
 $buttons.preview.Add_Click({
  try {
   $components=@{}; foreach ($name in $checks.Keys) {$components[$name]=$checks[$name].Checked}
   $plan=Build-Plan $components; $form.Tag.plan=$plan
   $lines=@("Файлов: $($plan.files) · $([math]::Round($plan.bytes/1MB,2)) МБ",'')
   $lines+=@($plan.entries | ForEach-Object {$_.component+' : '+$_.path})
   $lines+=@('','СОХРАНЯЕТСЯ:')+@($plan.kept)+@('Оригиналы входящих записей и ресурсы других программ.')
   $previewBox.Text=$lines -join "`r`n"; $buttons.remove.Enabled=($plan.files -gt 0 -and -not $Preview)
   $state.Text='План готов. Проверьте пути; удаление выбранного нельзя отменить.'
  } catch {$form.Tag.plan=$null; $buttons.remove.Enabled=$false; [Windows.Forms.MessageBox]::Show($_.Exception.Message,'Meeting Archive','OK','Warning') | Out-Null}
 })
 $buttons.remove.Add_Click({
  if (-not $form.Tag.plan -or $Preview) {return}
  $prompt="Удалить выбранные компоненты?`nФайлов: $($form.Tag.plan.files); объём: $([math]::Round($form.Tag.plan.bytes/1MB,2)) МБ.`nОтмеченные архивы и заметки удаляются необратимо."
  if ([Windows.Forms.MessageBox]::Show($prompt,'Подтвердите удаление','YesNo','Warning','Button2') -ne 'Yes') {return}
  $buttons.remove.Enabled=$false; $buttons.preview.Enabled=$false
  try {
   $report=Execute-Plan $form.Tag.plan
   $previewBox.Text=(@('УДАЛЕНО:')+@($report.removed)+@('','СОХРАНЕНО:')+@($report.preserved)+@('','ОШИБКИ:')+@($report.errors)) -join "`r`n"
   $state.Text=$(if ($report.completed) {'Выбранные компоненты удалены. Сохранённые архивы остаются на диске.'} else {'Удаление завершено частично. Ошибки показаны выше.'})
   foreach ($check in $checks.Values) {$check.Enabled=$false}; $buttons.close.Enabled=$false
  } catch { [Windows.Forms.MessageBox]::Show($_.Exception.Message,'Meeting Archive','OK','Error') | Out-Null; $buttons.preview.Enabled=$true }
 })
 if ($UiSmoke) {
  $form.Add_Shown({
   $buttons.preview.PerformClick()
   if (-not $form.Tag.plan -or $checks.chats.Checked -or $checks.meetings.Checked -or $checks.models.Checked -or -not $checks.application.Checked -or -not $checks.profile.Checked -or $buttons.remove.Enabled) { $form.Tag.smokeFailed=$true }
   if ($ReportPath) { @{smokePassed=(-not $form.Tag.smokeFailed);defaultArchivesPreserved=$true;previewOnly=$Preview;files=$form.Tag.plan.files} | ConvertTo-Json | Set-Content -LiteralPath $ReportPath -Encoding UTF8 }
   $form.Close()
  })
 }
 $form.ShowDialog() | Out-Null
 $failed=$form.Tag.smokeFailed; $form.Dispose()
 if ($UiSmoke -and $failed) { throw 'Synthetic GUI acceptance failed' }
}
