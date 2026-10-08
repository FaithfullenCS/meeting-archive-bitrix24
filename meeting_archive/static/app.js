"use strict";

(() => {
  // WebView2 can focus the first link on startup without keyboard navigation.
  document.addEventListener("keydown", event => {
    if (event.key === "Tab") document.documentElement.classList.add("keyboard-navigation");
  }, true);
  document.addEventListener("pointerdown", () => {
    document.documentElement.classList.remove("keyboard-navigation");
  }, true);
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { csrf: "", bootstrap: null, route: "archive", meetingId: null, detail: null,
    selected: new Set(), expandedPeople: new Set(), selectionAnchor: null, selectionList: "", items: [], total: 0, offset: 0, limit: 50, settingsReady: false,
    settingsDirty: false, pickedPaths: [], hardware: null, lastDetailAt: 0, lastListAt: 0,
    listRequest: 0, detailRequest: 0, busy: false, toastTimer: null, oauthAttempt: null,
    authRendered: false, moduleRendered: false,
    engines: null, lastEnginesAt: 0, installPlan: null, installPlanRequest: 0, estimateTimer: null,
    chats: [], chatIds: new Set(), chatsLoaded: false, chatsLoading: false, chatError: "", chatsOpen: false, chatCloseTimer: null, suppressChatFocus: false, chatCatalogueKey: "", chatsLoadedAt: 0,
    participants: [], participantIds: new Set(), participantsLoaded: false, participantsLoading: false, participantError: "", participantsOpen: false, participantCloseTimer: null, suppressParticipantFocus: false,
    participantCatalogueKey: "", participantsLoadedAt: 0, testMeetings: null, lastTestMeetingsAt: 0,
    filterTimer: null, filterPending: false, calendarMonth: new Date(new Date().getFullYear(), new Date().getMonth(), 1), calendarFocus: "", dateEndpoint: "from",
    secretEditing: { hf: false, webhook: false, client: false } };
  const labels = {
    saved: "Сохранено", ready: "Готово", complete: "Готово", completed: "Готово",
    short_call: "Короткий звонок", not_saved: "Не сохранено", pending: "Ожидается", queued: "В очереди", running: "В работе",
    unavailable: "Недоступно", missing: "Нет файла", error: "Ошибка", failed: "Ошибка",
    available: "Можно скачать", not_available: "Запись не предоставлена",
    cancelled: "Отменено", canceled: "Отменено", waiting: "Ожидается", done: "Готово", tested: "Короткий тест"
  };
  const kinds = { download: "Сохранение материалов совещания", fetch: "Сохранение материалов совещания", transcribe: "Локальная расшифровка", install: "Установка модуля", import: "Импорт записи", catalogue: "Обновление каталога" };
  const routes = { "chat-archive": "Архив чатов", archive: "Совещания", detail: "Совещание", jobs: "Очередь", module: "Настройки расшифровки", settings: "Настройки", connection: "Подключение Bitrix24", help: "Как пользоваться" };
  const iconPaths = {
    chat: '<path d="M21 11a8 8 0 0 1-8 8H7l-5 3V7a4 4 0 0 1 4-4h11a4 4 0 0 1 4 4zM7 8h10M7 12h6"/>',
    file: '<path d="M14 2H6v20h12V6zM14 2v5h5M9 12h6M9 16h6"/>',
    image: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8" cy="8" r="1"/><path d="m3 17 5-5 4 4 5-7 4 5"/>',
    music: '<path d="M9 18V5l12-2v13M9 8l12-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="18" cy="16" rx="3" ry="3"/>',
    video: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m10 8 6 4-6 4z"/>',
    reply: '<path d="m9 5-6 6 6 6M3 11h10a7 7 0 0 1 7 7"/>',
    forward: '<path d="m15 3 6 6-6 6M21 9H11a8 8 0 0 0-8 8v4"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    settings: '<path d="M4 7h16M4 17h16M8 4v6M16 14v6"/>', volume: '<path d="M3 10h4l5-4v12l-5-4H3zM16 8a6 6 0 0 1 0 8"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>', users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/><circle cx="9" cy="7" r="4"/>',
    plus: '<path d="M12 5v14M5 12h14"/>', x: '<path d="m6 6 12 12M6 18 18 6"/>', check: '<path d="m5 12 4 4L19 6"/>',
    play: '<path d="m8 5 11 7-11 7z"/>', pause: '<path d="M8 5v14M16 5v14"/>', download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>', upload: '<path d="M12 16V4m-5 5 5-5 5 5M4 16v5h16v-5"/>',
    folder: '<path d="M3 7V4h6l3 3h9v13H3z"/>', moon: '<path d="M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11z"/>', sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>', 'chevron-right': '<path d="m9 6 6 6-6 6"/>', 'arrow-left': '<path d="M20 12H4m6-6-6 6 6 6"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 2M18 17A7 7 0 0 1 6 19l-2-2"/>', copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>', undo: '<path d="M3 10h10a6 6 0 0 1 0 12M3 10l5-5M3 10l5 5"/>',
    external: '<path d="M14 3h7v7M10 14 21 3M21 14v7H3V3h7"/>', link: '<path d="m10 13 4-4M8 16l-2 2a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0M16 8l2-2a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>', alert: '<path d="m12 3 10 18H2zM12 9v4M12 17h.01"/>', calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18"/>',
    archive: '<rect x="3" y="3" width="18" height="5" rx="1"/><path d="M5 8v13h14V8M9 12h6"/>', cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4M12 2v2"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
    gear: '<path d="m9 3 1 2h4l1-2 3 2-1 2 2 3 2 1v3l-2 1-2 3 1 2-3 2-1-2h-4l-1 2-3-2 1-2-2-3-2-1v-3l2-1 2-3-1-2z"/><circle cx="12" cy="12.5" r="3"/>'
  };
  const icon = name => `<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${iconPaths[name] || iconPaths.check}</svg>`;
  function decorateButton(button, name) { if (button) button.innerHTML = `${icon(name)}<span>${escapeHtml(button.textContent.trim())}</span>`; }
  function setButtonLabel(button, label, name) {
    if (!button) return;
    const text = $("span", button);
    if (text && button.dataset.iconName === name) { if (text.textContent !== label) text.textContent = label; }
    else { button.innerHTML = `${icon(name)}<span>${escapeHtml(label)}</span>`; button.dataset.iconName = name; }
  }
  function decorateStaticIcons() {
    $$('[data-icon]').forEach(element => { element.innerHTML = icon(element.dataset.icon); });
    const names = { 'import-open': 'upload', 'refresh-catalogue': 'refresh', 'download-selected': 'download', 'detail-folder': 'folder', 'detail-download': 'download', 'download-audio': 'download', 'detail-import': 'upload', 'transcribe-button': 'play', 'jobs-refresh': 'refresh', 'settings-save': 'check', 'settings-revert': 'undo', 'open-archive': 'folder', 'resources-scan': 'search', 'resource-picker': 'folder', 'resource-plan-button': 'check', 'reuse-confirm': 'copy', 'module-install': 'download', 'module-cancel': 'x', 'module-remove': 'trash', 'hardware-refresh': 'refresh', 'hardware-probe': 'cpu', 'hf-check': 'check', 'hf-replace': 'refresh', 'hf-remove': 'trash', 'oauth-secret-replace': 'refresh', 'oauth-secret-remove': 'trash', 'webhook-replace': 'refresh', 'webhook-remove': 'trash', 'disconnect': 'x', 'copy-local-callback': 'copy', 'copy-install': 'copy', 'copy-callback': 'copy', 'import-picker': 'folder', 'import-submit': 'upload', 'previous-page': 'arrow-left', 'next-page': 'chevron-right' };
    Object.entries(names).forEach(([id, name]) => decorateButton($(`#${id}`), name));
    $$('.close-button').forEach(button => { button.innerHTML = icon('x'); });
    $$('.back-link').forEach(button => { button.innerHTML = `${icon('arrow-left')}<span>Все совещания</span>`; });
    $$('.empty-symbol').forEach(element => { element.innerHTML = icon('archive'); });
    $$('[data-pick]').forEach(button => decorateButton(button, 'folder'));
    decorateButton($('#filter-form button[type=reset]'), 'undo');
    decorateButton($('#oauth-form button[type=submit]'), 'external'); decorateButton($('#oauth-code-form button[type=submit]'), 'check');
  }
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const connected = () => Boolean(state.bootstrap?.connected);
  const statusClass = value => ["saved", "ready", "complete", "completed", "done"].includes(value) ? "success" : ["pending", "queued", "waiting"].includes(value) ? "waiting" : ["error", "failed"].includes(value) ? "error" : ["running", "tested"].includes(value) ? "active" : "neutral";
  const badge = value => `<span class="badge ${statusClass(value)}">${escapeHtml(labels[value] || value || "Не сохранено")}</span>`;
  const dateString = value => {
    if (!value) return "Дата не указана";
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("ru-RU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  };
  const durationString = value => {
    if (value == null || value === "") return "—";
    const seconds = Math.max(0, Number(value) || 0);
    if (seconds < 60) return `${Math.round(seconds)} сек`;
    const minutes = Math.round(seconds / 60);
    return minutes < 60 ? `${minutes} мин` : `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
  };
  const normalizeMeeting = item => {
    let metadata = item.metadata || {};
    if (typeof metadata === "string") { try { metadata = JSON.parse(metadata); } catch { metadata = {}; } }
    return { ...item, metadata, title: item.title || metadata.title || metadata.name || `Совещание ${item.call_id || item.id}`,
      startDate: item.startDate || metadata.startDate || metadata.date || metadata.createdAt,
      durationSeconds: item.durationSeconds ?? metadata.durationSeconds ?? metadata.duration };
  };
  const textValue = value => {
    if (typeof value === "string") return value;
    if (!value) return "";
    if (Array.isArray(value)) return value.map(textValue).join("\n");
    if (value.text) return String(value.text);
    if (value.segments) return value.segments.map(segment => {
      const seconds = Number(segment.start) || 0;
      const time = `${String(Math.floor(seconds / 3600)).padStart(2, "0")}:${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}:${String(Math.floor(seconds) % 60).padStart(2, "0")}`;
      return `[${time}] ${segment.speaker ? `${segment.speaker}: ` : ""}${segment.text || ""}`;
    }).join("\n");
    return JSON.stringify(value, null, 2);
  };
  const fileUrl = value => {
    try { const url = new URL(value, location.origin); return url.origin === location.origin && ["http:", "https:"].includes(url.protocol) ? url.pathname + url.search : ""; }
    catch { return ""; }
  };
  function setTheme(theme, save = false) {
    document.documentElement.dataset.theme = theme;
    if (save) { try { localStorage.setItem("meeting-archive-theme", theme); } catch { /* Storage may be disabled. */ } }
    const button = $("#theme-toggle");
    button.innerHTML = `${icon(theme === "dark" ? "sun" : "moon")}<span>${theme === "dark" ? "Светлая тема" : "Тёмная тема"}</span>`;
    button.setAttribute("aria-pressed", String(theme === "dark"));
  }
  const themePreference = window.matchMedia("(prefers-color-scheme: dark)");
  let storedTheme = null; try { storedTheme = localStorage.getItem("meeting-archive-theme"); } catch { /* Use the system preference. */ }
  setTheme(["light", "dark"].includes(storedTheme) ? storedTheme : themePreference.matches ? "dark" : "light");
  $("#theme-toggle").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true);
  themePreference.addEventListener("change", event => {
    let chosen = null; try { chosen = localStorage.getItem("meeting-archive-theme"); } catch { /* Use the system preference. */ }
    if (!["light", "dark"].includes(chosen)) setTheme(event.matches ? "dark" : "light");
  });
  function updateAudioDownloadPolicy() {
    const form = $("#settings-form"), audio = form.elements.auto_download_audio;
    const required = form.elements.auto_local.checked;
    if (required) audio.checked = true;
    audio.disabled = required;
    $("#audio-download-hint").textContent = required ? "Для локальной авторасшифровки нужна аудиозапись. Выключите авторасшифровку, чтобы изменить эту настройку." : "Если выключено, ручная и автоматическая загрузка сохраняют только текст Follow-up. Отдельно скачать аудио можно в карточке совещания.";
  }
  function updateDownloadButtons() {
    const settings = state.bootstrap?.settings || {};
    const audio = Boolean(settings.auto_download_audio || settings.auto_local);
    setButtonLabel($("#download-selected"), audio ? "Скачать выбранное" : "Скачать тексты Follow-up", "download");
    const shortCall = state.detail?.meeting?.id === state.meetingId && state.detail?.meeting?.bitrix === "short_call";
    setButtonLabel($("#detail-download"), shortCall ? "Проверить материалы Bitrix24" : audio ? "Скачать материалы" : "Скачать текст Follow-up", "download");
    for (const selector of ["#download-selected", "#detail-download"]) {
      $(selector).title = audio ? "Скачать текст Follow-up и аудиозапись" : "Скачать текст Follow-up без аудио";
    }
  }
  function updateDiarization() {
    const diarization = $("#settings-form").elements.diarization;
    const allowed = Boolean(state.bootstrap?.secret_status?.hf_token_saved && state.bootstrap?.hf_verified);
    diarization.disabled = !allowed;
    if (!allowed) diarization.checked = false;
    $("#diarization-options").hidden = !diarization.checked;
    $("#diarization-access-hint").textContent = allowed ? "Токен сохранён, доступ подтверждён." : "Сначала сохраните токен и проверьте доступ к моделям ниже.";
  }
  function updateScheduleVisibility() {
    $$("[data-schedule]").forEach(editor => { $("[data-schedule-window]", editor).hidden = $("[data-schedule-mode]", editor).value !== "window"; });
  }
  function fillSchedules(settings) {
    $$("[data-schedule]").forEach(editor => {
      const schedule = settings[`${editor.dataset.schedule}_schedule`] || { mode: "immediate", days: [0,1,2,3,4,5,6], start: "22:00", end: "06:00" };
      $("[data-schedule-mode]", editor).value = schedule.mode;
      $$("[data-day]", editor).forEach(input => { input.checked = schedule.days.includes(Number(input.dataset.day)); });
      $("[data-schedule-start]", editor).value = schedule.start; $("[data-schedule-end]", editor).value = schedule.end;
    });
    updateScheduleVisibility();
  }
  function readSchedule(editor) { return { mode: $("[data-schedule-mode]", editor).value, days: $$("[data-day]:checked", editor).map(input => Number(input.dataset.day)), start: $("[data-schedule-start]", editor).value, end: $("[data-schedule-end]", editor).value }; }
  function portalHost(value) {
    try { return new URL(/^https?:\/\//.test(value) ? value : `https://${value}`).hostname.toLowerCase(); } catch { return ""; }
  }
  function updateOauthFlow() {
    const form = $("#oauth-form"); const flow = form.elements.flow.value;
    $("#oauth-local-help").hidden = flow !== "local";
    $("#oauth-relay-fields").hidden = flow !== "relay"; $("#oauth-oob-help").hidden = flow !== "oob";
    form.elements.oauth_relay.required = flow === "relay";
    const base = form.elements.oauth_relay.value.trim().replace(/\/+$/, "");
    let valid = false; try { valid = new URL(base).protocol === "https:"; } catch { /* Address not entered yet. */ }
    $("#oauth-install").textContent = valid ? `${base}/oauth/install` : "Сначала укажите HTTPS-обработчик";
    $("#oauth-callback").textContent = valid ? `${base}/oauth/callback` : "Сначала укажите HTTPS-обработчик";
    renderSecrets();
  }
  function renderSecrets() {
    const secrets = state.bootstrap?.secret_status || {};
    const hf = Boolean(secrets.hf_token_saved); const webhook = Boolean(secrets.webhook_saved);
    $("#hf-secret-saved").hidden = !hf; $("#hf-token-entry").hidden = hf && !state.secretEditing.hf;
    $("#hf-token").placeholder = hf ? "Новый токен Read для замены" : "Токен Read";
    $("#hf-check").textContent = hf && !state.secretEditing.hf ? "Проверить сохранённый токен" : "Проверить доступ";
    $("#webhook-secret-saved").hidden = !webhook;
    $("#webhook-token-entry").hidden = webhook && !state.secretEditing.webhook;
    const webhookInput = $("#webhook-form").elements.webhook;
    webhookInput.required = !webhook || state.secretEditing.webhook;
    webhookInput.placeholder = webhook ? "Новая ссылка для замены сохранённой" : "Секретная ссылка из Bitrix24";
    $("#webhook-form button[type=submit]").textContent = webhook && !state.secretEditing.webhook ? "Webhook уже подключён" : "Проверить и подключить";
    $("#webhook-form button[type=submit]").disabled = webhook && !state.secretEditing.webhook;
    const form = $("#oauth-form");
    const clientSaved = Boolean(secrets.client_secret_saved && secrets.client_id === form.elements.client_id.value.trim() &&
      portalHost(form.elements.portal.value) === portalHost(state.bootstrap?.settings?.portal || ""));
    $("#oauth-secret-saved").hidden = !clientSaved;
    $("#oauth-secret-entry").hidden = clientSaved && !state.secretEditing.client;
    form.elements.client_secret.required = !clientSaved || state.secretEditing.client;
    form.elements.client_secret.placeholder = clientSaved ? "Новый секрет для замены" : "Секрет из карточки приложения";
    decorateButton($("#hf-check"), "check"); decorateButton($("#webhook-form button[type=submit]"), "link");
  }
  function renderParticipants() {
    const selected = [...state.participantIds];
    const chipsHtml = selected.map(id => { const person = state.participants.find(item => item.id === id); const label = person?.label || id; return `<span class="participant-chip"><span>${escapeHtml(label)}</span><button type="button" data-participant-remove="${escapeHtml(id)}" aria-label="Убрать участника ${escapeHtml(label)}">${icon("x")}</button></span>`; }).join("");
    if (state.participantChipsHtml !== chipsHtml) { $("#participant-chips").innerHTML = chipsHtml; state.participantChipsHtml = chipsHtml; }
    $("#participants-caption").textContent = selected.length ? "Добавить" : "Выбрать участников";
    const query = $("#participant-search").value.trim().toLocaleLowerCase("ru-RU");
    const items = state.participants.filter(item => `${item.label || ""} ${item.id}`.toLocaleLowerCase("ru-RU").includes(query));
    const optionsHtml = items.map(person => `<li><button type="button" data-participant-add="${escapeHtml(person.id)}" aria-label="${state.participantIds.has(person.id) ? "Убрать" : "Добавить"} участника ${escapeHtml(person.label || person.id)}" aria-pressed="${state.participantIds.has(person.id)}"><span class="participant-person"><strong>${escapeHtml(person.label || person.id)}</strong><small>Совещаний: ${Number(person.count) || 0}</small></span>${icon(state.participantIds.has(person.id) ? "check" : "plus")}</button></li>`).join("");
    // A refresh triggered by focusin must not replace a button between pointerdown
    // and click. Keep unchanged rows mounted while updating the loading caption.
    if (state.participantOptionsHtml !== optionsHtml) { $("#participant-options").innerHTML = optionsHtml; state.participantOptionsHtml = optionsHtml; }
    $("#participant-options-status").textContent = state.participantsLoading ? "Загружаем участников каталога…" : state.participantError || (!items.length ? query ? "Участники с таким именем не найдены." : state.bootstrap?.catalogue?.running ? "Обновляем каталог. Участники появятся автоматически." : "Список участников пуст. Обновите каталог совещаний." : `Найдено: ${items.length}. Выбрано: ${selected.length}.`);
  }
  function participantCatalogueKey() {
    const data = state.bootstrap || {}, catalogue = data.catalogue || {}, settings = data.settings || {};
    return JSON.stringify([data.connected, settings.portal, settings.user_id, catalogue.total, catalogue.count, catalogue.running]);
  }
  function participantsNeedRefresh() {
    return !state.participantsLoaded || state.participantCatalogueKey !== participantCatalogueKey()
      || Date.now() - state.participantsLoadedAt > 12000;
  }
  async function loadParticipants() {
    if (state.participantsLoading) return;
    const catalogueKey = participantCatalogueKey();
    state.participantsLoading = true; state.participantError = ""; renderParticipants();
    try { const result = await api("/api/participants"); state.participants = result.items || []; state.participantCatalogueKey = catalogueKey; state.participantsLoadedAt = Date.now(); state.participantsLoaded = true; }
    catch (error) { state.participantError = error.message; state.participantsLoaded = false; }
    finally { state.participantsLoading = false; renderParticipants(); }
  }
  function openParticipants(focus = false) {
    closeCalendar();
    clearTimeout(state.participantCloseTimer); state.participantsOpen = true; $("#participants-menu").hidden = false; $("#participants-toggle").setAttribute("aria-expanded", "true");
    if (participantsNeedRefresh()) loadParticipants();
    if (focus) $("#participant-search").focus();
  }
  function closeParticipants(focus = false) {
    clearTimeout(state.participantCloseTimer); state.participantsOpen = false; $("#participants-menu").hidden = true; $("#participants-toggle").setAttribute("aria-expanded", "false");
    if (focus) { state.suppressParticipantFocus = true; $("#participants-toggle").focus(); state.suppressParticipantFocus = false; }
  }
  function addParticipant(id) { if (!state.participants.some(item => item.id === id)) return; if (state.participantIds.has(id)) state.participantIds.delete(id); else state.participantIds.add(id); renderParticipants(); $("#participant-search").focus(); requestFilter(); }
  function renderChats() {
    const selected = [...state.chatIds];
    const chipsHtml = selected.map(id => { const person = state.chats.find(item => item.id === id); const label = person?.label || id; return `<span class="participant-chip"><span>${escapeHtml(label)}</span><button type="button" data-chat-remove="${escapeHtml(id)}" aria-label="Убрать чат ${escapeHtml(label)}">${icon("x")}</button></span>`; }).join("");
    if (state.chatChipsHtml !== chipsHtml) { $("#chat-chips").innerHTML = chipsHtml; state.chatChipsHtml = chipsHtml; }
    $("#chats-caption").textContent = selected.length ? "Добавить" : "Выбрать чаты";
    const query = $("#chat-search").value.trim().toLocaleLowerCase("ru-RU");
    const items = state.chats.filter(item => `${item.label || ""} ${item.id}`.toLocaleLowerCase("ru-RU").includes(query));
    const optionsHtml = items.map(person => `<li><button type="button" data-chat-add="${escapeHtml(person.id)}" aria-label="${state.chatIds.has(person.id) ? "Убрать" : "Добавить"} чат ${escapeHtml(person.label || person.id)}" aria-pressed="${state.chatIds.has(person.id)}"><span class="participant-person"><strong>${escapeHtml(person.label || person.id)}</strong><small>Совещаний: ${Number(person.count) || 0}</small></span>${icon(state.chatIds.has(person.id) ? "check" : "plus")}</button></li>`).join("");
    // A refresh triggered by focusin must not replace a button between pointerdown
    // and click. Keep unchanged rows mounted while updating the loading caption.
    if (state.chatOptionsHtml !== optionsHtml) { $("#chat-options").innerHTML = optionsHtml; state.chatOptionsHtml = optionsHtml; }
    $("#chat-options-status").textContent = state.chatsLoading ? "Загружаем чаты каталога…" : state.chatError || (!items.length ? query ? "Чаты с таким именем не найдены." : state.bootstrap?.catalogue?.running ? "Обновляем каталог. Чаты появятся автоматически." : "Список чатов пуст. Обновите каталог совещаний." : `Найдено: ${items.length}. Выбрано: ${selected.length}.`);
  }
  function chatCatalogueKey() {
    const data = state.bootstrap || {}, catalogue = data.catalogue || {}, settings = data.settings || {};
    return JSON.stringify([data.connected, settings.portal, settings.user_id, catalogue.total, catalogue.count, catalogue.running, data.chat_revision]);
  }
  function chatsNeedRefresh() {
    return !state.chatsLoaded || state.chatCatalogueKey !== chatCatalogueKey()
      || Date.now() - state.chatsLoadedAt > 12000;
  }
  async function loadChats() {
    if (state.chatsLoading) return;
    const catalogueKey = chatCatalogueKey();
    state.chatsLoading = true; state.chatError = ""; renderChats();
    try { const result = await api("/api/chats"); state.chats = result.items || []; state.chatCatalogueKey = catalogueKey; state.chatsLoadedAt = Date.now(); state.chatsLoaded = true; }
    catch (error) { state.chatError = error.message; state.chatsLoaded = false; }
    finally { state.chatsLoading = false; renderChats(); }
  }
  function openChats(focus = false) {
    closeCalendar();
    clearTimeout(state.chatCloseTimer); state.chatsOpen = true; $("#chats-menu").hidden = false; $("#chats-toggle").setAttribute("aria-expanded", "true");
    if (chatsNeedRefresh()) loadChats();
    if (focus) $("#chat-search").focus();
  }
  function closeChats(focus = false) {
    clearTimeout(state.chatCloseTimer); state.chatsOpen = false; $("#chats-menu").hidden = true; $("#chats-toggle").setAttribute("aria-expanded", "false");
    if (focus) { state.suppressChatFocus = true; $("#chats-toggle").focus(); state.suppressChatFocus = false; }
  }
  function addChatChoice(id) { if (!state.chats.some(item => item.id === id)) return; if (state.chatIds.has(id)) state.chatIds.delete(id); else state.chatIds.add(id); renderChats(); $("#chat-search").focus(); requestFilter(); }
  const isoDay = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const parseDay = value => { const parts = value.split("-").map(Number); return new Date(parts[0], parts[1] - 1, parts[2]); };
  const shortDay = value => value ? parseDay(value).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" }) : "Не выбрана";
  function renderCalendar() {
    const form = $("#filter-form"); const from = form.elements.date_from.value; const to = form.elements.date_to.value;
    $("#period-caption").textContent = from && to ? `${shortDay(from)} — ${shortDay(to)}` : from ? `С ${shortDay(from)}` : to ? `По ${shortDay(to)}` : "Период · любые даты";
    $("#period-from").textContent = shortDay(from); $("#period-to").textContent = shortDay(to);
    $$('[data-date-endpoint]').forEach(button => { const active = button.dataset.dateEndpoint === state.dateEndpoint; button.classList.toggle("active", active); button.setAttribute("aria-pressed", String(active)); });
    const month = state.calendarMonth; const days = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate(); const offset = (month.getDay() + 6) % 7;
    $("#calendar-month").textContent = month.toLocaleDateString("ru-RU", { month: "long", year: "numeric" });
    const today = isoDay(new Date()); const focused = state.calendarFocus.startsWith(isoDay(month).slice(0, 7)) ? state.calendarFocus : today.startsWith(isoDay(month).slice(0, 7)) ? today : isoDay(month);
    const cells = Array.from({ length: offset }, () => '<span class="calendar-blank" role="gridcell"></span>');
    cells.push(...Array.from({ length: days }, (_, index) => { const day = isoDay(new Date(month.getFullYear(), month.getMonth(), index + 1)); const selected = day === from || day === to; const inside = from && to && day > from && day < to; return `<span role="gridcell" aria-selected="${selected || Boolean(inside)}"><button type="button" data-calendar-day="${day}" tabindex="${day === focused ? "0" : "-1"}" class="calendar-day${selected ? " selected" : ""}${inside ? " in-range" : ""}${day === today ? " today" : ""}" ${day === today ? 'aria-current="date"' : ""} aria-label="${escapeHtml(parseDay(day).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric" }))}">${index + 1}</button></span>`; }));
    while (cells.length % 7) cells.push('<span class="calendar-blank" role="gridcell"></span>');
    $("#calendar-days").innerHTML = Array.from({ length: cells.length / 7 }, (_, index) => `<div role="row" class="calendar-row">${cells.slice(index * 7, index * 7 + 7).join("")}</div>`).join("");
    $("#calendar-hint").textContent = state.dateEndpoint === "from" ? "Выберите дату начала. Фильтр обновится сразу." : "Выберите дату окончания или оставьте период открытым.";
  }
  function closeCalendar(focus = false) { $("#period-menu").hidden = true; $("#period-toggle").setAttribute("aria-expanded", "false"); if (focus) $("#period-toggle").focus(); }
  function openCalendar() {
    closeParticipants(); const value = $("#filter-form").elements[state.dateEndpoint === "from" ? "date_from" : "date_to"].value || $("#filter-form").elements.date_from.value;
    if (value) { const date = parseDay(value); state.calendarMonth = new Date(date.getFullYear(), date.getMonth(), 1); state.calendarFocus = value; }
    renderCalendar(); $("#period-menu").hidden = false; $("#period-toggle").setAttribute("aria-expanded", "true"); $("#calendar-days button[tabindex='0']").focus();
  }
  function selectDay(value) {
    const form = $("#filter-form"); state.calendarFocus = value;
    if (state.dateEndpoint === "from") { form.elements.date_from.value = value; form.elements.date_to.value = ""; state.dateEndpoint = "to"; }
    else { form.elements.date_to.value = value; if (form.elements.date_from.value && form.elements.date_from.value > value) { form.elements.date_to.value = form.elements.date_from.value; form.elements.date_from.value = value; } state.dateEndpoint = "from"; }
    renderCalendar(); $(`#calendar-days [data-calendar-day="${value}"]`).focus(); requestFilter();
  }
  function shiftCalendar(months) { state.calendarMonth = new Date(state.calendarMonth.getFullYear(), state.calendarMonth.getMonth() + months, 1); state.calendarFocus = isoDay(state.calendarMonth); renderCalendar(); }
  function requestFilter(delay = 0) {
    clearTimeout(state.filterTimer); state.listRequest++; state.offset = 0; state.filterPending = true;
    const form = $("#filter-form"); const error = form.elements.min_minutes.value && form.elements.max_minutes.value && Number(form.elements.min_minutes.value) > Number(form.elements.max_minutes.value) ? "Длительность «от» должна быть не больше длительности «до»." : !form.elements.min_minutes.validity.valid || !form.elements.max_minutes.validity.valid ? "Укажите длительность в целых минутах, начиная с 0." : "";
    $("#filter-error").textContent = error; $("#filter-error").hidden = !error;
    if (error) return;
    $("#results-label").textContent = "Обновляем список…";
    state.filterTimer = setTimeout(async () => { state.filterTimer = null; state.filterPending = false; try { await loadMeetings(); } catch (error) { notify(error.message, true); } }, delay);
  }
  function notify(message, error = false) {
    const toast = $("#toast");
    clearTimeout(state.toastTimer);
    toast.textContent = message; toast.classList.toggle("error", error); toast.hidden = false;
    state.toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 9000 : 5000);
  }
  function showError(message) { const box = $("#global-error"); box.textContent = message; box.hidden = !message; }
  async function api(path, { method = "GET", data, form } = {}) {
    if (method !== "GET" && !state.csrf) throw new Error("Связь с локальным приложением ещё не установлена. Подождите несколько секунд.");
    const headers = { Accept: "application/json" };
    if (method !== "GET") headers["X-CSRF-Token"] = state.csrf;
    if (data !== undefined) headers["Content-Type"] = "application/json";
    let response;
    try { response = await fetch(path, { method, headers, credentials: "same-origin", body: form || (data === undefined ? undefined : JSON.stringify(data)) }); }
    catch { throw new Error("Нет связи с локальным приложением. Проверьте, что Meeting Archive запущен в трее."); }
    let result;
    try { result = await response.json(); } catch { result = {}; }
    if (!response.ok) {
      let message = result.detail || result.error || `Запрос не выполнен (${response.status}).`;
      if (typeof message !== "string") message = Array.isArray(message) ? message.map(item => item.msg || String(item)).join("; ") : JSON.stringify(message);
      throw new Error(message);
    }
    return result;
  }
  async function action(button, callback) {
    if (button?.classList.contains("busy")) return;
    if (button) { button.classList.add("busy"); button.setAttribute("aria-busy", "true"); }
    try { return await callback(); } catch (error) { notify(error.message, true); }
    finally { if (button) { button.classList.remove("busy"); button.removeAttribute("aria-busy"); } }
  }
  function confirmAction(title, message, yes = "Подтвердить", danger = false) {
    return new Promise(resolve => {
      const dialog = $("#confirm-dialog");
      $("#confirm-title").textContent = title; $("#confirm-message").textContent = message;
    const yesButton = $("#confirm-yes"); yesButton.textContent = yes;
    yesButton.className = `button ${danger ? "danger-button" : "primary"}`;
    decorateButton(yesButton, danger ? "trash" : "check");
      const finish = answer => { dialog.close(); yesButton.onclick = null; $("#confirm-no").onclick = null; $("#confirm-close").onclick = null; dialog.oncancel = null; resolve(answer); };
      yesButton.onclick = () => finish(true); $("#confirm-no").onclick = () => finish(false); $("#confirm-close").onclick = () => finish(false);
      dialog.oncancel = event => { event.preventDefault(); finish(false); };
      dialog.showModal(); $("#confirm-no").focus();
    });
  }
  function navigate(route, id = null) {
    if (!(route in routes)) route = "archive";
    location.hash = route === "detail" && id ? `meeting/${id}` : route;
    applyRoute();
  }
  function applyRoute() {
    const hash = location.hash.slice(1);
    const match = /^meeting\/(\d+)$/.exec(hash);
    const legacyProfile = hash === "module";
    const route = match ? "detail" : legacyProfile ? "settings" : (hash in routes ? hash : "archive");
    if (legacyProfile) { history.replaceState(null, "", "#settings"); window.ArchiveControls?.showCategory("profile"); }
    const previous = state.route; const oldMeeting = state.meetingId;
    if (previous !== route) { closeParticipants(); closeCalendar(); }
    state.route = route; state.meetingId = match ? Number(match[1]) : null;
    document.body.dataset.route=route;
    $$(".page").forEach(page => { page.hidden = page.id !== `page-${route}`; });
    $$(".nav-item").forEach(button => { const active = button.dataset.route === (route === "detail" ? "archive" : route === "module" ? "settings" : route); button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
    $$(".settings-tabs [data-route]").forEach(button => { const active = button.dataset.route === route; button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
    $("#breadcrumb").textContent = `Рабочее пространство / ${routes[route]}`;
    document.title = "Meeting Archive";
    if (previous !== route || oldMeeting !== state.meetingId) window.scrollTo({ top: 0, behavior: "instant" });
    $("#settings-savebar").hidden = !["settings", "module"].includes(route);
    if (route === "detail" && state.meetingId && (oldMeeting !== state.meetingId || !state.detail)) loadDetail().catch(error => showError(error.message));
    if (route === "module" && !state.hardware && state.csrf) loadHardware().catch(error => notify(error.message, true));
    if (route === "module" && !state.engines && state.csrf) loadEngines().catch(error => notify(error.message, true));
    if (route === "archive" && participantsNeedRefresh() && state.csrf) loadParticipants();
    if (state.csrf) caUI.onRoute(route).catch(error => notify(error.message, true));
  }
  function profileVisible() { return state.route === "settings" && !$("#settings-panel-profile").hidden; }
  async function loadProfile() {
    if (!state.csrf || !profileVisible()) return;
    if (!state.hardware || state.hardwareProfile !== hardwareProfile()) await loadHardware();
    if (!state.testMeetings || Date.now() - state.lastTestMeetingsAt > 12000) await loadTestMeetings();
    if (!state.engines || Date.now() - state.lastEnginesAt > 12000) await loadEngines();
    window.ArchiveControls?.refresh($("#settings-panel-profile"));
  }
  function updateSettingsForm(settings, force = false) {
    if (state.settingsReady && !force) return;
    for (const [name, value] of Object.entries(settings || {})) {
      const input = $("#settings-form").elements.namedItem(name);
      if (!input) continue;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else if (input.multiple) { for (const option of input.options) option.selected = (value || []).includes(Number(option.value)); }
      else if (["min_speakers", "max_speakers"].includes(name) && !value) input.value = "";
      else input.value = value ?? "";
    }
    state.settingsReady = true; state.settingsDirty = false;
    fillSchedules(settings);
    caUI.settingsReset(settings);
    window.ArchiveControls?.refresh(document);
    $("#settings-status").textContent = "Настройки сохранены.";
    $(".settings-footer").classList.remove("dirty");
    updateCpuAcknowledgement();
    updateAudioDownloadPolicy(); updateDiarization(); updateEngineControls();
    if (!$("#oauth-form").elements.portal.value) $("#oauth-form").elements.portal.value = settings.portal || "";
    if (!$("#oauth-form").elements.oauth_relay.value) $("#oauth-form").elements.oauth_relay.value = settings.oauth_relay || "";
    if (!$("#oauth-form").elements.client_id.value) $("#oauth-form").elements.client_id.value = state.bootstrap?.secret_status?.client_id || "";
    if (!state.authRendered) $("#oauth-form").elements.flow.value = settings.oauth_flow || "local";
    updateOauthFlow();
    $("#install-profile").value = settings.device || "cuda";
    if (!$("#install-packages").children.length) renderInstallModels();
  }
  function updateCpuAcknowledgement() { $("#cpu-confirmation").hidden = $("#device-select").value !== "cpu"; }
  function renderBootstrap(data) {
    $("#chat-filter-status").textContent = data.chat_warning || "";
    $("#chat-filter-status").hidden = !data.chat_warning;
    state.bootstrap = data; state.csrf = data.csrf || state.csrf;
    updateDownloadButtons();
    const settings = data.settings || {};
    if (data.chat_archive && $("#nav-chat-total")) $("#nav-chat-total").textContent = Number(data.chat_archive.count || 0).toLocaleString("ru");
    updateSettingsForm(settings); renderDesktopSettings(data);
    const isConnected = connected();
    $("#connection-dot").classList.toggle("connected", isConnected);
    const sidebar = $("#sidebar-connection"); sidebar.textContent = isConnected ? data.account_name || `Пользователь #${settings.user_id}` : "Bitrix24 не подключён";
    const sub = document.createElement("small"); sub.textContent = isConnected ? settings.portal || "Портал подключён" : "Подключите портал для загрузки истории"; sidebar.append(sub);
    $("#connection-nudge").hidden = isConnected;
    $("#connected-banner").hidden = !isConnected;
    $("#connected-portal").textContent = settings.portal ? `Подключён ${settings.portal}` : "Bitrix24 подключён";
    $("#connected-description").textContent = `Способ: ${settings.auth_mode === "webhook" ? "входящий webhook" : "OAuth"}. Каталог ограничен текущим пользователем${settings.user_id ? ` (ID ${settings.user_id})` : ""}.`;
    $("#refresh-catalogue").disabled = !isConnected || Boolean(data.catalogue?.running);
    const status = $("#app-status"); status.textContent = (settings.auto_download || settings.auto_local || settings.watch_enabled || settings.chat_auto_save || settings.chat_events) && settings.paused ? "Автоматизация на паузе" : "Приложение работает"; status.className = `badge ${settings.paused ? "waiting" : "success"}`;
    setButtonLabel($("#pause-button"), settings.auto_download || settings.auto_local || settings.watch_enabled || settings.chat_auto_save || settings.chat_events ? settings.paused ? "Продолжить автоматизацию" : "Приостановить автоматизацию" : "Настроить автоматизацию", settings.auto_download || settings.auto_local || settings.watch_enabled || settings.chat_auto_save || settings.chat_events ? settings.paused ? "play" : "pause" : "settings");
    const timing = data.automation_timing || {};
    $("#automation-timing-status").textContent = !isConnected ? "Для фоновой проверки подключите Bitrix24." : timing.catalogue_running ? "Сейчас проверяем каталог совещаний." : `${timing.last_scan ? `Последняя успешная проверка: ${dateString(timing.last_scan)}. ` : ""}Обычный интервал — минута после завершения предыдущей проверки; при ошибках ожидание увеличивается.`;
    $("#metric-download").textContent = settings.auto_download ? (settings.paused ? "На паузе" : "Включена") : "Выключена";
    $("#metric-download-sub").textContent = settings.auto_download ? (settings.auto_since ? `Новые совещания с ${dateString(settings.auto_since)}` : "Для следующих совещаний") : "Записи выбираете вы";
    $("#metric-local").textContent = settings.auto_local ? (settings.paused ? "На паузе" : "Автоматически") : "Вручную";
    $("#metric-local-sub").textContent = settings.auto_local ? "Для новых сохранённых записей" : "По кнопке в карточке совещания";
    $$("[data-schedule]").forEach(editor => {
      const window = data.automation_windows?.[editor.dataset.schedule];
      $("[data-schedule-status]", editor).textContent = (window?.label || "По времени этого компьютера. Расписание ограничивает запуск новых задач.") + (editor.dataset.schedule === "chat_attachment" ? " Сохранение текста продолжается вне окна скачивания." : "");
    });
    const count = data.catalogue?.total ?? state.total;
    $("#metric-total").textContent = Number(count).toLocaleString("ru-RU"); $("#nav-total").textContent = String(count);
    $("#catalogue-progress").hidden = !data.catalogue?.running;
    $("#catalogue-progress-text").textContent = `Получено ${Number(data.catalogue?.count || 0).toLocaleString("ru-RU")} записей в текущей проверке. Загрузка страниц продолжается…`;
    const errors = [];
    if (data.auth_error) errors.push(`Подключение Bitrix24 требует проверки: ${data.auth_error}`);
    if (data.catalogue?.error) errors.push(`Не удалось обновить каталог: ${data.catalogue.error}`);
    if (data.watch_error) errors.push(`Импорт из наблюдаемой папки: ${data.watch_error}`);
    showError(errors.join("\n"));
    renderSecrets(); updateDiarization();
    const automatic = data.local_automation;
    $("#local-automation-status").textContent = automatic ? `${automatic.reason}${automatic.pending ? `. Ожидают запуска: ${automatic.pending}.` : ""}` : "";
    $("#queue-automation-status").textContent = automatic?.pending ? `Авторасшифровка: ${automatic.reason}. Ожидают запуска: ${automatic.pending}.` : "";
    $("#queue-automation-status").hidden = !automatic?.pending;
    if (!state.authRendered) { authTab(settings.auth_mode === "webhook" ? "webhook" : "oauth"); state.authRendered = true; }
    if (data.hardware) { state.hardware = data.hardware; renderHardware(); }
    renderJobs(data.jobs || []); renderChatJobs(data.chat_queue); decorateJobIcons(); renderModule(data.module || {}); renderSelection(); renderTranscriptionControls(); renderModelTest();
    if (data.hf_verified && !$("#hf-token").value && $("#hf-result").textContent === "Токен сохраняется только в защищённом хранилище Windows.") $("#hf-result").textContent = "Доступ к файлам обеих моделей ранее проверен. Токен хранится в защищённом хранилище Windows.";
    $("#transcription-profile").textContent = `${settings.engine === "gigaam" ? "GigaAM v3 RNNT" : settings.engine === "parakeet" ? "Parakeet v3 Q8" : `Whisper ${settings.model || "large-v3"}`} · ${settings.device === "cpu" ? "CPU (выбран явно)" : "GPU NVIDIA / CUDA"} · ${settings.engine === "gigaam" ? "русский профиль" : settings.language === "auto" ? "определение языка" : settings.language === "en" ? "английский" : "русский"}${settings.diarization ? " · диаризация включена" : " · диаризация выключена"}`;
  }
  function renderSelection() {
    const count = state.selected.size;
    $("#selection-label").textContent = count ? `Выбрано: ${count}` : "Ничего не выбрано";
    $("#download-selected").disabled = count === 0 || !connected();
    $("#delete-selected").disabled = count === 0;
    const selectAll = $("#select-page");
    const selectedCount = state.items.filter(item => state.selected.has(item.id)).length;
    selectAll.checked = Boolean(state.items.length && selectedCount === state.items.length);
    selectAll.indeterminate = selectedCount > 0 && selectedCount < state.items.length;
    selectAll.disabled = state.items.length === 0;
  }
  function meetingPeople(item) {
    const people = item.participants || [];
    if (!people.length) return '<span class="meeting-subtitle">Участники не указаны</span>';
    const expanded = state.expandedPeople.has(item.id);
    return `<div class="meeting-people ${expanded ? "expanded" : ""}" data-people="${item.id}"><div class="meeting-people-list" id="meeting-people-${item.id}">${people.map(person => `<span class="participant-chip" title="${escapeHtml(person.label)}"><span>${escapeHtml(person.label)}</span></span>`).join("")}</div><button type="button" class="meeting-people-toggle" data-people-toggle="${item.id}" aria-controls="meeting-people-${item.id}" aria-expanded="${expanded}" aria-label="${expanded ? "Свернуть" : "Показать всех"} участников (${people.length})" hidden>${icon("chevron-down")}</button></div>`;
  }
  function updatePeopleOverflow() {
    $$(".meeting-people").forEach(group => {
      const list = $(".meeting-people-list", group);
      const overflow = list.scrollHeight > 35;
      group.classList.toggle("has-overflow", overflow);
      $(".meeting-people-toggle", group).hidden = !overflow;
    });
  }
  const peopleResizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(updatePeopleOverflow) : null;
  window.addEventListener("resize", updatePeopleOverflow);
  function selectMeeting(id, checked, shiftKey) {
    const current = state.items.findIndex(item => item.id === id);
    const anchor = state.items.findIndex(item => item.id === state.selectionAnchor);
    if (current < 0) return;
    const range = shiftKey && anchor >= 0 ? state.items.slice(Math.min(anchor, current), Math.max(anchor, current) + 1) : [state.items[current]];
    for (const item of range) {
      if (checked) state.selected.add(item.id); else state.selected.delete(item.id);
    }
    if (!shiftKey || anchor < 0) state.selectionAnchor = id;
    $$("#meeting-list input[data-select]").forEach(input => { input.checked = state.selected.has(Number(input.dataset.select)); });
    renderSelection();
  }
  async function loadMeetings() {
    const request = ++state.listRequest;
    const params = new URLSearchParams(new FormData($("#filter-form")));
    [...params].forEach(([key, value]) => { if (!value) params.delete(key); });
    params.set("offset", state.offset); params.set("limit", state.limit);
    if (state.participantIds.size) params.set("participants", [...state.participantIds].join(","));
    if (state.chatIds.size) params.set("chats", [...state.chatIds].join(","));
    const result = await api(`/api/meetings?${params}`);
    if (request !== state.listRequest) return;
    const selectionList = `${params}:${(result.items || []).map(item => item.id).join(",")}`;
    if (selectionList !== state.selectionList) state.selectionAnchor = null;
    state.selectionList = selectionList;
    state.items = (result.items || []).map(normalizeMeeting); state.total = Number(result.total) || 0; state.lastListAt = Date.now();
    $("#meeting-list").innerHTML = state.items.map(item => `<tr><td class="check-cell"><input type="checkbox" data-select="${item.id}" aria-label="Выбрать ${escapeHtml(item.title)}" ${state.selected.has(item.id) ? "checked" : ""}></td><td><button class="meeting-title" data-meeting="${item.id}" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</button>${meetingPeople(item)}<span class="meeting-subtitle" title="${escapeHtml(item.chat_id ? `ID беседы ${item.chat_id}` : "")}">${escapeHtml(item.chat_title || (item.chat_id ? "Название беседы недоступно" : "Без беседы"))}</span><span class="meeting-subtitle">${escapeHtml(dateString(item.startDate))}${item.source === "import" ? " · Импорт" : ""} · ID ${item.id}</span></td><td>${escapeHtml(durationString(item.durationSeconds))}</td><td>${badge(item.audio)}</td><td>${badge(item.bitrix)}</td><td>${badge(item.local)}</td><td><button class="row-open" data-meeting="${item.id}" aria-label="Открыть ${escapeHtml(item.title)}">${icon("chevron-right")}</button></td></tr>`).join("");
    peopleResizeObserver?.disconnect();
    $$(".meeting-people").forEach(group => peopleResizeObserver?.observe(group));
    updatePeopleOverflow();
    const filtered = [...params.keys()].some(key => !["offset", "limit"].includes(key));
    $("#archive-empty").hidden = state.items.length > 0;
    $("#archive-empty h2").textContent = filtered ? "Нет совещаний с такими параметрами" : "Здесь будет история совещаний";
    $("#archive-empty p").textContent = filtered ? "Измените фильтры или сбросьте их, чтобы вернуться ко всей истории." : "Подключите Bitrix24 или импортируйте запись с компьютера. История Follow-up может не включать звонки без включённого Follow-up.";
    $("#results-label").textContent = `Найдено совещаний: ${state.total.toLocaleString("ru-RU")}`;
    $("#pagination-label").textContent = state.total ? `${state.offset + 1}–${Math.min(state.offset + state.limit, state.total)} из ${state.total}` : "0 записей";
    $("#page-number").textContent = String(Math.floor(state.offset / state.limit) + 1);
    $("#previous-page").disabled = state.offset === 0; $("#next-page").disabled = state.offset + state.limit >= state.total;
    if (!state.bootstrap?.catalogue) { $("#metric-total").textContent = String(state.total); $("#nav-total").textContent = String(state.total); }
    renderSelection();
  }
  function renderText(selector, value, emptyMessage) {
    const box = $(selector); const text = textValue(value);
    const output = text || emptyMessage;
    if (box.textContent !== output) box.textContent = output;
    box.classList.toggle("empty", !text);
  }
  function renderPlayer() {
    const detail = state.detail; if (!detail) return;
    const files = (detail.files || []).filter(file => file.kind === "audio" || /^audio[\\/]/.test(file.name));
    const selected = $("#audio-file-select").value;
    const file = files.find(item => item.name === selected) || files[0];
    const player = $("#media-player"); $("#no-audio").hidden = Boolean(file);
    if (!file) { player.replaceChildren(); return; }
    const url = fileUrl(file.url); if (!url) { player.replaceChildren(); $("#no-audio").hidden = false; return; }
    const old = $("audio,video", player);
    if (old?.getAttribute("src") === url) return;
    const video = /\.(mp4|mkv|webm|avi|mov)(?:$|\?)/i.test(file.name);
    const media = document.createElement(video ? "video" : "audio"); media.controls = false; media.preload = "metadata"; media.src = url;
    media.setAttribute("aria-label", `Исходная запись ${file.name}`); const controls = document.createElement("div"); controls.className = "media-controls";
    controls.innerHTML = `<button class="button secondary media-play" type="button" aria-label="Воспроизвести">${icon("play")}</button><span class="media-time">0:00 / 0:00</span><input class="media-seek" type="range" min="0" max="1" step="0.1" value="0" autocomplete="off" aria-label="Позиция воспроизведения" disabled><button class="button quiet media-mute" type="button" aria-label="Отключить звук">${icon("volume")}</button><input class="media-volume" type="range" min="0" max="1" step="0.05" value="1" aria-label="Громкость"><select class="media-rate" aria-label="Скорость воспроизведения"><option value="0.75">0,75×</option><option value="1" selected>1×</option><option value="1.25">1,25×</option><option value="1.5">1,5×</option><option value="2">2×</option></select>`;
    bindMediaControls(media, controls);
    player.replaceChildren(media, controls);
  }
  function bindMediaControls(media, controls) {
    const play = $(".media-play", controls), seek = $(".media-seek", controls), time = $(".media-time", controls), mute = $(".media-mute", controls);
    const stamp = value => {value = Number.isFinite(value) ? Math.floor(value) : 0; return `${Math.floor(value / 60)}:${String(value % 60).padStart(2,"0")}`;};
    let dragging = false, pending = null, probing = false, probeAttempted = false;
    const length = () => {if (Number.isFinite(media.duration) && media.duration > 0) return media.duration; const r = media.seekable; const end = r?.length ? r.end(r.length - 1) : 0; return Number.isFinite(end) && end > 0 ? end : 0;};
    const update = () => {
      const duration = length(); seek.max = duration || 1; seek.disabled = !duration;
      const position = probing ? 0 : pending ?? media.currentTime;
      if (!dragging) seek.value = Math.max(0, Math.min(duration, position || 0));
      time.textContent = `${stamp(dragging ? Number(seek.value) : position)} / ${duration ? stamp(duration) : "…"}`;
      play.innerHTML = icon(media.paused ? "play" : "pause"); play.setAttribute("aria-label", media.paused ? "Воспроизвести" : "Приостановить");
    };
    const resolveDuration = () => {
      if (probing && length()) { probing = false; media.currentTime = 0; }
      if (!probeAttempted && media.duration === Infinity) {
        probeAttempted = true; probing = true;
        // A downloaded streaming WebM has no Duration field. Seek to its end
        // once so the decoder resolves the real length, then return to zero.
        try { media.currentTime = Number.MAX_SAFE_INTEGER; } catch { probing = false; }
      }
      update();
    };
    play.onclick = () => {if (media.paused) media.play().catch(() => notify("Браузер не смог воспроизвести файл. Откройте его из папки совещания.", true)); else media.pause();};
    seek.onpointerdown = () => { dragging = true; };
    seek.onpointerup = () => { dragging = false; update(); };
    seek.oninput = () => {pending = Math.max(0, Math.min(length(), Number(seek.value))); media.currentTime = pending; update();};
    seek.onchange = () => {dragging = false; pending = Number(seek.value); media.currentTime = pending; update();};
    seek.onpointercancel = () => { dragging = false; pending = null; update(); };
    $(".media-volume", controls).oninput = event => {media.volume = Number(event.target.value);};
    mute.onclick = () => {media.muted = !media.muted; mute.setAttribute("aria-pressed", String(media.muted)); mute.setAttribute("aria-label", media.muted ? "Включить звук" : "Отключить звук"); mute.style.opacity = media.muted ? ".5" : "1";};
    $(".media-rate", controls).onchange = event => {media.playbackRate = Number(event.target.value);};
    for (const event of ["loadedmetadata", "durationchange", "progress"]) media.addEventListener(event, resolveDuration);
    media.addEventListener("seeked", () => { if (probing) {probing = false; media.currentTime = 0;} else if (!dragging) pending = null; update(); });
    for (const event of ["timeupdate", "play", "pause", "ended"]) media.addEventListener(event, update);
    update();
  }
  function renderTranscriptionControls() {
    const ready = Boolean(state.bootstrap?.transcription?.ready ?? state.detail?.transcription?.ready);
    const audio = (state.detail?.files || []).filter(file => file.kind === "audio" || /^audio[\\/]/.test(file.name));
    setButtonLabel($("#transcribe-button"), ready ? "Расшифровать локально" : "Настроить расшифровку", ready ? "play" : "settings");
    $("#transcribe-button").disabled = ready && !audio.length;
    $("#transcribe-mode").closest("label").hidden = audio.length < 2;
    $("#transcription-files").hidden = audio.length < 2;
    $("#transcription-readiness").textContent = ready ? (audio.length ? "Выбранная модель готова." : "Скачайте запись или добавьте файл с компьютера.") : state.bootstrap?.transcription?.reason || "Выберите и установите модель в профиле расшифровки.";
    if (state.bootstrap?.settings?.auto_local) {
      $("#transcription-readiness").textContent += state.bootstrap.settings.paused ? " Автоматическая расшифровка на паузе." : " Новые скачанные записи обрабатываются автоматически по расписанию.";
    }
  }
  function renderLocalRun() {
    const detail = state.detail; if (!detail) return;
    const value = $("#local-run-select").value;
    const runs = detail.runs || [];
    const selected = runs.find((run, index) => String(run.id ?? run.name ?? index) === value);
    const text = selected?.text || selected?.transcript || detail.local_text;
    renderText("#local-text", text, detail.local_error && detail.meeting?.local === "error" ? `Последний запуск завершился с ошибкой: ${detail.local_error}. После исправления повторите расшифровку.` : "Локальной расшифровки ещё нет. Управление записью доступно в блоке «Локальная обработка».");
  }
  function renderDetail(detail) {
    const meeting = normalizeMeeting(detail.meeting || {}); detail.meeting = meeting; state.detail = detail;
    $("#detail-title").textContent = meeting.title; $("#detail-source").textContent = meeting.source === "import" ? "Импорт с компьютера" : meeting.portal || "Bitrix24";
    $("#detail-meta").textContent = `${dateString(meeting.startDate)} · ${durationString(meeting.durationSeconds)} · ${meeting.chat_title || (meeting.chat_id ? `Чат ID ${meeting.chat_id}` : "Без чата")} · ID ${meeting.id}${meeting.metadata.linkedTo ? ` · привязан к ID ${meeting.metadata.linkedTo}` : ""}`;
    $("#detail-statuses").innerHTML = `<div>Аудио ${badge(meeting.audio)}</div><div>Текст Follow-up ${badge(meeting.bitrix)}</div><div>Локальный текст ${badge(meeting.local)}</div>`;
    $("#detail-download").hidden = meeting.source === "import"; $("#detail-download").disabled = !connected(); $("#detail-folder").disabled = !meeting.folder;
    $("#import-link-panel").hidden = meeting.source !== "import";
    const linkPicker = $("#link-meeting-picker");
    if (linkPicker.dataset.owner !== String(meeting.id)) {linkPicker.dataset.owner = String(meeting.id); setMeetingTarget(linkPicker, null); closeMeetingPicker(linkPicker);}
    const files = detail.files || []; const audio = files.filter(file => file.kind === "audio" || /^audio[\\/]/.test(file.name));
    const audioSelect = $("#audio-file-select"); const oldAudio = audioSelect.value;
    audioSelect.innerHTML = audio.map(file => `<option value="${escapeHtml(file.name)}">${escapeHtml(file.name.replace(/^audio[\\/]/, ""))}</option>`).join("");
    if (audio.some(file => file.name === oldAudio)) audioSelect.value = oldAudio;
    audioSelect.disabled = !audio.length; renderPlayer();
    $("#download-audio").hidden = Boolean(audio.length) || meeting.source === "import";
    $("#download-audio").disabled = !connected();
    $("#no-audio").textContent = meeting.audio === "not_available" ? "Bitrix24 не предоставил запись. Добавьте готовый файл с компьютера." : "Аудио ещё не сохранено. Скачайте доступную запись или добавьте файл с компьютера.";
    const selectedFiles = new Set($$("#transcription-files input:checked").map(input => input.value));
    const previousFiles = new Set($$("#transcription-files input").map(input => input.value));
    $("#transcription-files").innerHTML = audio.map(file => `<label class="check-label"><input type="checkbox" value="${escapeHtml(file.name)}" ${!previousFiles.has(file.name) || selectedFiles.has(file.name) ? "checked" : ""}>${escapeHtml(file.name.replace(/^audio[\\/]/, ""))}</label>`).join("");
    renderTranscriptionControls();
    renderText("#bitrix-text", detail.bitrix_text, meeting.source === "import" ? "Это запись с компьютера. Запустите локальную расшифровку или привяжите запись к совещанию Bitrix24." : meeting.bitrix === "short_call" ? "Звонок короче минуты. Текст Bitrix24 не предоставлен; Follow-up обрабатывает звонки от одной минуты. Можно скачать запись и расшифровать локально." : meeting.bitrix === "available" ? "Текст готов в Bitrix24. Нажмите «Скачать материалы», чтобы сохранить его в архив." : ["pending", "waiting"].includes(meeting.bitrix) ? "Bitrix24 пока не предоставил готовый текст. После его появления архив сможет его сохранить." : "Доступность текста ещё не проверена. Обновите каталог.");
    updateDownloadButtons();
    $("#bitrix-text-state").textContent = textValue(detail.bitrix_text) ? "Сохранён" : labels[meeting.bitrix] || "Нет текста";
    $("#bitrix-text-state").className = `badge ${statusClass(meeting.bitrix)}`;
    $("#local-text-state").textContent = textValue(detail.local_text) ? "Сохранён" : labels[meeting.local] || "Нет текста";
    $("#local-text-state").className = `badge ${statusClass(meeting.local)}`;
    const runSelect = $("#local-run-select"); const oldRun = runSelect.value;
    runSelect.innerHTML = (detail.runs || []).map((run, index) => `<option value="${escapeHtml(run.id ?? run.name ?? index)}">${escapeHtml(run.label || run.name || run.id || `Запуск ${index + 1}`)}${run.model ? ` · ${escapeHtml(run.model)}` : ""}${run.sample ? " · короткий тест" : ""}${run.created ? ` · ${escapeHtml(dateString(run.created))}` : ""}</option>`).join("");
    if ($$("option", runSelect).some(option => option.value === oldRun)) runSelect.value = oldRun;
    runSelect.closest("label").hidden = !(detail.runs || []).length; runSelect.disabled = !(detail.runs || []).length; renderLocalRun();
    $("#detail-files").innerHTML = files.length ? files.map(file => {
      const url = fileUrl(file.url); return `<div class="file-item">${url ? `<a href="${escapeHtml(url)}" download>${escapeHtml(file.name)}</a>` : `<span>${escapeHtml(file.name)}</span>`}<small>${escapeHtml(file.kind || "Файл")}</small></div>`;
    }).join("") : '<p class="muted">Материалы пока не сохранены.</p>';
  }
  async function loadDetail() {
    if (!state.meetingId) return;
    const id = state.meetingId; const request = ++state.detailRequest;
    const detail = await api(`/api/meeting/${id}`);
    if (request !== state.detailRequest || id !== state.meetingId) return;
    renderDetail(detail); state.lastDetailAt = Date.now();
  }
  function queueGroups(jobs, namespace, overview=[]) {
    const groups = new Map(), totals = new Map(overview.map(group => [group.key, group]));
    for (const job of jobs) {
      const key = namespace === "chat" ? job.group_key || (job.chat ? `chat:${job.chat}` : "catalogue") : job.meeting_id ? `meeting:${job.meeting_id}` : job.kind === "install" ? "models" : "catalogue";
      if (!groups.has(key)) groups.set(key, {key, title: job.title || (job.meeting_id ? `Совещание ${job.meeting_id}` : "Модели и каталог"),
        meta: namespace === "chat" ? job.chat ? `${job.group === "tasks" ? `Чат задачи${job.task_id ? " №"+job.task_id : ""}` : "Чат"} · ID ${job.chat}` : "Обнаружение доступных чатов" : job.meeting_id ? `Совещание №${job.meeting_id}` : "Обслуживание приложения",
        items: [], pending: 0, running: 0, failed: 0});
      const group = groups.get(key); group.items.push(job);
      group.pending += Number(["queued", "running", "waiting"].includes(job.state) || Boolean(job.will_retry));
      group.running += Number(job.state === "running"); group.failed += Number(job.state === "failed");
    }
    for (const group of groups.values()) {
      group.items.sort((a,b) => Number(b.state === "running")-Number(a.state === "running") || Number(["queued","waiting","failed"].includes(b.state))-Number(["queued","waiting","failed"].includes(a.state)));
      const summary = totals.get(group.key);
      group.total = summary?.total ?? group.items.length;
      if (summary) for (const key of ["pending","running","failed"]) group[key] = summary[key];
    }
    return [...groups.values()].sort((a,b) => b.running-a.running || Number(b.pending>0)-Number(a.pending>0) || Number(b.failed>0)-Number(a.failed>0));
  }
  function renderQueueGroups(container, jobs, namespace, row, overview=[]) {
    state.queueOpen ||= new Map();
    $$("details.queue-group",container).forEach(group => state.queueOpen.set(group.dataset.queueGroup,group.open));
    const focused = container.contains(document.activeElement) ? document.activeElement : null, focusAttribute = focused && [...focused.attributes].find(attr => /^(data-job-(cancel|retry|now)|data-chat-job-action)$/.test(attr.name));
    const focusChat = focused?.dataset.chatJob;
    container.innerHTML = queueGroups(jobs,namespace,overview).map(group => {
      const key = `${namespace}:${group.key}`, open = state.queueOpen.get(key) ?? Boolean(group.pending || group.failed);
      const status = group.running ? "running" : group.failed ? "failed" : group.pending ? "queued" : "done";
      const counters = [`Работ: ${group.total}`,group.pending ? `ожидают или выполняются: ${group.pending}` : "",group.failed ? `ошибок: ${group.failed}` : "",group.items.length<group.total ? `показано: ${group.items.length}` : ""].filter(Boolean).join(" · ");
      return `<details class="queue-group" data-queue-group="${escapeHtml(key)}"${open ? " open" : ""}><summary><span class="queue-group-icon">${icon(namespace === "chat" ? group.key === "catalogue" ? "refresh" : "chat" : group.key === "models" ? "settings" : "calendar")}</span><span class="queue-group-heading"><strong>${escapeHtml(group.title)}</strong><span>${escapeHtml(group.meta)} · ${escapeHtml(counters)}</span></span>${badge(status)}${icon("chevron-down")}</summary><div class="queue-group-items">${group.items.map(row).join("")}</div></details>`;
    }).join("");
    if (focusAttribute && container.contains(focused) === false) {
      const candidate = $$(`[${focusAttribute.name}]`,container).find(button => button.getAttribute(focusAttribute.name)===focusAttribute.value && (!focusChat || button.dataset.chatJob===focusChat));
      candidate?.focus({preventScroll:true});
    }
  }
  function renderChatBackground(sync) {
    const element = $("#queue-chat-status"); if (!element || !sync) return;
    const title = !sync.connected ? "Чаты: требуется подключение Bitrix24" : !sync.worker_running ? "Фоновый обработчик чатов не запущен" : sync.active_chat ? "Чаты сохраняются в фоне" : sync.discovering ? "В фоне обновляется каталог чатов" : "Фоновый обработчик чатов работает";
    const detail = !sync.connected || !sync.worker_running ? "" : sync.paused ? "Автоматизация на паузе. Ручные загрузки продолжаются." : sync.automatic ? `${sync.discovering ? "Каталог обновляется" : sync.next_catalogue_at>Date.now()/1000 ? "Следующая проверка каталога: "+escapeHtml(dateString(new Date(sync.next_catalogue_at*1000).toISOString())) : "Проверка каталога ожидает свободного прохода"}` : "Автосохранение выключено. Выбранные вручную чаты продолжают загружаться.";
    const last = sync.last_progress?.at ? `Последнее сохранение: ${escapeHtml(dateString(new Date(sync.last_progress.at*1000).toISOString()))}${sync.last_progress.chat ? " · чат ID "+sync.last_progress.chat : ""}` : "Сохранений сообщений или файлов ещё не было.";
    element.innerHTML = `${icon(sync.worker_running ? "refresh" : "info")}<div><strong>${title}</strong><p>${detail}${detail ? " · " : ""}${last}</p>${sync.error ? `<p class="job-error">${escapeHtml(sync.error)}</p>` : ""}<p>Закрытие окна оставляет приложение в трее. «Выход» и сон компьютера останавливают работу.</p></div>`;
  }
  function renderJobs(jobs) {
    if (!Array.isArray(jobs)) jobs = jobs.items || [];
    const active = jobs.filter(job => ["queued", "running"].includes(job.state));
    $("#nav-jobs").textContent = String(active.length); $("#nav-jobs").hidden = !active.length;
    $("#jobs-summary").textContent = active.length ? `Активных задач: ${active.length}` : "Нет активных задач записей";
    if (jobs.length) renderQueueGroups($("#jobs-list"),jobs,"meeting",job => `<article class="job-item" data-job-id="${job.id}"><div><div class="job-title">${escapeHtml(kinds[job.kind] || job.kind)} ${job.schedule_wait ? '<span class="badge waiting">Ждёт окна</span>' : badge(job.state)}</div><div class="job-meta">Задача ${job.id}${job.meeting_id ? ` · Совещание ${job.meeting_id}` : ""} · ${escapeHtml(dateString(job.created))} · Попыток: ${Number(job.attempts) || 0}</div><div class="job-message">${escapeHtml(job.message || "")}</div>${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}${job.state === "running" ? `<progress class="job-progress" max="1" ${Number(job.progress) > 0 ? `value="${Math.min(1, Number(job.progress))}"` : ""} aria-label="Прогресс задачи ${job.id}"></progress>` : ""}</div><div class="job-actions">${job.schedule_wait ? `<button class="button primary small" data-job-now="${job.id}">Запустить сейчас</button>` : ""}${job.meeting_id ? `<button class="button quiet small" data-meeting="${job.meeting_id}">Открыть</button>` : ""}${["queued", "running"].includes(job.state) ? `<button class="button secondary small" data-job-cancel="${job.id}">Отменить</button>` : ""}${["failed", "cancelled", "canceled"].includes(job.state) ? `<button class="button secondary small" data-job-retry="${job.id}">Повторить</button>` : ""}</div></article>`);
    else $("#jobs-list").innerHTML = '<div class="empty-state"><h2>Заданий записей пока нет</h2><p>Здесь появятся загрузка записей, импорт, расшифровка и установка моделей.</p></div>';
  }
  function decorateJobIcons() {
    $$('[data-job-now]').forEach(button => decorateButton(button, 'play'));
    $$('[data-job-cancel]').forEach(button => decorateButton(button, 'x'));
    $$('[data-job-retry]').forEach(button => decorateButton(button, 'refresh'));
  }
  function renderChatJobs(data, append=false, replace=false) {
    if (!data || !$("#chat-jobs-list")) return;
    if(state.chatQueueAccount && state.chatQueueAccount!==data.account) { state.chatQueueItems=[]; for(const key of state.queueOpen?.keys() || []) if(key.startsWith("chat:")) state.queueOpen.delete(key); }
    state.chatQueueAccount=data.account;
    const previous=state.chatQueueItems||[], keep=!replace && state.route==="jobs" && previous.length>100;
    state.chatQueueItems=append ? [...previous,...data.items.filter(item=>!previous.some(old=>old.id===item.id))] : keep ? [...data.items,...previous.filter(old=>!data.items.some(item=>item.id===old.id))] : data.items;
    state.chatQueueTotal=data.total;
    const meetingActive=(state.bootstrap?.jobs||[]).filter(job=>["queued","running","waiting"].includes(job.state)).length;
    $("#nav-jobs").textContent=String(meetingActive+data.pending); $("#nav-jobs").hidden=meetingActive+data.pending===0;
    $("#chat-jobs-summary").textContent=`Ожидают или выполняются: ${data.pending}${data.failed ? " · Ошибок: "+data.failed : ""} · показано работ ${state.chatQueueItems.length} из ${data.total}`;
    const cancelBacklog = $("#chat-jobs-cancel-backlog");
    if (cancelBacklog) cancelBacklog.hidden = !data.items.some(job => job.kind === "file" || job.kind === "history" || job.kind.startsWith("period:"));
    if (state.chatQueueItems.length) renderQueueGroups($("#chat-jobs-list"),state.chatQueueItems,"chat",job => `<article class="job-item" data-chat-job-id="${job.id}"><div><div class="job-title">${icon(job.kind==="file" ? "file" : "chat")} ${escapeHtml(job.label)} ${badge(job.schedule_wait ? "waiting" : job.state)}</div><div class="job-meta">${escapeHtml(job.title)}${job.chat ? " · Чат ID "+job.chat : ""}${job.filename ? " · "+escapeHtml(job.filename) : ""}${job.automatic === true ? " · Автоматически" : job.automatic === false && job.kind!=="metadata" ? " · По вашему выбору" : ""}${job.touched ? " · "+escapeHtml(dateString(new Date(job.touched*1000).toISOString())) : ""}</div><div class="job-message">${escapeHtml(job.message||"")}${job.pages ? ` · Страниц: ${job.pages}` : ""}${job.messages ? ` · Обработано сообщений: ${job.messages}` : ""}${job.next_at>Date.now()/1000 ? ` · Повтор не раньше ${escapeHtml(dateString(new Date(job.next_at*1000).toISOString()))}` : ""}</div>${job.error ? `<div class="job-error">${escapeHtml(job.error)}</div>` : ""}${job.state==="running" ? `<progress class="job-progress" max="1"${job.total_bytes && job.downloaded_bytes ? ` value="${Math.min(1,job.downloaded_bytes/job.total_bytes)}"` : ""} aria-label="Выполнение задания чата"></progress>` : ""}</div><div class="job-actions">${job.state==="failed" || job.state==="cancelled" ? `<button class="button secondary small" data-chat-job-action="retry" data-chat-job="${job.id}">Повторить</button>` : ""}${job.state==="queued" || job.state==="failed" ? `<button class="button quiet small" data-chat-job-action="cancel" data-chat-job="${job.id}">Отменить</button>` : ""}</div></article>`, data.groups || []);
    else $("#chat-jobs-list").innerHTML = '<div class="empty-state"><p>Заданий чатов пока нет. Здесь появятся сохранение сообщений, история и вложения.</p></div>';
    $("#chat-jobs-more").hidden=state.chatQueueItems.length>=data.total;
    renderChatBackground(data.sync);
  }
  function renderModule(module) {
    const jobs = Array.isArray(state.bootstrap?.jobs) ? state.bootstrap.jobs : state.bootstrap?.jobs?.items || [];
    const installJob = jobs.find(job => job.kind === "install" && ["running", "queued"].includes(job.state));
    const installing = module.installing || Boolean(installJob);
    $("#module-title").textContent = installing ? "Установка моделей" : "Доступные модели";
    $("#module-state").innerHTML = `${installing ? badge("running") : ""}${module.size_gb ? `<span class="muted"> · Занимает ${Number(module.size_gb).toLocaleString("ru-RU")} ГБ</span>` : ""}${installJob ? `<p class="footnote">${escapeHtml(installJob.message || "Установка в очереди")}</p><progress class="job-progress" max="1" ${Number(installJob.progress) > 0 ? `value="${Math.min(1, Number(installJob.progress))}"` : ""} aria-label="Прогресс установки"></progress>` : ""}`;
    $("#module-install").disabled = installing || !selectedPackages().length || !state.installPlan || state.installPlan.runtime?.available === false;
    setButtonLabel($("#module-install"), needsProcessingSupport() && !$$("#install-packages input:checked:not(:disabled)").length ? "Установить поддержку обработки звука" : "Установить выбранные модели", "download");
    $("#module-cancel").hidden = !installing;
    $("#module-remove").hidden = installing || (!module.installed && !module.size_gb);
  }
  function renderEstimate() {
    state.installPlan = null;
    clearTimeout(state.estimateTimer);
    const packages = selectedPackages(); const request = ++state.installPlanRequest;
    setButtonLabel($("#module-install"), needsProcessingSupport() && !$$("#install-packages input:checked:not(:disabled)").length ? "Установить поддержку обработки звука" : "Установить выбранные модели", "download");
    $("#module-install").disabled = true;
    if (!packages.length) { $("#module-estimate").textContent = "Готовые модели можно выбрать в профиле справа. Для установки отметьте дополнительные."; return; }
    $("#module-estimate").textContent = "Рассчитываем общую загрузку и свободное место с учётом сохранённых ресурсов…";
    state.estimateTimer = setTimeout(async () => {
      try {
        const plan = await api("/api/module/install-plan", { method: "POST", data: { profile: $("#install-profile").value, packages } });
        if (request !== state.installPlanRequest) return;
        state.installPlan = plan; showInstallPlan(plan);
      } catch (error) { if (request === state.installPlanRequest) $("#module-estimate").textContent = error.message; }
    }, 250);
  }
  const gbText = value => `${Number(value || 0).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ГБ`;
  function needsProcessingSupport() {
    const s = state.bootstrap?.settings;
    return s && (s.diarization || s.noise_reduction || s.normalize) && (state.processingSupport?.requires_install ?? state.featuresReady === false);
  }
  function selectedPackages() {
    const packages = $$("#install-packages input:checked:not(:disabled)").map(input => ({ engine: input.dataset.engine, model: input.value }));
    if (!packages.length && needsProcessingSupport()) {
      const s = state.bootstrap.settings;
      packages.push({ engine: s.engine, model: s.engine === "whisper" ? s.model : s[s.engine + "_model"] });
    }
    return packages;
  }
  function showInstallPlan(plan) {
    $("#module-install").disabled = Boolean(state.bootstrap?.module?.installing) || plan.runtime?.available === false;
    if (plan.runtime?.available === false) { $("#module-estimate").textContent = plan.runtime.reason; return; }
    $("#module-estimate").innerHTML = `<strong>Оценка загрузки: ${escapeHtml(gbText(plan.download_gb))}</strong><span>Свободное место с запасом: ${escapeHtml(gbText(plan.required_gb))}</span><small>${escapeHtml(plan.common_ready ? "Общие библиотеки уже доступны; повторная установка не нужна." : plan.runtime?.description || "Общая среда будет подготовлена.")} ${escapeHtml(plan.note || "Модели учитываются отдельно.")}</small>`;
  }
  function renderHardware() {
    const hardware = state.hardware; if (!hardware) return;
    const gpu = hardware.gpu;
    $("#hardware-summary").innerHTML = `<div><span>Процессор</span><strong>${escapeHtml(hardware.cpu_name || `${hardware.cpu_cores || "—"} ядер · ${hardware.cpu_threads || "—"} потоков`)}</strong></div><div><span>Система</span><strong>${hardware.architecture === "arm64" ? "Windows ARM — локальная расшифровка пока не поддерживается" : escapeHtml(hardware.architecture || "x64")}</strong></div><div><span>Оперативная память</span><strong>${escapeHtml(hardware.ram_gb || "—")} ГБ${hardware.available_ram_gb ? ` · доступно ${escapeHtml(hardware.available_ram_gb)} ГБ` : ""}</strong></div><div><span>Видеокарта</span><strong>${escapeHtml(gpu?.name || hardware.gpu_adapters?.join(", ") || "Не обнаружена")}</strong></div><div><span>Память GPU</span><strong>${gpu ? `${escapeHtml(gpu.vram_gb)} ГБ · свободно ${escapeHtml(gpu.free_gb)} ГБ` : "—"}</strong></div><div><span>Проверка GPU</span><strong>${escapeHtml(hardware.cuda || "Не проверено")}</strong></div>`;
    $("#hardware-probe-result").textContent = hardware.probe ? `Последняя проверка: ${dateString(hardware.probe.checkedAt)}${hardware.probe.torch ? ` · PyTorch ${hardware.probe.torch}` : ""}.` : "Проверка выполняется в среде выбранного движка.";
    $("#hardware-probe").disabled = state.bootstrap?.settings?.device !== "cuda";
  }
  function renderModelTest() {
    const s = state.bootstrap?.settings || {}, model = s.engine === "whisper" ? s.model : s[s.engine + "_model"];
    let test = null; try { test = JSON.parse(state.bootstrap?.model_test || "null"); } catch {}
    const options = ["vad", "noise_reduction", "normalize", "diarization", "language", "min_speakers", "max_speakers"];
    const matches = test && test.engine === s.engine && test.model === model && test.device === s.device && options.every(key => test.settings?.[key] === s[key]);
    $("#model-test-profile").textContent = `${s.engine || "whisper"} · ${model || "—"} · ${s.device === "cuda" ? "GPU NVIDIA" : "CPU"}. Используется сохранённый профиль.`;
    $("#model-test-result").textContent = matches ? `Тест пройден: ${durationString(test.audio_seconds)} записи за ${Math.round(Number(test.seconds) || 0)} сек. ${dateString(test.testedAt)}. Проверьте точность полученного текста в карточке встречи.` : "Эта модель с текущими параметрами ещё не проверена на записи. Тест проверяет запуск и скорость; точность текста оценивайте по результату.";
    const active = state.bootstrap?.jobs?.some(job => ["transcribe", "install"].includes(job.kind) && ["queued", "running"].includes(job.state));
    $("#model-test-button").disabled = !state.bootstrap?.transcription?.ready || active || !$("#model-test-meeting").value;
  }
  async function loadTestMeetings() {
    const data = await api("/api/meetings?audio=saved&limit=200"), select = $("#model-test-meeting"), previous = select.value;
    const key = JSON.stringify(data.items.map(item => [item.id, item.title]));
    if (key !== state.testMeetings) {
      state.testMeetings = key;
      select.innerHTML = '<option value="">Выберите сохранённую запись</option>' + data.items.map(item => `<option value="${item.id}">${escapeHtml(item.title || `Совещание ${item.id}`)}</option>`).join("");
      if ([...select.options].some(option => option.value === previous)) select.value = previous;
    }
    state.lastTestMeetingsAt = Date.now(); renderModelTest();
  }
  function hardwareProfile() { const s = state.bootstrap?.settings || {}; return JSON.stringify([s.engine, s.device, s.engine === "whisper" ? s.model : s[s.engine + "_model"]]); }
  async function loadHardware() { const profile = hardwareProfile(); state.hardware = await api("/api/hardware"); state.hardwareProfile = profile; renderHardware(); }
  function updateEngineControls() {
    const form = $("#settings-form"); const engine = form.elements.engine.value || "whisper";
    $("#whisper-model-field").hidden = engine !== "whisper"; $("#parakeet-model-field").hidden = engine !== "parakeet"; $("#gigaam-model-field").hidden = engine !== "gigaam";
    const model = engine === "gigaam" ? form.elements.gigaam_model.value : engine === "parakeet" ? form.elements.parakeet_model.value : form.elements.model.value;
    const info = state.engines?.find(item => item.id === engine)?.models?.find(item => item.id === model);
    $("#engine-status").textContent = info ? `${info.label || model}: ${info.installed ? (info.origin === "computer" ? "найдена на компьютере и готова к использованию" : "готова к использованию") : "требует установки — отметьте её в каталоге моделей"}.` : "Выбирайте поддерживаемую модель. Доступность уточняется в каталоге установленных движков.";
    for (const e of state.engines || []) {
      const select = form.elements[e.id === "whisper" ? "model" : e.id + "_model"];
      for (const option of select.options) {
        const m = e.models.find(m => m.id === option.value);
        if (!m) continue;
        option.value = m.id;
        option.textContent = `${m.id} · ${m.installed ? "Готова" : "Требует установки"}`;
        option.disabled = !m.installed;
      }
    }
    const ruOnly = engine === "gigaam"; const enOnly = engine === "whisper" && model.endsWith(".en");
    if (ruOnly && form.elements.language.value === "en" || enOnly && form.elements.language.value === "ru") form.elements.language.value = "auto";
    [...form.elements.language.options].forEach(option => { option.disabled = ruOnly ? option.value === "en" : enOnly ? option.value === "ru" : false; });
    $("#language-hint").textContent = ruOnly ? "GigaAM распознаёт русский. «Автоматически» использует русский профиль, а не определение языка." : enOnly ? "Эта модель Whisper предназначена только для английского языка." : "По умолчанию язык определяется моделью.";
  }
  function renderInstallModels() {
    if (!state.engines) return;
    const previous = new Set(selectedPackages().map(item => `${item.engine}/${item.model}`));
    const expanded = new Set($$("#install-packages details[open]").map(item => item.dataset.engine));
    const first = !state.moduleRendered; state.moduleRendered = true;
    $("#install-packages").innerHTML = (state.engines || []).map(engine => `<details class="package-group" data-engine="${escapeHtml(engine.id)}" ${expanded.has(engine.id) || first && engine.id === "whisper" ? "open" : ""}><summary>${escapeHtml(engine.label)}<small>${engine.models.filter(model => model.installed).length} из ${engine.models.length} доступно</small></summary><div class="package-options">${engine.models.map(model => `<label class="package-option ${model.installed ? "model-ready" : "model-missing"}"><input type="checkbox" data-engine="${escapeHtml(engine.id)}" value="${escapeHtml(model.id)}" ${model.installed ? "checked disabled" : previous.has(`${engine.id}/${model.id}`) ? "checked" : ""}><span>${escapeHtml(model.label || model.id)}<small>${model.installed ? model.origin === "computer" ? "Готова · найдена на компьютере" : "Готова" : model.cached ? "Веса найдены · нужны библиотеки" : `Требует установки · ${escapeHtml(gbText(model.download_gb))}`}</small></span></label>`).join("")}</div></details>`).join("");
    renderEstimate();
  }
  async function loadEngines() {
    const result = await api("/api/module/engines"); const changed = JSON.stringify(state.engines) !== JSON.stringify(result.engines || []);
    const supportChanged = state.featuresReady !== result.features_ready || JSON.stringify(state.processingSupport) !== JSON.stringify(result.processing_support);
    state.engines = result.engines || []; state.featuresReady = result.features_ready; state.processingSupport = result.processing_support; state.lastEnginesAt = Date.now();
    if (changed) renderInstallModels(); else if (supportChanged) renderEstimate();
    updateEngineControls(); renderProcessingSupport(); renderModelRemoval();
  }
  function renderProcessingSupport() {
    const support = state.processingSupport, enabled = state.bootstrap?.settings;
    $("#processing-support").hidden = !support || !(enabled?.diarization || enabled?.noise_reduction || enabled?.normalize);
    $("#processing-support-text").textContent = support?.requires_install ? `${support.reason}. Нажмите «Подготовить поддержку». Сохранённые веса модели используются повторно.` : "Поддержка выбранных параметров готова.";
    $("#processing-prepare").hidden = !support?.requires_install;
    $("#processing-prepare").disabled = Boolean(state.bootstrap?.module?.installing);
  }
  function renderModelRemoval() {
    const previous = new Set($$("#removable-models input:checked").map(input => `${input.dataset.engine}/${input.value}`));
    const models = (state.engines || []).flatMap(engine => engine.models.filter(model => model.removable).map(model => ({...model, engine: engine.id})));
    $("#model-removal").hidden = !models.length;
    const html = models.map(model => `<label class="check-label"><input type="checkbox" data-engine="${escapeHtml(model.engine)}" value="${escapeHtml(model.id)}" ${previous.has(`${model.engine}/${model.id}`) ? "checked" : ""}>${escapeHtml(model.label || model.id)}</label>`).join("");
    if ($("#removable-models").innerHTML !== html) $("#removable-models").innerHTML = html;
    $("#models-delete").disabled = !$$("#removable-models input:checked").length || Boolean(state.bootstrap?.module?.installing);
  }
  const sizeText = bytes => {
    const value = Number(bytes || 0); if (!value) return "0 Б";
    if(value<1024) return `${value} Б`;
    if(value<1024**2) return `${(value/1024).toLocaleString("ru-RU",{maximumFractionDigits:1})} КБ`;
    return value < 1024 ** 3 ? `${(value / 1024 ** 2).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ` : `${(value / 1024 ** 3).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ГБ`;
  };
  const countText = (n,one,few,many) => `${n} ${n%10===1 && n%100!==11 ? one : n%10>=2 && n%10<=4 && !(n%100>=12 && n%100<=14) ? few : many}`;
  async function refreshMaterialPlan() {
    const selection = state.materialDelete; if (!selection) return;
    const targets = $$("#materials-choices input:checked").map(input => input.value), request = ++selection.request;
    $("#materials-delete").disabled = true; $("#materials-all").checked = targets.length === selection.choices.length;
    $("#materials-all").indeterminate = targets.length > 0 && targets.length < selection.choices.length;
    if (!targets.length) {selection.plan = null; $("#materials-summary").textContent = "Выберите материалы для удаления."; return;}
    $("#materials-summary").textContent = "Проверяем выбранные файлы…";
    try {
      const plan = await api(selection.endpoint+"/plan", {method:"POST", data:{ids:selection.ids, targets}});
      if (state.materialDelete !== selection || selection.request !== request) return;
      selection.plan = plan; $("#materials-summary").textContent = `${selection.type === "chat" ? countText(plan.chats,"чат","чата","чатов")+" · "+countText(plan.messages,"сообщение","сообщения","сообщений") : countText(plan.meetings,"совещание","совещания","совещаний")} · ${countText(plan.files,"файл","файла","файлов")} · ${sizeText(plan.bytes)}. Удаление с компьютера нельзя отменить.`;
      $("#materials-delete").disabled = !plan.files && !plan.messages;
    } catch (error) {if (selection.request === request) $("#materials-summary").textContent = error.message;}
  }
  async function openMaterialDelete(ids = null, type = "meeting", requestedTargets = null) {
    const endpoint=type === "chat" ? "/api/chat-archive/materials" : "/api/materials";
    const plan = await api(endpoint+"/plan", {method:"POST", data:{ids}});
    const choices=plan.choices || [];
    if (!choices.length) {notify("Нет сохранённых материалов для удаления."); return;}
    state.materialDelete = {ids:plan.ids, choices, plan:null, request:0, endpoint, type};
    $("#materials-title").textContent=type === "chat" ? "Удалить материалы чатов" : "Удалить сохранённые материалы";
    $("#materials-description").textContent=type === "chat" ? "Чаты останутся в каталоге. Удалённая переписка восстанавливается только по ручному запросу; новые сообщения продолжают сохраняться. Удалённые файлы не скачиваются автоматически заново. Заметки выбираются отдельно." : "Совещания останутся в каталоге. Выберите, что удалить: записи, тексты, отдельные запуски или заметки.";
    const selected = requestedTargets || (type === "chat" ? ["messages","attachments"] : choices.map(c=>c.target));
    $("#materials-choices").innerHTML = choices.map(choice => `<label class="check-label material-choice"><input type="checkbox" value="${escapeHtml(choice.target)}"${selected.includes(choice.target) ? " checked" : ""}><span><strong>${escapeHtml(choice.label)}</strong><small>${choice.messages ? countText(choice.messages,"сообщение","сообщения","сообщений")+" · " : ""}${countText(choice.files,"файл","файла","файлов")} · ${sizeText(choice.bytes)}</small></span></label>`).join("");
    $("#materials-dialog").showModal(); await refreshMaterialPlan();
  }

  function meetingTargetId(value) {return /^\d+$/.test(String(value || "")) ? Number(value) : 0;}
  function renderMeetingTargets(picker) {
    const chosen = picker.dataset.selected || "";
    const html = (picker.meetings || []).map(item => `<li><button type="button" role="checkbox" aria-checked="${String(item.id) === chosen}" data-target-id="${item.id}"><span class="choice-check">${String(item.id) === chosen ? icon("check") : ""}</span><span class="participant-person"><strong>${escapeHtml(item.title)}</strong><small>ID ${item.id} · ${escapeHtml(dateString(item.startDate))}</small></span></button></li>`).join("");
    const list = $(".meeting-picker-options", picker); if (list.innerHTML !== html) list.innerHTML = html;
    $(".meeting-picker-status", picker).textContent = picker.loading ? "Ищем совещания…" : (picker.meetings || []).length ? "Можно выбрать одно совещание. Повторное нажатие снимает выбор." : "Совещания не найдены. Уточните название или ID.";
  }
  function setMeetingTarget(picker, meeting) {
    picker.dataset.selected = meeting ? String(meeting.id) : "";
    $("input[type=hidden]", picker).value = picker.dataset.selected;
    $(".meeting-picker-caption", picker).textContent = meeting ? `${meeting.id} · ${meeting.title || "Совещание"}` : "Выбрать совещание";
    $(".meeting-picker-clear", picker).hidden = !meeting; renderMeetingTargets(picker);
  }
  async function loadMeetingTargets(picker) {
    const request = (picker.request || 0) + 1; picker.request = request; picker.loading = true; renderMeetingTargets(picker);
    try {
      const data = await api(`/api/meetings?limit=50&q=${encodeURIComponent($(".meeting-picker-search", picker).value.trim())}`);
      if (picker.request !== request) return;
      picker.meetings = data.items.filter(item => item.source !== "import" && !(picker.id === "link-meeting-picker" && item.id === state.meetingId));
    } finally {if (picker.request === request) {picker.loading = false; renderMeetingTargets(picker);}}
  }
  function closeMeetingPicker(picker) {$(".meeting-picker-popup", picker).hidden = true; $(".meeting-picker-toggle", picker).setAttribute("aria-expanded", "false");}
  function openMeetingPicker(picker) {
    $$(".meeting-picker").filter(other => other !== picker).forEach(closeMeetingPicker);
    $(".meeting-picker-popup", picker).hidden = false; $(".meeting-picker-toggle", picker).setAttribute("aria-expanded", "true");
    picker.classList.toggle("opens-up", window.innerHeight - picker.getBoundingClientRect().bottom < 340);
    $(".meeting-picker-search", picker).focus(); loadMeetingTargets(picker).catch(error => notify(error.message, true));
  }
  function openImport(target = null) {
    $("#import-form").reset(); state.pickedPaths = []; $("#import-picked").textContent = "";
    const picker = $("#import-meeting-picker"); $(".meeting-picker-search", picker).value = ""; closeMeetingPicker(picker);
    setMeetingTarget(picker, target ? state.detail?.meeting?.id === target ? state.detail.meeting : state.items.find(m => m.id === target) || {id:target,title:"Совещание"} : null);
    $("#import-dialog").showModal();
  }
  async function bootstrap(initial = false) {
    if (state.busy) return;
    state.busy = true;
    try {
      const previousChatRevision = state.bootstrap?.chat_revision;
      const data = await api("/api/bootstrap"); renderBootstrap(data);
      if(state.route==="jobs" && (state.chatQueueItems?.length||0)>100) renderChatJobs(await api(`/api/chat-archive/queue?ids=${state.chatQueueItems.map(item=>item.id).join(",")}`),false,true); if (["chat-archive","settings"].includes(state.route)) await caUI.onBootstrap(data, state.route); if (data.activation) await consumeActivation(data.activation);
      if (!state.windowAnnounced) { api("/api/desktop/ready", {method: "POST"}).then(result => { state.windowAnnounced = Boolean(result.ready); }).catch(() => {}); }
      if (state.route === "archive" && participantsNeedRefresh()) await loadParticipants();
      if (state.route === "archive" && chatsNeedRefresh()) await loadChats();
      if (state.route === "module" && (!state.hardware || state.hardwareProfile !== hardwareProfile())) await loadHardware();
      if (state.route === "module" && (!state.testMeetings || Date.now() - state.lastTestMeetingsAt > 12000)) await loadTestMeetings();
      if (state.route === "module" && (!state.engines || Date.now() - state.lastEnginesAt > 12000)) await loadEngines();
      if (state.route === "settings" && profileVisible()) await loadProfile();
      if (initial || (state.route === "archive" && !state.filterPending && (data.chat_revision !== previousChatRevision || Date.now() - state.lastListAt > (data.catalogue?.running ? 5000 : 12000)))) await loadMeetings();
      if (state.route === "detail" && Date.now() - state.lastDetailAt > 10000) await loadDetail();
    } catch (error) {
      $("#app-status").textContent = "Связь прервана"; $("#app-status").className = "badge error";
      showError(error.message);
      if (initial) { $("#results-label").textContent = "Нет связи с приложением"; $("#archive-empty").hidden = false; }
    } finally { state.busy = false; }
  }
  document.addEventListener("click", event => {
    const peopleToggle = event.target.closest("[data-people-toggle]");
    if (peopleToggle) {
      const id = Number(peopleToggle.dataset.peopleToggle);
      const expanded = !state.expandedPeople.has(id);
      if (expanded) state.expandedPeople.add(id); else state.expandedPeople.delete(id);
      peopleToggle.closest(".meeting-people").classList.toggle("expanded", expanded);
      peopleToggle.setAttribute("aria-expanded", String(expanded));
      const item = state.items.find(item => item.id === id);
      peopleToggle.setAttribute("aria-label", `${expanded ? "Свернуть" : "Показать всех"} участников (${item?.participants?.length || 0})`);
    }
    const withinParticipants = Boolean(event.target.closest("#participants-control"));
    const addPerson = event.target.closest("[data-participant-add]"); if (addPerson) addParticipant(addPerson.dataset.participantAdd);
    const removePerson = event.target.closest("[data-participant-remove]"); if (removePerson) { state.participantIds.delete(removePerson.dataset.participantRemove); renderParticipants(); requestFilter(); }
    if (state.participantsOpen && !withinParticipants) closeParticipants();
    const addChat = event.target.closest("[data-chat-add]"); if (addChat) addChatChoice(addChat.dataset.chatAdd);
    const removeChat = event.target.closest("[data-chat-remove]"); if (removeChat) { state.chatIds.delete(removeChat.dataset.chatRemove); renderChats(); requestFilter(); }
    if (state.chatsOpen && !event.target.closest("#chats-control")) closeChats();
    if (!event.target.closest("#period-control")) closeCalendar();
    const endpoint = event.target.closest("[data-date-endpoint]"); if (endpoint) { state.dateEndpoint = endpoint.dataset.dateEndpoint; renderCalendar(); }
    const day = event.target.closest("[data-calendar-day]"); if (day) selectDay(day.dataset.calendarDay);
    const routeButton = event.target.closest("[data-route]"); if (routeButton) navigate(routeButton.dataset.route);
    const meetingButton = event.target.closest("[data-meeting]"); if (meetingButton) navigate("detail", Number(meetingButton.dataset.meeting));
    const closeButton = event.target.closest("[data-close]"); if (closeButton) $(`#${closeButton.dataset.close}`).close();
    const startNowButton = event.target.closest("[data-job-now]"); if (startNowButton) action(startNowButton, async () => { await api(`/api/jobs/${startNowButton.dataset.jobNow}/start-now`, { method: "POST", data: {} }); notify("Задача запрошена для запуска сейчас, вне расписания."); await bootstrap(); });
    const cancelButton = event.target.closest("[data-job-cancel]"); if (cancelButton) action(cancelButton, async () => { await api(`/api/jobs/${cancelButton.dataset.jobCancel}/cancel`, { method: "POST", data: {} }); notify("Отмена задачи запрошена."); await bootstrap(); });
    const retryButton = event.target.closest("[data-job-retry]"); if (retryButton) action(retryButton, async () => { await api(`/api/jobs/${retryButton.dataset.jobRetry}/retry`, { method: "POST", data: {} }); notify("Задача поставлена на повтор."); await bootstrap(); });
    const pickButton = event.target.closest("[data-pick]"); if (pickButton) action(pickButton, async () => {
      const result = await api("/api/picker", { method: "POST", data: { kind: "folder" } });
      if (result.paths?.length) { $("#settings-form").elements.namedItem(pickButton.dataset.pick).value = result.paths[0]; markSettingsDirty(); }
    });
  });
  $("#meeting-list").addEventListener("click", event => { if (event.target.dataset.select) selectMeeting(Number(event.target.dataset.select), event.target.checked, event.shiftKey); });
  $("#select-page").addEventListener("change", event => { state.selectionAnchor = null; state.items.forEach(item => { if (event.target.checked) state.selected.add(item.id); else state.selected.delete(item.id); }); $$("#meeting-list input[data-select]").forEach(input => { input.checked = state.selected.has(Number(input.dataset.select)); }); renderSelection(); });
  $("#filter-form").addEventListener("submit", event => { event.preventDefault(); requestFilter(); });
  $("#filter-form").addEventListener("input", event => { if (["q", "chat"].includes(event.target.name)) requestFilter(280); else if (["min_minutes", "max_minutes"].includes(event.target.name)) requestFilter(); });
  $("#filter-form").addEventListener("change", event => { if (["audio", "bitrix", "local"].includes(event.target.name)) requestFilter(); });
  $("#filter-form").addEventListener("reset", () => { state.participantIds.clear(); state.chatIds.clear(); $("#chat-search").value = ""; closeChats(); renderChats(); $("#participant-search").value = ""; state.dateEndpoint = "from"; state.calendarFocus = ""; closeParticipants(); closeCalendar(); renderParticipants(); clearTimeout(state.filterTimer); state.listRequest++; setTimeout(() => { $("#filter-form").elements.date_from.value = ""; $("#filter-form").elements.date_to.value = ""; renderCalendar(); requestFilter(); }, 0); });
  $("#period-toggle").onclick = () => $("#period-menu").hidden ? openCalendar() : closeCalendar();
  $("#period-close").onclick = () => closeCalendar(true);
  $("#calendar-previous").onclick = () => shiftCalendar(-1); $("#calendar-next").onclick = () => shiftCalendar(1);
  $("#period-clear").onclick = () => { $("#filter-form").elements.date_from.value = ""; $("#filter-form").elements.date_to.value = ""; state.dateEndpoint = "from"; renderCalendar(); requestFilter(); };
  $("#period-this-month").onclick = () => { const now = new Date(); const form = $("#filter-form"); form.elements.date_from.value = isoDay(new Date(now.getFullYear(), now.getMonth(), 1)); form.elements.date_to.value = isoDay(new Date(now.getFullYear(), now.getMonth() + 1, 0)); state.calendarMonth = new Date(now.getFullYear(), now.getMonth(), 1); state.dateEndpoint = "from"; renderCalendar(); requestFilter(); };
  $("#period-control").addEventListener("focusout", event => { if (event.relatedTarget && !$("#period-control").contains(event.relatedTarget)) closeCalendar(); });
  $("#period-control").addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeCalendar(true); return; }
    const cell = event.target.closest("[data-calendar-day]"); if (!cell || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) return;
    event.preventDefault(); const date = parseDay(cell.dataset.calendarDay); const dayOfWeek = (date.getDay() + 6) % 7;
    if (["PageUp", "PageDown"].includes(event.key)) { const number = date.getDate(); date.setDate(1); date.setMonth(date.getMonth() + (event.key === "PageUp" ? -1 : 1)); date.setDate(Math.min(number, new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate())); }
    else date.setDate(date.getDate() + ({ ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -dayOfWeek, End: 6 - dayOfWeek }[event.key]));
    state.calendarFocus = isoDay(date); state.calendarMonth = new Date(date.getFullYear(), date.getMonth(), 1); renderCalendar(); $(`#calendar-days [data-calendar-day="${state.calendarFocus}"]`).focus();
  });
  $("#participants-toggle").onclick = () => openParticipants(true);
  $("#participants-close").onclick = () => closeParticipants(true);
  $("#participants-control").addEventListener("pointerenter", () => openParticipants());
  $("#participants-control").addEventListener("pointerleave", () => { state.participantCloseTimer = setTimeout(() => { if (!$("#participants-control").contains(document.activeElement)) closeParticipants(); }, 120); });
  $("#participants-control").addEventListener("focusin", () => { if (!state.suppressParticipantFocus) openParticipants(); });
  $("#participants-control").addEventListener("focusout", event => { if (event.relatedTarget && !$("#participants-control").contains(event.relatedTarget)) closeParticipants(); });
  $("#participant-search").oninput = renderParticipants;
  $("#participants-control").addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeParticipants(true); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End", "Enter"].includes(event.key)) return;
    if (event.key === "Enter" && event.target.id === "participant-search") { event.preventDefault(); const first = $("#participant-options button:not(:disabled)"); if (first) addParticipant(first.dataset.participantAdd); return; }
    if (event.key === "Enter") return;
    event.preventDefault(); openParticipants(); const options = $$("#participant-options button:not(:disabled)");
    if (!options.length) { $("#participant-search").focus(); return; }
    const current = options.indexOf(document.activeElement);
    if (event.key === "Home") options[0].focus();
    else if (event.key === "End") options.at(-1).focus();
    else if (event.key === "ArrowDown") options[Math.min(current + 1, options.length - 1)].focus();
    else if (current > 0) options[current - 1].focus();
    else $("#participant-search").focus();
  });
  $("#previous-page").onclick = () => { state.offset = Math.max(0, state.offset - state.limit); action($("#previous-page"), loadMeetings); };
  $("#next-page").onclick = () => { state.offset += state.limit; action($("#next-page"), loadMeetings); };
  $("#chats-toggle").onclick = () => openChats(true);
  $("#chats-close").onclick = () => closeChats(true);
  $("#chats-control").addEventListener("pointerenter", () => openChats());
  $("#chats-control").addEventListener("pointerleave", () => { state.chatCloseTimer = setTimeout(() => { if (!$("#chats-control").contains(document.activeElement)) closeChats(); }, 120); });
  $("#chats-control").addEventListener("focusin", () => { if (!state.suppressChatFocus) openChats(); });
  $("#chats-control").addEventListener("focusout", event => { if (event.relatedTarget && !$("#chats-control").contains(event.relatedTarget)) closeChats(); });
  $("#chat-search").oninput = renderChats;
  $("#chats-control").addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); closeChats(true); return; }
    if (!["ArrowDown", "ArrowUp", "Home", "End", "Enter"].includes(event.key)) return;
    if (event.key === "Enter" && event.target.id === "chat-search") { event.preventDefault(); const first = $("#chat-options button:not(:disabled)"); if (first) addChatChoice(first.dataset.chatAdd); return; }
    if (event.key === "Enter") return;
    event.preventDefault(); openChats(); const options = $$("#chat-options button:not(:disabled)");
    if (!options.length) { $("#chat-search").focus(); return; }
    const current = options.indexOf(document.activeElement);
    if (event.key === "Home") options[0].focus();
    else if (event.key === "End") options.at(-1).focus();
    else if (event.key === "ArrowDown") options[Math.min(current + 1, options.length - 1)].focus();
    else if (current > 0) options[current - 1].focus();
    else $("#chat-search").focus();
  });
  $("#previous-page").onclick = () => { state.offset = Math.max(0, state.offset - state.limit); action($("#previous-page"), loadMeetings); };
  $("#next-page").onclick = () => { state.offset += state.limit; action($("#next-page"), loadMeetings); };
  $("#refresh-catalogue").onclick = event => action(event.currentTarget, async () => { await api("/api/catalogue/refresh", { method: "POST", data: {} }); state.participantsLoaded = false; state.chatsLoaded = false; notify("Обновление каталога запущено."); await bootstrap(); });
  $("#download-selected").onclick = event => action(event.currentTarget, async () => { await api("/api/download", { method: "POST", data: { ids: [...state.selected] } }); notify(`Загрузка поставлена в очередь: ${state.selected.size} совещаний.`); state.selected.clear(); renderSelection(); await loadMeetings(); await bootstrap(); });
  $("#pause-button").onclick = event => action(event.currentTarget, async () => { if (!state.bootstrap?.settings?.auto_download && !state.bootstrap?.settings?.auto_local && !state.bootstrap?.settings?.watch_enabled && !state.bootstrap?.settings?.chat_auto_save && !state.bootstrap?.settings?.chat_events) { navigate("settings"); return; } const paused = !state.bootstrap?.settings?.paused; await api("/api/settings", { method: "POST", data: { paused } }); notify(paused ? "Автоматизация приостановлена." : "Автоматизация продолжена."); await bootstrap(); });
  $("#download-audio").onclick = event => action(event.currentTarget, async () => { await api("/api/download", { method: "POST", data: { ids: [state.meetingId], audio_only: true } }); notify("Аудиозапись поставлена в очередь загрузки."); await bootstrap(); await loadDetail(); });
  $("#detail-download").onclick = event => action(event.currentTarget, async () => { await api("/api/download", { method: "POST", data: { ids: [state.meetingId] } }); notify("Материалы поставлены в очередь загрузки."); await bootstrap(); await loadDetail(); });
  $("#detail-folder").onclick = event => action(event.currentTarget, () => api("/api/open-folder", { method: "POST", data: { id: state.meetingId } }));
  $("#open-archive").onclick = event => action(event.currentTarget, () => api("/api/open-folder", { method: "POST", data: {} }));
  $("#detail-import").onclick = () => openImport(state.meetingId); $("#import-open").onclick = () => openImport();
  $("#audio-file-select").onchange = renderPlayer; $("#local-run-select").onchange = renderLocalRun;
  $("#transcribe-button").onclick = event => action(event.currentTarget, async () => {
    if (!state.bootstrap?.transcription?.ready) { navigate("module"); return; }
    const names = $$("#transcription-files input:checked").map(input => input.value);
    if (!names.length) throw new Error("Выберите хотя бы одну запись для обработки.");
    const settings = state.bootstrap?.settings || {}; const engine = settings.engine || "whisper";
    const model = engine === "gigaam" ? settings.gigaam_model : engine === "parakeet" ? settings.parakeet_model : settings.model;
    if (!state.engines) await loadEngines();
    const installed = state.engines?.find(item => item.id === engine)?.models?.find(item => item.id === model)?.installed;
    if (!installed) { navigate("module"); throw new Error("Сначала установите выбранную модель или используйте найденные совместимые ресурсы в разделе локальной расшифровки."); }
    if (state.bootstrap?.settings?.device === "cpu" && !state.bootstrap?.settings?.cpu_confirmed) throw new Error("Явно разрешите обработку на CPU в настройках.");
    await api("/api/transcribe", { method: "POST", data: { id: state.meetingId, mode: $("#transcribe-mode").value, sample: false, file_names: names } });
    notify("Расшифровка поставлена в очередь."); await bootstrap(); await loadDetail();
  });
  for (const picker of $$(".meeting-picker")) {
    let timer;
    $(".meeting-picker-toggle", picker).onclick = () => $(".meeting-picker-popup", picker).hidden ? openMeetingPicker(picker) : closeMeetingPicker(picker);
    $(".meeting-picker-clear", picker).onclick = () => setMeetingTarget(picker, null);
    $(".meeting-picker-search", picker).addEventListener("input", () => {clearTimeout(timer); timer = setTimeout(() => loadMeetingTargets(picker).catch(error => notify(error.message, true)), 200);});
    $(".meeting-picker-options", picker).addEventListener("click", event => {
      const button = event.target.closest("[data-target-id]"); if (!button) return;
      const selected = picker.dataset.selected === button.dataset.targetId;
      setMeetingTarget(picker, selected ? null : picker.meetings.find(meeting => String(meeting.id) === button.dataset.targetId));
    });
    picker.addEventListener("keydown", event => {
      if (event.key === "Escape" && !$(".meeting-picker-popup", picker).hidden) {event.preventDefault(); event.stopPropagation(); closeMeetingPicker(picker); $(".meeting-picker-toggle", picker).focus();}
      if (event.key === "Enter" && event.target.matches(".meeting-picker-search")) {event.preventDefault(); $(".meeting-picker-options button", picker)?.focus();}
      if (["ArrowDown", "ArrowUp"].includes(event.key) && event.target.closest(".meeting-picker-options")) {
        event.preventDefault(); const options = $$(".meeting-picker-options button", picker), index = options.indexOf(event.target);
        options[Math.max(0, Math.min(options.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)))]?.focus();
      }
    });
  }
  document.addEventListener("click", event => $$(".meeting-picker").filter(picker => !event.composedPath().includes(picker)).forEach(closeMeetingPicker));
  $("#detail-delete").onclick = event => action(event.currentTarget, () => openMaterialDelete([state.meetingId]));
  $("#delete-selected").onclick = event => action(event.currentTarget, () => openMaterialDelete([...state.selected]));
  $("#archive-delete").onclick = event => action(event.currentTarget, () => openMaterialDelete());
  $("#materials-choices").onchange = refreshMaterialPlan;
  $("#materials-all").onchange = () => {$$("#materials-choices input").forEach(input => {input.checked = $("#materials-all").checked;}); refreshMaterialPlan();};
  $("#materials-delete").onclick = event => action(event.currentTarget, async () => {
    const selection=state.materialDelete, plan=selection?.plan;
    if (!plan || (!plan.files && !plan.messages)) return;
    const chat=selection.type === "chat";
    const summary=chat ? `${plan.chats} чатов · ${plan.messages} сообщений · ${plan.files} файлов · ${sizeText(plan.bytes)}.\nВыбранные материалы будут удалены с компьютера. В Bitrix24 сообщения и файлы сохранятся. Для восстановления нужен ручной запрос.` : `${plan.meetings} совещаний · ${plan.files} файлов · ${sizeText(plan.bytes)}.\nЗаписи, тексты и отмеченные заметки будут удалены с компьютера. Совещания останутся в каталоге.`;
    if (!(await confirmAction("Удалить выбранные материалы?",summary,"Удалить материалы",true))) return;
    await api(selection.endpoint+"/remove", {method:"POST",data:{ids:plan.ids,targets:plan.targets,token:plan.token,confirm:true}});
    $("#materials-dialog").close(); state.materialDelete=null; notify("Выбранные материалы удалены с компьютера.");
    await bootstrap();
    if(chat) await caUI.refreshAfterRemoval(); else {await loadMeetings(); if(state.route === "detail") await loadDetail();}
  });
  $("#chat-archive-delete").onclick=event=>action(event.currentTarget,()=>openMaterialDelete(null,"chat"));

  $("#removable-models").onchange = renderModelRemoval;
  $("#models-delete").onclick = event => action(event.currentTarget, async () => {
    const packages = $$("#removable-models input:checked").map(input => ({engine:input.dataset.engine, model:input.value}));
    const plan = await api("/api/module/models/remove-plan", {method:"POST", data:{packages}});
    if (!(await confirmAction("Удалить выбранные модели?", `${packages.map(item => item.model).join(", ")}\n${plan.files} файлов · ${sizeText(plan.bytes)}.\nБиблиотеки обработки и ресурсы других программ сохранятся. Эти модели можно установить снова.`, "Удалить модели", true))) return;
    await api("/api/module/models/remove", {method:"POST", data:{packages, token:plan.token, confirm:true}});
    $$("#removable-models input").forEach(input => {input.checked = false;}); await loadEngines(); await bootstrap(); notify("Выбранные модели удалены.");
  });
  $("#processing-prepare").onclick = event => action(event.currentTarget, async () => {
    if (state.settingsDirty) throw new Error("Сначала сохраните профиль расшифровки.");
    const settings = state.bootstrap.settings, profile = settings.device, packages = [{engine:settings.engine, model:settings.engine === "whisper" ? settings.model : settings[settings.engine + "_model"]}];
    const plan = await api("/api/module/install-plan", {method:"POST", data:{profile, packages}});
    if (plan.runtime?.available === false) throw new Error(plan.runtime.reason);
    if (!(await confirmAction("Подготовить поддержку обработки звука?", `Будут установлены недостающие библиотеки для сохранённого профиля.\nЗагрузка: ${gbText(plan.download_gb)}. Свободное место с запасом: ${gbText(plan.required_gb)}.\n${plan.note || "Сохранённые веса модели используются повторно."}\nУстановку можно отменить; архив сохраняется.`, "Подготовить поддержку"))) return;
    await api("/api/module/install", {method:"POST", data:{profile, packages}}); notify("Подготовка поддержки запущена. Прогресс доступен в очереди."); await bootstrap();
  });
  $("#link-import-button").onclick = event => action(event.currentTarget, async () => {
    const target = meetingTargetId($("#link-target").value); if (!Number.isInteger(target) || target < 1) throw new Error("Выберите совещание по названию или ID из списка.");
    if (target === state.meetingId) throw new Error("Выберите другое совещание для привязки.");
    await api("/api/link", { method: "POST", data: { source_id: state.meetingId, target_id: target } }); notify("Запись привязана. Исходный импорт сохранён."); await loadDetail();
  });
  $("#jobs-refresh").onclick = event => action(event.currentTarget, () => bootstrap());
  $("#chat-jobs-more").onclick=event=>action(event.currentTarget,async()=>renderChatJobs(await api(`/api/chat-archive/queue?offset=${state.chatQueueItems?.length||0}&limit=100`),true));
  $("#chat-jobs-cancel-backlog").onclick=event=>action(event.currentTarget,async()=>{
    const summary="Будут отменены задания старой истории, выбранных периодов и очереди вложений чатов. Новые сообщения и очередь совещаний останутся без изменений. Уже сохранённые материалы не удаляются.";
    if (!(await confirmAction("Отменить старую историю чатов?",summary,"Отменить задания",true))) return;
    const result=await api("/api/chat-archive/queue/cancel-backlog",{method:"POST",data:{confirm:true,files:true}});
    notify(`Отменено заданий чатов: ${result.cancelled}. Сохранённые материалы не удалены.`); await bootstrap();
  });
  document.addEventListener("click",event=>{ const button=event.target.closest("[data-chat-job-action]"); if(button) action(button,async()=>{ await api(`/api/chat-archive/queue/${button.dataset.chatJob}/${button.dataset.chatJobAction}`,{method:"POST"}); await bootstrap(); }); });
  function markSettingsDirty(event) { if (event?.target && !event.target.name && !event.target.closest("[data-schedule]")) return; state.settingsDirty = true; $("#settings-status").textContent = "Есть несохранённые изменения."; $(".settings-footer").classList.add("dirty"); updateCpuAcknowledgement(); updateAudioDownloadPolicy(); updateDiarization(); updateEngineControls(); updateScheduleVisibility(); }
  ["input", "change"].forEach(type => document.addEventListener(type, event => { if (event.target.form?.id === "settings-form") markSettingsDirty(event); }));
  $("#settings-revert").onclick = () => { updateSettingsForm(state.bootstrap?.settings || {}, true); notify("Несохранённые изменения отменены."); };
  $("#settings-form").addEventListener("submit", event => {
    event.preventDefault(); action(event.submitter, async () => {
      const form = event.currentTarget; const data = {};
      [...form.elements].filter(input => input.name).forEach(input => { data[input.name] = input.multiple ? [...input.selectedOptions].map(option => Number(option.value)) : input.type === "checkbox" ? input.checked : input.type === "number" || input.name === "chat_poll_seconds" ? Number(input.value) || 0 : input.value.trim(); });
      $$("[data-schedule]").forEach(editor => { const schedule = readSchedule(editor); if (schedule.mode === "window" && (!schedule.days.length || !schedule.start || !schedule.end)) throw new Error("Для расписания выберите хотя бы один день и заполните начало и окончание."); data[`${editor.dataset.schedule}_schedule`] = schedule; });
      if (data.device === "cpu" && !data.cpu_confirmed) throw new Error("Отметьте явное разрешение обработки на CPU.");
      if (data.watch_enabled && !data.watch_folder) throw new Error("Выберите папку для автоматического импорта записей.");
      if (data.min_speakers && data.max_speakers && data.min_speakers > data.max_speakers) throw new Error("Минимум говорящих должен быть не больше максимума.");
      const changed = Object.fromEntries(Object.entries(data).filter(([name, value]) => JSON.stringify(value) !== JSON.stringify(state.bootstrap?.settings?.[name])));
      await api("/api/settings", { method: "POST", data: changed }); state.settingsReady = false; await bootstrap(); await loadEngines(); renderEstimate(); notify("Настройки сохранены.");
    });
  });
  $("#install-profile").onchange = renderEstimate; $("#install-packages").addEventListener("change", renderEstimate);
  $("#module-install-form").addEventListener("submit", event => { event.preventDefault(); action(event.submitter, async () => {
    const profile = $("#install-profile").value; const packages = selectedPackages();
    if (!packages.length) throw new Error("Выберите хотя бы одну модель для установки.");
    if (profile === "cpu" && !state.bootstrap?.settings?.cpu_confirmed) throw new Error("Перед установкой CPU-профиля выберите CPU и подтвердите его использование в настройках, затем сохраните их.");
    const plan = await api("/api/module/install-plan", { method: "POST", data: { profile, packages } }); state.installPlan = plan; showInstallPlan(plan);
    if (plan.runtime?.available === false) throw new Error(plan.runtime.reason);
    const message = `Профиль: ${profile === "cuda" ? "GPU NVIDIA / CUDA" : "CPU"}.\nВыбрано: ${packages.map(item => item.model).join(", ")}.\nОценка загрузки: ${gbText(plan.download_gb)}. Свободное место с запасом: ${gbText(plan.required_gb)}.\n${plan.note || "Общие библиотеки используются один раз."}${profile === "cpu" ? "\nОбработка на CPU может занять существенно больше времени. Профиль установки сам по себе не переключает устройство." : ""}\nУстановку можно отменить. Архив сохраняется; драйвер NVIDIA не устанавливается.`;
    if (!(await confirmAction("Установить выбранные модели?", message, "Начать установку"))) return;
    await api("/api/module/install", { method: "POST", data: { profile, packages } }); notify("Установка запущена. Прогресс доступен здесь и в очереди."); await bootstrap();
  }); });
  $("#module-cancel").onclick = event => action(event.currentTarget, async () => { await api("/api/module/cancel", { method: "POST", data: {} }); notify("Отмена установки запрошена."); await bootstrap(); });
  $("#module-remove").onclick = event => action(event.currentTarget, async () => {
    const size = Number(state.bootstrap?.module?.size_gb || 0).toLocaleString("ru-RU");
    if (!(await confirmAction("Удалить модуль расшифровки?", `Будут остановлены его задачи и удалены собственная среда и модели Meeting Archive (около ${size} ГБ).\nАрхив и ресурсы других приложений сохраняются. Модуль можно установить снова.`, "Удалить модуль", true))) return;
    await api("/api/module/remove", { method: "POST", data: { confirm: true } }); notify("Модуль удалён."); await bootstrap();
  });
  $("#model-test-meeting").onchange = renderModelTest;
  $("#model-test-button").onclick = event => action(event.currentTarget, async () => {
    if (state.settingsDirty) throw new Error("Сначала сохраните профиль расшифровки.");
    const id = Number($("#model-test-meeting").value);
    const detail = await api(`/api/meeting/${id}`);
    const audio = detail.files.find(file => file.kind === "audio" || /^audio[\\/]/.test(file.name));
    if (!audio) throw new Error("Запись не найдена. Повторно скачайте материалы встречи.");
    await api("/api/transcribe", {method: "POST", data: {id, mode: "combined", sample: true, file_names: [audio.name]}});
    notify("Короткий тест поставлен в очередь. Результат появится здесь после обработки."); await bootstrap();
  });
  $("#hardware-refresh").onclick = event => action(event.currentTarget, async () => { await loadHardware(); notify("Оценка оборудования обновлена."); });
  $("#hardware-probe").onclick = event => action(event.currentTarget, async () => {
    $("#hardware-probe-result").textContent = "Проверяем доступность CUDA в среде модуля…";
    try { const result = await api("/api/hardware/probe", { method: "POST", data: {} }); await loadHardware(); $("#hardware-probe-result").textContent = result.compatible ? `CUDA доступна; архитектура совместима${result.torch ? `. PyTorch ${result.torch}` : ""}. Работу выбранной модели проверьте коротким тестом записи.` : `CUDA ${result.cuda ? "доступна" : "недоступна"}; совместимость архитектуры ${result.compatible ? "подтверждена" : "не подтверждена"}. Проверьте профиль модуля и драйвер.`; }
    catch (error) { $("#hardware-probe-result").textContent = error.message; throw error; }
  });
  $("#hf-check").onclick = event => action(event.currentTarget, async () => {
    const tokenInput = $("#hf-token"); const token = tokenInput.value.trim(); if (!token && (!state.bootstrap?.secret_status?.hf_token_saved || state.secretEditing.hf)) throw new Error("Введите токен Read для проверки доступа к моделям.");
    $("#hf-result").textContent = "Проверяем доступ к файлам обеих моделей…";
    try {
      const result = await api("/api/hf/check", { method: "POST", data: { token } });
      const checks = result.checks || [];
      const readable = Array.isArray(checks) ? checks.map(check => typeof check === "string" ? check : `${check.model || check.name || "Модель"}: ${check.ok || check.accessible || check.available ? "доступ подтверждён" : check.error || "доступ не подтверждён"}`).join("; ") : Object.entries(checks).map(([name, value]) => `${name}: ${value === true || value?.ok || value?.available ? "доступ подтверждён" : value?.error || "не подтверждён"}`).join("; ");
      $("#hf-result").textContent = (readable || result.message || "Проверка завершена.") + (result.error ? `. ${result.error}` : result.verified ? ". Токен сохранён в защищённом хранилище Windows." : "");
      notify(result.verified === false ? "Доступ к моделям не подтверждён. Проверьте условия и токен." : "Проверка доступа завершена.", result.verified === false);
    } catch (error) { $("#hf-result").textContent = error.message; throw error; }
    finally { tokenInput.value = ""; state.secretEditing.hf = false; await bootstrap(); }
  });
  $("#hf-replace").onclick = () => { state.secretEditing.hf = true; renderSecrets(); $("#hf-token-entry").closest("details").open = true; $("#hf-token").focus(); };
  $("#hf-remove").onclick = event => action(event.currentTarget, async () => {
    if (!(await confirmAction("Удалить токен Hugging Face?", "Токен будет удалён из защищённого хранилища Windows. Разделение по говорящим будет выключено; архив и тексты сохраняются.", "Удалить токен", true))) return;
    await api("/api/hf/remove", { method: "POST", data: {} }); state.secretEditing.hf = false; $("#hf-token").value = ""; $("#hf-result").textContent = "Токен удалён. Для разделения по говорящим снова проверьте доступ к моделям.";
    if (state.settingsDirty) { $("#settings-form").elements.diarization.checked = false; updateDiarization(); }
    else state.settingsReady = false;
    await bootstrap(); notify("Токен удалён, диаризация выключена.");
  });
  function authTab(mode) {
    const oauth = mode === "oauth";
    $("#oauth-panel").hidden = !oauth; $("#webhook-panel").hidden = oauth;
    $("#oauth-tab").classList.toggle("active", oauth); $("#webhook-tab").classList.toggle("active", !oauth);
    $("#oauth-tab").setAttribute("aria-selected", String(oauth)); $("#webhook-tab").setAttribute("aria-selected", String(!oauth));
  }
  $("#oauth-tab").onclick = () => authTab("oauth"); $("#webhook-tab").onclick = () => authTab("webhook");
  [$("#oauth-tab"), $("#webhook-tab")].forEach(tab => tab.addEventListener("keydown", event => { if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); const next = tab.id === "oauth-tab" ? $("#webhook-tab") : $("#oauth-tab"); next.click(); next.focus(); } }));
  $("#copy-local-callback").onclick = event => action(event.currentTarget, async () => { await navigator.clipboard.writeText("http://localhost:8765/callback"); notify("Адрес скопирован."); });
  ["callback", "install"].forEach(kind => { $(`#copy-${kind}`).onclick = event => action(event.currentTarget, async () => { const value = $(`#oauth-${kind}`).textContent; if (!value.startsWith("https://")) throw new Error("Сначала укажите свой HTTPS-обработчик."); await navigator.clipboard.writeText(value); notify("Адрес скопирован."); }); });
  ["input", "change"].forEach(type => $("#oauth-form").addEventListener(type, updateOauthFlow));
  $("#oauth-secret-replace").onclick = () => { state.secretEditing.client = true; renderSecrets(); $("#oauth-form").elements.client_secret.focus(); };
  $("#webhook-replace").onclick = () => { state.secretEditing.webhook = true; renderSecrets(); $("#webhook-form").elements.webhook.focus(); };
  $("#oauth-form").addEventListener("submit", event => {
    event.preventDefault(); const form = event.currentTarget;
    const separate = form.elements.flow.value === "oob";
    const authWindow = separate && !window.pywebview ? window.open("about:blank", "_blank") : null; if (authWindow) authWindow.opener = null;
    action(event.submitter, async () => {
      try {
        const result = await api("/api/auth/oauth", { method: "POST", data: { portal: form.elements.portal.value.trim(), client_id: form.elements.client_id.value.trim(), client_secret: form.elements.client_secret.value, oauth_relay: form.elements.oauth_relay.value.trim(), flow: form.elements.flow.value } });
        const url = new URL(result.url); if (url.protocol !== "https:") throw new Error("Получен некорректный адрес авторизации.");
        if (window.pywebview) window.open(url.href, "_blank");
        else if (authWindow) authWindow.location.href = url.href;
        else if (!separate) { location.assign(url.href); return; }
        const target = $("#oauth-link"); target.replaceChildren(); target.hidden = false;
        const link = document.createElement("a"); link.href = url.href; link.target = separate || window.pywebview ? "_blank" : "_self"; link.rel = "noreferrer"; link.textContent = "Открыть вход повторно, если браузер не открылся"; target.append(link);
        state.oauthAttempt = result.flow === "oob" ? result.attempt : null; $("#oauth-code-form").hidden = !state.oauthAttempt;
        notify(state.oauthAttempt ? "Войдите в Bitrix24, скопируйте выданный код и проверьте его здесь." : "Войдите в Bitrix24 в открывшемся окне браузера. После входа состояние обновится автоматически.");
      } catch (error) { authWindow?.close(); throw error; }
      finally { form.elements.client_secret.value = ""; state.secretEditing.client = false; await bootstrap(); }
    });
  });
  $("#oauth-code-form").addEventListener("submit", event => { event.preventDefault(); const form = event.currentTarget; action(event.submitter, async () => {
    if (!state.oauthAttempt) throw new Error("Сначала начните новую попытку входа через Bitrix24.");
    try { await api("/api/auth/oauth/code", { method: "POST", data: { attempt: state.oauthAttempt, code: form.elements.code.value.trim() } }); state.oauthAttempt = null; form.hidden = true; notify("OAuth подключён; доступ к каталогу проверен."); await bootstrap(); await loadMeetings(); }
    finally { form.elements.code.value = ""; }
  }); });
  $("#webhook-form").addEventListener("submit", event => { event.preventDefault(); const form = event.currentTarget; action(event.submitter, async () => {
    try { await api("/api/auth/webhook", { method: "POST", data: { webhook: form.elements.webhook.value.trim() } }); notify("Webhook подключён; доступ к каталогу проверен."); await bootstrap(); await loadMeetings(); }
    finally { form.elements.webhook.value = ""; state.secretEditing.webhook = false; await bootstrap(); }
  }); });
  async function disconnectPortal() {
    if (!(await confirmAction("Отключить Bitrix24?", "Автозагрузка из портала остановится. Уже сохранённые материалы останутся в архиве. Для новых загрузок потребуется повторное подключение.", "Отключить"))) return;
    await api("/api/auth/disconnect", { method: "POST", data: {} }); $("#oauth-link").replaceChildren(); $("#oauth-link").hidden = true; state.oauthAttempt = null; $("#oauth-code-form").hidden = true; state.secretEditing.webhook = false; state.secretEditing.client = false; $("#webhook-form").elements.webhook.value = ""; $("#oauth-form").elements.client_secret.value = ""; notify("Портал отключён, реквизиты удалены."); await bootstrap();
  }
  ["disconnect", "webhook-remove", "oauth-secret-remove"].forEach(id => { $(`#${id}`).onclick = event => action(event.currentTarget, disconnectPortal); });
  $("#import-picker").onclick = event => action(event.currentTarget, async () => {
    const result = await api("/api/picker", { method: "POST", data: { kind: "files" } });
    if (result.paths?.length) { state.pickedPaths = result.paths; $("#import-files").value = ""; $("#import-picked").textContent = result.paths.join("\n"); }
  });
  $("#import-files").onchange = () => { if ($("#import-files").files.length) { state.pickedPaths = []; $("#import-picked").textContent = ""; } };
  $("#import-form").addEventListener("submit", event => {
    event.preventDefault(); action(event.submitter, async () => {
      const files = $("#import-files").files; const targetText = $("#import-target").value.trim(); const target = meetingTargetId(targetText) || null; if (targetText && !target) throw new Error("Выберите совещание по названию или ID из списка.");
      if (!files.length && !state.pickedPaths.length) throw new Error("Выберите аудио или видео для импорта.");
      let result;
      if (state.pickedPaths.length) result = await api("/api/import", { method: "POST", data: { paths: state.pickedPaths, ...(target ? { meeting_id: target } : {}) } });
      else { const form = new FormData(); [...files].forEach(file => form.append("files", file)); if (target) form.append("meeting_id", String(target)); result = await api("/api/upload", { method: "POST", form }); }
      $("#import-dialog").close(); notify(result?.message || "Записи переданы на импорт."); state.pickedPaths = []; $("#import-form").reset(); await loadMeetings(); await bootstrap(); if (state.route === "detail") await loadDetail();
    });
  });
  window.addEventListener("hashchange", applyRoute);
  window.addEventListener("beforeunload", event => { if (state.settingsDirty) { event.preventDefault(); event.returnValue = ""; } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) bootstrap(); });
  const caUI = window.ChatArchiveUI.create({api, icon, escapeHtml, dateString, notify, action, getSettings: () => state.bootstrap?.settings || {}, openMaterialDelete});
  $("#settings-form").addEventListener("settings-category-change", event => { if (event.detail === "profile") loadProfile().catch(error => notify(error.message, true)); });
  window.ArchiveControls?.init(document, {icon});
  window.ArchiveControls?.dateRange($("#ca-period-control"), {from: $("#ca-filters").elements.date_from, to: $("#ca-filters").elements.date_to, label: "Период сообщений", icon});
  decorateStaticIcons(); renderParticipants(); renderCalendar(); applyRoute();

  function renderDesktopSettings(data) {
    const u = data.updates || {};
    const captions = {idle:"Проверка ещё не выполнена", checking:"Проверяем GitHub…", current:"Установлена последняя версия", available:"Новая версия доступна", downloading:"Скачиваем новую версию…", ready:"Обновление готово к установке", installing:"Перезапускаем приложение…", error:"Обновление не выполнено"};
    $("#update-status").textContent = `Установлена версия ${u.installed || "—"}. ${u.waiting ? "Ожидаем завершения текущих задач. Новые задачи временно не запускаются." : captions[u.state] || ""}${u.available ? ` Версия ${u.available}, ${(Number(u.size || 0)/1024/1024).toFixed(1)} МБ.` : ""}${u.checked_at ? ` Проверено: ${new Date(u.checked_at*1000).toLocaleString("ru-RU")}.` : ""}`;
    $("#update-error").textContent = u.error || "";
    $("#update-progress").hidden = u.state !== "downloading"; $("#update-progress").value = u.progress || 0;
    $("#update-check").disabled = ["checking","downloading","installing"].includes(u.state) || u.waiting;
    $("#update-download").hidden = u.state !== "available";
    $("#update-install").hidden = u.state !== "ready" || u.waiting;
    $("#update-install").disabled = !u.supported;
    $("#update-cancel").hidden = !u.waiting || u.state === "installing";
    $("#update-release").hidden = !u.release_url; if (u.release_url) $("#update-release").href = u.release_url;
    $("#notification-error").textContent = data.notification_error || "";
    updateNotificationControls();
  }
  function updateNotificationControls() {
    const enabled = $("#settings-form").elements.notifications_enabled.checked;
    $("#notification-options").classList.toggle("notifications-muted", !enabled);
    $$("#notification-options input").forEach(input => {input.disabled = !enabled;});
    $("#notification-test").disabled = !state.bootstrap?.settings?.notifications_enabled;
  }
  async function consumeActivation(activation) {
    if (!activation || activation.sequence === state.activationSequence) return;
    state.activationSequence = activation.sequence;
    if (activation.route === "meeting") {
      try { await api(`/api/meeting/${activation.ids[0]}`); navigate("detail", activation.ids[0]); }
      catch { navigate("archive"); notify("Совещание из уведомления больше недоступно."); }
    } else if (activation.route === "jobs") {
      navigate("jobs");
      $$("[data-job-id]").forEach(row => row.classList.toggle("notification-highlight", activation.ids.includes(Number(row.dataset.jobId))));
      const first = $(".notification-highlight"); if (first) first.scrollIntoView({block:"center"});
      else if (activation.ids.length) notify("Задача отсутствует среди последних задач очереди.");
    } else navigate(activation.route === "connection" ? "connection" : "settings");
    await api("/api/notifications/ack", {method:"POST", data:{sequence:activation.sequence}});
  }
  for (const operation of ["check", "download", "install", "cancel"]) {
    $("#update-" + operation).onclick = event => action(event.currentTarget, async () => {
      if (operation === "install" && state.settingsDirty) throw new Error("Сначала сохраните изменения настроек.");
      await api("/api/updates/" + operation, {method:"POST", data:{}}); await bootstrap();
    });
  }
  $("#settings-form").elements.notifications_enabled.addEventListener("change", updateNotificationControls);
  $("#notification-test").onclick = event => action(event.currentTarget, async () => {
    const result = await api("/api/notifications/test", {method:"POST", data:{}});
    notify(result.sent ? "Уведомление передано Windows. Если оно не появилось, проверьте настройки уведомлений Windows." : result.error || "Не удалось показать уведомление.", !result.sent);
  });

  bootstrap(true);
  setInterval(() => { bootstrap(); }, 4000);
})();
