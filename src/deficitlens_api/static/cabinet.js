/* Justmedit — личный кабинет (ADR 0009; маршруты — docs/CONTRACTS.md, «Хранение и кабинет»; формат истории —
   «Анализ истории»). Одна страница-скрипт на три адреса: /login (вход и регистрация), /share (врач принимает ссылку
   пациента), /cabinet (кабинет пациента или врача).

   Правила:
   * Чистый JS без библиотек. Всё, что пришло от сервера, вставляется только как текст (textContent); разметку
     строками не собираем. Стили — классы из cabinet.css; положение на графике — атрибуты SVG и свойства DOM.
   * Браузер ничего не хранит: ни localStorage, ни sessionStorage, ни cookie из JS. Сессия — cookie jm_session
     (HttpOnly), её ставит сервер; токен из ответа входа не сохраняется и не используется.
   * Изменяющие запросы — с заголовком X-Justmedit: 1 и Content-Type: application/json (защита от подделки
     запроса, auth.check_csrf), fetch с credentials: "same-origin".
   * Тексты пациенту (события, причины, отчёты) приходят с сервера уже без диагнозов, лекарств и доз; подписи
     интерфейса здесь — нейтральные. Пациенту не показываем проценты изменения (фильтр запрещённых слов, «%»).
   * Код доступа врача (/share#код) живёт только в памяти страницы и уходит на сервер телом POST-запроса. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var ROLE_RU = { patient: "Пациент", doctor: "Врач" };
  var SOURCE_RU = { form: "Вручную", photo: "Фото", pdf: "PDF", file: "Файл", text: "Текст", "import": "Импорт" };
  var FEED_PAGE = 10;                     // анализов в ленте за раз: дальше — «Показать ещё»
  var MON_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  var SVG_NS = "http://www.w3.org/2000/svg";

  // ---------- помощники DOM ----------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    setAttrs(node, attrs);
    append(node, children);
    return node;
  }
  function svg(tag, attrs, children) {
    var node = document.createElementNS(SVG_NS, tag);
    setAttrs(node, attrs);
    append(node, children);
    return node;
  }
  function setAttrs(node, attrs) {
    if (!attrs) { return; }
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) { return; }
      if (k === "class") { node.setAttribute("class", v); }
      else if (k === "text") { node.textContent = v; }
      else { node.setAttribute(k, v === true ? "" : String(v)); }
    });
  }
  function append(node, children) {
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) { return; }
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
  }
  function clear(node) { while (node && node.firstChild) { node.removeChild(node.firstChild); } }
  function on(node, ev, fn) { if (node) { node.addEventListener(ev, fn); } }
  function smooth() { return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"; }
  function scrollToNode(node) {
    if (!node) { return; }
    var top = document.querySelector(".top");
    var off = top && window.getComputedStyle(top).position === "sticky" ? top.offsetHeight + 16 : 12;
    var toc = Array.prototype.filter.call(document.querySelectorAll(".cab-toc"), function (t) { return t.offsetParent !== null; })[0];
    if (toc) { off += toc.offsetHeight + 12; }                     // закреплённое оглавление кабинета
    window.scrollTo({ top: Math.max(0, node.getBoundingClientRect().top + window.pageYOffset - off), behavior: smooth() });
  }

  // ---------- числа, даты, слова ----------
  var nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
  var nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
  function num(x) { return (x === null || x === undefined || isNaN(x)) ? "—" : nf.format(x); }
  function rub(x) { return "≈ " + nf0.format(x) + " ₽"; }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }
  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    return (m10 === 1 && m100 !== 11) ? one : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) ? few : many;
  }
  function count(n, one, few, many) { return n + " " + plural(n, one, few, many); }
  function isoParts(iso) { var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ""); return m ? [+m[1], +m[2], +m[3]] : null; }
  function ruDate(iso) { var p = isoParts(iso); return p ? pad(p[2]) + "." + pad(p[1]) + "." + p[0] : ""; }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function ruDateTime(iso) {               // «2026-10-04T18:00:00Z» -> местное «04.10.2026, 21:00»
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return ""; }
    return pad(d.getDate()) + "." + pad(d.getMonth() + 1) + "." + d.getFullYear() + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function localDay(iso) { var d = new Date(iso); return isNaN(d.getTime()) ? "" : pad(d.getDate()) + "." + pad(d.getMonth() + 1) + "." + d.getFullYear(); }
  function todayIso() { var d = new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function dayMs(iso) { var p = isoParts(iso); return p ? Date.UTC(p[0], p[1] - 1, p[2]) : NaN; }
  function daysBetween(a, b) { return Math.round((dayMs(b) - dayMs(a)) / 86400000); }
  // Промежуток словами — везде одинаково полными словами: «в один день», «за 12 дней», «за 6 месяцев»,
  // «за 1,5 года».
  function periodText(days) {
    if (!(days > 0)) { return "в один день"; }
    if (days < 45) { return "за " + count(days, "день", "дня", "дней"); }
    var months = Math.round(days / 30.4375);
    if (months < 24) { return "за " + count(months, "месяц", "месяца", "месяцев"); }
    var years = Math.round(days / 365.25 * 2) / 2;
    return "за " + nf.format(years) + " " + (years % 1 ? "года" : plural(years, "год", "года", "лет"));
  }
  function agoText(iso) {
    var d = daysBetween(iso, todayIso());
    if (isNaN(d)) { return ""; }
    if (d <= 0) { return "сегодня"; }
    if (d === 1) { return "вчера"; }
    if (d < 45) { return count(d, "день", "дня", "дней") + " назад"; }
    var m = Math.round(d / 30.4375);
    if (m < 24) { return count(m, "месяц", "месяца", "месяцев") + " назад"; }
    return Math.round(d / 365.25) + " " + plural(Math.round(d / 365.25), "год", "года", "лет") + " назад";
  }
  function daysLeft(iso) { var ms = new Date(iso).getTime() - Date.now(); return isNaN(ms) ? null : Math.max(0, Math.ceil(ms / 86400000)); }
  function parseNum(text) {
    var t = String(text).trim().replace(/\s+/g, "").replace(",", ".");
    return /^\d+(\.\d+)?$/.test(t) ? parseFloat(t) : NaN;
  }
  function initials(login) { var s = String(login || "?").replace(/[^A-Za-zА-Яа-яЁё0-9]/g, ""); return (s.charAt(0) || "?").toUpperCase(); }

  // ---------- запросы ----------
  // Ответ всегда {status, ok, data, retry}; ошибки сети — status 0; retry — заголовок Retry-After (429, 503).
  // Изменяющие запросы — с X-Justmedit (CSRF).
  // В кабинете 401 (кроме /auth/*) значит: сеанс закрыт — «выйти везде» или смена пароля на другом устройстве,
  // истёк срок. Тогда не показываем ошибку, а ведём на вход с плашкой «Сеанс завершён» (onUnauthorized).
  var onUnauthorized = null;
  function guard(url) {
    return function (r) {
      if (r.status === 401 && onUnauthorized && !/^\/api\/v1\/auth\//.test(url)) {
        onUnauthorized();
        return new Promise(function () {});                // страница уходит на вход: ответ дальше не нужен
      }
      return r;
    };
  }
  function api(method, url, body) {
    var headers = { "Accept": "application/json" };
    var opts = { method: method, headers: headers, credentials: "same-origin", cache: "no-store" };
    if (method !== "GET") { headers["X-Justmedit"] = "1"; }
    if (body !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(readResp, netError).then(guard(url));
  }
  function apiForm(url, fd, csrf) {
    var headers = { "Accept": "application/json" };
    if (csrf) { headers["X-Justmedit"] = "1"; }
    return fetch(url, { method: "POST", body: fd, headers: headers, credentials: "same-origin", cache: "no-store" }).then(readResp, netError).then(guard(url));
  }
  // История считается под слотом тяжёлых расчётов, если кеш устарел: занято — 503 service_busy с Retry-After.
  // Один автоповтор через Retry-After секунд (не дольше 15 с); не вышло — карточки покажут «Повторить».
  function getHistory(url) {
    return api("GET", url).then(function (r) {
      if (r.status !== 503) { return r; }
      var sec = Math.min(retrySec(r) || 3, 15);
      return new Promise(function (resolve) { setTimeout(resolve, sec * 1000); }).then(function () { return api("GET", url); });
    });
  }
  function sessionEnded() {
    onUnauthorized = null;
    setNav(null);
    var here = window.location.pathname + (window.location.hash || "");
    var go = function () { window.location.replace("/login?next=" + encodeURIComponent(here) + "&reason=expired"); };
    // выход стирает недействительную cookie: страница входа сразу рисует в шапке «Войти», без мигания
    api("POST", "/api/v1/auth/logout").then(go, go);
  }
  function readResp(resp) {
    return resp.text().then(function (text) {
      var data = null;
      try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
      return { status: resp.status, ok: resp.ok, data: data, retry: resp.headers.get("Retry-After") };
    });
  }
  function netError() { return { status: 0, ok: false, data: null, retry: null }; }
  function errCode(r) { return (r && r.data && r.data.error && r.data.error.code) || ""; }
  function errField(r) { return (r && r.data && r.data.error && r.data.error.field) || ""; }
  // Retry-After в секундах (сервер шлёт число) или null.
  function retrySec(r) { var n = r && r.retry ? parseInt(r.retry, 10) : NaN; return n > 0 ? n : null; }
  function waitText(sec) {
    if (!sec) { return "минуту"; }
    if (sec < 90) { return sec + " с"; }
    if (sec < 5400) { return Math.ceil(sec / 60) + " мин"; }
    return Math.ceil(sec / 3600) + " ч";
  }
  // Понятные тексты для ошибок пределов и нагрузки (коды — docs/CONTRACTS.md, «Безопасность кабинета»); для
  // остальных — сообщение сервера. Значения анализов в сообщениях не участвуют.
  var ERR_TEXT = {
    record_too_large: "Анализ слишком большой для сохранения в кабинете. Уберите лишние показатели или референсы бланка и сохраните снова.",
    too_many_tokens: "Токенов API уже максимум. Отзовите ненужный токен в списке ниже и создайте новый.",
    storage_full: "Хранилище сервиса заполнено: новые данные сейчас не принимаются. Просмотр, выгрузка и удаление работают — попробуйте позже.",
    signups_paused: "Регистрация временно приостановлена: на сегодня создано слишком много аккаунтов. Попробуйте позже — вход в существующий аккаунт работает.",
    service_busy: "Сервис занят, повторите через несколько секунд.",
    unknown_analyte: "В анализе есть показатель, которого нет в справочнике сервиса. Кабинет хранит только известные показатели — проверьте названия."
  };
  // what уточняет текст 429: "login" — вход, "register" — регистрация, "password" — пароль в форме или диалоге.
  function errText(r, what) {
    var code = errCode(r);
    if (r && r.status === 429) {
      var wait = waitText(retrySec(r));
      if (what === "login") { return "Слишком много попыток входа. Подождите " + wait + " и попробуйте снова."; }
      if (what === "register") { return "Слишком много попыток регистрации с вашего адреса. Подождите " + wait + " и попробуйте снова."; }
      if (what === "password") { return "Слишком много попыток ввода пароля. Подождите " + wait + " и попробуйте снова."; }
      return "Слишком много запросов. Повторите через " + wait + ".";
    }
    if (code === "quota_exceeded") {
      return C.me && C.me.role === "doctor" ? "В кабинете закончилось место для ваших данных. Удалите ненужные наборы референсов и повторите."
        : "В кабинете закончилось место для ваших данных. Удалите старые анализы и повторите.";
    }
    if (code === "unknown_analyte") {
      // референсы врача (файл или редактор): сервер называет строку и показатель — его текст точнее общего
      if (/^(file|ranges)\b/.test(errField(r)) && r.data.error.message) { return r.data.error.message; }
      var m = /^records\[(\d+)\]/.exec(errField(r));               // загрузка выгрузки: номер анализа в файле
      return (m ? "Анализ № " + (parseInt(m[1], 10) + 1) + " в файле: " : "") + ERR_TEXT.unknown_analyte;
    }
    if (ERR_TEXT[code]) { return ERR_TEXT[code]; }
    if (r && r.data && r.data.error && r.data.error.message) { return r.data.error.message; }
    if (!r || r.status === 0) { return "Нет связи с сервисом. Проверьте соединение и повторите."; }
    if (r.status === 413) { return "Слишком большой файл или запрос."; }
    if (r.status === 503) { return ERR_TEXT.service_busy; }
    return "Сервис не ответил (код " + r.status + "). Повторите.";
  }
  // Кнопка недоступна, пока идёт пауза сервера (Retry-After): «Повторить через N с».
  function cooldown(btn, sec, onEnd) {
    if (!btn || !sec) { return; }
    var label = btn.getAttribute("data-label") || btn.textContent, left = Math.min(sec, 3600);
    btn.disabled = true;
    var tick = function () {
      if (left <= 0) { btn.disabled = false; btn.textContent = label; if (onEnd) { onEnd(); } return; }
      btn.textContent = "Повторить через " + waitText(left);
      left -= 1;
      setTimeout(tick, 1000);
    };
    tick();
  }

  // ---------- общие элементы интерфейса ----------
  function showAlert(node, text) { if (!node) { return; } node.textContent = text || ""; node.hidden = !text; }
  function fieldError(input, errNode, text) {
    if (input) { input.setAttribute("aria-invalid", "true"); }
    if (errNode) { errNode.textContent = text; errNode.hidden = false; }
    for (var d = input && input.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) { d.open = true; }
    if (input && input.focus) { input.focus(); }
  }
  function clearErrors(root) {
    Array.prototype.forEach.call(root.querySelectorAll("[aria-invalid]"), function (i) { i.removeAttribute("aria-invalid"); });
    Array.prototype.forEach.call(root.querySelectorAll(".field__error"), function (p) { p.hidden = true; p.textContent = ""; });
  }
  function busy(btn, on, text) {
    if (!btn) { return; }
    if (on) { btn.setAttribute("data-label", btn.textContent); btn.textContent = text || "Подождите…"; btn.disabled = true; btn.classList.add("is-busy"); }
    else { if (btn.getAttribute("data-label")) { btn.textContent = btn.getAttribute("data-label"); } btn.disabled = false; btn.classList.remove("is-busy"); }
  }
  var EYE = "M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z";
  function eyeIcon(open) {
    return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false", class: "ico" }, [
      svg("path", { d: EYE, fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linejoin": "round" }),
      svg("circle", { cx: "12", cy: "12", r: "3", fill: "none", stroke: "currentColor", "stroke-width": "1.7" }),
      open ? null : svg("path", { d: "M4 20 20 4", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round" })
    ]);
  }
  // Кнопка «показать / скрыть пароль» у каждого .pw__eye.
  function initPasswordToggles(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll(".pw__eye"), function (b) {
      var input = $(b.getAttribute("data-for"));
      if (!input) { return; }
      var paint = function () {
        var shown = input.type === "text";
        clear(b); b.appendChild(eyeIcon(!shown));
        b.setAttribute("aria-pressed", shown ? "true" : "false");
        b.setAttribute("aria-label", shown ? "Скрыть пароль" : "Показать пароль");
      };
      b.addEventListener("click", function () { input.type = input.type === "password" ? "text" : "password"; paint(); input.focus(); });
      paint();
    });
  }
  var toastTimer = null;
  function toast(text, kind) {
    var t = $("cab-toast");
    if (!t) { return; }
    t.textContent = text;
    t.className = "toast" + (kind ? " toast--" + kind : "");
    t.hidden = false;
    void t.offsetWidth;
    t.classList.add("is-on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("is-on"); setTimeout(function () { t.hidden = true; }, 250); }, 2800);
  }
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) { return navigator.clipboard.writeText(text); }
    return new Promise(function (resolve, reject) {
      var ta = el("textarea", { class: "vh", readonly: true });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      if (ok) { resolve(); } else { reject(new Error("copy")); }
    });
  }
  // Выделить текст поля или элемента — запасной путь, если браузер не дал скопировать в буфер.
  function selectNode(node) {
    if (!node) { return; }
    if (typeof node.select === "function") { node.focus(); node.select(); return; }
    var sel = window.getSelection && window.getSelection();
    if (!sel || !document.createRange) { return; }
    var range = document.createRange();
    range.selectNodeContents(node);
    sel.removeAllRanges();
    sel.addRange(range);
  }
  // target — поле или элемент с текстом: при отказе буфера обмена текст в нём выделяется для Ctrl+C.
  function copyButton(getText, label, target) {
    var b = el("button", { type: "button", class: "btn btn--small btn--copy", text: label || "Копировать" });
    b.addEventListener("click", function () {
      copyText(getText()).then(function () {
        b.textContent = "Скопировано"; b.classList.add("is-done"); toast("Скопировано в буфер обмена", "ok");
        setTimeout(function () { b.textContent = label || "Копировать"; b.classList.remove("is-done"); }, 1800);
      }, function () {
        selectNode(target);
        toast(target ? "Не удалось скопировать — текст выделен: нажмите Ctrl+C (⌘+C на Mac)" : "Не удалось скопировать — выделите текст и скопируйте вручную", "warn");
      });
    });
    return b;
  }
  function yandexSearch(name) { return "https://yandex.ru/maps/?text=" + encodeURIComponent("сдать анализ " + String(name).toLowerCase()); }
  function extLink(href, text, cls) {
    return el("a", { href: href, target: "_blank", rel: "noopener noreferrer", class: "ext" + (cls ? " " + cls : "") },
      [text, el("span", { class: "ext__arrow", "aria-hidden": "true", text: "↗" })]);
  }
  function chip(text, kind) { return el("span", { class: "chip2" + (kind ? " chip2--" + kind : ""), text: text }); }
  // Пустое состояние: иконка, заголовок, подсказка «что сделать», кнопка.
  var ICONS = {
    chart: "M4 19h16M6 15l4-5 3 3 5-7",
    list: "M8 7h11M8 12h11M8 17h11M4.5 7h.01M4.5 12h.01M4.5 17h.01",
    doc: "M7.5 3.5h6L18 8v11a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 19V5a1.5 1.5 0 0 1 1.5-1.5zM13.5 3.5V8H18",
    people: "M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20a6.5 6.5 0 0 1 13 0M16 4.5a3.5 3.5 0 0 1 0 6.5M18 14a5.5 5.5 0 0 1 3.5 5.5",
    check: "m5 12.5 4.5 4.5L19 7.5",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    table: "M4 5h16v14H4zM4 10h16M10 10v9",
    lock: "M7.5 10.5V8a4.5 4.5 0 0 1 9 0v2.5M5.5 10.5h13v10h-13zM12 14.5v2.5",
    eye: EYE
  };
  function icon(name, cls) {
    return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false", class: cls || "ico" }, [
      svg("path", { d: ICONS[name] || ICONS.list, fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round" })]);
  }
  function emptyState(iconName, title, text, action) {
    return el("div", { class: "empty-state" }, [
      el("span", { class: "empty-state__ico" }, [icon(iconName)]),
      el("p", { class: "empty-state__t", text: title }),
      text ? el("p", { class: "empty-state__p", text: text }) : null,
      action || null
    ]);
  }
  function linkButton(text, onClick, cls) {
    var b = el("button", { type: "button", class: "btn " + (cls || ""), text: text });
    b.addEventListener("click", onClick);
    return b;
  }
  // История не загрузилась (чаще всего 503 service_busy — заняты слоты тяжёлых расчётов): текст и «Повторить».
  // again() -> Promise: повторный запрос перерисовывает карточку сам.
  function retryButton(again, cls) {
    var b = el("button", { type: "button", class: "btn btn--small " + (cls || ""), text: "Повторить" });
    b.addEventListener("click", function () { busy(b, true, "Загружаем…"); Promise.resolve(again()).then(function () { busy(b, false); }); });
    return b;
  }
  function historyRetry(r, again) {
    return emptyState("chart", errCode(r) === "service_busy" ? "Сервис занят" : "История не загрузилась", errText(r),
      retryButton(again, "btn--primary"));
  }
  // Диалог подтверждения (нативный <dialog>): заголовок, текст, необязательное поле пароля, кнопки.
  // o.ack — обязательная отметка «понимаю»; o.option — необязательная отметка (её значение уходит в onOk).
  // onOk(пароль, отметка option) -> Promise<текст ошибки | null>; null — успех, диалог закрывается.
  function openDialog(o) {
    var dlg = $("cab-dialog");
    if (!dlg || !dlg.showModal) {                         // очень старый браузер: обычное подтверждение
      if (window.confirm(o.title + "\n\n" + (o.text || ""))) { o.onOk(o.password ? window.prompt("Пароль") || "" : null, false); }
      return;
    }
    clear(dlg);
    dlg.className = "dlg" + (o.danger ? " dlg--danger" : "");
    var err = el("p", { class: "field__error", id: "dlg-err", role: "alert", hidden: true });
    var pw = null, ack = null, opt = null;
    var body = [el("h2", { class: "dlg__t", id: "cab-dialog-t", text: o.title })];
    (Array.isArray(o.text) ? o.text : [o.text]).forEach(function (t) { if (t) { body.push(el("p", { class: "dlg__p", text: t })); } });
    if (o.list) { body.push(el("ul", { class: "dlg__list" }, o.list.map(function (t) { return el("li", { text: t }); }))); }
    if (o.password) {
      pw = el("input", { class: "fx__input", id: "dlg-pw", type: "password", autocomplete: "current-password", maxlength: "128", "aria-describedby": "dlg-err" });
      body.push(el("div", { class: "fx" }, [el("label", { class: "fx__label", for: "dlg-pw", text: "Пароль от аккаунта" }),
        el("div", { class: "pw" }, [pw, el("button", { type: "button", class: "pw__eye", "data-for": "dlg-pw", "aria-pressed": "false", "aria-label": "Показать пароль" })])]));
    }
    if (o.option) {
      opt = el("input", { type: "checkbox", id: "dlg-opt" });
      body.push(el("label", { class: "consent consent--dlg", for: "dlg-opt" }, [opt, el("span", { text: o.option })]));
    }
    if (o.ack) {
      ack = el("input", { type: "checkbox", id: "dlg-ack" });
      body.push(el("label", { class: "consent consent--dlg", for: "dlg-ack" }, [ack, el("span", { text: o.ack })]));
    }
    body.push(err);
    var cancel = el("button", { type: "button", class: "btn", text: o.cancelText || "Отмена" });
    var ok = el("button", { type: "submit", class: "btn " + (o.danger ? "btn--danger" : "btn--primary"), text: o.okText || "Подтвердить" });
    body.push(el("div", { class: "dlg__btns" }, [cancel, ok]));
    var form = el("form", { class: "dlg__form", method: "dialog", novalidate: true }, body);
    dlg.appendChild(form);
    initPasswordToggles(dlg);
    // Пока идёт запрос, диалог не закрывается ни «Отменой», ни Esc: ответ (и текст ошибки) остаются в нём.
    dlg.jmBusy = false;
    if (!dlg.jmWired) {
      dlg.jmWired = true;
      dlg.addEventListener("cancel", function (e) { if (dlg.jmBusy) { e.preventDefault(); } });
    }
    cancel.addEventListener("click", function () { if (!dlg.jmBusy) { dlg.close(); } });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (dlg.jmBusy) { return; }
      err.hidden = true;
      if (pw && !pw.value) { fieldError(pw, err, "Введите пароль."); return; }
      if (ack && !ack.checked) { err.textContent = "Отметьте, что понимаете последствия."; err.hidden = false; ack.focus(); return; }
      busy(ok, true);
      cancel.disabled = true;
      dlg.jmBusy = true;
      Promise.resolve(o.onOk(pw ? pw.value : null, opt ? opt.checked : false)).then(function (msg) {
        dlg.jmBusy = false;
        busy(ok, false);
        cancel.disabled = false;
        if (msg) { fieldError(pw, err, msg); if (!pw) { ok.focus(); } return; }
        dlg.close();
      });
    });
    dlg.showModal();
    (pw || cancel).focus();
  }
  function safeNext(raw) {
    // Переход после входа: только свой путь — начинается с «/», не «//» и не «/\», тот же origin.
    if (!raw || typeof raw !== "string" || raw.charAt(0) !== "/" || raw.charAt(1) === "/" || raw.charAt(1) === "\\") { return null; }
    try {
      var u = new URL(raw, window.location.origin);
      if (u.origin !== window.location.origin || /^\/login\b/.test(u.pathname)) { return null; }
      return u.pathname + u.search + u.hash;
    } catch (e) { return null; }
  }
  function meReady() { return (window.JMNav && window.JMNav.me) ? window.JMNav.me : Promise.resolve(null); }
  function setNav(user) { if (window.JMNav && window.JMNav.set) { window.JMNav.set(user); } }
  function logout() {
    return api("POST", "/api/v1/auth/logout").then(function () { setNav(null); window.location.href = "/"; });
  }
  // Проверка полей входа и регистрации на странице — те же правила, что у сервера (auth.py): буквы одного
  // алфавита (латиница или кириллица, не вперемешку), цифры и «._-@».
  var LOGIN_RE = /^[A-Za-zА-Яа-яЁё0-9._@-]{3,64}$/;
  var LOGIN_RULE = "Логин — от 3 до 64 символов: буквы одного алфавита (латиница или кириллица), цифры и знаки «._-@».";
  function loginOk(login) { return LOGIN_RE.test(login) && !(/[A-Za-z]/.test(login) && /[А-Яа-яЁё]/.test(login)); }
  // Плашка на странице входа: текст только фиксированный, по коду из адреса (?reason=…), сам адрес не выводится.
  var REASON_TEXT = {
    expired: "Сеанс завершён — войдите снова. Так бывает после «Выйти на всех устройствах», смены пароля на другом устройстве или через 7 дней после входа.",
    deleted: "Аккаунт и все его данные удалены. Восстановить их нельзя; новый аккаунт можно создать в любой момент."
  };

  // =================================================================================================
  // /login — вход и регистрация
  // =================================================================================================
  function initLogin() {
    var params = new URLSearchParams(window.location.search);
    var next = safeNext(params.get("next")) || "/cabinet";
    var go = function () { window.location.href = next; };
    var reason = params.get("reason");
    if (REASON_TEXT.hasOwnProperty(reason) && $("auth-note")) {
      $("auth-note").textContent = REASON_TEXT[reason];
      $("auth-note").hidden = false;
      if (reason === "deleted") { $("auth-note").classList.add("auth-note--ok"); }
    }
    function selectTab(name, focus) {
      ["login", "register"].forEach(function (n) {
        var t = $("tab-" + n), on = n === name;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        $("pane-" + n).hidden = !on;
        if (on && focus) { t.focus(); }
      });
      showAlert($("auth-alert"), "");
    }
    ["login", "register"].forEach(function (n) {
      on($("tab-" + n), "click", function () { selectTab(n, false); });
      on($("tab-" + n), "keydown", function (e) {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); selectTab(n === "login" ? "register" : "login", true); }
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-goto]"), function (b) {
      b.addEventListener("click", function () { selectTab(b.getAttribute("data-goto"), false); $(b.getAttribute("data-goto") === "login" ? "login-login" : "reg-login").focus(); });
    });
    initPasswordToggles(document);
    var syncRole = function () {
      var doctor = document.querySelector('#pane-register input[name="role"]:checked').value === "doctor";
      $("reg-clinic-box").hidden = !doctor;
      $("reg-submit").textContent = doctor ? "Создать кабинет врача" : "Создать кабинет";
    };
    Array.prototype.forEach.call(document.querySelectorAll('#pane-register input[name="role"]'), function (r) { r.addEventListener("change", syncRole); });
    if (params.get("role") === "doctor") { document.querySelector('#pane-register input[value="doctor"]').checked = true; }
    syncRole();
    if (params.get("mode") === "register") { selectTab("register", false); }
    // Индикатор длины пароля: короче 8 — красный, 8–11 — янтарный, от 12 — мятный.
    on($("reg-password"), "input", function () {
      var n = $("reg-password").value.length, bar = $("pw-meter");
      bar.className = "pw-meter__bar" + (n === 0 ? "" : n < 8 ? " is-weak" : n < 12 ? " is-ok" : " is-strong");
    });
    Array.prototype.forEach.call(document.querySelectorAll(".auth-form input"), function (i) {
      i.addEventListener("input", function () { i.removeAttribute("aria-invalid"); var e = $("err-" + i.id); if (e) { e.hidden = true; } });
    });

    // Уже вошли — предложить кабинет (или вернуться туда, откуда пришли).
    meReady().then(function (user) {
      if (!user) { return; }
      $("pane-login").hidden = true; $("pane-register").hidden = true;
      document.querySelector(".auth-tabs").hidden = true;
      $("auth-signed-name").textContent = user.login + " (" + (ROLE_RU[user.role] || "").toLowerCase() + ")";
      $("auth-signed-go").setAttribute("href", next);
      $("auth-signed").hidden = false;
    });
    on($("auth-signed-out"), "click", function () {
      api("POST", "/api/v1/auth/logout").then(function () { setNav(null); window.location.reload(); });
    });

    on($("pane-login"), "submit", function (e) {
      e.preventDefault();
      var form = $("pane-login");
      clearErrors(form); showAlert($("auth-alert"), "");
      var login = $("login-login").value.trim(), password = $("login-password").value;
      if (!login) { fieldError($("login-login"), $("err-login-login"), "Введите логин."); return; }
      if (!password) { fieldError($("login-password"), $("err-login-password"), "Введите пароль."); return; }
      busy($("login-submit"), true, "Входим…");
      api("POST", "/api/v1/auth/login", { login: login, password: password }).then(function (r) {
        if (r.ok && r.data && r.data.user) { setNav(r.data.user); go(); return; }
        busy($("login-submit"), false);
        if (r.status === 401) { fieldError($("login-password"), $("err-login-password"), errText(r)); $("login-password").select(); return; }
        showAlert($("auth-alert"), errText(r, "login"));
        // пауза входа (429 с Retry-After): кнопка недоступна до её конца
        if (r.status === 429) { cooldown($("login-submit"), retrySec(r), function () { showAlert($("auth-alert"), ""); }); }
      });
    });

    on($("pane-register"), "submit", function (e) {
      e.preventDefault();
      var form = $("pane-register");
      clearErrors(form); showAlert($("auth-alert"), "");
      var role = document.querySelector('#pane-register input[name="role"]:checked').value;
      var login = $("reg-login").value.trim(), password = $("reg-password").value, clinic = $("reg-clinic").value.trim();
      if (!loginOk(login)) { fieldError($("reg-login"), $("err-reg-login"), LOGIN_RULE); return; }
      if (password.length < 8) { fieldError($("reg-password"), $("err-reg-password"), "Пароль — не меньше 8 символов."); return; }
      if (!$("reg-consent").checked) { fieldError($("reg-consent"), $("err-reg-consent"), "Без согласия кабинет не создать: данные хранятся на сервере."); return; }
      var body = { login: login, password: password, role: role, consent: true };
      if (role === "doctor" && clinic) { body.clinic = clinic; }
      busy($("reg-submit"), true, "Создаём кабинет…");
      api("POST", "/api/v1/auth/register", body).then(function (r) {
        if (r.ok && r.data && r.data.user) { setNav(r.data.user); go(); return; }
        busy($("reg-submit"), false);
        var map = { login: "reg-login", password: "reg-password", clinic: "reg-clinic", consent: "reg-consent" };
        var f = errCode(r) === "login_taken" ? "login" : errField(r);
        if (map[f]) { fieldError($(map[f]), $("err-" + map[f]), errCode(r) === "login_taken" ? "Такой логин уже занят — выберите другой." : errText(r)); return; }
        showAlert($("auth-alert"), errText(r, "register"));
      });
    });
  }

  // =================================================================================================
  // /share#код — врач принимает ссылку пациента
  // =================================================================================================
  function initShare() {
    // Код живёт только в памяти страницы. Новую ссылку вставляют в адрес на этой же странице — меняется только
    // «#…», страница не перезагружается: по hashchange код перечитывается и проверка запускается заново.
    function readToken() {
      var raw = window.location.hash.replace(/^#/, "");
      return /^[A-Za-z0-9_-]{16,200}$/.test(raw) ? raw : null;
    }
    var token = readToken();
    var states = ["share-wait", "share-login", "share-ok", "share-patient", "share-bad"];
    function show(id) { states.forEach(function (s) { $(s).hidden = s !== id; }); }
    function forget() {                              // код одноразовый: после ответа сервера убираем его из адреса
      token = null;
      if (window.history && window.history.replaceState) { window.history.replaceState(null, "", "/share"); }
    }
    // home — кнопка «На главную» вместо «В кабинет» (когда в ссылке нет кода)
    function bad(title, text, home) {
      show("share-bad");
      $("share-bad-t").textContent = title;
      $("share-bad-p").textContent = text;
      $("share-bad-go").setAttribute("href", home ? "/" : "/cabinet");
      $("share-bad-go").textContent = home ? "На главную" : "В кабинет";
      $("share-bad-t").focus();
    }
    function patientView(user) {
      show("share-patient");
      $("share-patient-name").textContent = user.login;
    }
    function accept() {
      show("share-wait");
      if (!token) { bad("Ссылка уже использована", "Обновите кабинет — доступ, если он был открыт, уже там."); return; }
      api("POST", "/api/v1/shares/accept", { token: token }).then(function (r) {
        if (r.ok && r.data && r.data.patient) {
          forget();
          show("share-ok");
          var exp = localDay(r.data.expires);
          $("share-ok-t").textContent = "Доступ к истории " + r.data.patient.login + " открыт" + (exp ? " до " + exp : "");
          $("share-ok-go").setAttribute("href", "/cabinet#p-" + r.data.patient.id);
          $("share-ok-t").focus();
          return;
        }
        if (r.status === 401) { show("share-login"); return; }
        if (r.status === 403 && errCode(r) === "forbidden") { meReady().then(function (u) { if (u) { patientView(u); } else { show("share-login"); } }); return; }
        if (errCode(r) === "share_invalid") { forget(); bad("Ссылка недействительна", errText(r)); return; }
        bad("Не удалось открыть доступ", errText(r) + " Откройте ссылку ещё раз из сообщения пациента.");
      });
    }
    initPasswordToggles(document);
    on($("share-patient-out"), "click", function () {
      api("POST", "/api/v1/auth/logout").then(function () { setNav(null); show("share-login"); $("sl-login").focus(); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".auth-form input"), function (i) {
      i.addEventListener("input", function () { i.removeAttribute("aria-invalid"); var e = $("err-" + i.id); if (e) { e.hidden = true; } });
    });
    function afterAuth(r, btn) {
      if (r.ok && r.data && r.data.user) {
        setNav(r.data.user);
        if (r.data.user.role !== "doctor") { busy(btn, false); patientView(r.data.user); return true; }
        accept();
        return true;
      }
      busy(btn, false);
      return false;
    }
    on($("share-login-form"), "submit", function (e) {
      e.preventDefault();
      clearErrors($("share-login-form")); showAlert($("share-alert"), "");
      var login = $("sl-login").value.trim(), password = $("sl-password").value;
      if (!login) { fieldError($("sl-login"), $("err-sl-login"), "Введите логин."); return; }
      if (!password) { fieldError($("sl-password"), $("err-sl-password"), "Введите пароль."); return; }
      busy($("sl-submit"), true, "Входим…");
      api("POST", "/api/v1/auth/login", { login: login, password: password }).then(function (r) {
        if (afterAuth(r, $("sl-submit"))) { return; }
        if (r.status === 401) { fieldError($("sl-password"), $("err-sl-password"), errText(r)); return; }
        showAlert($("share-alert"), errText(r, "login"));
        if (r.status === 429) { cooldown($("sl-submit"), retrySec(r), function () { showAlert($("share-alert"), ""); }); }
      });
    });
    on($("share-reg-form"), "submit", function (e) {
      e.preventDefault();
      clearErrors($("share-reg-form")); showAlert($("share-alert"), "");
      var login = $("sr-login").value.trim(), password = $("sr-password").value, clinic = $("sr-clinic").value.trim();
      if (!loginOk(login)) { fieldError($("sr-login"), $("err-sr-login"), LOGIN_RULE); return; }
      if (password.length < 8) { fieldError($("sr-password"), $("err-sr-password"), "Пароль — не меньше 8 символов."); return; }
      if (!$("sr-consent").checked) { fieldError($("sr-consent"), $("err-sr-consent"), "Без согласия кабинет не создать."); return; }
      var body = { login: login, password: password, role: "doctor", consent: true };
      if (clinic) { body.clinic = clinic; }
      busy($("sr-submit"), true, "Создаём кабинет…");
      api("POST", "/api/v1/auth/register", body).then(function (r) {
        if (afterAuth(r, $("sr-submit"))) { return; }
        var map = { login: "sr-login", password: "sr-password", clinic: "sr-clinic", consent: "sr-consent" };
        var f = errCode(r) === "login_taken" ? "login" : errField(r);
        if (map[f]) { fieldError($(map[f]), $("err-" + map[f]), errCode(r) === "login_taken" ? "Такой логин уже занят — выберите другой." : errText(r)); return; }
        showAlert($("share-alert"), errText(r, "register"));
      });
    });

    function check() {
      if (!token) {
        bad("В ссылке нет кода доступа", "Откройте ссылку из сообщения пациента целиком — вместе с частью после «#». Если она обрезалась, попросите пациента прислать её ещё раз.", true);
        return;
      }
      show("share-wait");
      meReady().then(function (user) {
        if (!user) { show("share-login"); return; }
        if (user.role === "doctor") { accept(); return; }
        patientView(user);
      });
    }
    window.addEventListener("hashchange", function () {
      var next = readToken();
      if (!next && !window.location.hash) { return; }      // replaceState после ответа — не новая ссылка
      token = next;
      showAlert($("share-alert"), "");
      check();
    });
    check();
  }

  // =================================================================================================
  // /cabinet — общее состояние кабинета
  // =================================================================================================
  var C = {
    me: null, ref: null, byCode: {},
    history: null, records: [], recCache: {}, shares: [], log: [], tokens: [],
    sel: {},                    // выбранный показатель графика: ключ «me» или id пациента
    charts: [],                 // перерисовка графиков при смене ширины
    lastInput: null,            // пол / возраст из последнего анализа — подставляются в форму
    patients: [], patient: null, labrefs: [], editSet: null
  };

  function initCabinet() {
    on($("cab-logout"), "click", function () { logout(); });
    meReady().then(function (user) {
      if (!user) {
        // cookie сессии была, но не подошла (её уже стёр ответ /api/v1/me) — сеанс завершён, а не «не входили»
        var stale = document.body.getAttribute("data-session") === "1";
        window.location.replace("/login?next=" + encodeURIComponent("/cabinet" + (window.location.hash || "")) + (stale ? "&reason=expired" : ""));
        return;
      }
      C.me = user;
      onUnauthorized = sessionEnded;
      paintUser(user);
      return api("GET", "/ui/reference").then(function (r) {
        if (r.ok && r.data) {
          C.ref = r.data;
          (r.data.analytes || []).forEach(function (a) { C.byCode[a.code] = a; });
        }
        if (user.role === "doctor") { return initDoctor(); }
        return initPatient();
      });
    }).catch(function () {
      $("cab-loading").hidden = true;
      showAlert($("cab-alert"), "Не удалось загрузить кабинет. Обновите страницу.");
    });
    var t = null;
    window.addEventListener("resize", function () {
      clearTimeout(t);
      t = setTimeout(function () { C.charts.forEach(function (c) { if (document.body.contains(c.plot)) { c.draw(); } }); }, 120);
    });
  }
  // Адрес вида /cabinet#sec-doctors (например, после входа с next): раздел появляется после загрузки данных.
  function scrollToHash() {
    var m = /^#(sec-[a-z]+)$/.exec(window.location.hash || "");
    if (m && $(m[1])) { setTimeout(function () { scrollToNode($(m[1])); }, 60); }
  }
  function paintUser(user) {
    $("cab-user").hidden = false;
    $("cab-user-ava").textContent = initials(user.login);
    $("cab-user-name").textContent = user.login;
    $("cab-user-role").textContent = ROLE_RU[user.role] || "";
  }
  function heroTiles(tiles) {
    var ul = $("cab-tiles");
    clear(ul);
    tiles.forEach(function (t) {
      ul.appendChild(el("li", { class: "cab-tile" + (t.kind ? " cab-tile--" + t.kind : "") }, [
        el("span", { class: "cab-tile__k", text: t.k }),
        el("b", { class: "cab-tile__v" + (String(t.v).length > 10 ? " cab-tile__v--text" : ""), text: t.v }),
        t.s ? el("span", { class: "cab-tile__s", text: t.s }) : null
      ]));
    });
  }
  function heroCta(buttons) {
    var box = $("cab-cta");
    clear(box);
    buttons.forEach(function (b) {
      box.appendChild(el("a", { class: "btn " + (b.primary ? "btn--primary btn--hero" : "btn--ghost"), href: b.href, text: b.text }));
    });
  }
  function showRole(id) {
    $("cab-loading").hidden = true;
    $(id).hidden = false;
  }

  // ---------- нормы для шкал и графика ----------
  // Референс проекта по умолчанию (/ui/reference → reference_ranges.by_sex, канонические единицы); референсы
  // лаборатории, сохранённые с анализом (input.reference_ranges в канонической единице), — приоритетнее.
  function rangesFor(input) {
    var rr = (C.ref && C.ref.reference_ranges) || {}, out = {};
    var pregnant = input && input.pregnancy && input.pregnancy.status === "yes";
    if (!(pregnant && !rr.apply_in_pregnancy)) {
      var bySex = (rr.by_sex || {})[(input && input.sex) || "F"] || {};
      Object.keys(bySex).forEach(function (c) { out[c] = [bySex[c][0], bySex[c][1]]; });
    }
    var lab = (input && input.reference_ranges) || {};
    Object.keys(lab).forEach(function (c) {
      var r = lab[c], a = C.byCode[c];
      if (!r || !a || (r.unit && r.unit !== a.unit && r.unit !== a.unit_ru)) { return; }
      var cur = out[c] || [null, null];
      out[c] = [typeof r.low === "number" ? r.low : cur[0], typeof r.high === "number" ? r.high : cur[1]];
    });
    return out;
  }
  function shortName(code, fallback) { var a = C.byCode[code]; return (a && a.short_ru) || fallback || code; }

  // =================================================================================================
  // График динамики (свой SVG): полоса нормы, линия, точки с подписью значения, ось дат, подсказка при наведении
  // =================================================================================================
  var chartSeq = 0;
  function lastStatus(s) {
    var r = s.ref || {}, v = s.last;
    if (typeof r.low === "number" && v < r.low) { return "low"; }
    if (typeof r.high === "number" && v > r.high) { return "high"; }
    return (typeof r.low === "number" || typeof r.high === "number") ? "in" : "none";
  }
  function pointStatus(v, ref) {
    if (!ref) { return "none"; }
    if (typeof ref.low === "number" && v < ref.low) { return "low"; }
    if (typeof ref.high === "number" && v > ref.high) { return "high"; }
    return (typeof ref.low === "number" || typeof ref.high === "number") ? "in" : "none";
  }
  var STATUS_WORD = { low: "ниже нормы", high: "выше нормы", "in": "в норме", none: "" };
  function refText(ref, unit) {
    if (!ref) { return ""; }
    var lo = typeof ref.low === "number", hi = typeof ref.high === "number";
    if (lo && hi) { return num(ref.low) + "–" + num(ref.high) + " " + unit; }
    if (lo) { return "от " + num(ref.low) + " " + unit; }
    if (hi) { return "до " + num(ref.high) + " " + unit; }
    return "";
  }
  function signed(x) { return (x > 0 ? "+" : x < 0 ? "−" : "±") + num(Math.abs(x)); }
  // Изменение за всю историю и (если точек больше двух) — с прошлого анализа: «±0 г/л за 3 года» без второго
  // числа не говорит, что было между. Пациенту — без процентов.
  function deltaInfo(s, role) {
    if (!s.points || s.points.length < 2 || s.delta === null || s.delta === undefined) { return null; }
    var pts = s.points, last = pts[pts.length - 1], prev = pts[pts.length - 2];
    var days = daysBetween(pts[0].date, last.date);
    var sign = s.delta > 0 ? "+" : s.delta < 0 ? "−" : "±";
    var text = signed(s.delta) + " " + s.unit + " " + periodText(days);
    if (role === "doctor" && typeof s.delta_pct === "number") { text += " (" + sign + num(Math.abs(s.delta_pct)) + " %)"; }
    var arrow = s.direction === "up" ? "↑" : s.direction === "down" ? "↓" : "→";
    var recent = null;
    if (pts.length > 2 && typeof last.value === "number" && typeof prev.value === "number") {
      var step = Math.round((last.value - prev.value) * 100) / 100, gap = daysBetween(prev.date, last.date);
      recent = (gap > 0 ? "с прошлого анализа: " : "с прошлого анализа того же дня: ") + signed(step) + " " + s.unit + (gap > 0 ? " " + periodText(gap) : "");
    }
    return { text: text, arrow: arrow, dir: s.direction, recent: recent };
  }
  function niceTicks(lo, hi, n) {
    var span = hi - lo;
    if (!(span > 0)) { return [lo]; }
    var step = Math.pow(10, Math.floor(Math.log(span / n) / Math.LN10)), err = span / n / step;
    if (err >= 7.5) { step *= 10; } else if (err >= 3.5) { step *= 5; } else if (err >= 1.5) { step *= 2; }
    var out = [];
    for (var v = Math.floor(lo / step) * step; v <= Math.ceil(hi / step) * step + step / 2; v += step) { out.push(Math.round(v * 1e6) / 1e6); }
    return out;
  }
  function chartDescription(s) {
    var parts = s.points.map(function (p) { return num(p.value) + " " + s.unit + " — " + ruDate(p.date); });
    var t = s.name_ru + ": " + parts.join("; ");
    if (s.ref) { t += ". Норма " + refText(s.ref, s.unit); }
    var d = deltaInfo(s, "patient");
    if (d) { t += ". Изменение " + d.text + (d.recent ? "; " + d.recent : ""); }
    return t + ".";
  }

  // box — карточка; history — ответ /history; key — «me» или id пациента; role — тексты;
  // onPoint(record_id) — открыть анализ в ленте по нажатию на точку.
  function chartCard(box, history, key, role, onPoint) {
    clear(box);
    C.charts = C.charts.filter(function (c) { return c.key !== key; });
    var series = (history && history.series) || [];
    if (!series.length) {
      box.appendChild(emptyState("chart", role === "doctor" ? "У пациента пока нет анализов" : "Здесь появятся графики",
        role === "doctor" ? "Когда пациент добавит анализы, здесь будет их динамика." : "Добавьте анализы с разными датами — увидите, как меняются гемоглобин, ферритин и другие показатели.",
        role === "doctor" ? null : el("a", { class: "btn btn--primary", href: "#sec-add", text: "Добавить анализ" })));
      return { select: function () {} };
    }
    var sel = C.sel[key];
    if (!series.some(function (s) { return s.analyte === sel; })) { sel = series[0].analyte; }
    var pills = el("div", { class: "ch-pills", role: "group", "aria-label": "Показатель на графике" });
    var head = el("div", { class: "ch-head" });
    var plot = el("div", { class: "ch-plot" });
    var tableBox = el("details", { class: "fold ch-table" });
    box.appendChild(pills);
    box.appendChild(head);
    box.appendChild(plot);
    box.appendChild(tableBox);

    series.forEach(function (s) {
      var st = lastStatus(s);
      var b = el("button", { type: "button", class: "ch-pill", "aria-pressed": s.analyte === sel ? "true" : "false", "data-a": s.analyte }, [
        el("span", { class: "ch-pill__n", text: shortName(s.analyte, s.name_ru) }),
        el("span", { class: "ch-pill__v", text: num(s.last) }),
        (st === "low" || st === "high") ? el("span", { class: "ch-pill__dot", title: STATUS_WORD[st] }, [el("span", { class: "vh", text: ", " + STATUS_WORD[st] })]) : null
      ]);
      b.addEventListener("click", function () { select(s.analyte); });
      pills.appendChild(b);
    });

    var entry = { key: key, plot: plot, draw: function () {} };
    C.charts.push(entry);
    function select(code) {
      sel = code; C.sel[key] = code;
      Array.prototype.forEach.call(pills.querySelectorAll(".ch-pill"), function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-a") === code ? "true" : "false"); });
      var s = series.filter(function (x) { return x.analyte === code; })[0];
      paintHead(head, s, role);
      entry.draw = function () { drawChart(plot, s, onPoint); };
      entry.draw();
      paintTable(tableBox, s);
    }
    select(sel);
    return { select: function (code) { if (series.some(function (s) { return s.analyte === code; })) { select(code); } } };
  }
  function paintHead(head, s, role) {
    clear(head);
    var st = lastStatus(s), d = deltaInfo(s, role);
    var last = s.points[s.points.length - 1];
    head.appendChild(el("div", { class: "ch-head__main" }, [
      el("p", { class: "ch-head__name", text: s.name_ru }),
      el("p", { class: "ch-head__val" }, [el("b", { text: num(s.last) }), " " + s.unit,
        STATUS_WORD[st] ? el("span", { class: "ch-status ch-status--" + st, text: STATUS_WORD[st] }) : null])
    ]));
    var side = el("div", { class: "ch-head__side" });
    if (d) {
      side.appendChild(el("span", { class: "ch-delta ch-delta--" + d.dir }, [el("span", { class: "ch-delta__arrow", "aria-hidden": "true", text: d.arrow }), d.text]));
      if (d.recent) { side.appendChild(el("span", { class: "ch-head__recent", text: d.recent })); }
    } else {
      side.appendChild(el("span", { class: "ch-delta ch-delta--single", text: "одна точка — изменение появится после следующего анализа" }));
    }
    side.appendChild(el("span", { class: "ch-head__meta", text: (s.points.length > 1 ? count(s.points.length, "анализ", "анализа", "анализов") + " · " + ruDate(s.points[0].date) + " — " : "") + ruDate(last.date) +
      (s.ref ? " · норма " + refText(s.ref, s.unit) : "") }));
    head.appendChild(side);
  }
  function paintTable(box, s) {
    clear(box);
    box.appendChild(el("summary", { text: "Значения таблицей" }));
    var rows = s.points.slice().reverse().map(function (p) {
      var st = pointStatus(p.value, s.ref);
      return el("tr", null, [el("td", { text: ruDate(p.date) }), el("td", { class: "num", text: num(p.value) + " " + s.unit }),
        el("td", { text: STATUS_WORD[st] || "—" })]);
    });
    box.appendChild(el("div", { class: "tbl-wrap" }, [el("table", { class: "ch-tbl" }, [
      el("caption", { class: "vh", text: s.name_ru + ", " + s.unit }),
      el("thead", null, [el("tr", null, [el("th", { scope: "col", text: "Дата" }), el("th", { scope: "col", class: "num", text: "Значение" }), el("th", { scope: "col", text: "Оценка" })])]),
      el("tbody", null, rows)])]));
  }
  function drawChart(plot, s, onPoint) {
    clear(plot);
    var W = Math.max(280, Math.round(plot.clientWidth || 640));
    var H = W < 520 ? 230 : 290;
    var padL = W < 520 ? 40 : 52, padR = W < 520 ? 14 : 22, padT = 30, padB = 36;
    var pts = s.points.map(function (p) { return { t: dayMs(p.date), v: p.value, d: p.date, id: p.record_id }; });
    var ref = s.ref || null;
    var lo = ref && typeof ref.low === "number" ? ref.low : null, hi = ref && typeof ref.high === "number" ? ref.high : null;
    var vals = pts.map(function (p) { return p.v; });
    var dLo = Math.min.apply(null, vals), dHi = Math.max.apply(null, vals);
    var dSpan = Math.max(dHi - dLo, Math.abs(dHi) * 0.15, 1e-6);
    var near = function (b) { return b !== null && b >= dLo - 1.5 * dSpan && b <= dHi + 1.5 * dSpan; };
    var bounds = vals.concat([lo, hi].filter(near));
    var yLo = Math.min.apply(null, bounds), yHi = Math.max.apply(null, bounds);
    if (yHi === yLo) { var e = Math.abs(yLo) * 0.1 || 1; yLo -= e; yHi += e; }
    var span = yHi - yLo;
    yLo -= span * 0.12; yHi += span * 0.2;
    if (Math.min.apply(null, vals) >= 0 && yLo < 0) { yLo = 0; }
    var ticks = niceTicks(yLo, yHi, H < 260 ? 3 : 4);
    yLo = ticks[0]; yHi = ticks[ticks.length - 1];
    var inner = pts.length > 1 ? 26 : 0;
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    var X = function (t) { return t1 === t0 ? padL + (W - padL - padR) / 2 : padL + inner + (t - t0) / (t1 - t0) * (W - padL - padR - 2 * inner); };
    var Y = function (v) { return padT + (yHi - v) / (yHi - yLo) * (H - padT - padB); };
    var id = "chg" + (++chartSeq);
    var root = svg("svg", { class: "ch", viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", "aria-label": chartDescription(s) });
    root.appendChild(svg("defs", null, [
      svg("linearGradient", { id: id, x1: "0", y1: "0", x2: "0", y2: "1" }, [
        svg("stop", { offset: "0", class: "ch__stop1" }), svg("stop", { offset: "1", class: "ch__stop2" })])]));
    // сетка и подписи оси значений
    ticks.forEach(function (tv) {
      var y = Y(tv);
      root.appendChild(svg("line", { class: "ch__grid", x1: padL, x2: W - padR, y1: y, y2: y }));
      root.appendChild(svg("text", { class: "ch__ylab", x: padL - 8, y: y + 4, "text-anchor": "end" }, [num(tv)]));
    });
    // полоса нормы (одна граница — до края графика)
    if (lo !== null || hi !== null) {
      var yTop = hi !== null ? Y(Math.min(hi, yHi)) : padT, yBot = lo !== null ? Y(Math.max(lo, yLo)) : H - padB;
      if (yBot > yTop) {
        root.appendChild(svg("rect", { class: "ch__band", x: padL, y: yTop, width: W - padL - padR, height: yBot - yTop }));
        if (hi !== null && hi <= yHi) { root.appendChild(svg("line", { class: "ch__bandline", x1: padL, x2: W - padR, y1: yTop, y2: yTop })); }
        if (lo !== null && lo >= yLo) { root.appendChild(svg("line", { class: "ch__bandline", x1: padL, x2: W - padR, y1: yBot, y2: yBot })); }
        if (yBot - yTop > 16) {
          root.appendChild(svg("text", { class: "ch__bandlab", x: W - padR - 8, y: yTop + 14, "text-anchor": "end" }, ["норма " + refText(ref, s.unit)]));
        }
      }
    }
    // линия и заливка под ней
    var xy = pts.map(function (p) { return [X(p.t), Y(p.v)]; });
    // Несколько анализов в один день — не вертикальная черта: точки дня разводятся по горизонтали на 18 px
    // (подпись даты — одна, по центру дня). groups — индексы точек одного дня (точки идут по дате).
    var groups = [];
    pts.forEach(function (p, i) {
      if (i && p.d === pts[i - 1].d) { groups[groups.length - 1].to = i; } else { groups.push({ from: i, to: i, x: xy[i][0] }); }
    });
    groups.forEach(function (g) {
      var m = g.to - g.from + 1;
      for (var k = 0; m > 1 && k < m; k++) {
        xy[g.from + k][0] = Math.max(padL + 6, Math.min(W - padR - 6, g.x + (k - (m - 1) / 2) * 18));
      }
    });
    if (pts.length > 1) {
      var line = xy.map(function (q, i) { return (i ? "L" : "M") + q[0].toFixed(1) + "," + q[1].toFixed(1); }).join(" ");
      var base = (H - padB).toFixed(1);
      root.appendChild(svg("path", { class: "ch__area", d: line + " L" + xy[xy.length - 1][0].toFixed(1) + "," + base + " L" + xy[0][0].toFixed(1) + "," + base + " Z", fill: "url(#" + id + ")" }));
      root.appendChild(svg("path", { class: "ch__line", d: line }));
    }
    // ось дат: по одной подписи на день, подписи не наезжают друг на друга (не ближе 80 px); последняя — всегда.
    // На узком графике — короткий вид «05.10.26», на широком — «05 окт 26».
    var minGap = 80, shownG = [], lastX = groups[groups.length - 1].x;
    groups.forEach(function (g, i) {
      if (i === groups.length - 1) { shownG.push(g); return; }
      var prev = shownG.length ? shownG[shownG.length - 1].x : -Infinity;
      if (g.x - prev >= minGap && lastX - g.x >= minGap) { shownG.push(g); }
    });
    shownG.forEach(function (g) {
      var parts = isoParts(pts[g.from].d);
      var label = W < 520 ? pad(parts[2]) + "." + pad(parts[1]) + "." + String(parts[0]).slice(2)
        : pad(parts[2]) + " " + MON_SHORT[parts[1] - 1] + " " + String(parts[0]).slice(2);
      root.appendChild(svg("text", { class: "ch__xlab", x: g.x, y: H - padB + 22, "text-anchor": "middle" }, [label]));
    });
    // точки и подписи значений (если точек много — первая, последняя, минимум и максимум)
    var labelSet = {};
    if (pts.length <= 8) { pts.forEach(function (p, i) { labelSet[i] = true; }); }
    else {
      var iMin = vals.indexOf(Math.min.apply(null, vals)), iMax = vals.indexOf(Math.max.apply(null, vals));
      [0, pts.length - 1, iMin, iMax].forEach(function (i) { labelSet[i] = true; });
    }
    var guide = svg("line", { class: "ch__guide", x1: 0, x2: 0, y1: padT - 6, y2: H - padB, visibility: "hidden" });
    root.appendChild(guide);
    var dots = [];
    pts.forEach(function (p, i) {
      var st = pointStatus(p.v, ref), x = xy[i][0], y = xy[i][1];
      var last = i === pts.length - 1;
      if (last) { root.appendChild(svg("circle", { class: "ch__halo ch__halo--" + st, cx: x, cy: y, r: 12 })); }
      var dot = svg("circle", { class: "ch__pt ch__pt--" + st + (last ? " ch__pt--last" : ""), cx: x, cy: y, r: last ? 6.5 : 5.5 });
      dots.push(dot);
      root.appendChild(dot);
      if (labelSet[i]) {
        var ly = y - 13 < 12 ? y + 22 : y - 13;
        var anchor = x < padL + 20 ? "start" : (x > W - padR - 20 ? "end" : "middle");
        root.appendChild(svg("text", { class: "ch__val" + (last ? " ch__val--last" : ""), x: x, y: ly, "text-anchor": anchor }, [num(p.v)]));
      }
    });
    // наведение: ближайшая точка по горизонтали — направляющая и подсказка; нажатие — открыть анализ
    var hit = svg("rect", { class: "ch__hit", x: padL, y: 0, width: W - padL - padR, height: H - padB + 4 });
    root.appendChild(hit);
    plot.appendChild(root);
    var tip = el("div", { class: "ch-tip", hidden: true });
    plot.appendChild(tip);
    var current = -1;
    function nearest(evt) {
      var rect = root.getBoundingClientRect();
      var x = (evt.clientX - rect.left) * (W / rect.width), best = 0, bd = Infinity;
      xy.forEach(function (q, i) { var dd = Math.abs(q[0] - x); if (dd < bd) { bd = dd; best = i; } });
      return best;
    }
    function showTip(i) {
      if (i === current) { return; }
      current = i;
      dots.forEach(function (d, j) { d.classList.toggle("is-hot", j === i); });
      guide.setAttribute("x1", xy[i][0]); guide.setAttribute("x2", xy[i][0]); guide.setAttribute("visibility", "visible");
      var st = pointStatus(pts[i].v, ref);
      clear(tip);
      tip.appendChild(el("span", { class: "ch-tip__d", text: ruDate(pts[i].d) }));
      tip.appendChild(el("b", { class: "ch-tip__v", text: num(pts[i].v) + " " + s.unit }));
      if (STATUS_WORD[st]) { tip.appendChild(el("span", { class: "ch-tip__s ch-tip__s--" + st, text: STATUS_WORD[st] })); }
      if (onPoint) { tip.appendChild(el("span", { class: "ch-tip__h", text: "нажмите — открыть анализ" })); }
      tip.hidden = false;
      var px = xy[i][0] / W * plot.clientWidth, py = xy[i][1] / H * plot.clientHeight;
      var tw = tip.offsetWidth;
      tip.style.left = Math.max(4, Math.min(plot.clientWidth - tw - 4, px - tw / 2)) + "px";
      var above = py - tip.offsetHeight - 30;
      tip.style.top = (above >= 0 ? above : py + 16) + "px";
    }
    function hideTip() { current = -1; tip.hidden = true; guide.setAttribute("visibility", "hidden"); dots.forEach(function (d) { d.classList.remove("is-hot"); }); }
    hit.addEventListener("pointermove", function (e) { showTip(nearest(e)); });
    hit.addEventListener("pointerleave", hideTip);
    if (onPoint) {
      hit.addEventListener("click", function (e) { var i = nearest(e); if (pts[i].id) { onPoint(pts[i].id); } });
      hit.classList.add("is-link");
    }
  }

  // =================================================================================================
  // События истории и «что досдать»
  // =================================================================================================
  function eventsList(box, history, role, onAnalyte) {
    clear(box);
    var events = (history && history.events) || [];
    if (!events.length) {
      var n = (history && history.n_records) || 0;
      box.appendChild(emptyState("check", n < 2 ? "Пока без изменений" : "Заметных изменений нет",
        n < 2 ? "Изменения появятся, когда в истории будет хотя бы два анализа с разными датами." : "Между анализами нет изменений, на которые стоит обратить внимание."));
      return;
    }
    var ul = el("ul", { class: "evts" });
    events.forEach(function (ev) {
      var sev = ev.severity === "attention" ? "attention" : "info";
      var chips = (ev.analytes || []).map(function (a) {
        var b = el("button", { type: "button", class: "evt__chip", text: shortName(a) });
        b.setAttribute("aria-label", "Показать на графике: " + ((C.byCode[a] && C.byCode[a].name_ru) || a));
        b.addEventListener("click", function () { onAnalyte(a); });
        return b;
      });
      ul.appendChild(el("li", { class: "evt evt--" + sev }, [
        el("span", { class: "evt__ico", "aria-hidden": "true", text: sev === "attention" ? "!" : "i" }),
        el("div", { class: "evt__body" }, [
          el("h3", { class: "evt__t" }, [ev.title, el("span", { class: "vh", text: sev === "attention" ? " (обратить внимание)" : " (к сведению)" })]),
          el("p", { class: "evt__p", text: ev.text }),
          el("p", { class: "evt__meta" }, [el("span", { text: "анализ от " + ruDate(ev.date) })].concat(chips))
        ])
      ]));
    });
    box.appendChild(ul);
  }
  var TODO_ST = {
    never: { patient: "ещё не сдавали", doctor: "не сдавался" },
    stale: { patient: "давно", doctor: "давно" },
    ok: { patient: "актуально", doctor: "актуально" }
  };
  function todoRow(it, role) {
    var st = TODO_ST[it.status] ? it.status : "never";
    var label = TODO_ST[st][role === "doctor" ? "doctor" : "patient"];
    if (it.last_date && st !== "never") { label += " · " + ruDate(it.last_date); }
    // «Ещё не сдавали.» в конце причины повторяет метку рядом с названием — убираем повтор.
    var why = it.reason || "";
    if (st === "never") { why = why.replace(/\s*(Ещё не сдавали|Не сдавался)\.?\s*$/i, ""); }
    var side = [it.price_rub ? el("span", { class: "todo__price", text: rub(it.price_rub) }) : null];
    if (role !== "doctor" && st !== "ok") { side.push(extLink(yandexSearch(it.name_ru), "Где сдать", "todo__where")); }
    return el("li", { class: "todo todo--" + st }, [
      el("span", { class: "todo__st", "aria-hidden": "true" }, [st === "ok" ? icon("check", "ico ico--sm") : el("span", { text: st === "stale" ? "↻" : "" })]),
      el("div", { class: "todo__body" }, [
        el("p", { class: "todo__name" }, [it.name_ru, chip(label, st === "ok" ? "ok" : st === "stale" ? "warn" : "none")]),
        why ? el("p", { class: "todo__why", text: why }) : null
      ]),
      el("div", { class: "todo__side" }, side)
    ]);
  }
  function todoList(box, history, role) {
    clear(box);
    var items = (history && history.completeness) || [];
    var need = items.filter(function (i) { return i.status !== "ok"; }), ok = items.filter(function (i) { return i.status === "ok"; });
    var sum = need.reduce(function (a, i) { return a + (i.price_rub || 0); }, 0);
    if (!need.length) {
      box.appendChild(emptyState("check", "Базовый набор сдан и актуален", role === "doctor" ? "Все анализы базового набора сданы в срок." : "Новые анализы — по решению врача."));
    } else {
      box.appendChild(el("div", { class: "todo-sum" }, [
        el("p", { class: "todo-sum__t" }, [role === "doctor" ? "Не хватает: " : "Досдать: ", el("b", { text: count(need.length, "анализ", "анализа", "анализов") })]),
        sum ? el("p", { class: "todo-sum__p" }, [el("b", { text: rub(sum) }), " — средняя цена в Москве, без взятия крови"]) : null
      ]));
      box.appendChild(el("ul", { class: "todos" }, need.map(function (i) { return todoRow(i, role); })));
    }
    if (ok.length) {
      box.appendChild(el("details", { class: "fold fold--more todo-ok" }, [
        el("summary", { text: "Актуально: " + ok.length }),
        el("ul", { class: "todos" }, ok.map(function (i) { return todoRow(i, role); }))]));
    }
  }

  // =================================================================================================
  // Лента анализов и отчёты (пациенту — памятка с разделами; врачу — отчёт врача)
  // =================================================================================================
  var STATUS_CHIP = {
    low: ["ниже нормы", "warn"], high: ["выше нормы", "danger"], normal: ["в норме", "ok"], borderline: ["на границе", "warn"], unknown: ["без оценки", "none"]
  };
  function tone(r) { var l1 = r.level1 || {}; return (l1.urgent && l1.urgent.length) ? "urgent" : (l1.anemia ? "anemia" : "ok"); }
  function personText(i) {
    if (!i) { return ""; }
    var parts = [i.sex === "M" ? "мужчина" : "женщина"];
    if (i.age_years) { parts.push(i.age_years + " " + plural(i.age_years, "год", "года", "лет")); }
    if (i.pregnancy && i.pregnancy.status === "yes") { parts.push("беременность"); }
    return parts.join(", ");
  }
  function psection(title, lines, open) {
    var content = lines.length > 1 ? el("ul", { class: "psec__list" }, lines.map(function (t) { return el("li", { text: t }); })) : el("p", { text: lines[0] || "" });
    return el("details", { class: "psec", open: !!open }, [
      el("summary", null, [el("h4", { class: "psec__t", text: title }), el("span", { class: "fold__n", text: String(lines.length) })]),
      el("div", { class: "psec__body" }, [content])
    ]);
  }
  function scalesNode(rec, result) {
    var defs = rangesFor(rec.input), rows = [];
    (result.values || []).forEach(function (v) {
      var d = defs[v.analyte];
      if (v.value === null || v.value === undefined || !d || d[0] === null || d[1] === null || d[1] <= d[0]) { return; }
      var lo = d[0], hi = d[1], sp = hi - lo, min = lo - sp * 0.6, max = hi + sp * 0.6;
      var pos = function (x) { return Math.max(0, Math.min(100, (x - min) / (max - min) * 100)); };
      var st = (v.norm && v.norm.status) || "";
      var word = { normal: "в норме", low: "ниже нормы", high: "выше нормы", borderline: "на границе нормы" }[st] || (v.value < lo ? "ниже нормы" : v.value > hi ? "выше нормы" : "в норме");
      var kind = word === "в норме" ? "ok" : (word === "на границе нормы" ? "warn" : "danger");
      var band = el("span", { class: "scale__band" }), dot = el("span", { class: "scale__dot scale__dot--" + kind });
      band.style.left = pos(lo).toFixed(1) + "%"; band.style.right = (100 - pos(hi)).toFixed(1) + "%";
      dot.style.left = pos(v.value).toFixed(1) + "%";
      rows.push(el("li", { class: "scale" }, [
        el("span", { class: "scale__name", text: v.name_ru }),
        el("span", { class: "scale__val", text: num(v.value) + " " + (v.unit || "") }),
        el("span", { class: "scale__bar", role: "img", "aria-label": v.name_ru + ": " + num(v.value) + " " + (v.unit || "") + ", норма " + num(lo) + "–" + num(hi) + ", " + word }, [band, dot]),
        el("span", { class: "scale__word scale__word--" + kind, text: word })
      ]));
    });
    if (!rows.length) { return null; }
    return el("details", { class: "psec", open: true }, [
      el("summary", null, [el("h4", { class: "psec__t", text: "Ваши показатели" }), el("span", { class: "fold__n", text: String(rows.length) })]),
      el("div", { class: "psec__body" }, [el("ul", { class: "scales" }, rows),
        el("p", { class: "hint", text: "Зелёная полоса — норма (лаборатории, если она сохранена с анализом, иначе — для взрослых вашего пола); точка — ваше значение." })])
    ]);
  }
  function patientReport(rec, result) {
    var rep = (result.reports || {}).patient || {};
    var nodes = [el("p", { class: "rep-head rep-head--" + tone(result), text: rep.headline || "" })];
    var scales = scalesNode(rec, result), sections = rep.sections || [], placed = false;
    sections.forEach(function (s, i) {
      nodes.push(psection(s.title, s.lines || [], i === 0));
      if (i === 0 && scales) { nodes.push(scales); placed = true; }
    });
    if (scales && !placed) { nodes.push(scales); }
    return nodes;
  }
  function doctorTiles(r) {
    var l1 = r.level1 || {}, h = r.hidden_deficiency || {}, n = (r.next_tests || []).length;
    var tiles = [
      l1.anemia ? ["danger", "Анемия", l1.severity_ru ? cap(l1.severity_ru) : "Есть", "Hb " + num(l1.hemoglobin) + " при пороге " + num(l1.threshold_g_l) + " г/л"]
                : ["ok", "Анемия", "Нет", "Hb " + num(l1.hemoglobin) + " при пороге " + num(l1.threshold_g_l) + " г/л"],
      !h.applicable ? ["none", "Скрытый дефицит", "Не оценивается", "при анемии — класс и причина"]
        : h.detected ? ["warn", "Скрытый дефицит", "Есть сигнал", (h.screening && h.screening.applicable && h.screening.risk_ru) ? "риск по ОАК: " + h.screening.risk_ru : "по сданным анализам"]
        : ["ok", "Скрытый дефицит", "Сигналов нет", "по сданным анализам"],
      n ? ["info", "Досдать", count(n, "анализ", "анализа", "анализов"), "по правилам и расчёту"] : ["ok", "Досдать", "Ничего", "по правилам и расчёту"]
    ];
    return el("div", { class: "rep-tiles" }, tiles.map(function (t) {
      return el("div", { class: "rep-tile rep-tile--" + t[0] }, [el("span", { class: "rep-tile__k", text: t[1] }), el("b", { class: "rep-tile__v", text: t[2] }), el("span", { class: "rep-tile__s", text: t[3] })]);
    }));
  }
  function doctorReport(rec, result) {
    var rep = (result.reports || {}).doctor || {}, tn = tone(result);
    var nodes = [el("div", { class: "rep-head rep-head--" + tn }, [
      el("span", { class: "rep-head__tag", text: { urgent: "Срочно", anemia: "Анемия", ok: "Анемии нет" }[tn] }), el("span", { text: rep.headline || "" })])];
    nodes.push(doctorTiles(result));
    var l1 = result.level1 || {};
    if (l1.urgent && l1.urgent.length) {
      nodes.push(el("div", { class: "rep-urgent", role: "note" }, [el("strong", { text: "Срочно" }),
        el("ul", null, l1.urgent.map(function (u) { return el("li", { text: u.text + (u.source ? " (" + u.source + ")" : "") }); }))]));
    }
    if (result.references && result.references.warning) { nodes.push(el("p", { class: "rep-note", text: result.references.warning })); }
    if (result.flags && result.flags.length) {
      nodes.push(el("h4", { class: "rep-sub", text: "На что обратить внимание" }));
      nodes.push(el("ul", { class: "rep-flags" }, result.flags.map(function (f) {
        return el("li", { class: "rep-flag" + (f.severity === "info" ? " rep-flag--info" : "") }, [
          el("b", { text: f.title }), el("p", { text: f.text }), f.source ? el("p", { class: "source", text: "Источник: " + f.source }) : null]);
      })));
    }
    if (result.next_tests && result.next_tests.length) {
      nodes.push(el("h4", { class: "rep-sub", text: "Что досдать" }));
      nodes.push(el("ol", { class: "rep-tests" }, result.next_tests.map(function (t) {
        return el("li", null, [el("b", { text: t.name_ru }), el("span", { text: " — " + t.reason }), t.price_rub ? el("span", { class: "rep-tests__p", text: " " + rub(t.price_rub) }) : null]);
      })));
    }
    var vals = (result.values || []).filter(function (v) { return v.value !== null && v.value !== undefined; });
    if (vals.length) {
      nodes.push(el("details", { class: "psec", open: true }, [
        el("summary", null, [el("h4", { class: "psec__t", text: "Показатели" }), el("span", { class: "fold__n", text: String(vals.length) })]),
        el("div", { class: "psec__body" }, [el("div", { class: "tbl-wrap" }, [el("table", { class: "rep-tbl" }, [
          el("thead", null, [el("tr", null, [el("th", { scope: "col", text: "Показатель" }), el("th", { scope: "col", class: "num", text: "Значение" }), el("th", { scope: "col", text: "Статус" }), el("th", { scope: "col", text: "Порог и источник" })])]),
          el("tbody", null, vals.map(function (v) {
            var st = STATUS_CHIP[(v.norm && v.norm.status) || "unknown"] || STATUS_CHIP.unknown;
            return el("tr", null, [el("th", { scope: "row", text: v.name_ru }), el("td", { class: "num", "data-label": "Значение", text: num(v.value) + " " + (v.unit || "") }),
              el("td", { "data-label": "Статус" }, [chip(st[0], st[1])]), el("td", { "data-label": "Порог", class: "rep-tbl__src", text: (v.norm && v.norm.threshold_text) || "" })]);
          }))])])])
      ]));
    }
    (rep.sections || []).forEach(function (s) { nodes.push(psection(s.title, s.lines || [], false)); });
    return nodes;
  }
  // Модель документа для export.js (DOCX в браузере, как на главной).
  function docxModel(kind, rec, result) {
    var rep = (result.reports || {})[kind] || {}, tn = tone(result);
    var title = kind === "doctor" ? "Отчёт врачу" : "Памятка пациенту";
    var b = [{ t: "brand", kind: title, date: new Date().toLocaleDateString("ru-RU") },
      { t: "meta", text: "Пациент: " + personText(rec.input) + " · дата анализа: " + ruDate(rec.date) },
      { t: "lead", text: rep.headline || "", tone: tn, tag: kind === "doctor" ? { urgent: "Срочно", anemia: "Анемия", ok: "Анемии нет" }[tn] : null }];
    var vals = (result.values || []).filter(function (v) { return v.value !== null && v.value !== undefined; });
    var word = { low: "ниже нормы", high: "выше нормы", normal: "в норме", borderline: "на границе нормы" };
    if (vals.length) {
      b.push({ t: "h2", text: kind === "doctor" ? "Показатели" : "Ваши показатели" });
      if (kind === "doctor") {
        b.push({ t: "table", head: ["Показатель", "Значение", "Статус", "Порог и источник"], widths: [2500, 1500, 1500, 4138],
          rows: vals.map(function (v) { var st = (v.norm && v.norm.status) || "unknown"; return [v.name_ru, num(v.value) + " " + (v.unit || ""), { text: (STATUS_CHIP[st] || STATUS_CHIP.unknown)[0], tone: st }, (v.norm && v.norm.threshold_text) || ""]; }) });
      } else {
        b.push({ t: "table", head: ["Показатель", "Значение", "Оценка"], widths: [4200, 2400, 3038],
          rows: vals.map(function (v) { var st = (v.norm && v.norm.status) || "unknown"; return [v.name_ru, num(v.value) + " " + (v.unit || ""), { text: word[st] || "—", tone: st }]; }) });
      }
    }
    (rep.sections || []).forEach(function (s) {
      b.push({ t: "h2", text: s.title });
      (s.lines || []).forEach(function (line) { b.push({ t: (s.lines.length > 1 ? "li" : "p"), text: line }); });
    });
    b.push({ t: "small", text: "Справочная информация, не медицинское заключение. Создано сервисом Justmedit из личного кабинета." });
    return { title: "Justmedit — " + title.toLowerCase(), blocks: b, footer: (C.ref && C.ref.disclaimer) || "Исследовательский прототип, не медицинское изделие. Решение принимает врач." };
  }
  function downloadDocx(kind, rec, result) {
    if (!window.JMExport) { toast("Выгрузка недоступна: обновите страницу", "warn"); return; }
    var name = "justmedit-" + (kind === "doctor" ? "otchet-vrachu" : "pamyatka-pacientu") + "-" + rec.date + ".docx";
    window.JMExport.download(window.JMExport.docx(docxModel(kind, rec, result)), name);
  }

  // box — контейнер ленты; records — [{id,date,source,headline,anemia,created}]; o.role — вид отчёта;
  // o.load(id) -> Promise<{record,result}>; o.onDelete(rec) — только пациенту; o.empty — пустое состояние;
  // o.onForbidden() — врачу: пациент закрыл доступ (403 при открытии отчёта).
  // Показываются последние FEED_PAGE анализов, дальше — «Показать ещё N» (36 записей — это 3,5 тыс. px ленты).
  var FEEDS = {};                          // id контейнера -> {reveal(id)}: открыть анализ с точки графика
  function feed(box, records, o) {
    clear(box);
    delete FEEDS[box.id];
    if (!records.length) { box.appendChild(el("div", { class: "cab-card" }, [o.empty])); return; }
    var sorted = records.slice().sort(function (a, b) { return a.date < b.date ? 1 : a.date > b.date ? -1 : (a.created < b.created ? 1 : -1); });
    var list = el("ol", { class: "feed" }), year = null, shown = 0;
    var more = el("button", { type: "button", class: "btn feed__more" });
    var note = el("p", { class: "feed__note", "aria-live": "polite" });
    var moreBox = el("div", { class: "feed__morebox" }, [note, more]);
    function upTo(n) {
      var first = null;
      for (n = Math.min(n, sorted.length); shown < n; shown++) {
        var rec = sorted[shown], y = rec.date.slice(0, 4);
        if (y !== year) { year = y; list.appendChild(el("li", { class: "feed__year", "aria-hidden": "true", text: y })); }
        var item = feedItem(rec, o);
        first = first || item;
        list.appendChild(item);
      }
      var left = sorted.length - shown;
      moreBox.hidden = !left;
      note.textContent = "Показано " + shown + " из " + sorted.length;
      more.textContent = "Показать ещё " + Math.min(FEED_PAGE, left);
      return first;
    }
    more.addEventListener("click", function () {
      var first = upTo(shown + FEED_PAGE);
      var head = first && first.querySelector(".rec__head");
      if (head) { head.focus(); }                          // фокус — на первый из показанных анализов
    });
    box.appendChild(list);
    box.appendChild(moreBox);
    upTo(FEED_PAGE);
    if (box.id) {
      FEEDS[box.id] = { reveal: function (id) {
        for (var i = 0; i < sorted.length; i++) { if (sorted[i].id === id) { break; } }
        if (i < sorted.length && i >= shown) { upTo(Math.ceil((i + 1) / FEED_PAGE) * FEED_PAGE); }
      } };
    }
  }
  function feedItem(rec, o) {
    var p = isoParts(rec.date);
    var bodyId = "rec-" + rec.id;
    var stChip = o.role === "doctor" ? (rec.anemia ? chip("анемия", "warn") : chip("без анемии", "ok"))
                                     : (rec.anemia ? chip("гемоглобин ниже порога", "warn") : chip("гемоглобин не ниже порога", "ok"));
    var head = el("button", { type: "button", class: "rec__head", "aria-expanded": "false", "aria-controls": bodyId }, [
      el("span", { class: "rec__date", "aria-hidden": "true" }, [el("b", { text: pad(p[2]) }), el("span", { text: MON_SHORT[p[1] - 1] + " " + p[0] })]),
      el("span", { class: "rec__main" }, [
        el("span", { class: "vh", text: "Анализ от " + ruDate(rec.date) + ". " }),
        el("span", { class: "rec__title", text: rec.headline || "Анализ" }),
        el("span", { class: "rec__chips" }, [rec.source ? chip(SOURCE_RU[rec.source] || rec.source, "src") : null, rec.anemia === null || rec.anemia === undefined ? null : stChip])
      ]),
      el("span", { class: "rec__chev", "aria-hidden": "true" })
    ]);
    var body = el("div", { class: "rec__body", id: bodyId, hidden: true });
    var item = el("li", { class: "rec", "data-id": rec.id }, [head, body]);
    var loaded = false;
    head.addEventListener("click", function () {
      var open = head.getAttribute("aria-expanded") !== "true";
      head.setAttribute("aria-expanded", open ? "true" : "false");
      item.classList.toggle("is-open", open);
      body.hidden = !open;
      if (open && !loaded) { loaded = true; loadBody(); }
    });
    function loadBody() {
      clear(body);
      body.appendChild(el("p", { class: "rec__loading", role: "status" }, [el("span", { class: "spinner", "aria-hidden": "true" }), "Загружаем отчёт…"]));
      o.load(rec.id).then(function (r) {
        clear(body);
        if (r.status === 403 && o.onForbidden) { o.onForbidden(); return; }
        if (!r.ok || !r.data) { loaded = false; body.appendChild(el("p", { class: "cab-alert", text: errText(r) })); return; }
        var full = r.data.record, result = r.data.result;
        append(body, o.role === "doctor" ? doctorReport(full, result) : patientReport(full, result));
        var acts = el("div", { class: "rec__acts" });
        var what = o.role === "doctor" ? "отчёт врачу" : "памятку";
        var dl = el("button", { type: "button", class: "btn btn--small btn--dl2", "aria-label": "Скачать " + what + " от " + ruDate(rec.date) + ", DOCX" }, [dlIcon(), "DOCX"]);
        dl.addEventListener("click", function () { downloadDocx(o.role === "doctor" ? "doctor" : "patient", full, result); });
        acts.appendChild(dl);
        acts.appendChild(el("span", { class: "rec__meta", text: personText(full.input) + " · добавлен " + localDay(rec.created) }));
        if (o.onDelete) {
          var del = el("button", { type: "button", class: "btn btn--small btn--ghostdanger", text: "Удалить" });
          del.addEventListener("click", function () { o.onDelete(rec); });
          acts.appendChild(del);
        }
        body.appendChild(acts);
      });
    }
    item.openNow = function () {
      if (head.getAttribute("aria-expanded") !== "true") { head.click(); }
      item.classList.add("is-flash");
      setTimeout(function () { item.classList.remove("is-flash"); }, 1600);
      scrollToNode(item);
    };
    return item;
  }
  function dlIcon() {
    return svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false", class: "ico ico--sm" }, [
      svg("path", { d: "M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19.5h14", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round" })]);
  }
  function openRecordIn(box, id) {
    if (FEEDS[box.id]) { FEEDS[box.id].reveal(id); }          // анализ за «Показать ещё» — сначала показать
    var item = box.querySelector('.rec[data-id="' + id + '"]');
    if (item && item.openNow) { item.openNow(); }
  }

  // =================================================================================================
  // Кабинет пациента
  // =================================================================================================
  var chartApi = null;
  function initPatient() {
    $("cab-kicker").textContent = "Кабинет пациента";
    $("cab-h").textContent = "Здравствуйте, " + C.me.login;
    heroCta([{ text: "Добавить анализ", href: "#sec-add", primary: true }, { text: "Поделиться с врачом", href: "#sec-doctors" }]);
    showRole("cab-patient");
    buildAdd();
    buildShareNew();
    buildData();
    tokensCard($("p-tokens"), true);
    accountCard($("p-account"));
    deleteCard($("p-delete"), "patient");
    // Ссылка «ждёт врача»: врач примет её на своём устройстве — статус обновляется, когда пациент вернулся
    // на вкладку (visibilitychange / focus), пока есть ожидающие ссылки; не чаще раза в 3 с.
    var lastCheck = 0;
    var recheck = function () {
      if (document.visibilityState !== "visible" || Date.now() - lastCheck < 3000) { return; }
      if (!C.shares.some(function (s) { return s.status === "pending"; })) { return; }
      lastCheck = Date.now();
      refreshShares();
    };
    document.addEventListener("visibilitychange", recheck);
    window.addEventListener("focus", recheck);
    return refreshPatient().then(function () { refreshShares(); refreshTokens(); scrollToHash(); });
  }
  function refreshPatient() {
    return Promise.all([getHistory("/api/v1/me/history"), api("GET", "/api/v1/me/records")]).then(function (rs) {
      // История считается под слотом тяжёлых расчётов: занято — 503 service_busy; тогда в карточках — «Повторить».
      C.historyErr = rs[0].ok ? null : rs[0];
      C.history = rs[0].ok ? rs[0].data : null;
      C.records = (rs[1].ok && rs[1].data && rs[1].data.records) || [];
      paintPatient();
      // Пол и возраст последнего анализа — подставить в форму «Добавить анализ».
      var latest = C.history && C.history.latest;
      if (latest && latest.record_id && !C.lastInput) {
        loadMyRecord(latest.record_id).then(function (r) {
          if (r.ok && r.data && r.data.record) { C.lastInput = r.data.record.input; prefillPerson(); }
        });
      }
    });
  }
  function loadMyRecord(id) {
    if (C.recCache[id]) { return Promise.resolve(C.recCache[id]); }
    return api("GET", "/api/v1/me/records/" + id).then(function (r) { if (r.ok) { C.recCache[id] = r; } return r; });
  }
  function paintPatient() {
    var h = C.history || { n_records: C.records.length, series: [], events: [], completeness: [] };
    var n = C.records.length;
    var need = (h.completeness || []).filter(function (i) { return i.status !== "ok"; });
    var sum = need.reduce(function (a, i) { return a + (i.price_rub || 0); }, 0);
    var failed = n && C.historyErr ? C.historyErr : null;
    $("cab-lead").textContent = failed ? errText(failed) : n ? (h.summary || "") : "Добавьте первый анализ — здесь появятся динамика показателей и подсказки, что досдать.";
    // Без истории — дата последнего анализа из списка записей (он отсортирован сервером: новые сверху).
    var last = h.latest || (failed && C.records[0] ? { date: C.records[0].date } : null);
    // Число анализов — как в сводке истории: одинаковые записи одного дня считаются один раз (n_records истории);
    // если записей больше, это сказано подписью. История не загрузилась — число записей.
    var distinct = !failed && C.history && typeof C.history.n_records === "number" ? C.history.n_records : n;
    var since = h.first_date && h.first_date !== h.last_date ? "с " + ruDate(h.first_date) : distinct === 1 ? "один анализ" : "";
    var tileSub = !n ? "пока пусто" : failed ? "история не загрузилась"
      : distinct !== n ? count(n, "запись", "записи", "записей") + ": повторы одного дня — один анализ" : since;
    var todo = h.completeness || [];
    heroTiles([
      { k: "Анализов в истории", v: String(distinct), s: tileSub },
      { k: "Последний анализ", v: last ? ruDate(last.date) : "—", s: last ? agoText(last.date) : "добавьте первый" },
      failed ? { k: "Досдать", v: "—", s: "после загрузки истории" }
        : todo.length ? { k: "Досдать", v: String(need.length), s: need.length ? (sum ? rub(sum) + " в Москве" : "для полной картины") : "базовый набор сдан", kind: need.length ? "warn" : "ok" }
        : { k: "Досдать", v: "—", s: "подскажем после первого анализа" }
    ]);
    if (failed) {
      chartApi = null;
      C.charts = C.charts.filter(function (c) { return c.key !== "me"; });
      clear($("p-chart"));
      $("p-chart").appendChild(historyRetry(failed, refreshPatient));
      [$("p-events"), $("p-todo")].forEach(function (box) {
        clear(box);
        box.appendChild(el("p", { class: "cab-card__p cab-wait", text: "Появится, когда загрузится история анализов." }));
      });
    } else {
      chartApi = chartCard($("p-chart"), h, "me", "patient", function (id) { openRecordIn($("p-feed"), id); });
      eventsList($("p-events"), h, "patient", function (a) { if (chartApi) { chartApi.select(a); } scrollToNode($("sec-dyn")); });
      todoList($("p-todo"), h, "patient");
    }
    feed($("p-feed"), C.records, {
      role: "patient", load: loadMyRecord, onDelete: deleteRecord,
      empty: emptyState("doc", "Анализов пока нет", "Загрузите фото или PDF бланка — или введите значения вручную.", el("a", { class: "btn btn--primary", href: "#sec-add", text: "Добавить анализ" }))
    });
  }
  function deleteRecord(rec) {
    openDialog({
      title: "Удалить анализ от " + ruDate(rec.date) + "?", danger: true, okText: "Удалить",
      text: "Анализ исчезнет из истории, графиков и выгрузки. Восстановить его нельзя.",
      onOk: function () {
        return api("DELETE", "/api/v1/me/records/" + rec.id).then(function (r) {
          if (!r.ok) { return errText(r); }
          delete C.recCache[rec.id];
          toast("Анализ от " + ruDate(rec.date) + " удалён", "ok");
          refreshPatient();
          return null;
        });
      }
    });
  }

  // ---------- «Добавить анализ»: фото, PDF, файл или текст, вручную → сверка → сохранить ----------
  var ADD = { source: "form", refs: null };
  var MAIN_EXTRA = ["ferritin", "serum_iron", "TIBC", "TSAT"];      // к ОАК — основные показатели обмена железа
  function methodTile(kind, title, sub, iconPath, forInput) {
    var kids = [
      el("span", { class: "add-m__ico", "aria-hidden": "true" }, [svg("svg", { viewBox: "0 0 24 24", focusable: "false", class: "ico" }, [
        svg("path", { d: iconPath, fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round" })])]),
      el("span", { class: "add-m__t", text: title }), el("span", { class: "add-m__s", text: sub })];
    if (forInput) { return el("label", { class: "add-m", for: forInput, "data-m": kind, tabindex: "0", role: "button" }, kids); }
    return el("button", { type: "button", class: "add-m", "data-m": kind }, kids);
  }
  function buildAdd() {
    var box = $("p-add");
    clear(box);
    // Без capture: на телефоне можно и снять бланк, и выбрать готовое фото из галереи.
    var photo = el("input", { type: "file", id: "add-photo", class: "vh", accept: "image/*", tabindex: "-1", "aria-hidden": "true" });
    var pdf = el("input", { type: "file", id: "add-pdf", class: "vh", accept: ".pdf,application/pdf", tabindex: "-1", "aria-hidden": "true" });
    var methods = el("div", { class: "add-ms", role: "group", "aria-label": "Как добавить анализ" }, [
      methodTile("photo", "Фото бланка", "снимок или картинка", "M4 8.5A2.5 2.5 0 0 1 6.5 6h1.6l1.2-1.8A1.5 1.5 0 0 1 10.6 3.5h2.8a1.5 1.5 0 0 1 1.3.7L15.9 6h1.6A2.5 2.5 0 0 1 20 8.5v8a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 16.5zM12 15.7a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8z", "add-photo"),
      methodTile("pdf", "PDF", "ЕМИАС, Госуслуги, лаборатория", ICONS.doc + "M9 12.5h6M9 16h4", "add-pdf"),
      methodTile("text", "Файл или текст", "TXT, CSV, XLSX или вставить", "M5 6h14M5 10h14M5 14h9M5 18h6"),
      methodTile("manual", "Вручную", "пол, возраст и значения", "M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3zM13.5 8.5l3 3")
    ]);
    var textPanel = el("div", { class: "add-text", id: "add-text", hidden: true }, [
      el("label", { class: "fx__label", for: "add-file", text: "Файл бланка: фото, PDF, TXT, CSV или XLSX" }),
      el("div", { class: "add-text__row" }, [
        el("input", { type: "file", id: "add-file", accept: "image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp,.pdf,.txt,.csv,.xlsx" }),
        el("button", { type: "button", class: "btn btn--small", id: "add-file-run", text: "Распознать файл" })]),
      el("label", { class: "fx__label", for: "add-paste", text: "Или вставьте текст бланка" }),
      el("textarea", { id: "add-paste", rows: "4", spellcheck: "false", placeholder: "Гемоглобин 124 г/л\nФерритин 9 мкг/л" }),
      el("div", { class: "add-text__row" }, [el("button", { type: "button", class: "btn btn--small", id: "add-paste-run", text: "Распознать текст" })])
    ]);
    var status = el("div", { class: "add-status", id: "add-status", "aria-live": "polite" });
    var done = el("div", { class: "add-done", id: "add-done", hidden: true, role: "status" });
    box.appendChild(photo); box.appendChild(pdf);
    box.appendChild(methods);
    box.appendChild(textPanel);
    box.appendChild(status);
    box.appendChild(done);
    box.appendChild(buildAddForm());

    Array.prototype.forEach.call(methods.querySelectorAll(".add-m"), function (m) {
      var kind = m.getAttribute("data-m");
      if (m.tagName === "LABEL") {
        m.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $(m.getAttribute("for")).click(); } });
        return;
      }
      m.addEventListener("click", function () {
        markMethod(kind);
        $("add-done").hidden = true;
        if (kind === "text") { textPanel.hidden = false; $("add-paste").focus(); }
        else { textPanel.hidden = true; clear(status); ADD.source = "form"; ADD.refs = null; openForm(true); }
      });
    });
    [["add-photo", "photo"], ["add-pdf", "pdf"]].forEach(function (pair) {
      on($(pair[0]), "change", function () {
        var f = $(pair[0]).files[0];
        if (!f) { return; }
        markMethod(pair[1]); textPanel.hidden = true;
        parseFile(f, pair[1]);
        $(pair[0]).value = "";
      });
    });
    on($("add-file-run"), "click", function () {
      var f = $("add-file").files[0];
      if (!f) { setStatus("err", "Выберите файл бланка."); return; }
      var kind = /^image\//.test(f.type || "") ? "photo" : (/\.pdf$/i.test(f.name || "") ? "pdf" : "file");
      parseFile(f, kind);
    });
    on($("add-paste-run"), "click", function () {
      var text = $("add-paste").value;
      if (!text.trim()) { setStatus("err", "Вставьте текст бланка."); return; }
      $("add-paste-run").disabled = true;
      setStatus("wait", "Читаем текст…");
      api("POST", "/ui/parse", { text: text }).then(function (r) {
        $("add-paste-run").disabled = false;
        afterParse(r, "text");
      });
    });
  }
  function markMethod(kind) {
    Array.prototype.forEach.call(document.querySelectorAll("#p-add .add-m"), function (m) { m.classList.toggle("is-on", m.getAttribute("data-m") === kind); });
  }
  function setStatus(kind, text, extra) {
    var box = $("add-status");
    clear(box);
    if (!text) { return; }
    box.appendChild(el("div", { class: "add-status__line add-status__line--" + kind }, [
      kind === "wait" ? el("span", { class: "spinner", "aria-hidden": "true" }) : null, el("span", { text: text })]));
    (extra || []).forEach(function (n) { if (n) { box.appendChild(n); } });
  }
  function parseFile(file, kind) {
    var fd = new FormData();
    fd.append("file", file, file.name || "photo.jpg");
    var what = kind === "photo" ? "Распознаём фото" : kind === "pdf" ? "Читаем PDF" : "Читаем файл";
    setStatus("wait", what + " — это займёт несколько секунд…");
    $("add-done").hidden = true;
    apiForm("/ui/parse", fd, false).then(function (r) { afterParse(r, kind); });
  }
  function afterParse(r, kind) {
    if (!r.ok || !r.data) { setStatus("err", errText(r)); return; }
    var d = r.data, codes = Object.keys(d.values || {}), filled = [];
    resetValues();
    codes.forEach(function (code) {
      var v = d.values[code];
      if (setField(code, v.value, v.unit)) { filled.push(shortName(code)); }
    });
    (d.check || []).forEach(markCheck);
    // Дата с бланка; нет её (или она позже сегодняшней) — сегодняшняя, и поле даты подсвечено: проверьте.
    var dated = !!(d.date && isoParts(d.date) && d.date <= todayIso());
    $("add-date").value = dated ? d.date : todayIso();
    markDate(!dated);
    ADD.source = kind;
    ADD.refs = pickRefs(d.reference_ranges);
    var extra = [];
    extra.push(dated ? el("p", { class: "add-status__p", text: "Дата анализа с бланка: " + ruDate(d.date) + "." })
      : el("p", { class: "add-status__p add-status__p--warn", text: "Дату на бланке не нашли — поставили сегодняшнюю, исправьте, если анализ сдан в другой день." }));
    if (d.warnings && d.warnings.length) { extra.push(el("ul", { class: "add-status__warn" }, d.warnings.map(function (w) { return el("li", { text: w }); }))); }
    // Замечания разбора — как на главной: что пересчитано, что не так с единицами или строками.
    if (d.notes && d.notes.length) {
      extra.push(el("p", { class: "add-status__p add-status__p--warn", text: "Замечания разбора:" }));
      extra.push(el("ul", { class: "add-status__warn" }, d.notes.map(function (n) { return el("li", { text: typeof n === "string" ? n : (n && n.text) || "" }); })));
    }
    [["Не распознаны строки", d.unrecognized], ["Распознано, но сервисом не используется", d.ignored]].forEach(function (pair) {
      var items = pair[1] || [];
      if (!items.length) { return; }
      extra.push(el("details", { class: "fold fold--list add-status__fold" }, [
        el("summary", null, [pair[0] + " ", el("span", { class: "fold__n", text: String(items.length) })]),
        el("ul", { class: "add-status__list" }, items.map(function (t) { return el("li", { text: String(t) }); }))]));
    });
    if (!filled.length) { setStatus("err", "Показатели не распознаны. Введите значения вручную или попробуйте другой файл.", extra); }
    else { setStatus("ok", "Распознано: " + count(filled.length, "показатель", "показателя", "показателей") + " — " + filled.join(", ") + ". Проверьте значения и дату, затем сохраните.", extra); }
    paintRefs();
    openForm(false);
  }
  // Поле даты подсвечено, пока пользователь не проверил дату (дату на бланке не нашли).
  function markDate(on) {
    var input = $("add-date"), hint = $("add-date-hint");
    if (!input || !hint) { return; }
    input.classList.toggle("is-check", !!on);
    hint.hidden = !on;
  }
  function pickRefs(rr) {
    var out = {};
    Object.keys(rr || {}).forEach(function (code) {
      var r = rr[code], a = C.byCode[code];
      if (!a || !r || typeof r !== "object") { return; }
      var lo = typeof r.low === "number" && isFinite(r.low) ? r.low : null, hi = typeof r.high === "number" && isFinite(r.high) ? r.high : null;
      if ((lo === null && hi === null) || (lo !== null && hi !== null && lo > hi)) { return; }
      var spec = {};
      if (lo !== null) { spec.low = lo; }
      if (hi !== null) { spec.high = hi; }
      if (r.unit) { spec.unit = r.unit; }
      out[code] = spec;
    });
    return Object.keys(out).length ? out : null;
  }
  function paintRefs() {
    var box = $("add-refs");
    clear(box);
    box.hidden = !ADD.refs;
    if (!ADD.refs) { return; }
    var names = Object.keys(ADD.refs).map(function (c) { return shortName(c); });
    var cb = el("input", { type: "checkbox", id: "add-refs-on", checked: true });
    box.appendChild(el("label", { class: "consent", for: "add-refs-on" }, [cb, el("span", null, [el("b", { text: "Сохранить референсы лаборатории с бланка" }),
      " — " + count(names.length, "показатель", "показателя", "показателей") + ": " + names.join(", ") + ". По ним будут оцениваться значения этого анализа."])]));
  }
  function fieldNode(code) {
    var a = C.byCode[code];
    if (!a) { return null; }
    var id = "add-v-" + code;
    return el("div", { class: "fx2", "data-code": code }, [
      el("label", { class: "fx2__label", for: id }, [el("span", { class: "fx2__short", text: a.short_ru }), a.short_ru !== a.name_ru ? " " : null,
        a.short_ru !== a.name_ru ? el("span", { class: "fx2__name", text: a.name_ru }) : null]),
      el("div", { class: "fx2__row" }, [
        el("input", { class: "fx__input fx2__input", id: id, type: "text", inputmode: "decimal", maxlength: "12", "data-code": code, autocomplete: "off", "aria-describedby": "add-u-" + code + " add-err-" + code }),
        el("span", { class: "fx2__unit", id: "add-u-" + code, text: a.unit_ru })]),
      el("p", { class: "field__error", id: "add-err-" + code, hidden: true })
    ]);
  }
  function buildAddForm() {
    var groups = (C.ref && C.ref.form_groups) || [];
    var cbc = (groups.filter(function (g) { return g.id === "cbc"; })[0] || { analytes: [] }).analytes;
    var main = cbc.concat(MAIN_EXTRA.filter(function (c) { return C.byCode[c]; }));
    var mainGrid = el("div", { class: "fgrid" }, main.map(fieldNode));
    var more = el("details", { class: "fold add-more", id: "add-more" }, [el("summary", null, ["Другие показатели ", el("span", { class: "fold__n", id: "add-more-n", text: "0" })])]);
    groups.forEach(function (g) {
      var codes = g.analytes.filter(function (c) { return main.indexOf(c) < 0 && C.byCode[c]; });
      if (!codes.length) { return; }
      more.appendChild(el("p", { class: "add-more__t", text: g.title }));
      more.appendChild(el("div", { class: "fgrid" }, codes.map(fieldNode)));
    });
    var maxDate = todayIso();
    var form = el("form", { class: "add-form", id: "add-form", novalidate: true, autocomplete: "off", hidden: true }, [
      el("div", { class: "add-form__person" }, [
        el("div", { class: "fx" }, [el("label", { class: "fx__label", for: "add-date", text: "Дата анализа" }),
          el("input", { class: "fx__input", id: "add-date", type: "date", min: "1900-01-01", max: maxDate, required: true, "aria-describedby": "add-date-hint add-err-date" }),
          el("p", { class: "fx__hint fx__hint--warn", id: "add-date-hint", hidden: true, text: "Дату на бланке не нашли — стоит сегодняшняя. Исправьте, если анализ сдан в другой день." }),
          el("p", { class: "field__error", id: "add-err-date", hidden: true })]),
        el("fieldset", { class: "fx" }, [el("legend", { class: "fx__label", text: "Пол" }),
          el("div", { class: "seg2" }, [
            el("label", null, [el("input", { type: "radio", name: "add-sex", value: "F", checked: true }), el("span", { text: "Женский" })]),
            el("label", null, [el("input", { type: "radio", name: "add-sex", value: "M" }), el("span", { text: "Мужской" })])]),
          el("p", { class: "field__error", id: "add-err-sex", hidden: true })]),
        el("div", { class: "fx" }, [el("label", { class: "fx__label", for: "add-age", text: "Возраст, лет" }),
          el("input", { class: "fx__input", id: "add-age", type: "text", inputmode: "numeric", maxlength: "3", placeholder: "18–120", "aria-describedby": "add-err-age_years" }),
          el("p", { class: "field__error", id: "add-err-age_years", hidden: true })]),
        el("div", { class: "fx", id: "add-preg-box" }, [el("label", { class: "fx__label", for: "add-preg", text: "Беременность" }),
          el("select", { class: "fx__input", id: "add-preg", "aria-describedby": "add-err-pregnancy" }, [
            el("option", { value: "no", text: "нет" }), el("option", { value: "yes", text: "да" }), el("option", { value: "unknown", text: "неизвестно" })]),
          el("p", { class: "field__error", id: "add-err-pregnancy", hidden: true })])
      ]),
      el("p", { class: "add-form__sub", text: "Общий анализ крови и железо" }),
      mainGrid,
      more,
      el("div", { class: "add-refs", id: "add-refs", hidden: true }),
      el("p", { class: "hint", text: "Пустое поле — показатель не сдавали. Гемоглобин нужен обязательно. Десятичный разделитель — запятая или точка." }),
      el("div", { class: "cab-alert", id: "add-alert", role: "alert", hidden: true }),
      el("div", { class: "add-form__btns" }, [
        el("button", { type: "submit", class: "btn btn--primary", id: "add-save", text: "Сохранить в кабинет" }),
        el("button", { type: "button", class: "btn", id: "add-cancel", text: "Отмена" })])
    ]);
    form.addEventListener("input", function (e) {
      var t = e.target;
      if (t && t.id === "add-date") { markDate(false); }               // дату проверили и поправили
      if (t && t.getAttribute && t.getAttribute("aria-invalid")) {
        t.removeAttribute("aria-invalid");
        var err = $("add-err-" + (t.getAttribute("data-code") || (t.id === "add-age" ? "age_years" : t.id === "add-date" ? "date" : "")));
        if (err) { err.hidden = true; }
      }
      if (t && t.getAttribute && t.getAttribute("data-code")) {
        var f = t.closest(".fx2"); if (f) { f.classList.remove("fx2--check"); }
        countMore();
      }
    });
    form.addEventListener("change", function (e) { if (e.target && e.target.name === "add-sex") { syncPreg(); } });
    form.addEventListener("submit", function (e) { e.preventDefault(); saveRecord(); });
    form.querySelector("#add-cancel").addEventListener("click", function () { closeForm(); });
    return form;
  }
  function syncPreg() {
    var f = document.querySelector('input[name="add-sex"]:checked');
    $("add-preg-box").hidden = !(f && f.value === "F");
  }
  function prefillPerson() {
    var i = C.lastInput;
    if (!i || !$("add-age") || $("add-age").value) { return; }
    var r = document.querySelector('input[name="add-sex"][value="' + (i.sex === "M" ? "M" : "F") + '"]');
    if (r) { r.checked = true; }
    if (i.age_years) { $("add-age").value = String(i.age_years); }
    syncPreg();
  }
  function addInputs() { return Array.prototype.slice.call(document.querySelectorAll("#add-form input[data-code]")); }
  function resetValues() {
    addInputs().forEach(function (i) {
      i.value = ""; i.removeAttribute("data-unit"); i.removeAttribute("aria-invalid");
      var code = i.getAttribute("data-code");
      $("add-u-" + code).textContent = C.byCode[code].unit_ru;
      var f = i.closest(".fx2"); f.classList.remove("fx2--check");
      var chk = f.querySelector(".fx2__check"); if (chk) { f.removeChild(chk); }
    });
    clearErrors($("add-form"));
    countMore();
  }
  function setField(code, value, unit) {
    var input = $("add-v-" + code);
    if (!input) { return false; }
    input.value = (value === null || value === undefined) ? "" : String(value).replace(".", ",");
    if (unit) { input.setAttribute("data-unit", unit); $("add-u-" + code).textContent = unit; }
    var d = input.closest("details"); if (d && input.value) { d.open = true; }
    countMore();
    return true;
  }
  function markCheck(code) {
    var input = $("add-v-" + code);
    if (!input) { return; }
    var f = input.closest(".fx2");
    f.classList.add("fx2--check");
    if (!f.querySelector(".fx2__check")) { f.appendChild(el("p", { class: "fx2__check", text: "проверьте: распознано неуверенно" })); }
  }
  function countMore() {
    var n = Array.prototype.filter.call(document.querySelectorAll("#add-more input[data-code]"), function (i) { return i.value.trim() !== ""; }).length;
    if ($("add-more-n")) { $("add-more-n").textContent = String(n); }
  }
  function openForm(fresh) {
    var form = $("add-form");
    if (fresh) { resetValues(); ADD.refs = null; paintRefs(); markDate(false); }
    if (!$("add-date").value) { $("add-date").value = todayIso(); }
    prefillPerson();
    syncPreg();
    showAlert($("add-alert"), "");
    form.hidden = false;
    form.classList.remove("is-in"); void form.offsetWidth; form.classList.add("is-in");
    if (fresh) { var first = $("add-v-hemoglobin") || form.querySelector("input[data-code]"); if (first) { first.focus({ preventScroll: true }); } }
    scrollToNode(fresh ? form : $("add-status"));
  }
  function closeForm() {
    $("add-form").hidden = true;
    resetValues();
    $("add-date").value = "";
    markDate(false);
    ADD.refs = null; ADD.source = "form";
    paintRefs();
    setStatus(null, "");
    markMethod(null);
    $("add-text").hidden = true;
  }
  function collectRecord() {
    var form = $("add-form");
    clearErrors(form); showAlert($("add-alert"), "");
    var date = $("add-date").value;
    if (!isoParts(date)) { fieldError($("add-date"), $("add-err-date"), "Укажите дату анализа."); return null; }
    if (date > todayIso()) { fieldError($("add-date"), $("add-err-date"), "Дата анализа не может быть в будущем."); return null; }
    var ageT = $("add-age").value.trim(), age = /^\d{1,3}$/.test(ageT) ? parseInt(ageT, 10) : NaN;
    if (isNaN(age) || age < 18 || age > 120) { fieldError($("add-age"), $("add-err-age_years"), "Укажите возраст целым числом от 18 до 120."); return null; }
    var values = {}, bad = null;
    addInputs().forEach(function (i) {
      if (bad) { return; }
      var raw = i.value.trim();
      if (!raw) { return; }
      var v = parseNum(raw), code = i.getAttribute("data-code");
      if (isNaN(v)) { bad = [i, $("add-err-" + code), "Введите число, например 12,5."]; return; }
      var unit = i.getAttribute("data-unit");
      values[code] = unit ? { value: v, unit: unit } : v;
    });
    if (bad) { fieldError(bad[0], bad[1], bad[2]); return null; }
    if (values.hemoglobin === undefined) { fieldError($("add-v-hemoglobin"), $("add-err-hemoglobin"), "Гемоглобин нужен, чтобы сохранить и оценить анализ."); return null; }
    var sex = document.querySelector('input[name="add-sex"]:checked').value;
    var input = { sex: sex, age_years: age, values: values };
    if (sex === "F") { input.pregnancy = { status: $("add-preg").value }; }
    if (ADD.refs && $("add-refs-on") && $("add-refs-on").checked) { input.reference_ranges = ADD.refs; }
    return { date: date, input: input, source: ADD.source || "form" };
  }
  function saveRecord() {
    var body = collectRecord();
    if (!body) { return; }
    var btn = $("add-save");
    busy(btn, true, "Сохраняем…");
    api("POST", "/api/v1/me/records", body).then(function (r) {
      busy(btn, false);
      if (r.ok && r.data && r.data.record) {
        var rec = r.data.record;
        C.lastInput = body.input;
        C.recCache[rec.id] = { ok: true, status: 200, data: { record: Object.assign({ input: body.input }, rec), result: r.data.result } };
        closeForm();
        var done = $("add-done");
        clear(done);
        var see = el("button", { type: "button", class: "link-btn", text: "Открыть в ленте" });
        see.addEventListener("click", function () { openRecordIn($("p-feed"), rec.id); });
        var more = el("button", { type: "button", class: "link-btn", text: "Добавить ещё" });
        more.addEventListener("click", function () { done.hidden = true; markMethod("manual"); openForm(true); });
        done.appendChild(el("span", { class: "add-done__ico", "aria-hidden": "true" }, [icon("check")]));
        done.appendChild(el("p", null, [el("b", { text: "Анализ от " + ruDate(rec.date) + " сохранён." }), " Графики и подсказки обновлены. "]));
        done.appendChild(el("p", { class: "add-done__btns" }, [see, more]));
        done.hidden = false;
        scrollToNode($("sec-add"));
        toast("Анализ от " + ruDate(rec.date) + " сохранён", "ok");
        refreshPatient();
        return;
      }
      var f = errField(r), msg = errText(r);
      var map = { date: ["add-date", "add-err-date"], age_years: ["add-age", "add-err-age_years"], pregnancy: ["add-preg", "add-err-pregnancy"], sex: [null, "add-err-sex"] };
      if (r.status === 422 && f) {
        var key = f.replace(/^input\./, "").replace(/^values\./, "");
        if (map[key]) { fieldError(map[key][0] ? $(map[key][0]) : null, $(map[key][1]), msg); return; }
        if ($("add-v-" + key)) { fieldError($("add-v-" + key), $("add-err-" + key), msg); return; }
      }
      if (errCode(r) === "missing_hemoglobin") { fieldError($("add-v-hemoglobin"), $("add-err-hemoglobin"), msg); return; }
      showAlert($("add-alert"), msg);
    });
  }

  // ---------- «Врачи с доступом»: ссылка, связи, журнал ----------
  var SHARE_DAYS = [1, 7, 30, 90];
  function buildShareNew() {
    var box = $("p-share-new");
    clear(box);
    var seg = el("div", { class: "seg2 seg2--4", role: "radiogroup", "aria-label": "Срок доступа" }, SHARE_DAYS.map(function (d) {
      return el("label", null, [el("input", { type: "radio", name: "share-days", value: String(d), checked: d === 7 }), el("span", { text: count(d, "день", "дня", "дней") })]);
    }));
    var btn = el("button", { type: "button", class: "btn btn--primary", id: "share-make", text: "Создать ссылку для врача" });
    var out = el("div", { class: "share-out", id: "share-out", hidden: true });
    box.appendChild(el("h3", { class: "cab-card__t" }, [icon("link", "ico ico--t"), "Поделиться с врачом"]));
    box.appendChild(el("p", { class: "cab-card__p", text: "Врач откроет ссылку в своём кабинете и увидит историю анализов, графики и отчёты для врача. На какой срок открыть доступ?" }));
    box.appendChild(seg);
    box.appendChild(el("div", { class: "cab-card__btns" }, [btn]));
    box.appendChild(out);
    btn.addEventListener("click", function () {
      var days = parseInt(document.querySelector('input[name="share-days"]:checked').value, 10);
      busy(btn, true, "Создаём…");
      api("POST", "/api/v1/me/shares", { days: days }).then(function (r) {
        busy(btn, false);
        if (!r.ok || !r.data || !r.data.link) { toast(errText(r), "warn"); return; }
        var url = window.location.origin + r.data.link;
        clear(out);
        var field = el("input", { class: "fx__input share-out__url", type: "text", readonly: true, value: url, "aria-label": "Ссылка для врача" });
        field.addEventListener("focus", function () { field.select(); });
        var row = el("div", { class: "share-out__row" }, [field, copyButton(function () { return url; }, null, field)]);
        if (navigator.share) {
          var sh = el("button", { type: "button", class: "btn btn--small", text: "Отправить" });
          sh.addEventListener("click", function () { navigator.share({ title: "Доступ к истории анализов", url: url }).catch(function () {}); });
          row.appendChild(sh);
        }
        out.appendChild(el("p", { class: "share-out__t", text: "Ссылка готова — отправьте её врачу" }));
        out.appendChild(row);
        out.appendChild(el("ul", { class: "share-out__notes" }, [
          el("li", { text: "Ссылка одноразовая и действует 48 ч." }),
          el("li", { text: "Врач должен войти как врач — со своим аккаунтом." }),
          el("li", { text: "После принятия доступ открыт на " + count(days, "день", "дня", "дней") + "; закрыть его можно в любой момент." }),
          el("li", { text: "Ссылка показана один раз: сохраните её или создайте новую." })
        ]));
        out.hidden = false;
        field.focus();
        refreshShares();
      });
    });
  }
  function refreshShares() {
    return Promise.all([api("GET", "/api/v1/me/shares"), api("GET", "/api/v1/me/access-log")]).then(function (rs) {
      C.shares = (rs[0].ok && rs[0].data && rs[0].data.shares) || [];
      C.log = (rs[1].ok && rs[1].data && rs[1].data.items) || [];
      paintShares();
      paintLog();
    });
  }
  // Врач связи или строки журнала: логин и клиника; удалил аккаунт — «логин (аккаунт удалён)» по снимку сервера.
  function doctorName(d, withClinic) {
    if (!d) { return null; }
    return d.login + (d.deleted ? " (аккаунт удалён)" : "") + (withClinic && d.clinic ? " · " + d.clinic : "");
  }
  // Одно слово на действие — «Закрыть доступ» (и для открытой связи, и для ещё не принятой ссылки).
  function shareRow(s) {
    var d = s.doctor;
    var who = d ? doctorName(d, true) : (s.accepted ? "Врач (аккаунт удалён)" : "Ссылка ещё не открыта");
    var meta, kind, label;
    if (s.status === "active") { var left = daysLeft(s.expires); label = "доступ открыт"; kind = "ok"; meta = "до " + localDay(s.expires) + (left !== null ? " · осталось " + count(left, "день", "дня", "дней") : ""); }
    else if (s.status === "pending") { label = "ждёт врача"; kind = "info"; meta = "ссылка действует до " + ruDateTime(s.expires) + " · доступ на " + count(s.days, "день", "дня", "дней"); }
    else if (s.status === "expired") { label = "срок истёк"; kind = "none"; meta = localDay(s.expires); }
    else { label = "доступ закрыт"; kind = "none"; meta = s.accepted ? "был открыт " + localDay(s.accepted) : "ссылка не использована"; }
    var kids = [
      el("span", { class: "shr__ava" + (d ? "" : " shr__ava--link"), "aria-hidden": "true" }, [d ? initials(d.login) : icon("link", "ico ico--sm")]),
      el("div", { class: "shr__body" }, [el("p", { class: "shr__who" }, [who, chip(label, kind)]), el("p", { class: "shr__meta", text: meta })])
    ];
    if (s.status === "active" || s.status === "pending") {
      var b = el("button", { type: "button", class: "btn btn--small btn--ghostdanger", text: "Закрыть доступ" });
      b.addEventListener("click", function () { revokeShare(s); });
      kids.push(b);
    }
    return el("li", { class: "shr shr--" + s.status }, kids);
  }
  function paintShares() {
    var box = $("p-shares");
    clear(box);
    box.appendChild(el("h3", { class: "cab-card__t" }, [icon("people", "ico ico--t"), "Связи с врачами"]));
    var live = C.shares.filter(function (s) { return s.status === "active" || s.status === "pending"; });
    var past = C.shares.filter(function (s) { return s.status !== "active" && s.status !== "pending"; });
    if (!live.length) {
      box.appendChild(emptyState("people", "Доступа нет ни у одного врача", "Создайте ссылку в блоке «Поделиться с врачом» и отправьте её врачу — после принятия он появится здесь."));
    } else {
      box.appendChild(el("ul", { class: "shrs" }, live.map(shareRow)));
    }
    if (past.length) {
      box.appendChild(el("details", { class: "fold fold--more" }, [el("summary", { text: "Прошлые: " + past.length }), el("ul", { class: "shrs shrs--past" }, past.map(shareRow))]));
    }
  }
  function revokeShare(s) {
    var active = s.status === "active";
    openDialog({
      title: active ? "Закрыть доступ врачу " + (s.doctor ? s.doctor.login : "") + "?" : "Закрыть доступ по ссылке?",
      text: active ? "Врач сразу перестанет видеть вашу историю. Чтобы открыть доступ снова, создайте новую ссылку." : "Ссылка перестанет работать, даже если врач её ещё не открыл.",
      okText: "Закрыть доступ", danger: true,
      onOk: function () {
        return api("DELETE", "/api/v1/me/shares/" + s.id).then(function (r) {
          if (!r.ok) { return errText(r); }
          toast("Доступ закрыт", "ok");
          refreshShares();
          return null;
        });
      }
    });
  }
  function paintLog() {
    var box = $("p-log");
    clear(box);
    box.appendChild(el("h3", { class: "cab-card__t" }, [icon("eye", "ico ico--t"), "Журнал просмотров"]));
    if (!C.log.length) {
      box.appendChild(el("p", { class: "cab-card__p", text: "Врачи пока не открывали вашу историю. Каждый просмотр истории или отдельного анализа появится здесь." }));
      return;
    }
    var byId = {};
    C.records.forEach(function (r) { byId[r.id] = r; });
    var rows = C.log.map(function (it) {
      var d = it.doctor;
      var what = it.what === "record" ? ("открыл анализ" + (byId[it.record_id] ? " от " + ruDate(byId[it.record_id].date) : (it.record_id ? "" : "ы")))
        : it.what === "records" ? "открыл список анализов" : "открыл историю анализов";
      return el("li", { class: "log" }, [
        el("span", { class: "log__time", text: ruDateTime(it.at) }),
        el("span", { class: "log__who", text: d ? doctorName(d, true) : "Врач (аккаунт удалён)" }),
        el("span", { class: "log__what", text: what })
      ]);
    });
    var list = el("ol", { class: "logs" }, rows.slice(0, 8));
    box.appendChild(list);
    if (rows.length > 8) { box.appendChild(el("details", { class: "fold fold--more" }, [el("summary", { text: "ещё " + (rows.length - 8) }), el("ol", { class: "logs" }, rows.slice(8))])); }
  }

  // ---------- «Мои данные»: выгрузка, загрузка ----------
  function dataTile(title, text, action) {
    return el("div", { class: "data-tile" }, [el("p", { class: "data-tile__t", text: title }), el("p", { class: "data-tile__p", text: text }), action]);
  }
  function buildData() {
    var box = $("p-data");
    clear(box);
    // Выгрузка — обычная ссылка (GET с cookie сайта): сервер отдаёт файл с Content-Disposition: attachment.
    // В имени файла — местная дата (?day=): сервер считает «сегодня» по UTC, а у пациента ночью дата уже другая.
    ["json", "csv"].forEach(function (fmt) {
      var a = el("a", { class: "btn btn--small", href: "/api/v1/me/export?format=" + fmt, download: "", "aria-label": "Скачать все анализы, " + fmt.toUpperCase() }, [dlIcon(), fmt.toUpperCase()]);
      var stamp = function () { a.setAttribute("href", "/api/v1/me/export?format=" + fmt + "&day=" + todayIso()); };
      a.addEventListener("pointerdown", stamp);
      a.addEventListener("focus", stamp);
      a.addEventListener("click", stamp);
      stamp();
      box.appendChild(fmt === "json" ? dataTile("Скачать JSON", "Все анализы с датами — чтобы перенести в другой аккаунт.", a)
        : dataTile("Скачать CSV", "Таблица в схеме кейса: пол, возраст, показатели, дата.", a));
    });
    var file = el("input", { type: "file", id: "import-file", class: "vh", accept: ".json,application/json" });
    var lab = el("label", { class: "btn btn--small", for: "import-file", tabindex: "0", role: "button" }, ["Загрузить JSON"]);
    lab.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
    box.appendChild(dataTile("Загрузить JSON", "Файл выгрузки Justmedit: новые анализы добавятся к истории, уже сохранённые не повторятся.", el("span", null, [file, lab])));
    file.addEventListener("change", function () {
      var f = file.files[0];
      file.value = "";
      if (!f) { return; }
      if (f.size > 10 * 1024 * 1024) { toast("Файл больше 10 МБ", "warn"); return; }
      var reader = new FileReader();
      reader.onload = function () {
        var data;
        try { data = JSON.parse(String(reader.result)); } catch (e) { toast("Это не JSON-файл выгрузки", "warn"); return; }
        var n = data && Array.isArray(data.records) ? data.records.length : 0;
        if (!n) { toast("В файле нет анализов для загрузки", "warn"); return; }
        openDialog({
          title: "Загрузить " + count(n, "анализ", "анализа", "анализов") + "?",
          text: "Новые анализы из файла добавятся к вашей истории. Те, что уже есть в кабинете (та же дата и те же значения), второй раз не загрузятся. Каждый новый анализ будет проверен и пересчитан.",
          okText: "Загрузить",
          onOk: function () {
            // ошибка (например, анализ № 3 в файле) остаётся в диалоге, а не в коротком всплывающем сообщении
            return api("POST", "/api/v1/me/import", data).then(function (r) {
              if (!r.ok || !r.data) { return errText(r); }
              var got = r.data.imported || 0, dup = r.data.skipped || 0;
              toast(got ? "Загружено " + count(got, "анализ", "анализа", "анализов") + (dup ? ", дублей пропущено " + dup : "")
                : "Новых анализов нет: " + (dup === 1 ? "этот анализ уже" : "все " + dup + " уже") + " в кабинете", "ok");
              if (got) { refreshPatient(); }
              return null;
            });
          }
        });
      };
      reader.readAsText(f);
    });
  }

  // ---------- токены API (пациент и врач) ----------
  var tokenBox = null;
  function tokensCard(box, withTitle) {
    tokenBox = box;
    clear(box);
    if (withTitle) {
      box.appendChild(el("h3", { class: "cab-card__t" }, [icon("table", "ico ico--t"), "Токены API"]));
      box.appendChild(el("p", { class: "cab-card__p" }, ["Для приложений и сайтов, которые работают с вашим кабинетом: заголовок ", el("code", { text: "Authorization: Bearer …" }), ". ", el("a", { href: "/developers", text: "Документация API" })]));
    }
    var name = el("input", { class: "fx__input", id: "tok-name", type: "text", maxlength: "60", placeholder: "Например, «Мой сайт»", "aria-label": "Название токена" });
    var make = el("button", { type: "button", class: "btn btn--small", text: "Создать токен" });
    var err = el("p", { class: "cab-alert tok-err", id: "tok-err", role: "alert", hidden: true });
    box.appendChild(el("div", { class: "tok-new" }, [name, make]));
    box.appendChild(err);
    box.appendChild(el("div", { class: "tok-reveal", id: "tok-reveal", hidden: true }));
    box.appendChild(el("ul", { class: "toks", id: "tok-list" }));
    box.appendChild(el("p", { class: "tok-logins", id: "tok-logins", hidden: true }));
    name.addEventListener("input", function () { showAlert(err, ""); });
    make.addEventListener("click", function () {
      busy(make, true, "Создаём…");
      showAlert(err, "");
      var body = name.value.trim() ? { name: name.value.trim() } : {};
      api("POST", "/api/v1/me/tokens", body).then(function (r) {
        busy(make, false);
        // 422 too_many_tokens — предел именованных токенов; текст остаётся у формы, пока не отзовут лишний
        if (!r.ok || !r.data || !r.data.value) { showAlert(err, errText(r)); return; }
        name.value = "";
        var value = r.data.value, rv = $("tok-reveal");
        clear(rv);
        rv.appendChild(el("p", { class: "tok-reveal__t", text: "Скопируйте токен сейчас — потом его не будет видно" }));
        var code = el("code", { class: "tok-reveal__v", text: value });
        rv.appendChild(el("div", { class: "tok-reveal__row" }, [code, copyButton(function () { return value; }, null, code)]));
        rv.appendChild(el("p", { class: "tok-reveal__p", text: "Действует 30 дней. Храните его как пароль: с ним можно читать и менять данные кабинета." }));
        rv.hidden = false;
        refreshTokens();
      });
    });
  }
  // Список — только токены приложений (kind = named). Токены входа (kind = login: пара cookie сайта и ответы
  // /auth/login) — техническая деталь: вместо них строка «Входов: N» со ссылкой на «Выйти на всех устройствах».
  function isLoginToken(t) { return t.kind ? t.kind === "login" : t.name === "Вход"; }
  function refreshTokens() {
    return api("GET", "/api/v1/me/tokens").then(function (r) {
      C.tokens = (r.ok && r.data && r.data.tokens) || [];
      var ul = $("tok-list");
      if (!ul) { return; }
      clear(ul);
      var named = C.tokens.filter(function (t) { return !isLoginToken(t); });
      var logins = C.tokens.length - named.length;
      var info = $("tok-logins");
      if (info) {
        clear(info);
        info.hidden = !logins;
        var outBtn = el("button", { type: "button", class: "link-btn", text: "Выйти на всех устройствах" });
        outBtn.addEventListener("click", function () { var b = $("acc-out"); if (b) { scrollToNode(b); b.focus({ preventScroll: true }); } });
        append(info, ["Активных входов на сайте и в приложениях: " + logins + ". Закрыть другие — ", outBtn, "."]);
      }
      if (!named.length) { ul.appendChild(el("li", { class: "tok tok--empty", text: "Токенов для приложений нет." })); return; }
      named.forEach(function (t) {
        var b = el("button", { type: "button", class: "btn btn--small btn--ghostdanger", text: "Отозвать", "aria-label": "Отозвать токен «" + t.name + "»" });
        b.addEventListener("click", function () {
          openDialog({
            title: "Отозвать токен «" + t.name + "»?", danger: true, okText: "Отозвать",
            text: t.current ? "Этим токеном подписан текущий запрос: после отзыва этот вход и приложения с ним перестанут работать." : "Приложения с этим токеном сразу потеряют доступ к кабинету.",
            onOk: function () {
              return api("DELETE", "/api/v1/me/tokens/" + t.id).then(function (rr) {
                if (!rr.ok) { return errText(rr); }
                toast("Токен отозван", "ok");
                showAlert($("tok-err"), "");
                refreshTokens();
                return null;
              });
            }
          });
        });
        // last_used = null — токеном ещё не пользовались (сервер ставит отметку с первого запроса этим токеном)
        ul.appendChild(el("li", { class: "tok" }, [
          el("div", { class: "tok__body" }, [el("p", { class: "tok__name", text: t.name }),
            el("p", { class: "tok__meta", text: "создан " + localDay(t.created) + " · до " + localDay(t.expires) +
              (t.last_used ? " · использован " + ruDateTime(t.last_used) : " · ещё не использовался") })]),
          b]));
      });
    });
  }

  // ---------- удаление аккаунта ----------
  function deleteCard(box, role) {
    clear(box);
    var patient = role === "patient";
    box.appendChild(el("div", { class: "danger-zone__body" }, [
      el("h3", { class: "cab-card__t", text: patient ? "Удалить всё" : "Удалить аккаунт" }),
      el("p", { class: "cab-card__p", text: patient ? "Аккаунт, все анализы, ссылки и доступы врачей, журнал просмотров — стираются сразу и целиком. Перед удалением можно скачать JSON." : "Аккаунт, референсы лаборатории, токены и все связи с пациентами — стираются сразу и целиком." })
    ]));
    var b = el("button", { type: "button", class: "btn btn--danger", text: patient ? "Удалить всё…" : "Удалить аккаунт…" });
    box.appendChild(b);
    b.addEventListener("click", function () {
      openDialog({
        title: patient ? "Удалить всё без возможности восстановления?" : "Удалить аккаунт врача?",
        danger: true, okText: patient ? "Удалить всё" : "Удалить аккаунт",
        text: "Будет удалено:",
        list: patient ? ["аккаунт «" + C.me.login + "»", "все анализы и отчёты (" + C.records.length + ")", "ссылки и доступы врачей", "журнал просмотров и токены API"]
                      : ["аккаунт «" + C.me.login + "»", "референсы лаборатории", "доступы к пациентам", "токены API"],
        ack: "Понимаю: восстановить данные будет нельзя",
        password: true,
        onOk: function (pw) {
          return api("DELETE", "/api/v1/me", { password: pw }).then(function (r) {
            // экран-подтверждение: страница входа с плашкой «Аккаунт и все его данные удалены» (?reason=deleted)
            if (r.ok) { onUnauthorized = null; setNav(null); window.location.replace("/login?reason=deleted"); return new Promise(function () {}); }
            if (errCode(r) === "wrong_password") { return "Неверный пароль: ничего не удалено."; }
            return errText(r, "password");
          });
        }
      });
    });
  }

  // ---------- вход и безопасность: смена пароля, «выйти на всех устройствах» (пациент и врач) ----------
  // POST /api/v1/me/password {old, new} — остальные сеансы и все токены API отзываются, текущий вход остаётся;
  // POST /api/v1/me/logout-all {} — то же без смены пароля; {"all": true} — и этот вход (cookie стирается).
  // С cookie сайта пароль для logout-all не нужен (docs/CONTRACTS.md, «Безопасность кабинета»).
  function pwField(id, label, autocomplete, hint) {
    return el("div", { class: "fx" }, [
      el("label", { class: "fx__label", for: id, text: label }),
      el("div", { class: "pw" }, [
        el("input", { class: "fx__input", id: id, type: "password", autocomplete: autocomplete, maxlength: "128", required: true, "aria-describedby": (hint ? id + "-hint " : "") + "err-" + id }),
        el("button", { type: "button", class: "pw__eye", "data-for": id, "aria-pressed": "false", "aria-label": "Показать пароль" })]),
      hint ? el("p", { class: "fx__hint", id: id + "-hint", text: hint }) : null,
      el("p", { class: "field__error", id: "err-" + id, hidden: true })
    ]);
  }
  function accountCard(box) {
    clear(box);
    var alert = el("p", { class: "cab-alert", id: "acc-alert", role: "alert", hidden: true });
    var done = el("p", { class: "acc-done", id: "acc-done", role: "status", hidden: true });
    var save = el("button", { type: "submit", class: "btn btn--primary btn--small", id: "acc-save", text: "Сменить пароль" });
    var meter = el("span", { class: "pw-meter__bar", id: "acc-meter" });
    // Скрытое поле логина — менеджер паролей привяжет новый пароль к этому аккаунту.
    var form = el("form", { class: "acc-form", id: "acc-form", novalidate: true }, [
      el("input", { type: "text", class: "vh", name: "username", autocomplete: "username", value: C.me.login, readonly: true, tabindex: "-1", "aria-hidden": "true" }),
      pwField("acc-old", "Текущий пароль", "current-password"),
      pwField("acc-new", "Новый пароль", "new-password", "Не короче 8 символов; лучше — от 12."),
      el("span", { class: "pw-meter", "aria-hidden": "true" }, [meter]),
      pwField("acc-new2", "Новый пароль ещё раз", "new-password"),
      alert, done,
      el("div", { class: "cab-card__btns" }, [save])
    ]);
    var out = el("button", { type: "button", class: "btn btn--small", id: "acc-out", text: "Выйти на всех устройствах…" });
    var outDone = el("p", { class: "acc-done", id: "acc-out-done", role: "status", hidden: true });
    box.appendChild(el("h3", { class: "cab-card__t" }, [icon("lock", "ico ico--t"), "Вход и безопасность"]));
    box.appendChild(el("div", { class: "acc" }, [
      el("div", { class: "acc__col" }, [
        el("p", { class: "acc__t", text: "Сменить пароль" }),
        el("p", { class: "cab-card__p", text: "После смены пароля закроются все остальные сеансы и перестанут работать токены API — этот вход останется." }),
        form]),
      el("div", { class: "acc__col acc__col--out" }, [
        el("p", { class: "acc__t", text: "Выйти на всех устройствах" }),
        el("p", { class: "cab-card__p", text: "Если вы входили с чужого компьютера или потеряли телефон: закроем входы на других устройствах и отзовём токены API. Можно выйти и здесь." }),
        el("div", null, [out]), outDone])
    ]));
    initPasswordToggles(box);
    form.addEventListener("input", function (e) {
      var t = e.target;
      if (t && t.getAttribute("aria-invalid")) { t.removeAttribute("aria-invalid"); var er = $("err-" + t.id); if (er) { er.hidden = true; } }
      if (t && t.id === "acc-new") { var n = t.value.length; meter.className = "pw-meter__bar" + (n === 0 ? "" : n < 8 ? " is-weak" : n < 12 ? " is-ok" : " is-strong"); }
      showAlert(alert, ""); done.hidden = true;
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      clearErrors(form); showAlert(alert, ""); done.hidden = true;
      var old = $("acc-old").value, nw = $("acc-new").value, nw2 = $("acc-new2").value;
      if (!old) { fieldError($("acc-old"), $("err-acc-old"), "Введите текущий пароль."); return; }
      if (nw.length < 8) { fieldError($("acc-new"), $("err-acc-new"), "Новый пароль — не меньше 8 символов."); return; }
      if (nw === old) { fieldError($("acc-new"), $("err-acc-new"), "Новый пароль совпадает с текущим."); return; }
      if (nw2 !== nw) { fieldError($("acc-new2"), $("err-acc-new2"), "Пароли не совпадают."); return; }
      busy(save, true, "Меняем…");
      api("POST", "/api/v1/me/password", { old: old, "new": nw }).then(function (r) {
        busy(save, false);
        if (r.ok) {
          form.reset(); meter.className = "pw-meter__bar";
          done.textContent = "Пароль изменён, остальные сеансы закрыты.";
          done.hidden = false;
          toast("Пароль изменён, остальные сеансы закрыты", "ok");
          refreshTokens();
          return;
        }
        if (errCode(r) === "wrong_password") { fieldError($("acc-old"), $("err-acc-old"), "Неверный текущий пароль: пароль не изменён."); $("acc-old").select(); return; }
        if (errField(r) === "new") { fieldError($("acc-new"), $("err-acc-new"), errText(r)); return; }
        showAlert(alert, errText(r, "password"));
        if (r.status === 429) { cooldown(save, retrySec(r), function () { showAlert(alert, ""); }); }
      });
    });
    out.addEventListener("click", function () {
      openDialog({
        title: "Выйти на всех устройствах?", okText: "Выйти везде",
        text: ["Закроем входы на всех других устройствах и в других браузерах. Токены API — и для приложений — перестанут работать: при необходимости создайте новые.",
          "Пароль не меняется. Если подозреваете, что его знает кто-то ещё, — лучше смените пароль."],
        option: "И на этом устройстве тоже",
        onOk: function (pw, here) {
          return api("POST", "/api/v1/me/logout-all", here ? { all: true } : {}).then(function (r) {
            if (!r.ok) { return errText(r); }
            if (here) { onUnauthorized = null; setNav(null); window.location.href = "/"; return null; }
            // revoked считает строки сессий: вход на сайте — это cookie и её токен «Вход», поэтому без числа
            outDone.textContent = (r.data && r.data.revoked) ? "Готово: входы на других устройствах и токены API закрыты. Этот вход остался."
              : "Других входов не было — остался только этот.";
            outDone.hidden = false;
            toast("Остальные сеансы закрыты", "ok");
            refreshTokens();
            return null;
          });
        }
      });
    });
  }

  // =================================================================================================
  // Кабинет врача
  // =================================================================================================
  function initDoctor() {
    var clinic = C.me.clinic;
    $("cab-kicker").textContent = "Кабинет врача";
    $("cab-h").textContent = "Кабинет врача";
    // Клиника — отдельной строкой под заголовком (длинное название — не больше двух строк, полностью — в title).
    if (clinic && $("cab-clinic")) {
      $("cab-clinic").textContent = clinic;
      $("cab-clinic").setAttribute("title", clinic);
      $("cab-clinic").hidden = false;
    }
    heroCta([{ text: "Расшифровать анализ", href: "/", primary: true }, { text: "Референсы лаборатории", href: "#sec-labrefs" }]);
    showRole("cab-doctor");
    tokensCard($("d-tokens"), false);
    accountCard($("d-account"));
    deleteCard($("d-delete"), "doctor");
    return Promise.all([refreshPatients(), refreshLabRefs()]).then(function () {
      C.doctorReady = true;
      paintDoctorHero();
      refreshTokens();
      var m = /^#p-([0-9a-f]{32})$/.exec(window.location.hash || "");
      if (m) { openPatient(m[1]); } else { scrollToHash(); }
    });
  }
  function paintDoctorHero() {
    var n = C.patients.length, def = C.labrefs.filter(function (s) { return s.default; })[0];
    $("cab-lead").textContent = "Здравствуйте, " + C.me.login + ". " + (n ? "Пациентов с действующим доступом: " + n + "." : "Сейчас действующих доступов нет — пациент создаёт ссылку в своём кабинете.");
    heroTiles([
      { k: "Пациентов с доступом", v: String(n), s: n ? "по ссылкам пациентов" : "ждём ссылку пациента" },
      { k: "Наборов референсов", v: String(C.labrefs.length), s: C.labrefs.length ? "своей лаборатории" : "в расчётах — нормы проекта" },
      { k: "Референсы в расчётах", v: def ? def.name : "Нормы проекта", s: def ? "ваш набор по умолчанию" : "свой набор не выбран", kind: def ? "ok" : "" }
    ]);
  }
  function refreshPatients() {
    return api("GET", "/api/v1/doctor/patients").then(function (r) {
      C.patients = (r.ok && r.data && r.data.patients) || [];
      paintPatients();
      if (C.doctorReady) { paintDoctorHero(); }
    });
  }
  function paintPatients() {
    var box = $("d-patients");
    clear(box);
    // Пациент закрыл доступ, пока карточка была открыта: короткое объяснение над списком (до следующего выбора).
    if (C.lostNote) { box.appendChild(el("p", { class: "cab-note", role: "status", text: C.lostNote })); }
    if (!C.patients.length) {
      box.appendChild(el("div", { class: "cab-card" }, [emptyState("people", "Сейчас действующих доступов нет",
        "Пациент создаёт ссылку в своём кабинете («Врачи с доступом» → «Поделиться с врачом») и пересылает её вам. Откройте ссылку, войдя как врач, — пациент появится здесь. Закрытые и истёкшие доступы в списке не показываются.")]));
      return;
    }
    var grid = el("ul", { class: "pats" });
    C.patients.forEach(function (p) {
      var left = daysLeft(p.expires);
      var b = el("button", { type: "button", class: "pat" + (C.patient && C.patient.id === p.id ? " is-on" : ""), "aria-pressed": C.patient && C.patient.id === p.id ? "true" : "false" }, [
        el("span", { class: "pat__ava", "aria-hidden": "true", text: initials(p.login) }),
        el("span", { class: "pat__main" }, [
          el("span", { class: "pat__name", text: p.login }),
          el("span", { class: "pat__meta", text: "доступ до " + localDay(p.expires) + (left !== null ? " · осталось " + count(left, "день", "дня", "дней") : "") }),
          el("span", { class: "pat__meta", text: count(p.n_records, "запись", "записи", "записей") + (p.last_date ? " · последняя " + ruDate(p.last_date) : "") }),
          p.last_headline ? el("span", { class: "pat__head", text: p.last_headline }) : null
        ]),
        el("span", { class: "pat__go", "aria-hidden": "true", text: "→" })
      ]);
      b.addEventListener("click", function () { openPatient(p.id); });
      grid.appendChild(el("li", null, [b]));
    });
    box.appendChild(grid);
  }
  function openPatient(pid) {
    var p = C.patients.filter(function (x) { return x.id === pid; })[0];
    var box = $("d-patient");
    if (!p) { showAlert($("cab-alert"), "Нет действующего доступа к этому пациенту: попросите новую ссылку."); return; }
    showAlert($("cab-alert"), "");
    C.patient = p;
    C.lostNote = null;
    paintPatients();
    $("d-patients").hidden = true;
    box.hidden = false;
    clear(box);
    var back = el("button", { type: "button", class: "dpat__back" }, [el("span", { "aria-hidden": "true", text: "←" }), " Все пациенты"]);
    function closeCard() {
      C.patient = null; clear(box); box.hidden = true; $("d-patients").hidden = false; paintPatients();
      if (window.history && window.history.replaceState) { window.history.replaceState(null, "", "/cabinet"); }
      scrollToNode($("sec-patients"));
    }
    back.addEventListener("click", closeCard);
    // 403 на историю, ленту или отчёт — пациент закрыл доступ (или срок истёк): карточку с графиком и лентой
    // закрываем, над списком — объяснение, список пациентов — заново.
    var lost = false;
    function accessLost() {
      if (lost || !here()) { return; }
      lost = true;
      C.lostNote = "Пациент " + p.login + " закрыл доступ (или его срок истёк) — история больше недоступна. Чтобы снова её увидеть, попросите новую ссылку.";
      closeCard();
      toast("Пациент закрыл доступ", "warn");
      refreshPatients();
    }
    var left = daysLeft(p.expires);
    var head = el("div", { class: "cab-card dpat__head" }, [
      back,
      el("div", { class: "dpat__who" }, [
        el("span", { class: "pat__ava pat__ava--lg", "aria-hidden": "true", text: initials(p.login) }),
        el("div", null, [el("h3", { class: "dpat__name", text: p.login }),
          el("p", { class: "dpat__meta", id: "dpat-meta", text: "доступ до " + localDay(p.expires) + (left !== null ? " · осталось " + count(left, "день", "дня", "дней") : "") + " · " + count(p.n_records, "запись", "записи", "записей") })])
      ]),
      el("p", { class: "dpat__sum", id: "dpat-sum" }, [el("span", { class: "spinner", "aria-hidden": "true" }), " Загружаем историю…"])
    ]);
    var chartBox = el("div", { class: "cab-card chart-card", id: "d-chart" });
    var evBox = el("div", { class: "cab-card", id: "d-events" });
    var todoBox = el("div", { class: "cab-card", id: "d-todo" });
    var feedBox = el("div", { id: "d-feed" });
    append(box, [head,
      el("h3", { class: "dpat__h", text: "Динамика" }), chartBox,
      el("div", { class: "cab-grid2" }, [
        el("div", null, [el("h3", { class: "dpat__h", text: "События по истории" }), evBox]),
        el("div", null, [el("h3", { class: "dpat__h", text: "Полнота обследования" }), todoBox])]),
      el("h3", { class: "dpat__h", text: "Анализы" }), feedBox]);
    if (window.history && window.history.replaceState) { window.history.replaceState(null, "", "/cabinet#p-" + p.id); }
    scrollToNode($("sec-patients"));
    var base = "/api/v1/doctor/patients/" + p.id;
    var cache = {};
    var here = function () { return C.patient === p && document.body.contains(feedBox); };   // врач не ушёл к другому
    // История (графики, события, полнота): занят слот тяжёлых расчётов — один автоповтор, потом «Повторить».
    function loadHistory() {
      return getHistory(base + "/history").then(function (r) {
        if (!here()) { return; }
        var sum = $("dpat-sum");
        clear(sum);
        if (!r.ok || !r.data) {
          if (r.status === 403) { accessLost(); return; }
          sum.classList.add("dpat__sum--err");
          append(sum, [el("span", { text: errText(r) }), retryButton(loadHistory)]);
          [chartBox, evBox, todoBox].forEach(function (b) {
            clear(b);
            b.appendChild(el("p", { class: "cab-card__p cab-wait", text: "Появится, когда загрузится история анализов." }));
          });
          return;
        }
        sum.classList.remove("dpat__sum--err");
        var h = r.data;
        sum.textContent = h.summary || "";
        // анализов в истории (одинаковые записи одного дня — один анализ) и записей в кабинете — оба числа
        if (typeof h.n_records === "number" && $("dpat-meta")) {
          $("dpat-meta").textContent = "доступ до " + localDay(p.expires) + (left !== null ? " · осталось " + count(left, "день", "дня", "дней") : "") + " · " +
            count(h.n_records, "анализ", "анализа", "анализов") + (h.n_records !== p.n_records ? " (записей " + p.n_records + ": повторы одного дня — один анализ)" : "");
        }
        var api2 = chartCard(chartBox, h, p.id, "doctor", function (rid) { openRecordIn(feedBox, rid); });
        eventsList(evBox, h, "doctor", function (a) { api2.select(a); scrollToNode(chartBox); });
        todoList(todoBox, h, "doctor");
      });
    }
    // Лента анализов — GET doctor/patients/{id}/records: id, дата, источник, вывод врачу, анемия (новые сверху);
    // полный отчёт — по нажатию, doctor/patients/{id}/records/{rid}. Оба запроса пишутся в журнал пациента.
    function loadRecords() {
      clear(feedBox);
      feedBox.appendChild(el("div", { class: "cab-card" }, [el("p", { class: "rec__loading", role: "status" }, [el("span", { class: "spinner", "aria-hidden": "true" }), "Загружаем анализы…"])]));
      return api("GET", base + "/records").then(function (r) {
        if (!here()) { return; }
        if (!r.ok || !r.data) {
          clear(feedBox);
          if (r.status === 403) { accessLost(); return; }
          feedBox.appendChild(el("div", { class: "cab-card" }, [emptyState("doc", "Список анализов не загрузился", errText(r), retryButton(loadRecords, "btn--primary"))]));
          return;
        }
        feed(feedBox, r.data.records || [], {
          role: "doctor",
          load: function (rid) {
            if (cache[rid]) { return Promise.resolve(cache[rid]); }
            return api("GET", base + "/records/" + rid).then(function (rr) { if (rr.ok) { cache[rid] = rr; } return rr; });
          },
          onForbidden: accessLost,
          empty: emptyState("doc", "Анализов нет", "Пациент ещё не добавил анализы в кабинет.")
        });
      });
    }
    loadHistory();
    loadRecords();
  }

  // ---------- референсы лаборатории врача ----------
  function refreshLabRefs() {
    return api("GET", "/api/v1/doctor/lab-refs").then(function (r) {
      C.labrefs = (r.ok && r.data && r.data.sets) || [];
      paintLabRefs();
    });
  }
  function setLabRefs(sets) { C.labrefs = sets || []; paintLabRefs(); paintDoctorHero(); }
  function rangeText(code, r) {
    var a = C.byCode[code], unit = a ? a.unit_ru : (r.unit || "");
    return shortName(code) + " " + refText({ low: r.low, high: r.high }, unit);
  }
  function paintLabRefs() {
    var box = $("d-labrefs");
    clear(box);
    var file = el("input", { type: "file", id: "lr-file", class: "vh", accept: ".csv,.txt,.xlsx,text/csv" });
    var upl = el("label", { class: "btn btn--small", for: "lr-file", tabindex: "0", role: "button" }, ["Загрузить CSV или XLSX"]);
    upl.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
    var mk = linkButton("Новый набор", function () { openEditor(null); }, "btn--primary btn--small");
    var sample = linkButton("Образец CSV", downloadSample, "btn--small btn--quiet");
    box.appendChild(el("div", { class: "lr-bar" }, [mk, file, upl, sample]));
    box.appendChild(el("div", { class: "lr-upload", id: "lr-upload", hidden: true }));
    box.appendChild(el("div", { class: "cab-card lr-editor", id: "lr-editor", hidden: true }));
    file.addEventListener("change", function () { var f = file.files[0]; file.value = ""; if (f) { uploadPanel(f); } });
    if (!C.labrefs.length) {
      box.appendChild(el("div", { class: "cab-card" }, [emptyState("table", "Наборов пока нет",
        "Создайте набор вручную или загрузите файл «показатель;нижняя;верхняя;единица». Набор по умолчанию будет подставляться в ваши расчёты на главной.")]));
      return;
    }
    var ul = el("ul", { class: "lrs" });
    C.labrefs.forEach(function (s) {
      var codes = Object.keys(s.ranges || {});
      var preview = codes.slice(0, 4).map(function (c) { return rangeText(c, s.ranges[c]); }).join(" · ") + (codes.length > 4 ? " · ещё " + (codes.length - 4) : "");
      var acts = [linkButton("Изменить", function () { openEditor(s); }, "btn--small")];
      var defBtn = s.default ? linkButton("Снять «по умолчанию»", function () { makeDefault(s, false, defBtn); }, "btn--small btn--quiet")
                             : linkButton("Сделать по умолчанию", function () { makeDefault(s, true, defBtn); }, "btn--small");
      defBtn.setAttribute("data-act", "default");
      acts.push(defBtn);
      acts.push(linkButton("Удалить", function () { deleteSet(s); }, "btn--small btn--ghostdanger"));
      ul.appendChild(el("li", { class: "lr" + (s.default ? " lr--def" : ""), "data-id": s.id }, [
        el("div", { class: "lr__body" }, [
          el("p", { class: "lr__name" }, [s.name, s.default ? chip("по умолчанию", "ok") : null]),
          el("p", { class: "lr__meta", text: count(codes.length, "показатель", "показателя", "показателей") }),
          el("p", { class: "lr__prev", text: preview })]),
        el("div", { class: "lr__acts" }, acts)]));
    });
    box.appendChild(ul);
  }
  function downloadSample() {
    var text = "показатель;нижняя;верхняя;единица\nГемоглобин;120;150;г/л\nMCV;80;100;фл\nФерритин;15;150;мкг/л\nRDW;11,5;14,5;%\n";
    var blob = new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" });
    var a = el("a", { href: URL.createObjectURL(blob), download: "referensy-laboratorii-obrazec.csv" });
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }
  function uploadPanel(f) {
    var box = $("lr-upload");
    clear(box);
    var nm = el("input", { class: "fx__input", id: "lr-up-name", type: "text", maxlength: "80", value: (f.name || "").replace(/\.[^.]+$/, "").slice(0, 80) });
    var def = el("input", { type: "checkbox", id: "lr-up-def", checked: !C.labrefs.length });
    var go = el("button", { type: "button", class: "btn btn--primary btn--small", text: "Загрузить набор" });
    var cancel = linkButton("Отмена", function () { box.hidden = true; }, "btn--small");
    var err = el("p", { class: "cab-alert", hidden: true, role: "alert" });
    append(box, [
      el("p", { class: "lr-upload__t" }, ["Файл: ", el("b", { text: f.name || "файл" })]),
      el("div", { class: "fx" }, [el("label", { class: "fx__label", for: "lr-up-name", text: "Название набора" }), nm]),
      el("label", { class: "consent", for: "lr-up-def" }, [def, el("span", { text: "Использовать по умолчанию в моих расчётах" })]),
      err, el("div", { class: "cab-card__btns" }, [go, cancel])]);
    box.hidden = false;
    go.addEventListener("click", function () {
      var fd = new FormData();
      fd.append("file", f, f.name || "refs.csv");
      if (nm.value.trim()) { fd.append("name", nm.value.trim()); }
      fd.append("default", def.checked ? "1" : "0");
      busy(go, true, "Загружаем…");
      apiForm("/api/v1/doctor/lab-refs", fd, true).then(function (r) {
        busy(go, false);
        if (!r.ok) { showAlert(err, errText(r)); return; }
        box.hidden = true;
        toast("Набор референсов загружен", "ok");
        setLabRefs(r.data.sets);
      });
    });
  }
  var EDITOR_DEFAULT = ["hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC", "RDW", "ferritin", "serum_iron", "TSAT"];
  function analyteOptions(selected) {
    var groups = (C.ref && C.ref.form_groups) || [];
    var sel = el("select", { class: "fx__input lr-row__a", "aria-label": "Показатель" });
    sel.appendChild(el("option", { value: "", text: "— показатель —" }));
    groups.forEach(function (g) {
      var og = el("optgroup", { label: g.title });
      g.analytes.forEach(function (c) { var a = C.byCode[c]; if (a) { og.appendChild(el("option", { value: c, text: a.short_ru === a.name_ru ? a.name_ru : a.short_ru + " — " + a.name_ru, selected: c === selected })); } });
      sel.appendChild(og);
    });
    return sel;
  }
  function unitOptions(sel, code, unit) {
    clear(sel);
    var a = C.byCode[code];
    if (!a) { sel.appendChild(el("option", { value: "", text: "—" })); return; }
    sel.appendChild(el("option", { value: a.unit, text: a.unit_ru || a.unit }));
    (a.units || []).forEach(function (u) {
      if (u.toLowerCase() === String(a.unit).toLowerCase() || u === a.unit_ru) { return; }
      sel.appendChild(el("option", { value: u, text: u, selected: unit === u }));
    });
  }
  function defaultsHint(code) {
    var bs = ((C.ref && C.ref.reference_ranges) || {}).by_sex || {}, f = (bs.F || {})[code], m = (bs.M || {})[code];
    var t = function (r) { return r ? (r[0] !== null ? num(r[0]) : "…") + "–" + (r[1] !== null ? num(r[1]) : "…") : "—"; };
    return (f || m) ? "проект: Ж " + t(f) + " · М " + t(m) : "";
  }
  function editorRow(code, r) {
    var a = analyteOptions(code);
    var lo = el("input", { class: "fx__input lr-row__n", type: "text", inputmode: "decimal", maxlength: "12", value: r && r.low !== null && r.low !== undefined ? String(r.low).replace(".", ",") : "" });
    var hi = el("input", { class: "fx__input lr-row__n", type: "text", inputmode: "decimal", maxlength: "12", value: r && r.high !== null && r.high !== undefined ? String(r.high).replace(".", ",") : "" });
    var u = el("select", { class: "fx__input lr-row__u" });
    // Подписи для скринридера — с названием показателя строки: «Нижняя граница: Гемоглобин».
    var labels = function () {
      var nm = a.value ? shortName(a.value) : "показатель не выбран";
      lo.setAttribute("aria-label", "Нижняя граница: " + nm);
      hi.setAttribute("aria-label", "Верхняя граница: " + nm);
      u.setAttribute("aria-label", "Единица: " + nm);
      del.setAttribute("aria-label", "Убрать строку: " + nm);
    };
    var hint = el("span", { class: "lr-row__hint", text: defaultsHint(code) });
    var del = el("button", { type: "button", class: "lr-row__del", "aria-label": "Убрать строку", text: "×" });
    unitOptions(u, code, r && r.unit);
    var row = el("tr", { class: "lr-row" }, [
      el("td", { "data-label": "Показатель" }, [a, hint]), el("td", { "data-label": "Нижняя" }, [lo]), el("td", { "data-label": "Верхняя" }, [hi]),
      el("td", { "data-label": "Единица" }, [u]), el("td", { class: "lr-row__x" }, [del])]);
    labels();
    a.addEventListener("change", function () { unitOptions(u, a.value, null); hint.textContent = defaultsHint(a.value); labels(); });
    del.addEventListener("click", function () { row.parentNode.removeChild(row); });
    [lo, hi].forEach(function (i) { i.addEventListener("input", function () { i.removeAttribute("aria-invalid"); }); });
    return row;
  }
  function openEditor(set) {
    C.editSet = set;
    var box = $("lr-editor");
    clear(box);
    var name = el("input", { class: "fx__input", id: "lr-name", type: "text", maxlength: "80", value: set ? set.name : "", placeholder: "Например, «Лаборатория клиники»" });
    var def = el("input", { type: "checkbox", id: "lr-def", checked: set ? !!set.default : !C.labrefs.length });
    var body = el("tbody");
    var codes = set ? Object.keys(set.ranges || {}) : EDITOR_DEFAULT.filter(function (c) { return C.byCode[c]; });
    codes.forEach(function (c) { body.appendChild(editorRow(c, set ? set.ranges[c] : null)); });
    var add = linkButton("+ Показатель", function () { body.appendChild(editorRow("", null)); }, "btn--small btn--quiet");
    var err = el("p", { class: "cab-alert", role: "alert", hidden: true });
    var save = el("button", { type: "button", class: "btn btn--primary btn--small", text: set ? "Сохранить изменения" : "Сохранить набор" });
    var cancel = linkButton("Отмена", function () { box.hidden = true; C.editSet = null; }, "btn--small");
    append(box, [
      el("h3", { class: "cab-card__t", text: set ? "Изменить набор" : "Новый набор референсов" }),
      el("p", { class: "cab-card__p", text: "Пустые строки не сохраняются. Можно указать одну границу (например, только нижнюю). Единица по умолчанию — как в форме сервиса; другие пересчитаются." }),
      el("div", { class: "lr-ed__top" }, [
        el("div", { class: "fx" }, [el("label", { class: "fx__label", for: "lr-name", text: "Название" }), name]),
        el("label", { class: "consent", for: "lr-def" }, [def, el("span", { text: "По умолчанию в моих расчётах" })])]),
      el("div", { class: "tbl-wrap" }, [el("table", { class: "lr-tbl" }, [
        el("thead", null, [el("tr", null, [el("th", { scope: "col", text: "Показатель" }), el("th", { scope: "col", text: "Нижняя" }), el("th", { scope: "col", text: "Верхняя" }), el("th", { scope: "col", text: "Единица" }), el("th", { scope: "col" }, [el("span", { class: "vh", text: "Убрать" })])])]),
        body])]),
      add, err, el("div", { class: "cab-card__btns" }, [save, cancel])]);
    box.hidden = false;
    scrollToNode(box);
    name.focus({ preventScroll: true });
    save.addEventListener("click", function () {
      showAlert(err, "");
      if (!name.value.trim()) { name.setAttribute("aria-invalid", "true"); showAlert(err, "Укажите название набора."); name.focus(); return; }
      var ranges = {}, bad = null;
      Array.prototype.forEach.call(body.querySelectorAll(".lr-row"), function (row) {
        if (bad) { return; }
        var code = row.querySelector(".lr-row__a").value;
        var ins = row.querySelectorAll(".lr-row__n"), loT = ins[0].value.trim(), hiT = ins[1].value.trim();
        if (!code || (!loT && !hiT)) { return; }
        var lo = loT ? parseNum(loT) : null, hi = hiT ? parseNum(hiT) : null, nm = shortName(code);
        if (loT && isNaN(lo)) { bad = [ins[0], nm + ": нижняя граница — число."]; return; }
        if (hiT && isNaN(hi)) { bad = [ins[1], nm + ": верхняя граница — число."]; return; }
        if (lo !== null && hi !== null && lo > hi) { bad = [ins[0], nm + ": нижняя граница больше верхней."]; return; }
        if (ranges[code]) { bad = [row.querySelector(".lr-row__a"), nm + ": показатель указан дважды."]; return; }
        var spec = { unit: row.querySelector(".lr-row__u").value || C.byCode[code].unit };
        if (lo !== null) { spec.low = lo; }
        if (hi !== null) { spec.high = hi; }
        ranges[code] = spec;
      });
      if (bad) { bad[0].setAttribute("aria-invalid", "true"); bad[0].focus(); showAlert(err, bad[1]); return; }
      if (!Object.keys(ranges).length) { showAlert(err, "Заполните границы хотя бы одного показателя."); return; }
      busy(save, true, "Сохраняем…");
      saveSet(name.value.trim(), ranges, def.checked, set).then(function (msg) {
        busy(save, false);
        if (msg) { showAlert(err, msg); return; }
        box.hidden = true; C.editSet = null;
        scrollToNode($("sec-labrefs"));
        toast(!set ? "Набор сохранён" : msg === false ? "Изменений нет" : "Набор обновлён", "ok");
      });
    });
  }
  // Границы набора равны? Сервер отдаёт их в канонической единице ({low|null, high|null, unit}); редактор —
  // в выбранной единице, без пустых границ. Разная единица — считаем изменением (сервер пересчитает).
  function sameRanges(a, b) {
    var ka = Object.keys(a || {}).sort(), kb = Object.keys(b || {}).sort();
    if (ka.join("|") !== kb.join("|")) { return false; }
    var v = function (x) { return typeof x === "number" ? x : null; };
    return ka.every(function (c) {
      var x = a[c] || {}, y = b[c] || {};
      return v(x.low) === v(y.low) && v(x.high) === v(y.high) && (x.unit || "") === (y.unit || "");
    });
  }
  // Новый набор — POST; правка — PATCH /api/v1/doctor/lab-refs/{id} только с изменёнными полями (имя, границы,
  // «по умолчанию»): набор правится на месте, id и предел числа наборов не меняются. -> текст ошибки | null (сохранено)
  // | false (правок нет).
  function saveSet(name, ranges, isDefault, old) {
    if (!old) {
      return api("POST", "/api/v1/doctor/lab-refs", { name: name, ranges: ranges, "default": isDefault }).then(function (r) {
        if (!r.ok) { return errText(r); }
        setLabRefs(r.data.sets);
        return null;
      });
    }
    var patch = {};
    if (name !== old.name) { patch.name = name; }
    if (isDefault !== !!old.default) { patch["default"] = isDefault; }
    if (!sameRanges(ranges, old.ranges)) { patch.ranges = ranges; }
    if (!Object.keys(patch).length) { return Promise.resolve(false); }     // false — изменений нет, запрос не нужен
    return patchSet(old, patch);
  }
  function patchSet(s, patch) {
    return api("PATCH", "/api/v1/doctor/lab-refs/" + s.id, patch).then(function (r) {
      if (!r.ok || !r.data) {
        if (r.status === 404) { refreshLabRefs().then(paintDoctorHero); }     // набор удалён в другой вкладке
        return errText(r);
      }
      setLabRefs(r.data.sets);
      return null;
    });
  }
  // Кнопка занята на время запроса; список перерисовывается — фокус возвращается на ту же кнопку набора.
  function makeDefault(s, on, btn) {
    busy(btn, true, "Сохраняем…");
    patchSet(s, { "default": on }).then(function (msg) {
      busy(btn, false);
      toast(msg || (on ? "«" + s.name + "» — набор по умолчанию" : "В расчётах — нормы проекта"), msg ? "warn" : "ok");
      var again = document.querySelector('#d-labrefs .lr[data-id="' + s.id + '"] [data-act="default"]');
      if (again) { again.focus(); }
    });
  }
  function deleteSet(s) {
    openDialog({
      title: "Удалить набор «" + s.name + "»?", danger: true, okText: "Удалить",
      text: s.default ? "Это набор по умолчанию: после удаления в расчётах будут нормы проекта." : "Набор исчезнет из списка; ваши прошлые расчёты не изменятся.",
      onOk: function () {
        return api("DELETE", "/api/v1/doctor/lab-refs/" + s.id).then(function (r) {
          if (!r.ok) { return errText(r); }
          setLabRefs(r.data.sets);
          toast("Набор удалён", "ok");
          return null;
        });
      }
    });
  }

  // =================================================================================================
  // запуск: какая это страница
  // =================================================================================================
  function start() {
    if ($("auth")) { initLogin(); }
    else if ($("share")) { initShare(); }
    else if ($("cab")) { initCabinet(); }
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
})();
