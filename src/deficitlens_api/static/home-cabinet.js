/* Justmedit — главная страница и личный кабинет (ADR 0009).
   app.js после каждого расчёта посылает событие «jm:result» с тем входом, что ушёл на /ui/analyze, датой анализа
   с бланка и источником значений. Здесь, без правки логики расчёта:
   * пациент: вкладка «Врачу» скрыта, открыта «Пациенту»; под результатом — «Сохранить в кабинет» (дата — с бланка
     или сегодня; POST /api/v1/me/records с тем же входом и референсами с бланка, если они применены) и блок
     «С учётом вашей истории» (GET /api/v1/me/history — события кратко, ссылка в кабинет);
   * врач: «Врачу» — первой (как и было), обе вкладки доступны; его набор референсов по умолчанию сервер
     подставляет в расчёт сам (cookie уходит с запросом страницы);
   * без входа — подсказка «Войдите, чтобы сохранять анализы и видеть динамику»; вход — здесь же, формой под
     результатом (POST /api/v1/auth/login, cookie ставит сервер): результат не теряется, блок «Сохранить в кабинет»
     перерисовывается с ним же, шапка обновляется через nav.js (JMNav.set). Регистрация — на /login.
   Браузер ничего не хранит (ни localStorage, ни sessionStorage, ни cookie из JS); данные — только textContent;
   токен из ответа входа не используется. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var me = null, ready = false, last = null, saved = null, justIn = false;

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) { return; }
      if (k === "class") { node.className = v; } else if (k === "text") { node.textContent = v; } else { node.setAttribute(k, v === true ? "" : String(v)); }
    });
    (children || []).forEach(function (c) { if (c) { node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); } });
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function todayIso() { var d = new Date(); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()); }
  function ruDate(iso) { var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ""); return m ? m[3] + "." + m[2] + "." + m[1] : ""; }
  function api(method, url, body) {
    var headers = { "Accept": "application/json" }, opts = { method: method, headers: headers, credentials: "same-origin", cache: "no-store" };
    if (method !== "GET") { headers["X-Justmedit"] = "1"; }
    if (body !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(function (resp) {
      return resp.text().then(function (t) { var d = null; try { d = t ? JSON.parse(t) : null; } catch (e) { d = null; } return { ok: resp.ok, status: resp.status, data: d }; });
    }, function () { return { ok: false, status: 0, data: null }; });
  }
  function errText(r) {
    if (r && r.data && r.data.error && r.data.error.message) { return r.data.error.message; }
    return r && r.status ? "Не удалось сохранить (код " + r.status + ")." : "Нет связи с сервисом.";
  }
  function setNav(user) { if (window.JMNav && window.JMNav.set) { window.JMNav.set(user); } }

  // ---------- вкладки отчёта по роли ----------
  function applyRole() {
    if (!me || me.role !== "patient") { return; }
    var td = $("tab-doctor"), tp = $("tab-patient");
    if (!td || !tp) { return; }
    td.hidden = true;
    $("panel-doctor").hidden = true;
    if (tp.getAttribute("aria-selected") !== "true") { tp.click(); }
  }
  // Стрелки на вкладке «Пациенту» не должны открывать скрытую «Врачу».
  document.addEventListener("keydown", function (e) {
    if (me && me.role === "patient" && e.target && e.target.id === "tab-patient" && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      e.preventDefault(); e.stopPropagation();
    }
  }, true);

  // ---------- блок под результатом ----------
  // Вход для кабинета — только поля AnalysisInput, которые ушли на расчёт (без порогов показа).
  function cleanInput(i) {
    var out = { sex: i.sex, age_years: i.age_years, values: i.values };
    if (i.pregnancy) { out.pregnancy = i.pregnancy; }
    if (i.reference_ranges && Object.keys(i.reference_ranges).length) { out.reference_ranges = i.reference_ranges; }
    return out;
  }
  function render() {
    var box = $("cab-save");
    if (!box || !last || !ready) { return; }
    clear(box);
    if (me && me.role === "doctor") {
      // врач вошёл прямо здесь: его набор референсов применится к следующему расчёту
      box.hidden = !justIn;
      if (justIn) {
        box.className = "cabsave cabsave--hint";
        box.appendChild(el("p", { role: "status" }, [el("span", { class: "cabsave__dot", "aria-hidden": "true" }),
          "Вы вошли как врач " + me.login + ". Ваш набор референсов по умолчанию применится к следующему расчёту — нажмите «Рассчитать» ещё раз."]));
      }
      return;
    }
    box.hidden = false;
    if (!me) {
      box.className = "cabsave cabsave--hint";
      // ссылка на /login — запасной путь; по нажатию форма входа открывается здесь же
      var open = el("a", { href: "/login?next=%2F", text: "Войдите" });
      open.addEventListener("click", function (e) { e.preventDefault(); loginForm(box); });
      box.appendChild(el("p", null, [el("span", { class: "cabsave__dot", "aria-hidden": "true" }),
        open, ", чтобы сохранить этот анализ и видеть динамику в личном кабинете — вход здесь же, результат не пропадёт."]));
      return;
    }
    box.className = "cabsave";
    var date = el("input", { type: "date", id: "cabsave-date", class: "cabsave__date", min: "1900-01-01", max: todayIso(), value: last.analysisDate && ruDate(last.analysisDate) ? last.analysisDate : todayIso() });
    var save = el("button", { type: "button", class: "btn btn--primary cabsave__btn", id: "cabsave-btn", text: "Сохранить в кабинет" });
    var cmp = el("button", { type: "button", class: "btn cabsave__btn2", id: "cabsave-cmp", text: "Сравнить с историей" });
    var status = el("p", { class: "cabsave__status", id: "cabsave-status", role: "status" });
    var hist = el("div", { class: "cabsave__hist", id: "cabsave-hist", hidden: true });
    box.appendChild(el("div", { class: "cabsave__head" }, [
      el("div", null, [el("h3", { class: "cabsave__t", text: "Сохранить в кабинет" }),
        el("p", { class: "cabsave__p", text: "Анализ появится в истории и на графиках; значения хранятся зашифрованно до удаления." })]),
      el("div", { class: "cabsave__row" }, [el("label", { class: "cabsave__lab", for: "cabsave-date", text: "Дата анализа" }), date, save, cmp])
    ]));
    box.appendChild(status);
    box.appendChild(hist);
    if (saved) { markSaved(saved); }
    save.addEventListener("click", function () {
      var d = date.value;
      if (!/^\d{4}-\d{2}-\d{2}$/.test(d) || d > todayIso()) { status.className = "cabsave__status is-err"; status.textContent = "Укажите дату анализа (не позже сегодня)."; date.focus(); return; }
      save.disabled = true; save.textContent = "Сохраняем…";
      api("POST", "/api/v1/me/records", { date: d, input: cleanInput(last.input), source: last.source || "form" }).then(function (r) {
        if (r.ok && r.data && r.data.record) {
          saved = r.data.record;
          markSaved(saved);
          loadHistory();
          return;
        }
        save.disabled = false; save.textContent = "Сохранить в кабинет";
        status.className = "cabsave__status is-err";
        status.textContent = errText(r);
      });
    });
    cmp.addEventListener("click", loadHistory);
  }
  // Вход на месте: логин и пароль -> POST /api/v1/auth/login (cookie сессии ставит сервер, токен из ответа не
  // нужен) -> профиль в шапку -> блок «Сохранить в кабинет» с тем же результатом.
  function loginForm(box) {
    clear(box);
    box.className = "cabsave";
    // без атрибута type (по умолчанию — text): общее правило app.css input[type="text"] { width: 100% } не
    // растягивает поле на всю строку — оно того же вида и размера, что поле даты
    var login = el("input", { id: "cabsave-login", class: "cabsave__date", autocomplete: "username", autocapitalize: "off", spellcheck: "false", maxlength: "64", required: true });
    var pw = el("input", { type: "password", id: "cabsave-pw", class: "cabsave__date", autocomplete: "current-password", maxlength: "128", required: true });
    var go = el("button", { type: "submit", class: "btn btn--primary cabsave__btn", text: "Войти" });
    var cancel = el("button", { type: "button", class: "btn cabsave__btn2", text: "Отмена" });
    var status = el("p", { class: "cabsave__status", role: "alert" });
    var form = el("form", { class: "cabsave__head", novalidate: true, autocomplete: "on" }, [
      el("div", null, [el("h3", { class: "cabsave__t", text: "Вход в кабинет" }),
        el("p", { class: "cabsave__p" }, ["Результат расчёта останется на странице. Нет аккаунта? ",
          el("a", { href: "/login?mode=register&next=%2F", text: "Зарегистрируйтесь" }), " — откроется отдельная страница, анализ придётся рассчитать заново."])]),
      // подпись и поле — одной меткой: на узком экране переносятся вместе
      el("div", { class: "cabsave__row" }, [
        el("label", { class: "cabsave__lab" }, ["Логин ", login]),
        el("label", { class: "cabsave__lab" }, ["Пароль ", pw]), go, cancel])
    ]);
    box.appendChild(form);
    box.appendChild(status);
    login.focus();
    cancel.addEventListener("click", function () { render(); });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      status.className = "cabsave__status is-err";
      if (!login.value.trim()) { status.textContent = "Введите логин."; login.focus(); return; }
      if (!pw.value) { status.textContent = "Введите пароль."; pw.focus(); return; }
      go.disabled = true; go.textContent = "Входим…";
      api("POST", "/api/v1/auth/login", { login: login.value.trim(), password: pw.value }).then(function (r) {
        if (r.ok && r.data && r.data.user) {
          me = r.data.user;
          justIn = true;
          setNav(me);
          applyRole();
          render();
          var btn = $("cabsave-btn");
          if (btn) { btn.focus(); }
          return;
        }
        go.disabled = false; go.textContent = "Войти";
        status.textContent = r.status === 401 ? "Неверный логин или пароль." : errText(r);
        if (r.status === 401) { pw.select(); pw.focus(); }
      });
    });
  }
  function markSaved(rec) {
    var save = $("cabsave-btn"), status = $("cabsave-status");
    if (!save) { return; }
    save.disabled = true; save.textContent = "Сохранено ✓";
    $("cabsave-date").disabled = true;
    status.className = "cabsave__status is-ok";
    clear(status);
    status.appendChild(document.createTextNode("Анализ от " + ruDate(rec.date) + " — в вашем кабинете. "));
    status.appendChild(el("a", { href: "/cabinet", text: "Открыть кабинет →" }));
  }
  function loadHistory() {
    var box = $("cabsave-hist"), cmp = $("cabsave-cmp");
    if (!box) { return; }
    if (cmp) { cmp.disabled = true; }
    box.hidden = false;
    clear(box);
    box.appendChild(el("p", { class: "cabsave__muted", text: "Загружаем историю…" }));
    api("GET", "/api/v1/me/history").then(function (r) {
      if (cmp) { cmp.disabled = false; cmp.textContent = "Обновить"; }
      clear(box);
      if (!r.ok || !r.data) { box.appendChild(el("p", { class: "cabsave__muted", text: errText(r) })); return; }
      var h = r.data, ev = h.events || [];
      box.appendChild(el("h4", { class: "cabsave__ht", text: "С учётом вашей истории" }));
      box.appendChild(el("p", { class: "cabsave__sum", text: h.summary || "" }));
      if (!h.n_records) {
        box.appendChild(el("p", { class: "cabsave__muted", text: "В кабинете пока нет анализов — сохраните этот, чтобы начать историю." }));
      } else if (!ev.length) {
        box.appendChild(el("p", { class: "cabsave__muted", text: h.n_records < 2 ? "Изменения появятся, когда в истории будет хотя бы два анализа." : "Заметных изменений между анализами нет." }));
      } else {
        box.appendChild(el("ul", { class: "cabsave__evts" }, ev.slice(0, 3).map(function (e) {
          return el("li", { class: "cabsave__evt cabsave__evt--" + (e.severity === "attention" ? "attention" : "info") }, [
            el("b", { text: e.title }), el("span", { text: e.text })]);
        })));
        if (ev.length > 3) { box.appendChild(el("p", { class: "cabsave__muted", text: "И ещё " + (ev.length - 3) + " — в кабинете." })); }
      }
      if (!saved) { box.appendChild(el("p", { class: "cabsave__muted", text: "Этот анализ ещё не сохранён и в историю не входит." })); }
      box.appendChild(el("p", null, [el("a", { class: "cabsave__go", href: "/cabinet", text: "Открыть кабинет: графики и что досдать →" })]));
    });
  }

  document.addEventListener("jm:result", function (e) {
    last = e.detail || null;
    saved = null;
    applyRole();
    render();
  });
  var mePromise = (window.JMNav && window.JMNav.me) || Promise.resolve(null);
  mePromise.then(function (u) {
    me = u; ready = true;
    applyRole();
    render();
  });
})();
