"use strict";
window.ChatArchiveUI = {
  create({api, icon, escapeHtml: esc, dateString, notify, action, getSettings}) {
    const $ = selector => document.querySelector(selector);
    const form = $("#ca-filters");
    const s = {account: "", chats: [], chat: 0, items: [], total: 0, offset: 0, nextOffset: 0, request: 0, last: 0, busy: false, optionsLoaded: false};
    const coverage = {pending: "Ожидает сохранения", backfilling: "Догружается…", source_boundary: "Доступная история сохранена", tariff_limited: "История ограничена тарифом", access_lost: "Потеря доступа", error: "Ошибка — повторим позже", boundary_unverified: "Граница не подтверждена", verifying_boundary: "Проверяется граница"};
    const files = {not_saved: "Не скачан", queued: "В очереди", running: "Скачивается…", saved: "Скачан", unavailable: "Недоступен", error: "Ошибка", missing: "Файл перемещён"};
    const fileIcons = {images: "image", documents: "file", audio: "music", video: "video", other: "file"};
    const avatar = name => `<span class="ca-avatar" aria-hidden="true">${esc(String(name || "?").split(/\s+/).filter(Boolean).slice(0,2).map(n => n[0]).join("").toUpperCase())}</span>`;
    const selected = selector => [...$(selector).selectedOptions].map(option => Number(option.value));
    function values() { return Object.fromEntries(new FormData(form).entries()); }
    function showError(message) { $("#ca-error").hidden = !message; $("#ca-error").textContent = message || ""; }
    function populateSettings() {
      const settings = getSettings();
      for (const [selector, saved] of [["#ca-selected-chats",settings.chat_selected_ids || []],["#ca-excluded-chats",settings.chat_excluded_ids || []],["#ca-period-chats",selected("#ca-period-chats")]]) {
        const element = $(selector);
        const current = s.optionsLoaded ? selected(selector) : saved;
        const items = [...s.chats];
        for (const id of [...current, ...saved]) if (!items.some(c => c.id === id)) items.push({id,title:`Чат ID ${id}`});
        element.innerHTML = items.map(c => `<option value="${c.id}"${current.includes(c.id) ? " selected" : ""}>${esc(c.title)} · ID ${c.id}</option>`).join("");
      }
      s.optionsLoaded = true;
    }
    function renderChats() {
      const q = $("#ca-chat-search").value.trim().toLocaleLowerCase("ru");
      const filters = values();
      const chats = s.chats.filter(c => (!q || c.title.toLocaleLowerCase("ru").includes(q) || String(c.id) === q) && (!filters.type || c.type === filters.type) && (!filters.coverage || c.coverage === filters.coverage) && (!filters.participant || c.participants?.some(p => p.id === Number(filters.participant))));
      $("#ca-chat-count").textContent = `${chats.length}`;
      $("#ca-chat-list").innerHTML = chats.length ? chats.map(c => `<button type="button" class="ca-chat${s.chat === c.id ? " active" : ""}" data-ca-chat="${c.id}" aria-pressed="${s.chat === c.id}">${avatar(c.title)}<span class="ca-chat-text"><strong>${esc(c.title)}</strong><small>${c.type === "user" ? "Личный чат" : "Групповой чат"} · ${Number(c.count.messages ?? c.count.n ?? 0).toLocaleString("ru")} сообщений</small><small class="ca-chat-preview">${esc(c.preview)}</small><small class="ca-chat-state">${icon(c.coverage === "source_boundary" ? "check" : ["error","access_lost","tariff_limited"].includes(c.coverage) ? "alert" : "refresh")}${esc(coverage[c.coverage] || c.coverage)}</small></span></button>`).join("") : '<div class="ca-empty"><p>Сохранённых чатов пока нет. Нажмите «Обновить» или добавьте доступный чат в настройках.</p></div>';
    }
    function renderHeading() {
      const chat = s.chats.find(c => c.id === s.chat);
      if (!chat) {
        $("#ca-thread-heading").innerHTML = `<h2>${s.items.length || values().q ? "Поиск по архиву" : "Выберите чат"}</h2>`;
        $("#ca-coverage").hidden = true;
        return;
      }
      $("#ca-thread-heading").innerHTML = `${avatar(chat.title)}<div><h2>${esc(chat.title)}</h2><p class="footnote">${chat.type === "user" ? "Личный чат" : "Групповой чат"} · ID ${chat.id}</p></div><details><summary>Участники</summary><p class="footnote">${chat.participants?.length ? chat.participants.map(p => esc(p.name || `ID ${p.id}`)).join(", ") : "Список участников не предоставлен источником"}</p></details>`;
      const first = chat.count.first?.slice(0,10) || "—", last = chat.count.last?.slice(0,10) || "—";
      $("#ca-coverage").hidden = false;
      $("#ca-coverage").innerHTML = `${icon("info")} ${esc(coverage[chat.coverage] || chat.coverage)} · ${esc(first)} — ${esc(last)} · Проверено: ${esc(chat.last_checked ? dateString(chat.last_checked) : "ещё не проверено")}${chat.history_since ? `<br>Граница сбора: ${esc(chat.history_since)}` : ""}${chat.event_gap ? "<br>Возможен разрыв событий: промежуточные редакции могут отсутствовать." : ""}${(chat.limitations || []).map(t => `<br>${esc(t)}`).join("")}${chat.error ? `<br>${esc(chat.error)}` : ""}${chat.meeting_ids?.length ? `<br>Совещания: ${chat.meeting_ids.map(id => `<a href="#meeting/${id}">№${id}</a>`).join(", ")}` : ""}`;
    }
    function renderMessage(m) {
      const name = m.author || (m.system ? "Bitrix24" : `Автор ID ${m.author_id}`);
      const links = (m.relations || []).map(link => `<div class="ca-relation ${link.kind}"><div class="ca-relation-title">${icon(link.kind === "forward" ? "forward" : "reply")}<strong>${link.kind === "forward" ? "Переслано от" : link.kind === "reply" ? "Ответ" : "Цитата"} ${esc(link.author || (link.author_id ? `ID ${link.author_id}` : "· автор не предоставлен"))}</strong></div><small class="muted">Чат ${link.chat_id} · сообщение ${link.message_id}${link.date ? " · " + esc(dateString(link.date)) : ""}</small>${link.excerpt ? `<p>${esc(link.excerpt)}</p>` : ""}<button type="button" class="ca-source-link" data-ca-source="${link.message_id}" data-source-chat="${link.chat_id}" data-owner-chat="${m.chat_id}">К исходному</button></div>`).join("");
      const attachments = (m.files || []).map(f => `<div class="ca-file">${icon(fileIcons[f.category] || "file")}<div class="ca-file-info"><strong>${esc(f.name)}</strong><small>${f.size ? `${(f.size/1024).toLocaleString("ru",{maximumFractionDigits:1})} КБ · ` : ""}${esc(files[f.state] || f.state)}${f.error ? " · " + esc(f.error) : ""}</small></div>${f.path ? `<a class="button secondary" href="/api/chat-archive/chats/${m.chat_id}/files/${f.id}">Открыть</a>` : ""}<button type="button" class="button ${f.state === "saved" ? "quiet" : "secondary"}" data-ca-file="${f.id}" data-file-chat="${m.chat_id}"${["queued","running"].includes(f.state) ? " disabled" : ""}>${icon("download")}<span>${f.state === "saved" ? "Обновить файл" : "Скачать"}</span></button></div>`).join("");
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
      $("#ca-more").hidden = s.nextOffset >= s.total;
    }
    async function loadMessages(append=false, around=0) {
      const request = ++s.request;
      const filters = values();
      // Type, participant and coverage are catalogue filters, never collection rules.
      const params = new URLSearchParams(Object.entries({...filters, chat:s.chat || "", around:around || "", offset:append ? s.nextOffset : 0, limit:50}).filter(([,value])=>value !== ""));
      const result = await api(`/api/chat-archive/messages?${params}`);
      if (request !== s.request) return;
      const incoming = result.items;
      const scroll = $("#ca-messages"), top=scroll.scrollTop, height=scroll.scrollHeight;
      s.items = append ? [...s.items, ...incoming] : incoming;
      s.total = result.total; s.offset = result.offset; s.nextOffset=result.offset+result.items.length;
      renderHeading(); renderMessages();
      scroll.scrollTop=append ? top+scroll.scrollHeight-height : scroll.scrollHeight;
    }
    async function refresh(load=true,force=false) {
      if (s.busy) return;
      s.busy = true;
      try {
        const result = await api("/api/chat-archive");
        if (s.account && s.account !== result.account) { s.chat=0; s.items=[]; s.request++; s.optionsLoaded=false; }
        s.account=result.account; s.chats=result.items; s.last=Date.now();
        $("#ca-sync-status").textContent = `${result.auto_save ? "Автосохранение включено" : "Автосохранение выключено"}${result.active ? " · " + result.active : ""}`;
        $("#ca-discovery").textContent=result.discovery;
        showError(result.error || result.events_error);
        $("#ca-events-status").textContent=result.events_status;
        populateSettings(); renderChats();
        const scroll=$("#ca-messages"), readingOlder=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight>100;
        const keepView=s.items.length && (readingOlder || s.items.length>50 || scroll.querySelector("details[open]"));
        if (load && (s.chat || values().q || values().kind || values().attachment) && (force || !keepView)) await loadMessages();
        else renderHeading();
      } catch(error) { showError(error.message); }
      finally { s.busy=false; }
    }
    async function loadPeople() {
      const result = await api("/api/chat-archive/filters");
      for (const name of ["author","participant"]) {
        const element = form.elements[name], current=element.value;
        element.innerHTML='<option value="">Все</option>' + result.authors.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
        element.value=current;
      }
      const element=form.elements.type, current=element.value;
      element.innerHTML='<option value="">Все</option>' + [...new Set(["user","chat",...result.types])].map(type => `<option value="${esc(type)}">${type === "user" ? "Личный" : type === "chat" ? "Групповой" : esc(type)}</option>`).join(""); element.value=current;
    }
    async function choose(id,around=0) { s.chat=Number(id); renderChats(); await loadMessages(false,around); }
    let timer, silentReset = false;
    function resetForContext() { clearTimeout(timer); silentReset = true; form.reset(); silentReset = false; }
    form.addEventListener("submit", e => e.preventDefault());
    form.addEventListener("input", () => { clearTimeout(timer); timer=setTimeout(() => { if (form.elements.q.value) s.chat=0; renderChats(); loadMessages().catch(e=>showError(e.message)); },250); });
    form.addEventListener("reset", () => { if (!silentReset) setTimeout(() => { renderChats(); loadMessages().catch(e=>showError(e.message)); },0); });
    $("#ca-chat-search").oninput=renderChats;
    $("#ca-chat-list").addEventListener("click", e=>{ const button=e.target.closest("[data-ca-chat]"); if(button) choose(button.dataset.caChat).catch(e=>showError(e.message)); });
    $("#ca-refresh").onclick=e=>action(e.currentTarget,async()=>{ await api("/api/chat-archive/sync",{method:"POST"}); notify("Сохранение чатов поставлено в очередь."); await refresh(true,true); });
    $("#ca-export").onclick=e=>action(e.currentTarget,async()=>{ const result=await api("/api/chat-archive/export",{method:"POST",data:{ids:s.chat?[s.chat]:[]}}); const link=document.createElement("a"); link.href=result.url; link.download="Архив чатов.zip"; link.click(); });
    $("#ca-more").onclick=e=>action(e.currentTarget,()=>loadMessages(true));
    $("#ca-backfill").onclick=e=>action(e.currentTarget,async()=>{ const result=await api("/api/chat-archive/backfill",{method:"POST",data:{}}); $("#ca-settings-result").textContent=`В очереди: ${result.queued} чатов. Параметры сбора берутся из сохранённых настроек.`; });
    $("#ca-period").onclick=e=>action(e.currentTarget,async()=>{ const result=await api("/api/chat-archive/backfill",{method:"POST",data:{ids:selected("#ca-period-chats"),date_from:$("#ca-period-from").value,date_to:$("#ca-period-to").value}}); $("#ca-settings-result").textContent=`Сохранение периода поставлено в очередь: ${result.queued} чатов.`; });
    $("#ca-add-chat").onclick=e=>action(e.currentTarget,async()=>{ await api("/api/chat-archive/chats",{method:"POST",data:{dialog:$("#ca-dialog").value}}); $("#ca-dialog").value=""; await refresh(false); $("#ca-settings-result").textContent="Чат добавлен. Сохранение истории в очереди."; });
    $("#ca-messages").addEventListener("click", e=> {
      const file=e.target.closest("[data-ca-file]"), source=e.target.closest("[data-ca-source]"), chat=e.target.closest("[data-ca-chat]");
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
    for (const [id,name] of [["ca-refresh","refresh"],["ca-export","download"],["ca-more","clock"],["ca-backfill","refresh"],["ca-period","calendar"],["ca-add-chat","plus"]]) {
      const button=$("#"+id); button.innerHTML=icon(name)+`<span>${esc(button.textContent)}</span>`;
    }
    return {
      async onRoute(route) { if(route === "chat-archive" || route === "settings") { await refresh(route === "chat-archive"); if(route === "chat-archive") await loadPeople(); } },
      async onBootstrap(data,route) { if (["chat-archive","settings"].includes(route) && Date.now()-s.last>7000) await refresh(route === "chat-archive"); },
      settingsReset(settings) { s.optionsLoaded=false; populateSettings(); for (const [selector,key] of [["#ca-selected-chats","chat_selected_ids"],["#ca-excluded-chats","chat_excluded_ids"]]) for(const option of $(selector).options) option.selected=(settings[key]||[]).includes(Number(option.value)); },
    };
  }
};
