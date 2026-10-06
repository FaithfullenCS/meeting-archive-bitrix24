"use strict";

// Settings inherit Meeting Archive's outline icons, green accent and shared save bar.
// Category tiles reveal one panel; the backing form retains every setting across tabs.
(() => {
  const controls = new Set();
  const registered = new WeakMap();
  let sequence = 0;
  let iconProvider = null;
  const paths = {
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
    database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
    gear: '<path d="m9 3-1 3-3 1 1 3-2 2 2 2-1 3 3 1 1 3h6l1-3 3-1-1-3 2-2-2-2 1-3-3-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
    inbox: '<path d="M5 4h14l3 13v4H2v-4zM2 14h6l2 3h4l2-3h6"/>',
    file: '<path d="M14 2H6v20h12V6zM14 2v5h5M9 12h6M9 16h6"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    'chevron-right': '<path d="m9 6 6 6-6 6"/>',
    'arrow-left': '<path d="M20 12H4m6-6-6 6 6 6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="m6 6 12 12M6 18 18 6"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4M12 17h.01"/>'
  };
  const svg = name => paths[name] ? `<svg class="ui-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${paths[name]}</svg>` : (iconProvider?.(name) || "");
  const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
  function button(className, text, name) { const node = element("button", className); node.type = "button"; if (name) node.innerHTML = svg(name); if (text) node.append(element("span", "", text)); return node; }
  function changed(source) { source.dispatchEvent(new Event("input", {bubbles: true})); source.dispatchEvent(new Event("change", {bubbles: true})); }
  function closeOthers(except) { controls.forEach(control => { if (control !== except) control.close?.(); }); }
  function fieldName(source) { const label = source.closest("label"); return source.getAttribute("aria-label") || label?.firstChild?.textContent.trim() || source.name || "Выбор"; }
  function installOutside(control) {
    control.wrapper.addEventListener("keydown", event => { if (event.key === "Escape" && !control.popup.hidden) { event.preventDefault(); event.stopPropagation(); control.close(); control.toggle.focus(); } });
  }
  function choice(source, options = {}) {
    if (!source) return null;
    if (registered.has(source)) { const current = registered.get(source); Object.assign(current.options, options); current.refresh(); return current; }
    if (source.multiple && source.form?.id === "ca-filters") return peopleChoice(source, options);
    const multiple = source.multiple;
    const label = fieldName(source);
    const wrapper = element("div", `archive-choice${multiple ? " archive-choice-multiple" : ""}`);
    source.before(wrapper); wrapper.append(source); source.hidden = true; source.tabIndex = -1; source.classList.add("archive-control-source");
    const selected = element("div", "archive-choice-chips");
    const toggle = button("archive-choice-toggle", "", multiple ? "plus" : "chevron-down");
    const caption = element("span", "archive-choice-caption"); toggle.prepend(caption);
    toggle.setAttribute("aria-haspopup", "listbox"); toggle.setAttribute("aria-expanded", "false"); toggle.setAttribute("aria-label", label);
    if (source.hasAttribute("aria-describedby")) toggle.setAttribute("aria-describedby", source.getAttribute("aria-describedby"));
    const popup = element("div", "archive-choice-popup"); popup.id = `archive-choice-${++sequence}`; popup.hidden = true; toggle.setAttribute("aria-controls", popup.id);
    const search = element("input", "archive-choice-search"); search.type = "search"; search.autocomplete = "off"; search.placeholder = multiple ? "Найти и добавить…" : "Поиск…"; search.setAttribute("aria-label", `Поиск: ${label}`);
    const list = element("div", "archive-choice-options"); list.setAttribute("role", "listbox"); list.setAttribute("aria-label", label); if (multiple) list.setAttribute("aria-multiselectable", "true");
    const status = element("p", "archive-choice-status footnote"); status.setAttribute("role", "status");
    popup.append(search, list, status); if (multiple) wrapper.append(selected); wrapper.append(toggle, popup);
    const control = {source, options, wrapper, toggle, popup, close() { popup.hidden = true; toggle.setAttribute("aria-expanded", "false"); }, refresh() {
      toggle.disabled = source.disabled;
      const chosen = [...source.selectedOptions].filter(option => multiple || option.value);
      caption.textContent = multiple ? (options.placeholder || source.dataset.choicePlaceholder || "Добавить") : (source.selectedOptions[0]?.textContent || "Выберите");
      selected.replaceChildren(); selected.hidden = !chosen.length;
      if (multiple) chosen.forEach(option => {
        const chip = button("participant-chip archive-choice-chip", option.textContent, "x"); chip.disabled = source.disabled; chip.setAttribute("aria-label", `Убрать: ${option.textContent}`);
        chip.onclick = () => { option.selected = false; changed(source); control.refresh(); }; selected.append(chip);
      });
      renderOptions();
    }};
    function removeExclusive(value) {
      const other = typeof options.exclusiveWith === "string" ? document.querySelector(options.exclusiveWith) : options.exclusiveWith;
      if (!other) return;
      let removed = false; [...other.options].forEach(option => { if (option.value === value && option.selected) { option.selected = false; removed = true; } });
      if (removed) { changed(other); registered.get(other)?.refresh(); }
    }
    function choose(option) {
      if (!option || option.disabled || source.disabled) return;
      if (multiple) { option.selected = !option.selected; if (option.selected) removeExclusive(option.value); }
      else { source.value = option.value; control.close(); toggle.focus(); }
      changed(source); control.refresh();
      if (multiple) search.focus();
    }
    let renderedOptions = "";
    function renderOptions() {
      const query = search.value.trim().toLocaleLowerCase("ru-RU");
      const matches = [...source.options].filter(option => !query || `${option.textContent} ${option.value}`.toLocaleLowerCase("ru-RU").includes(query));
      const fingerprint = JSON.stringify([query, matches.length, matches.slice(0, 100).map(option => [option.value, option.textContent, option.selected, option.disabled])]);
      if (fingerprint === renderedOptions) return;
      renderedOptions = fingerprint;
      list.replaceChildren();
      matches.slice(0, 100).forEach(option => {
        const item = button("archive-choice-option", option.textContent, option.selected ? "check" : null); item.setAttribute("role", "option"); item.setAttribute("aria-selected", String(option.selected)); item.dataset.value = option.value; item.disabled = option.disabled; item.onclick = () => choose([...source.options].find(current => current.value === item.dataset.value)); list.append(item);
      });
      status.textContent = matches.length > 100 ? `Показано 100 из ${matches.length}. Уточните поиск.` : !matches.length ? ([...source.options].length ? "Совпадений нет." : "Список пока пуст. Обновите каталог чатов.") : "";
      status.hidden = !status.textContent;
    }
    function open(focusOption = false) { if (source.disabled) return; closeOthers(control); search.value = ""; renderOptions(); popup.hidden = false; toggle.setAttribute("aria-expanded", "true"); const item = list.querySelector('[aria-selected="true"]') || list.querySelector("button:not(:disabled)"); if (focusOption && !multiple) item?.focus(); else search.focus(); }
    toggle.onclick = () => popup.hidden ? open() : control.close();
    toggle.addEventListener("keydown", event => { if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); open(true); } });
    search.addEventListener("input", event => { event.stopPropagation(); renderOptions(); });
    search.addEventListener("change", event => event.stopPropagation());
    search.addEventListener("keydown", event => { if (event.key === "ArrowDown") { event.preventDefault(); list.querySelector("button:not(:disabled)")?.focus(); } });
    list.addEventListener("keydown", event => { if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return; event.preventDefault(); const items = [...list.querySelectorAll("button:not(:disabled)")]; const index = items.indexOf(document.activeElement); const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : Math.max(0, Math.min(items.length - 1, index + (event.key === "ArrowDown" ? 1 : -1))); items[next]?.focus(); });
    source.addEventListener("change", () => control.refresh());
    new MutationObserver(() => control.refresh()).observe(source, {childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "selected"]});
    registered.set(source, control); controls.add(control); installOutside(control); control.refresh(); return control;
  }

  function peopleChoice(source, options) {
    const label = fieldName(source), participant = source.name === "participant";
    const wrapper = element("div", "participants-field archive-people-field");
    source.before(wrapper); wrapper.append(source); source.hidden = true; source.tabIndex = -1;
    const value = element("div", "participants-value"), chips = element("div", "participant-chips");
    const toggle = button("button quiet small", "", "users"), caption = element("span", ""); toggle.append(caption); toggle.insertAdjacentHTML("beforeend", svg("chevron-down"));
    toggle.setAttribute("aria-haspopup", "dialog"); toggle.setAttribute("aria-expanded", "false"); toggle.setAttribute("aria-label", label);
    const popup = element("div", "participants-menu"); popup.id = `archive-people-${++sequence}`; popup.hidden = true; popup.setAttribute("role", "dialog"); popup.setAttribute("aria-label", label); toggle.setAttribute("aria-controls", popup.id);
    const heading = element("div", "participants-menu-heading"), close = button("close-button", "", "x"); close.setAttribute("aria-label", `Закрыть выбор: ${label}`); heading.append(element("strong", "", label), close);
    const search = element("input", ""); search.type = "search"; search.autocomplete = "off"; search.placeholder = "Найти по имени"; search.setAttribute("aria-label", `Поиск: ${label}`);
    const searchBox = element("div", "participant-search"); searchBox.append(search);
    const hint = element("p", "participants-hint", participant ? "Показываем чаты, где есть все выбранные участники." : "Показываем сообщения любого выбранного автора.");
    const list = element("ul", "participant-options"), status = element("div", "footnote"); list.setAttribute("aria-label", `Доступные: ${label}`); status.setAttribute("role", "status");
    value.append(chips, toggle); popup.append(heading, searchBox, hint, list, status); wrapper.append(value, popup);
    let timer, suppressFocus = false, fingerprint = "";
    const control = {source, options, wrapper, toggle, popup, close(focus = false) { clearTimeout(timer); popup.hidden = true; toggle.setAttribute("aria-expanded", "false"); if (focus) { suppressFocus = true; toggle.focus(); suppressFocus = false; } }, refresh() {
      const chosen = [...source.selectedOptions]; caption.textContent = chosen.length ? "Добавить" : participant ? "Выбрать участников" : "Выбрать авторов";
      chips.replaceChildren(); chosen.forEach(option => { const chip = element("span", "participant-chip"), remove = button("", "", "x"); remove.setAttribute("aria-label", `Убрать участника ${option.textContent}`); remove.onclick = () => { [...source.options].find(o => o.value === option.value).selected = false; changed(source); control.refresh(); }; chip.append(element("span", "", option.textContent), remove); chips.append(chip); });
      const q = search.value.trim().toLocaleLowerCase("ru-RU"), matches = [...source.options].filter(o => `${o.textContent} ${o.value}`.toLocaleLowerCase("ru-RU").includes(q));
      const next = JSON.stringify(matches.map(o => [o.value, o.textContent, o.selected, o.dataset.count]));
      if (next !== fingerprint) { fingerprint = next; list.replaceChildren(); matches.slice(0,100).forEach(option => {
        const row = element("li", ""), item = button("", ""); item.dataset.value = option.value; item.setAttribute("aria-pressed", String(option.selected)); item.setAttribute("aria-label", `${option.selected ? "Убрать" : "Добавить"} участника ${option.textContent}`);
        const person = element("span", "participant-person"); person.append(element("strong", "", option.textContent), element("small", "", `${participant ? "Чатов" : "Сообщений"}: ${Number(option.dataset.count) || 0}`)); item.append(person); item.insertAdjacentHTML("beforeend", svg(option.selected ? "check" : "plus"));
        item.onclick = () => { const current = [...source.options].find(o => o.value === item.dataset.value); if (!current) return; current.selected = !current.selected; changed(source); control.refresh(); search.focus(); }; row.append(item); list.append(row);
      }); }
      status.textContent = matches.length ? `Найдено: ${matches.length}. Выбрано: ${chosen.length}.${matches.length > 100 ? " Уточните поиск для остальных." : ""}` : "Участники не найдены. Обновите каталог чатов.";
    }};
    function open(focus = false) { clearTimeout(timer); closeOthers(control); popup.hidden = false; toggle.setAttribute("aria-expanded", "true"); control.refresh(); if (focus) search.focus(); }
    toggle.onclick = () => open(true); close.onclick = () => control.close(true);
    wrapper.addEventListener("pointerenter", () => open()); wrapper.addEventListener("pointerleave", () => { timer = setTimeout(() => { if (!wrapper.contains(document.activeElement)) control.close(); }, 120); });
    wrapper.addEventListener("focusin", () => { if (!suppressFocus) open(); }); wrapper.addEventListener("focusout", e => { if (e.relatedTarget && !wrapper.contains(e.relatedTarget)) control.close(); });
    wrapper.addEventListener("keydown", e => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); control.close(true); } else if (["ArrowDown","ArrowUp","Home","End"].includes(e.key)) { e.preventDefault(); open(); const items = [...list.querySelectorAll("button")], i = items.indexOf(document.activeElement); const n = e.key === "Home" ? 0 : e.key === "End" ? items.length-1 : Math.max(0, Math.min(items.length-1,i+(e.key === "ArrowDown" ? 1 : -1))); items[n]?.focus(); } });
    search.addEventListener("input", e => { e.stopPropagation(); control.refresh(); }); search.addEventListener("change", e => e.stopPropagation()); source.addEventListener("change", () => control.refresh());
    registered.set(source, control); controls.add(control); control.refresh(); return control;
  }

  const iso = date => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const parse = value => { const parts = String(value || "").split("-").map(Number); return parts.length === 3 && parts.every(Number.isFinite) && parts[0] > 0 ? new Date(parts[0], parts[1] - 1, parts[2]) : new Date(); };
  const dateLabel = value => value ? parse(value).toLocaleDateString("ru-RU") : "Не задана";
  function dateRange(mount, options = {}) {
    if (!mount) return null;
    if (registered.has(mount)) return registered.get(mount);
    const from = options.from, to = options.to;
    if (!from || !to) return null;
    const initialFrom = from.defaultValue, initialTo = to.defaultValue;
    from.type = "hidden"; to.type = "hidden";
    const wrapper = element("div", "period-control archive-date-control"); mount.append(wrapper);
    const toggle = button("button secondary archive-date-toggle", "", "calendar"); const caption = element("span", ""); toggle.append(caption); toggle.setAttribute("aria-haspopup", "dialog"); toggle.setAttribute("aria-expanded", "false");
    const popup = element("div", "period-menu archive-date-popup"); popup.hidden = true; popup.id = `archive-period-${++sequence}`; popup.setAttribute("role", "dialog"); popup.setAttribute("aria-label", options.label || "Период сообщений"); toggle.setAttribute("aria-controls", popup.id);
    const heading = element("div", "participants-menu-heading"); heading.append(element("strong", "", "Период")); const close = button("close-button", "", "x"); close.setAttribute("aria-label", "Закрыть календарь"); heading.append(close);
    const endpoints = element("div", "period-endpoints"); const start = button("", ""); const end = button("", ""); endpoints.append(start, end);
    const navigation = element("div", "calendar-navigation"); const previous = button("button quiet", "", "arrow-left"); previous.setAttribute("aria-label", "Предыдущий месяц"); const title = element("strong", ""); title.setAttribute("aria-live", "polite"); const next = button("button quiet", "", "chevron-right"); next.setAttribute("aria-label", "Следующий месяц"); navigation.append(previous, title, next);
    const weekdays = element("div", "calendar-weekdays"); ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].forEach(day => weekdays.append(element("span", "", day)));
    const days = element("div", "archive-calendar-days"); days.setAttribute("role", "grid"); days.setAttribute("aria-label", "Календарь");
    const hint = element("p", "participants-hint");
    const shortcuts = element("div", "calendar-shortcuts"); const monthButton = button("button quiet small", "Этот месяц"); const clear = button("button quiet small", "За всё время"); shortcuts.append(monthButton, clear);
    popup.append(heading, endpoints, navigation, weekdays, days, hint, shortcuts); wrapper.append(toggle, popup);
    let month = new Date(new Date().getFullYear(), new Date().getMonth(), 1), endpoint = "from", focused = "";
    const control = {wrapper, popup, toggle, source: from, close() { popup.hidden = true; toggle.setAttribute("aria-expanded", "false"); }, refresh() { render(); }};
    function emit() { changed(from); changed(to); options.onChange?.(); }
    function renderEndpoint(node, titleText, value, active) { node.replaceChildren(element("small", "", titleText), element("strong", "", dateLabel(value))); node.classList.toggle("active", active); }
    function render() {
      caption.textContent = from.value || to.value ? `${from.value ? dateLabel(from.value) : "Начало"} — ${to.value ? dateLabel(to.value) : "Сегодня"}` : "За всё время";
      renderEndpoint(start, "С даты", from.value, endpoint === "from"); renderEndpoint(end, "По дату", to.value, endpoint === "to");
      title.textContent = month.toLocaleDateString("ru-RU", {month: "long", year: "numeric"});
      hint.textContent = endpoint === "from" ? "Выберите дату начала." : "Выберите дату окончания. Период можно оставить открытым.";
      days.replaceChildren(); const today = iso(new Date()), monthKey = iso(month).slice(0, 7); const focus = focused.startsWith(monthKey) ? focused : today.startsWith(monthKey) ? today : iso(month);
      const offset = (month.getDay() + 6) % 7, count = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate(); const cells = Array.from({length: offset}, () => element("span", "calendar-blank"));
      for (let number = 1; number <= count; number++) {
        const date = new Date(month.getFullYear(), month.getMonth(), number), value = iso(date), chosen = value === from.value || value === to.value, inside = from.value && to.value && value > from.value && value < to.value;
        const cell = element("span", ""); cell.setAttribute("role", "gridcell"); cell.setAttribute("aria-selected", String(chosen || !!inside)); const day = button(`calendar-day${chosen ? " selected" : ""}${inside ? " in-range" : ""}${value === today ? " today" : ""}`, String(number)); day.dataset.archiveDay = value; day.tabIndex = value === focus ? 0 : -1; day.setAttribute("aria-label", date.toLocaleDateString("ru-RU", {day: "numeric", month: "long", year: "numeric"})); if (value === today) day.setAttribute("aria-current", "date"); day.onclick = () => select(value); cell.append(day); cells.push(cell);
      }
      while (cells.length % 7) cells.push(element("span", "calendar-blank"));
      for (let index = 0; index < cells.length; index += 7) { const row = element("div", "calendar-row"); row.setAttribute("role", "row"); row.append(...cells.slice(index, index + 7)); days.append(row); }
    }
    function select(value) { focused = value; if (endpoint === "from") { from.value = value; if (to.value && to.value < value) to.value = ""; endpoint = "to"; } else { to.value = value; if (from.value && from.value > value) from.value = value; endpoint = "from"; } render(); days.querySelector(`[data-archive-day="${value}"]`)?.focus(); emit(); }
    function shift(amount) { month = new Date(month.getFullYear(), month.getMonth() + amount, 1); focused = iso(month); render(); }
    function open() { closeOthers(control); const value = endpoint === "from" ? from.value : to.value; if (value) { const date = parse(value); month = new Date(date.getFullYear(), date.getMonth(), 1); focused = value; } render(); popup.hidden = false; toggle.setAttribute("aria-expanded", "true"); days.querySelector('button[tabindex="0"]')?.focus(); }
    toggle.onclick = () => popup.hidden ? open() : control.close(); close.onclick = () => { control.close(); toggle.focus(); }; previous.onclick = () => shift(-1); next.onclick = () => shift(1);
    start.onclick = () => { endpoint = "from"; render(); }; end.onclick = () => { endpoint = "to"; render(); };
    monthButton.onclick = () => { const now = new Date(); month = new Date(now.getFullYear(), now.getMonth(), 1); from.value = iso(month); to.value = iso(new Date(now.getFullYear(), now.getMonth() + 1, 0)); endpoint = "from"; render(); emit(); };
    clear.onclick = () => { from.value = ""; to.value = ""; endpoint = "from"; render(); emit(); };
    days.addEventListener("keydown", event => { const target = event.target.closest("[data-archive-day]"); if (!target || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) return; event.preventDefault(); const date = parse(target.dataset.archiveDay); const weekday = (date.getDay() + 6) % 7; if (event.key === "PageUp" || event.key === "PageDown") date.setMonth(date.getMonth() + (event.key === "PageUp" ? -1 : 1)); else date.setDate(date.getDate() + ({ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -weekday, End: 6 - weekday}[event.key])); focused = iso(date); month = new Date(date.getFullYear(), date.getMonth(), 1); render(); days.querySelector(`[data-archive-day="${focused}"]`)?.focus(); });
    from.addEventListener("change", () => render()); to.addEventListener("change", () => render());
    from.form?.addEventListener("reset", () => { from.value = initialFrom; to.value = initialTo; endpoint = "from"; focused = ""; setTimeout(() => render(), 0); });
    registered.set(mount, control); controls.add(control); installOutside(control); render(); return control;
  }

  function settings(root) {
    const form = root.querySelector("#settings-form"); if (!form || form.dataset.categoriesReady) return; form.dataset.categoriesReady = "true";
    const tabs = [...form.querySelectorAll("[data-settings-category]")], panels = [...form.querySelectorAll("[data-settings-panel]")];
    function show(name) { if (!panels.some(panel => panel.dataset.settingsPanel === name)) name = "automation"; closeOthers(); tabs.forEach(tab => { const active = tab.dataset.settingsCategory === name; tab.classList.toggle("active", active); tab.setAttribute("aria-pressed", String(active)); }); panels.forEach(panel => { panel.hidden = panel.dataset.settingsPanel !== name; }); form.dispatchEvent(new CustomEvent("settings-category-change", {bubbles:true, detail:name})); }
    form._archiveShowCategory = show;
    tabs.forEach(tab => { const name = tab.dataset.settingsCategory; tab.addEventListener("click", () => show(name)); tab.querySelector("[data-settings-icon]").innerHTML = svg(tab.dataset.settingsIcon); tab.querySelector(".settings-category-arrow").innerHTML = svg("chevron-right"); });
    form.addEventListener("invalid", event => { const panel = event.target.closest("[data-settings-panel]"); if (panel) { show(panel.dataset.settingsPanel); requestAnimationFrame(() => (registered.get(event.target)?.toggle || event.target).focus()); } }, true);
    form.querySelectorAll("[data-help-toggle]").forEach(toggle => { toggle.innerHTML = svg("help"); toggle.addEventListener("click", () => { const content = document.getElementById(toggle.dataset.helpToggle); if (!content) return; content.hidden = !content.hidden; toggle.setAttribute("aria-expanded", String(!content.hidden)); }); });
    const scope = form.elements.chat_scope;
    function scopeState() { const selected = form.querySelector("[data-chat-selected-scope]"); if (selected) selected.hidden = scope?.value !== "selected"; }
    scope?.addEventListener("change", scopeState); form.addEventListener("reset", () => setTimeout(() => { scopeState(); refresh(form); }, 0)); form._archiveScopeState = scopeState;
    show("automation"); scopeState();
  }
  function init(root = document, options = {}) {
    iconProvider = options.icon || iconProvider;
    root.querySelectorAll("#settings-form select, #ca-filters select, #page-module select, #filter-form select").forEach(source => {
      const exclusiveWith = source.id === "ca-selected-chats" ? "#ca-excluded-chats" : source.id === "ca-excluded-chats" ? "#ca-selected-chats" : undefined;
      choice(source, {exclusiveWith});
    });
    settings(root);
  }
  function refresh(root = document) { controls.forEach(control => { if (root === document || root.contains(control.wrapper)) control.refresh(); }); root.querySelector?.("#settings-form")?._archiveScopeState?.(); if (root.id === "settings-form") root._archiveScopeState?.(); }
  document.addEventListener("pointerdown", event => { controls.forEach(control => { if (!control.wrapper.contains(event.target)) control.close(); }); });
  document.addEventListener("reset", event => { setTimeout(() => refresh(event.target), 0); }, true);
  window.ArchiveControls = {init, refresh, choice, dateRange, showCategory(name) { document.querySelector("#settings-form")?._archiveShowCategory?.(name); }};
})();
