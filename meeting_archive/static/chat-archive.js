"use strict";
window.ChatArchiveUI = {
  create({api, icon, escapeHtml: esc, dateString, notify, action, getSettings, openMaterialDelete}) {
    const $ = selector => document.querySelector(selector);
    const form = $("#ca-filters");
    const s = {account: "", chats: [], chat: 0, items: [], total: 0, offset: 0, nextOffset: 0, request: 0, last: 0, busy: false, optionsLoaded: false, selected: new Set(), chatOffset: 0, visible: [], pageItems: [], anchor: 0, messageBusy: false, peopleExpanded: false};
    const coverage = {pending: "Ожидает сохранения", backfilling: "Догружается…", source_boundary: "Доступная история сохранена", tariff_limited: "История ограничена тарифом", access_lost: "Потеря доступа", error: "Ошибка — повторим позже", boundary_unverified: "Граница не подтверждена", verifying_boundary: "Проверяется граница"};
    const files = {not_saved: "Не скачан", queued: "В очереди", running: "Скачивается…", saved: "Скачан", unavailable: "Недоступен", error: "Ошибка", missing: "Файл перемещён", size_limited: "Превышен лимит размера"};
    const fileIcons = {images: "image", documents: "file", audio: "music", video: "video", other: "file"};
    const avatar = name => `<span class="ca-avatar" aria-hidden="true">${esc(String(name || "?").split(/\s+/).filter(Boolean).slice(0,2).map(n => n[0]).join("").toUpperCase())}</span>`;
    const selected = selector => [...$(selector).selectedOptions].map(option => Number(option.value));
    function values() {
      const data = Object.fromEntries(new FormData(form).entries());
      for (const name of ["author","participant"]) data[name] = [...form.elements[name].selectedOptions].map(o=>o.value).join(",");
      return data;
    }
    function showError(message) { $("#ca-error").hidden = !message; $("#ca-error").textContent = message || ""; }
    function populateSettings() {
      const settings = getSettings();
      for (const [selector, saved] of [["#ca-selected-chats",settings.chat_selected_ids || []],["#ca-excluded-chats",settings.chat_excluded_ids || []]]) {
        const element = $(selector);
        const current = s.optionsLoaded ? selected(selector) : saved;
        const items = [...s.chats];
        for (const id of [...current, ...saved]) if (!items.some(c => c.id === id)) items.push({id,title:`Чат ID ${id}`});
        element.innerHTML = items.map(c => `<option value="${c.id}"${current.includes(c.id) ? " selected" : ""}>${esc(c.title)} · ID ${c.id}</option>`).join("");
      }
      s.optionsLoaded = true;
      window.ArchiveControls?.refresh($("#settings-form"));
    }
    function renderSelection() {
      const n=s.selected.size, count=s.pageItems.filter(c=>s.selected.has(c.id)).length;
      $("#ca-selection-label").textContent=n ? `Выбрано: ${n}` : "Выбрать страницу";
      $("#ca-save-selected").disabled=!n;
      $("#ca-delete-selected").disabled=!n;
      const check=$("#ca-select-page"); check.checked=Boolean(s.pageItems.length && count===s.pageItems.length);
      check.indeterminate=count>0 && count<s.pageItems.length; check.disabled=!s.pageItems.length;
    }
    function renderChats() {
      const q = $("#ca-chat-search").value.trim().toLocaleLowerCase("ru"), filters=values();
      const participants=filters.participant.split(",").filter(Boolean).map(Number);
      s.visible=s.chats.filter(c=>(!q || [c.title,...(c.aliases||[])].some(label=>label.toLocaleLowerCase("ru").includes(q)) || String(c.id)===q) && (!filters.type || (["tasks","conversations"].includes(filters.type) ? (c.group||"conversations")===filters.type : c.type===filters.type && (filters.type!=="chat" || c.group!=="tasks"))) && (!filters.coverage || c.coverage===filters.coverage) && participants.every(id=>c.participants?.some(p=>p.id===id)));
      if(s.chatOffset>=s.visible.length) s.chatOffset=Math.max(0,Math.floor((s.visible.length-1)/50)*50);
      s.pageItems=s.visible.slice(s.chatOffset,s.chatOffset+50);
      $("#ca-chat-count").textContent=`${s.visible.length}`;
      $("#ca-chat-list").innerHTML=s.pageItems.length ? s.pageItems.map(c=>`<div class="ca-chat-row${s.chat===c.id ? " active" : ""}"><input type="checkbox" data-ca-select="${c.id}" aria-label="Выбрать ${esc(c.title)}"${s.selected.has(c.id) ? " checked" : ""}><button type="button" class="ca-chat" data-ca-chat="${c.id}" aria-pressed="${s.chat===c.id}">${avatar(c.title)}<span class="ca-chat-text"><strong>${esc(c.title)}</strong><small>${c.group==="tasks" ? "Чат задачи" : c.type==="user" ? "Личный чат" : "Групповой чат"} · ${Number(c.count.messages ?? c.count.n ?? 0).toLocaleString("ru")} сообщений</small><small class="ca-chat-preview">${esc(c.preview)}</small>${c.source_last_message_at ? `<small>Последнее: ${esc(dateString(c.source_last_message_at))}</small>` : ""}${c.peer_active === false ? '<small class="ca-inactive">Пользователь неактивен</small>' : ""}<small class="ca-chat-state">${icon(c.coverage==="source_boundary" ? "check" : ["error","access_lost","tariff_limited"].includes(c.coverage) ? "alert" : c.coverage==="pending" ? "archive" : "refresh")}${esc(coverage[c.coverage] || c.coverage)}</small></span></button></div>`).join("") : '<div class="ca-empty"><p>Чаты не найдены. Обновите каталог или измените фильтры.</p></div>';
      $("#ca-chat-page-label").textContent=s.visible.length ? `${s.chatOffset+1}–${Math.min(s.chatOffset+50,s.visible.length)} из ${s.visible.length}` : "0 чатов";
      $("#ca-chat-page").textContent=String(Math.floor(s.chatOffset/50)+1);
      $("#ca-chat-previous").disabled=!s.chatOffset; $("#ca-chat-next").disabled=s.chatOffset+50>=s.visible.length;
      renderSelection();
    }
    function renderSummary(result) {
      const total=result.count ?? result.total ?? s.chats.length;
      $("#nav-chat-total").textContent=Number(total).toLocaleString("ru");
      $("#ca-metric-chats").textContent=Number(total).toLocaleString("ru");
      $("#ca-metric-messages").textContent=Number(result.messages ?? s.chats.reduce((n,c)=>n+Number(c.count.messages ?? c.count.n ?? 0),0)).toLocaleString("ru");
      $("#ca-metric-saved").textContent=`Чатов с сообщениями: ${result.archived ?? s.chats.filter(c=>Number(c.count.messages ?? c.count.n ?? 0)).length}`;
      $("#ca-metric-auto").textContent=result.auto_save ? "Включено" : "Выключено";
      $("#ca-metric-poll").textContent=`Новые сообщения · раз в ${Math.max(1,Math.round((result.poll_seconds || getSettings().chat_poll_seconds || 300)/60))} мин.`;
      const pending=Number(result.pending ?? result.pending_total ?? 0);
      const sync=result.sync;
      $("#ca-metric-loading").textContent=result.active ? "В работе" : pending ? "В очереди" : "Нет задач";
      const last=sync?.last_progress?.at ? `Сохранено: ${dateString(new Date(sync.last_progress.at*1000).toISOString())}` : "";
      $("#ca-sync-status").textContent=sync && !sync.worker_running ? "Фоновый обработчик не запущен" : [result.active || (pending ? `Работ: ${pending}` : "Старая история — по выбору"),last].filter(Boolean).join(" · ");
    }
    function renderHeading() {
      const chat = s.chats.find(c => c.id === s.chat);
      $("#ca-delete-current").disabled=!chat;
      if (!chat) {
        $("#ca-thread-heading").innerHTML = `<h2>${s.items.length || values().q ? "Поиск по архиву" : "Выберите чат"}</h2>`;
        $("#ca-coverage").hidden = true;
        return;
      }
      const people = chat.participants || [];
      $("#ca-thread-heading").innerHTML = `${avatar(chat.title)}<div class="ca-thread-identity"><h2>${esc(chat.title)}</h2><p class="footnote">${chat.group === "tasks" ? `Чат задачи${chat.task_id ? " №"+chat.task_id : ""}` : chat.type === "user" ? "Личный чат" : "Групповой чат"} · ID ${chat.id}${chat.peer_active === false ? " · Пользователь неактивен" : ""}</p></div><div class="meeting-people ca-thread-people${s.peopleExpanded ? " expanded" : ""}"><div class="meeting-people-list" id="ca-thread-people-list">${people.map(p => `<span class="participant-chip" title="${esc(p.name || `ID ${p.id}`)}${p.active === false ? " · Неактивный пользователь" : ""}"><span>${esc(p.name || `ID ${p.id}`)}</span></span>`).join("") || '<span class="footnote">Участники ещё не предоставлены</span>'}</div><button class="meeting-people-toggle" id="ca-people-toggle" type="button" aria-controls="ca-thread-people-list" aria-expanded="${s.peopleExpanded}" aria-label="${s.peopleExpanded ? "Свернуть" : "Показать всех"} участников (${people.length})">${icon("chevron-down")}</button></div>`;
      updatePeopleOverflow();
      const first = chat.count.first?.slice(0,10) || "—", last = chat.count.last?.slice(0,10) || "—";
      $("#ca-coverage").hidden = false;
      $("#ca-coverage").innerHTML = `${icon("info")} ${esc(coverage[chat.coverage] || chat.coverage)} · ${esc(first)} — ${esc(last)} · Проверено: ${esc(chat.last_checked ? dateString(chat.last_checked) : "ещё не проверено")}${chat.history_since ? `<br>Граница сбора: ${esc(chat.history_since)}` : ""}${chat.event_gap ? "<br>Пропущены уведомления об изменениях: промежуточные редакции могут отсутствовать." : ""}${(chat.limitations || []).map(t => `<br>${esc(t)}`).join("")}${chat.error ? `<br>${esc(chat.error)}` : ""}${chat.meeting_ids?.length ? `<br>Совещания: ${chat.meeting_ids.map(id => `<a href="#meeting/${id}">№${id}</a>`).join(", ")}` : ""}`;
    }
    function renderMessage(m) {
      const name = m.author || (m.system ? "Bitrix24" : `Автор ID ${m.author_id}`);
      const links = (m.relations || []).map(link => `<div class="ca-relation ${link.kind}"><div class="ca-relation-title">${icon(link.kind === "forward" ? "forward" : "reply")}<strong>${link.kind === "forward" ? "Переслано от" : link.kind === "reply" ? "Ответ" : "Цитата"} ${esc(link.author || (link.author_id ? `ID ${link.author_id}` : "· автор не предоставлен"))}</strong></div><small class="muted">Чат ${link.chat_id} · сообщение ${link.message_id}${link.date ? " · " + esc(dateString(link.date)) : ""}</small>${link.excerpt ? `<p>${esc(link.excerpt)}</p>` : ""}<button type="button" class="ca-source-link" data-ca-source="${link.message_id}" data-source-chat="${link.chat_id}" data-owner-chat="${m.chat_id}">К исходному</button></div>`).join("");
      const attachments = (m.files || []).map(f => `<div class="ca-file">${icon(fileIcons[f.category] || "file")}<div class="ca-file-info"><strong>${esc(f.name)}</strong><small>${f.size ? `${(f.size/1024).toLocaleString("ru",{maximumFractionDigits:1})} КБ · ` : ""}${esc(files[f.state] || f.state)}${f.error ? " · " + esc(f.error) : ""}</small></div>${f.path ? `<a class="button secondary" href="/api/chat-archive/chats/${m.chat_id}/files/${f.id}">Открыть</a>` : ""}${f.path || f.contents?.length ? `<button type="button" class="button quiet ca-file-delete" data-ca-remove-file="${f.id}" data-file-chat="${m.chat_id}" aria-label="Удалить сохранённый файл ${esc(f.name)}">${icon("trash")}</button>` : ""}<button type="button" class="button ${f.state === "saved" ? "quiet" : "secondary"}" data-ca-file="${f.id}" data-file-chat="${m.chat_id}"${["queued","running"].includes(f.state) ? " disabled" : ""}>${icon("download")}<span>${f.state === "saved" ? "Обновить файл" : "Скачать"}</span></button></div>`).join("");
      const counters = m.reactions?.reactionCounters || {};
      const reactions = Object.keys(counters).length ? `<div class="ca-reactions">Реакции: ${Object.entries(counters).map(([type,count]) => `${esc(type)} · ${Number(count)}`).join(" · ")}</div>` : "";
      const body = `${links}<div class="ca-body">${m.html || esc(m.text).replace(/\n/g,"<br>")}</div>${attachments}${reactions}`;
      if (m.deleted) return `<article class="ca-message" id="ca-message-${m.chat_id}-${m.id}"><details class="ca-deleted"><summary>${icon("trash")} Сообщение удалено в Bitrix24 · сохранённая версия · ID ${m.id}</summary><p>${esc(name)} · ${esc(dateString(m.date))}</p>${body}</details></article>`;
      return `<article class="ca-message${m.system ? " ca-system" : ""}" id="ca-message-${m.chat_id}-${m.id}">${m.system ? "" : avatar(name)}<div class="ca-message-content"><div class="ca-message-meta"><strong>${esc(name)}</strong><span>${esc(dateString(m.date))}</span><span>ID ${m.id}${m.version_count ? " · Изменено" : ""}</span>${!s.chat ? `<button type="button" class="ca-source-link" data-ca-chat="${m.chat_id}">В чат</button>` : ""}</div>${body}${m.version_count ? `<details class="ca-versions" data-ca-versions="${m.id}" data-version-chat="${m.chat_id}"><summary>${m.version_count} предыдущих редакций</summary><div class="ca-version-list"></div></details>` : ""}</div></article>`;
    }
    function renderMessages() {
      let day = "";
      $("#ca-messages").innerHTML = s.items.length ? [...s.items].reverse().map(m => { const current = m.date?.slice(0,10) || "Дата не предоставлена"; const separator = day !== current ? `<div class="ca-day">${esc(current)}</div>` : ""; day = current; return separator + renderMessage(m); }).join("") : '<div class="ca-empty"><h3>Сообщения не найдены</h3><p>Измените фильтры или дождитесь сохранения истории.</p></div>';
      $("#ca-message-count").textContent = `${s.items.length.toLocaleString("ru")} из ${s.total.toLocaleString("ru")} · Только просмотр`;

    }
    function updatePeopleOverflow() {
      const list=$("#ca-thread-people-list"), toggle=$("#ca-people-toggle"); if (!list || !toggle) return;
      toggle.hidden=!s.peopleExpanded && list.scrollHeight<=35;
      toggle.closest(".meeting-people").classList.toggle("has-overflow", list.scrollHeight>35 || s.peopleExpanded);
    }
    function loadStatus(text, error=false) { const status=$("#ca-load-status"); status.textContent=text || ""; status.hidden=!text; status.classList.toggle("filter-error",error); }
    async function loadMessages(append=false, around=0) {
      if (append && (s.messageBusy || s.nextOffset>=s.total)) return;
      const request=++s.request; s.messageBusy=true; loadStatus(append ? "Показываем более ранние сообщения…" : "Загружаем сообщения…");
      try {
        const params=new URLSearchParams(Object.entries({...values(),chat:s.chat || "",around:around || "",offset:append ? s.nextOffset : 0,limit:50}).filter(([,value])=>value!==""));
        const result=await api(`/api/chat-archive/messages?${params}`); if(request!==s.request) return;
        const scroll=$("#ca-messages"), top=scroll.scrollTop, height=scroll.scrollHeight;
        const known=new Set(s.items.map(m=>`${m.chat_id}:${m.id}`));
        s.items=append ? [...s.items,...result.items.filter(m=>!known.has(`${m.chat_id}:${m.id}`))] : result.items;
        s.total=result.total; s.offset=result.offset; s.nextOffset=result.offset+result.items.length;
        renderHeading(); renderMessages(); scroll.scrollTop=append ? top+scroll.scrollHeight-height : scroll.scrollHeight;
        loadStatus(s.nextOffset<s.total ? "Прокрутите вверх, чтобы увидеть предыдущие сообщения" : "");
      } catch(error) { if(request===s.request) { loadStatus("Не удалось показать сообщения. Откройте этот чат ещё раз для повтора.",true); showError(error.message); } }
      finally { if(request===s.request) s.messageBusy=false; }
    }
    async function refresh(load=true,force=false) {
      if (s.busy) return;
      s.busy = true;
      try {
        const result = await api("/api/chat-archive");
        if (s.account && s.account !== result.account) { s.chat=0; s.items=[]; s.request++; s.optionsLoaded=false; s.selected.clear(); s.chatOffset=0; }
        s.account=result.account; s.chats=result.items; s.last=Date.now();
        renderSummary(result);
        $("#ca-catalogue-progress").hidden=!result.sync?.discovering;
        $("#ca-catalogue-progress-text").textContent=result.sync?.last_catalogue_at ? "Получаем изменения каталога. Старая история сохраняется по выбору." : "Получаем доступные диалоги текущего аккаунта…";
        $("#ca-discovery").textContent=result.discovery;
        showError(result.error || result.events_error);
        $("#ca-events-status").textContent=result.events_status;
        populateSettings(); renderChats();
        const peopleKey=JSON.stringify([s.account,result.messages,s.chats.map(c=>[c.id,c.participants_at,c.participants?.length])]);
        if (load && (s.peopleKey!==peopleKey || Date.now()-(s.peopleAt || 0)>30000)) { await loadPeople(); s.peopleKey=peopleKey; s.peopleAt=Date.now(); }
        const scroll=$("#ca-messages"), readingOlder=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight>100;
        const keepView=s.items.length && (readingOlder || s.items.length>50 || scroll.querySelector("details[open]"));
        if (load && (s.chat || s.items.length || Object.values(values()).some(Boolean)) && (force || !keepView)) await loadMessages();
        else renderHeading();
      } catch(error) { showError(error.message); }
      finally { s.busy=false; }
    }
    async function loadPeople() {
      const result=await api("/api/chat-archive/filters");
      for(const name of ["author","participant"]) {
        const element=form.elements[name], current=new Set([...element.selectedOptions].map(o=>o.value));
        element.innerHTML=result.authors.map(p=>`<option data-count="${Number(p[name === "participant" ? "chats" : "messages"]) || 0}" value="${p.id}"${current.has(String(p.id)) ? " selected" : ""}>${esc(p.name)}</option>`).join("");
      }
      const element=form.elements.type, current=element.value;
      element.innerHTML='<option value="">Все</option>'+[...new Set(["conversations","tasks","user","chat",...result.types.filter(t=>t!=="tasksTask")])].map(type=>`<option value="${esc(type)}">${type==="conversations" ? "Личные и групповые" : type==="tasks" ? "Чаты задач" : type==="user" ? "Личный" : type==="chat" ? "Групповой" : esc(type)}</option>`).join(""); element.value=current;
      window.ArchiveControls?.refresh(form);
    }
    async function choose(id,around=0) { if(s.chat!==Number(id)) s.peopleExpanded=false; s.chat=Number(id); renderChats(); await loadMessages(false,around); }
    let timer, silentReset = false;
    function clearFilters() {
      for (const input of form.elements) {
        if (!input.name) continue;
        if (input.multiple) [...input.options].forEach(option=>option.selected=false);
        else input.value="";
      }
      window.ArchiveControls?.refresh(form);
    }
    function resetForContext() { clearTimeout(timer); silentReset = true; form.reset(); clearFilters(); silentReset = false; }
    form.addEventListener("submit", e => e.preventDefault());
    form.addEventListener("input", () => { clearTimeout(timer); timer=setTimeout(() => { if (form.elements.q.value) s.chat=0; s.chatOffset=0; renderChats(); loadMessages().catch(e=>showError(e.message)); },250); });
    form.addEventListener("reset", () => { if (!silentReset) setTimeout(() => { clearFilters(); s.chatOffset=0; renderChats(); loadMessages().catch(e=>showError(e.message)); },0); });
    $("#ca-chat-search").oninput=()=>{s.chatOffset=0; renderChats();};
    $("#ca-chat-list").addEventListener("click", e=>{ const button=e.target.closest("[data-ca-chat]"); if(button) choose(button.dataset.caChat).catch(e=>showError(e.message)); });
    $("#ca-refresh").onclick=e=>action(e.currentTarget,async()=>{ await api("/api/chat-archive/sync",{method:"POST"}); notify("Обновление каталога поставлено в очередь. Старая история сохраняется по выбору."); await refresh(true,true); });
    $("#ca-export").onclick=e=>action(e.currentTarget,async()=>{ const result=await api("/api/chat-archive/export",{method:"POST",data:{ids:s.chat?[s.chat]:[]}}); const link=document.createElement("a"); link.href=result.url; link.download="Архив чатов.zip"; link.click(); });
    $("#ca-messages").addEventListener("scroll",()=>{ if($("#ca-messages").scrollTop<100 && !s.messageBusy && s.nextOffset<s.total) loadMessages(true); },{passive:true});
    $("#ca-thread-heading").addEventListener("click",e=>{ if(e.target.closest("#ca-people-toggle")) {s.peopleExpanded=!s.peopleExpanded; renderHeading();} });
    window.addEventListener("resize",updatePeopleOverflow);
    $("#ca-chat-previous").onclick=()=>{s.chatOffset=Math.max(0,s.chatOffset-50); s.anchor=0; renderChats(); $("#ca-chat-list").scrollTop=0;};
    $("#ca-chat-next").onclick=()=>{s.chatOffset+=50; s.anchor=0; renderChats(); $("#ca-chat-list").scrollTop=0;};
    $("#ca-select-page").onchange=e=>{s.pageItems.forEach(c=>e.target.checked ? s.selected.add(c.id) : s.selected.delete(c.id)); renderChats();};
    $("#ca-chat-list").addEventListener("click",e=>{
      const input=e.target.closest("[data-ca-select]"); if(!input) return;
      const id=Number(input.dataset.caSelect), current=s.pageItems.findIndex(c=>c.id===id), anchor=s.pageItems.findIndex(c=>c.id===s.anchor);
      const range=e.shiftKey && anchor>=0 ? s.pageItems.slice(Math.min(anchor,current),Math.max(anchor,current)+1) : [s.pageItems[current]];
      range.forEach(c=>input.checked ? s.selected.add(c.id) : s.selected.delete(c.id)); s.anchor=id;
      document.querySelectorAll("[data-ca-select]").forEach(node=>node.checked=s.selected.has(Number(node.dataset.caSelect))); renderSelection();
    });
    $("#ca-save-selected").onclick=e=>action(e.currentTarget,async()=>{
      const result=await api("/api/chat-archive/backfill",{method:"POST",data:{ids:[...s.selected]}});
      notify(`Вся доступная история поставлена в очередь: ${result.queued} чатов. Вложения — по сохранённым настройкам.`);
      s.selected.clear(); renderChats(); await refresh(false,true);
    });
    $("#ca-delete-selected").onclick=e=>action(e.currentTarget,()=>openMaterialDelete([...s.selected],"chat"));
    $("#ca-delete-current").onclick=e=>action(e.currentTarget,()=>openMaterialDelete([s.chat],"chat"));
    $("#ca-messages").addEventListener("click", e=> {
      const file=e.target.closest("[data-ca-file]"), source=e.target.closest("[data-ca-source]"), chat=e.target.closest("[data-ca-chat]");
      const remove=e.target.closest("[data-ca-remove-file]");
      if(remove) action(remove,()=>openMaterialDelete([Number(remove.dataset.fileChat)],"chat",["attachments/file-"+remove.dataset.caRemoveFile]));
      if(file) action(file,async()=>{ await api(`/api/chat-archive/chats/${file.dataset.fileChat}/files/${file.dataset.caFile}/download`,{method:"POST"}); notify("Вложение поставлено в очередь."); await loadMessages(); });
      if(chat) { const owner=chat.closest("article"); const id=Number(owner?.id.split("-").pop()) || 0; resetForContext(); choose(chat.dataset.caChat,id).catch(e=>showError(e.message)); }
      if(source) action(source,async()=>{
        const sourceChat=Number(source.dataset.sourceChat), id=Number(source.dataset.caSource);
        try {
          await api(`/api/chat-archive/chats/${sourceChat}/messages/${id}`);
          resetForContext(); await choose(sourceChat,id);
          const node=$(`#ca-message-${sourceChat}-${id}`); node?.scrollIntoView({block:"center",behavior:"smooth"});
        } catch {
          const result=await api(`/api/chat-archive/chats/${source.dataset.ownerChat}/context/${sourceChat}/${id}`);
          const box=source.closest(".ca-relation");
          let note=box.querySelector(".ca-context-note"); if(!note) { note=document.createElement("p"); note.className="ca-context-note"; box.append(note); }
          note.textContent=result.items.length ? "Оригинал недоступен. Предоставленный источником фрагмент сохранён: " + result.items[0].text : "Оригинал недоступен; источник не предоставил дополнительный фрагмент.";
        }
      });
    });
    $("#ca-messages").addEventListener("toggle", e=>{
      const details=e.target;
      if(details.matches?.("[data-ca-versions]") && details.open && !details.dataset.loaded) {
        const box=details.querySelector(".ca-version-list"); box.textContent="Загрузка…";
        api(`/api/chat-archive/chats/${details.dataset.versionChat}/messages/${details.dataset.caVersions}/versions`).then(result=>{
          details.dataset.loaded="true"; box.innerHTML=result.items.map(item=>`<div class="ca-version"><p>Получено: ${esc(dateString(item.observed_at))} · ${esc(item.hash)}</p>${item.html}</div>`).join("");
        }).catch(error=>{box.textContent=error.message;});
      }
    },true);
    for (const [id,name] of [["ca-refresh","refresh"],["ca-export","download"],["ca-save-selected","download"],["ca-delete-current","trash"],["ca-delete-selected","trash"]]) {
      const button=$("#"+id); button.innerHTML=icon(name)+`<span>${esc(button.textContent)}</span>`;
    }
    return {
      async refreshAfterRemoval() { s.selected.clear(); s.last=0; await refresh(true,true); },
      async onRoute(route) { if(route === "chat-archive" || route === "settings") await refresh(route === "chat-archive"); },
      async onBootstrap(data,route) { if (["chat-archive","settings"].includes(route) && Date.now()-s.last>7000) await refresh(route === "chat-archive"); },
      settingsReset(settings) { s.optionsLoaded=false; populateSettings(); for (const [selector,key] of [["#ca-selected-chats","chat_selected_ids"],["#ca-excluded-chats","chat_excluded_ids"]]) for(const option of $(selector).options) option.selected=(settings[key]||[]).includes(Number(option.value)); window.ArchiveControls?.refresh($("#settings-form")); },
    };
  }
};
