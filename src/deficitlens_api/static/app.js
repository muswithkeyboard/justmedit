/* Justmedit — главная страница.
   Чистый JS без библиотек. Список показателей, единицы, пороги и цены приходят из /ui/reference;
   в коде страницы их нет. Браузер ничего не хранит: ни localStorage, ни sessionStorage, ни cookies.
   Всё, что пришло от сервера, вставляется только как текст (textContent). */
(function () {
  "use strict";

  // analysisDate — дата анализа с бланка (ответ /parse → date, ISO), blankRefs — референсы лаборатории с бланка
  // (ответ /parse → reference_ranges); живут только в памяти страницы.
  // source — откуда значения формы (для «Сохранить в кабинет»): form | photo | pdf | file.
  var state = { ref: null, byCode: {}, examples: [], example: null, followupDone: false, result: null, busy: false,
                analysisDate: null, blankRefs: null, source: "form" };
  var $ = function (id) { return document.getElementById(id); };

  // ---------- помощники DOM ----------
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) { return; }
        if (k === "class") { node.className = v; }
        else if (k === "text") { node.textContent = v; }
        else { node.setAttribute(k, v === true ? "" : String(v)); }
      });
    }
    (children || []).forEach(function (c) {
      if (c === null || c === undefined || c === false) { return; }
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function card(title, children, extraClass) {
    return el("section", { class: "card" + (extraClass ? " " + extraClass : "") },
      [el("h3", { text: title })].concat(children));
  }
  function chip(text, kind) { return el("span", { class: "chip" + (kind ? " " + kind : ""), text: text }); }
  // Число пунктов у заголовка свёрнутого блока: видно цифрой, читалка слышит «3 пункта».
  function countBadge(n, one, few, many) {
    return el("span", { class: "fold__n" }, [String(n), el("span", { class: "vh", text: " " + plural(n, one || "пункт", few || "пункта", many || "пунктов") })]);
  }
  // Длинный список: первые `keep` элементов видны, остальные — под «ещё N» (свёрнуто).
  function moreFold(nodes, keep, words) {
    if (nodes.length <= keep) { return null; }
    var rest = nodes.length - keep;
    return el("details", { class: "fold fold--more" }, [
      el("summary", { text: "ещё " + rest + (words ? " " + plural(rest, words[0], words[1], words[2]) : "") })
    ].concat(nodes.slice(keep)));
  }

  var nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
  function num(x) { return (x === null || x === undefined || isNaN(x)) ? "—" : nf.format(x); }
  function pct(p) { return (p === null || p === undefined) ? "—" : Math.round(p * 100) + " %"; }
  function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

  // ---------- запросы ----------
  function request(method, url, body, headers) {
    // credentials: "same-origin" — cookie кабинета (если вошли) уходит на свой сервер: врачу /ui/analyze подставляет
    // его набор референсов по умолчанию (ADR 0009). Без входа cookie нет — запрос такой же, как раньше.
    var opts = { method: method, headers: Object.assign({ "Accept": "application/json" }, headers || {}), credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch(url, opts).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        return { status: resp.status, ok: resp.ok, data: data, retryAfter: resp.headers.get("Retry-After") };
      });
    });
  }
  function errorText(r) {
    if (r.data && r.data.error && r.data.error.message) { return r.data.error.message; }
    if (r.status === 429) { return "Слишком много запросов. Повторите через минуту."; }
    if (r.status === 413) { return "Слишком большой запрос."; }
    return "Сервис не ответил (код " + r.status + "). Повторите запрос.";
  }
  function showAlert(text) { var a = $("alert"); a.textContent = text; a.hidden = !text; }
  // Ошибка расчёта (429, нет связи) после нажатия примера в полосе: сообщение — под формой, далеко внизу;
  // если его не видно, прокручиваем к нему (role="alert" читалка объявит сама).
  function revealAlert() {
    var a = $("alert"), r = a.getBoundingClientRect();
    if (!a.hidden && (r.top < 60 || r.bottom > window.innerHeight)) { scrollUnderTop(a); }
  }

  // ---------- форма ----------
  function buildFields() {
    var box = $("fields");
    clear(box);
    state.ref.form_groups.forEach(function (g) {
      var body = el("div", { class: "group__body" });
      g.analytes.forEach(function (code) {
        var a = state.byCode[code];
        if (!a) { return; }
        var id = "v-" + code;
        var same = a.short_ru === a.name_ru;
        var label = el("label", { class: "f__label", for: id }, [
          el("span", { class: "f__short", text: a.short_ru }),
          same ? null : el("span", { class: "f__name", text: a.name_ru })
        ]);
        var input = el("input", { id: id, type: "text", inputmode: "decimal", "data-code": code,
          "aria-describedby": "u-" + code + " err-" + code, maxlength: "12" });
        // Показатель с другими единицами в ходу (г/дл, мкг/дл, пмоль/л): выбор единицы рядом с полем, по умолчанию —
        // каноническая. Варианты — /ui/reference → unit_choices (по одному на множитель пересчёта); пересчитывает сервер.
        var choices = a.unit_choices || [];
        var unit = el("span", { class: "f__unit" + (choices.length > 1 ? " vh" : ""), id: "u-" + code, text: a.unit_ru });
        var sel = null;
        if (choices.length > 1) {
          sel = el("select", { class: "f__unitsel", id: "us-" + code, "data-code": code, "aria-label": "Единица: " + a.name_ru });
          choices.forEach(function (c) { sel.appendChild(el("option", { value: c.factor === 1 ? "" : c.unit, text: c.factor === 1 ? a.unit_ru : c.unit })); });
          sel.addEventListener("change", function () { pickUnit(code, sel.value); });
        }
        var err = el("p", { class: "field__error", id: "err-" + code, hidden: true });
        body.appendChild(el("div", { class: "f" + (sel ? " f--unitsel" : "") }, [label, input, sel ? el("span", { class: "f__unitbox" }, [sel, unit]) : unit, err]));
      });
      if (g.always_open) {
        box.appendChild(el("div", { class: "group" }, [el("div", { class: "group__title", text: g.title }), body]));
      } else {
        var count = el("span", { class: "group__count", hidden: true });
        var d = el("details", { class: "group", "data-group": g.id }, [el("summary", null, [g.title, count]), body]);
        box.appendChild(d);
      }
    });
    box.addEventListener("input", function (e) {
      if (e.target && e.target.getAttribute("data-code")) { clearFieldError(e.target); updateCounts(); }
    });
  }
  function inputs() { return Array.prototype.slice.call(document.querySelectorAll("#fields input[data-code]")); }
  function updateCounts() {
    Array.prototype.forEach.call(document.querySelectorAll("#fields details.group"), function (d) {
      var n = Array.prototype.filter.call(d.querySelectorAll("input[data-code]"), function (i) { return i.value.trim() !== ""; }).length;
      var c = d.querySelector(".group__count");
      c.textContent = "заполнено: " + n;
      c.hidden = n === 0;
    });
    // Итог на свёрнутом блоке «Ввести вручную»: сколько показателей заполнено (форма не загромождает страницу).
    var total = Array.prototype.filter.call(document.querySelectorAll("#fields input[data-code]"), function (i) { return i.value.trim() !== ""; }).length;
    $("manual-count").textContent = total ? "(заполнено: " + total + ")" : "";
  }
  function sex() { return document.querySelector('input[name="sex"]:checked').value; }
  function norms() { return document.querySelector('input[name="norms"]:checked').value; }
  function syncPregnancy() {
    var female = sex() === "F";
    $("pregnancy-box").hidden = !female;
    $("trimester-box").hidden = !(female && $("pregnancy").value === "yes");
    if (state.ref && $("labref-rows").firstChild) { syncLabRef(); }   // значения по умолчанию зависят от пола
  }
  function setUnit(code, unit) {
    // Единица из бланка (например, г/дл): показывается рядом с полем и уходит в запрос вместе со значением.
    var input = $("v-" + code), label = $("u-" + code), sel = $("us-" + code);
    if (!input) { return; }
    if (unit) { input.setAttribute("data-unit", unit); label.textContent = unit; }
    else { input.removeAttribute("data-unit"); label.textContent = state.byCode[code].unit_ru; }
    if (sel) { syncUnitSelect(sel, code, unit); }
  }
  // Выбор единицы в списке: каноническая (value "") — без поля unit в запросе, иначе {value, unit}.
  function pickUnit(code, unit) {
    var input = $("v-" + code);
    if (unit) { input.setAttribute("data-unit", unit); } else { input.removeAttribute("data-unit"); }
    $("u-" + code).textContent = unit || state.byCode[code].unit_ru;
    clearFieldError(input);
  }
  function unitKey(u) { return String(u || "").toLowerCase().replace(/\s+/g, "").replace(/[µμ]/g, "u"); }
  // Список единиц показывает единицу с бланка: совпала с вариантом — выбираем его; это синоним (нг/мл для ферритина)
  // или редкая запись — добавляем её отдельным пунктом, сервер пересчитает по названию.
  function syncUnitSelect(sel, code, unit) {
    Array.prototype.slice.call(sel.querySelectorAll("option[data-extra]")).forEach(function (o) { sel.removeChild(o); });
    if (!unit || unitKey(unit) === unitKey(state.byCode[code].unit_ru)) { sel.value = ""; return; }
    var hit = Array.prototype.filter.call(sel.options, function (o) { return o.value && unitKey(o.value) === unitKey(unit); })[0];
    if (!hit) { hit = el("option", { value: unit, text: unit, "data-extra": "1" }); sel.appendChild(hit); }
    sel.value = hit.value;
  }
  function setValue(code, value, unit) {
    var input = $("v-" + code);
    if (!input) { return false; }
    input.value = (value === null || value === undefined) ? "" : String(value).replace(".", ",");
    setUnit(code, unit || null);
    clearFieldError(input);
    var d = input.closest("details");
    if (d && input.value) { d.open = true; }
    return true;
  }
  function clearForm() {
    inputs().forEach(function (i) { i.value = ""; setUnit(i.getAttribute("data-code"), null); clearFieldError(i); });
    $("age").value = "";
    $("pregnancy").value = "no";
    $("trimester").value = "";
    ["age", "pregnancy"].forEach(function (id) { $(id).removeAttribute("aria-invalid"); });
    ["err-age_years", "err-pregnancy", "err-sex", "age-hint"].forEach(function (id) { $(id).hidden = true; });
    Array.prototype.forEach.call(document.querySelectorAll("#fields details.group"), function (d) { d.open = false; });
    unapplyBlankRefs();
    state.analysisDate = null;
    state.source = "form";
    syncPregnancy();
    updateCounts();
  }
  function fieldError(target, errNode, message) {
    if (target) { target.setAttribute("aria-invalid", "true"); }
    if (errNode) { errNode.textContent = message; errNode.hidden = false; }
    for (var d = target && target.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) { d.open = true; }
    if (target && target.focus) { target.focus(); }
  }
  function clearFieldError(input) {
    input.removeAttribute("aria-invalid");
    var err = $("err-" + (input.getAttribute("data-code") || ""));
    if (err) { err.hidden = true; }
  }
  function showServerFieldError(field, message) {
    var map = { age_years: ["age", "err-age_years"], pregnancy: ["pregnancy", "err-pregnancy"], sex: [null, "err-sex"] };
    if (map[field]) { fieldError(map[field][0] ? $(map[field][0]) : null, $(map[field][1]), message); return true; }
    var input = $("v-" + field);
    if (input) { fieldError(input, $("err-" + field), message); return true; }
    if (field.indexOf("reference_ranges") === 0) {
      $("labref").open = true;
      fieldError($("lr-lo-" + field.split(".")[1]), $("err-labref"), message);
      return true;
    }
    return false;
  }

  // ---------- референсы лаборатории ----------
  // Значения по умолчанию — референс проекта (/ui/reference → reference_ranges.by_sex, канонические единицы).
  // Заполненное поле заменяет значение по умолчанию и уходит в запрос как reference_ranges; сервер пересчитывает
  // единицы и добавляет в отчёт врачу предупреждение о референсах лаборатории. Браузер ничего не сохраняет.
  function labrefDefaults() { return labrefDefaultsFor(sex(), $("pregnancy").value); }
  function labrefDefaultsFor(sexV, pregStatus) {
    var rr = state.ref.reference_ranges || {};
    var pregnantNoDefaults = sexV === "F" && pregStatus === "yes" && !rr.apply_in_pregnancy;
    return pregnantNoDefaults ? {} : ((rr.by_sex || {})[sexV] || {});
  }
  function labrefCodes() {
    var byS = (state.ref.reference_ranges || {}).by_sex || {};
    var codes = [];
    state.ref.form_groups.forEach(function (g) {
      g.analytes.forEach(function (c) {
        if (((byS.F || {})[c] || (byS.M || {})[c]) && codes.indexOf(c) < 0) { codes.push(c); }
      });
    });
    return codes;
  }
  function buildLabRef() {
    var body = $("labref-rows");
    clear(body);
    labrefCodes().forEach(function (code) {
      var a = state.byCode[code];
      if (!a) { return; }
      var lo = el("input", { id: "lr-lo-" + code, type: "text", inputmode: "decimal", maxlength: "12",
        "data-code": code, "data-bound": "low", "aria-label": a.name_ru + ": нижняя граница, " + a.unit_ru });
      var hi = el("input", { id: "lr-hi-" + code, type: "text", inputmode: "decimal", maxlength: "12",
        "data-code": code, "data-bound": "high", "aria-label": a.name_ru + ": верхняя граница, " + a.unit_ru });
      [lo, hi].forEach(function (i) { i.addEventListener("input", function () { i.removeAttribute("aria-invalid"); $("err-labref").hidden = true; labrefCount(); }); });
      body.appendChild(el("tr", null, [
        el("th", { scope: "row" }, [a.short_ru + (a.unit_ru ? ", " + a.unit_ru : ""),
          el("span", { class: "labref__def", id: "lr-def-" + code })]),
        el("td", { "data-label": "Нижняя" }, [lo]),
        el("td", { "data-label": "Верхняя" }, [hi])
      ]));
    });
    syncLabRef();
  }
  function syncLabRef() {
    var defs = labrefDefaults();
    labrefCodes().forEach(function (code) {
      var d = defs[code], cell = $("lr-def-" + code);
      if (!cell) { return; }
      var lo = d && d[0] !== null && d[0] !== undefined ? num(d[0]) : "", hi = d && d[1] !== null && d[1] !== undefined ? num(d[1]) : "";
      cell.textContent = d ? (lo && hi ? lo + "–" + hi : (lo ? "от " + lo : "до " + hi)) : "не задан";
      $("lr-lo-" + code).setAttribute("placeholder", lo);
      $("lr-hi-" + code).setAttribute("placeholder", hi);
    });
  }
  function labrefCount() {
    var n = 0;
    labrefCodes().forEach(function (c) { if ($("lr-lo-" + c).value.trim() || $("lr-hi-" + c).value.trim()) { n += 1; } });
    $("labref-count").textContent = n ? "(своих: " + n + ")" : "";
  }
  // Референсы с бланка: ответ /parse → reference_ranges {код: {low, high, unit}} в канонической единице сервиса
  // (контракт /parse, CONTRACTS.md «Добавления 04.10.2026, вечер»). По решению врача (п. 11) референс лаборатории
  // приоритетнее референса проекта, поэтому галочка «Применить…» включена сразу: она заполняет поля блока
  // «Референсы лаборатории». Снятая галочка очищает только те поля, которые заполнили мы и пользователь не менял.
  function blankText(x) { return String(x).replace(".", ","); }
  function pickBlankRefs(rr) {
    var out = {}, codes = labrefCodes();
    Object.keys(rr || {}).forEach(function (code) {
      var r = rr[code], a = state.byCode[code];
      if (!a || codes.indexOf(code) < 0 || !r || typeof r !== "object") { return; }
      if (r.unit && r.unit !== a.unit && r.unit !== a.unit_ru) { return; }          // не каноническая единица — пропускаем
      var lo = typeof r.low === "number" && isFinite(r.low) ? r.low : null;
      var hi = typeof r.high === "number" && isFinite(r.high) ? r.high : null;
      if ((lo === null && hi === null) || (lo !== null && hi !== null && lo > hi)) { return; }
      out[code] = { low: lo, high: hi };
    });
    return Object.keys(out).length ? out : null;
  }
  function applyBlankRefs(on) {
    var b = state.blankRefs;
    if (!b) { return; }
    Object.keys(b.ranges).forEach(function (code) {
      [["lo", b.ranges[code].low], ["hi", b.ranges[code].high]].forEach(function (pair) {
        var input = $("lr-" + pair[0] + "-" + code);
        if (!input || pair[1] === null) { return; }
        var text = blankText(pair[1]);
        if (on) { input.value = text; } else if (input.value.trim() === text) { input.value = ""; }
        input.removeAttribute("aria-invalid");
      });
    });
    b.on = on;
    $("err-labref").hidden = true;
    labrefCount();
  }
  function unapplyBlankRefs() {
    if (state.blankRefs && state.blankRefs.on) { applyBlankRefs(false); }
    state.blankRefs = null;
  }
  // Сводка «Референсы с бланка» в #paste-out: галочка и список показателей.
  function blankRefsNode() {
    var b = state.blankRefs;
    var box = el("input", { type: "checkbox", id: "blank-refs" });
    box.checked = !!b.on;
    box.addEventListener("change", function () { applyBlankRefs(box.checked); if (state.result) { calculate(true); } });   // как переключатель порога
    var names = Object.keys(b.ranges).map(function (c) { return state.byCode[c].short_ru; });
    return el("div", { class: "blankref" }, [
      el("label", { class: "blankref__line", for: "blank-refs" }, [box, el("span", { text: "Применить референсы лаборатории с бланка" })]),
      el("p", { class: "hint", text: names.length + " " + plural(names.length, "показатель", "показателя", "показателей") + ": " + names.join(", ") +
        ". Поля — в «Настройки для врача → Референсы лаборатории»." })
    ]);
  }

  // Собирает reference_ranges. null — ошибка ввода (показана у блока), {} — ничего не заполнено.
  function collectLabRef(quiet) {
    var out = {}, bad = null;
    labrefCodes().forEach(function (code) {
      if (bad) { return; }
      var loI = $("lr-lo-" + code), hiI = $("lr-hi-" + code);
      var loT = loI.value.trim(), hiT = hiI.value.trim();
      if (!loT && !hiT) { return; }
      var lo = loT ? parseNumber(loT) : null, hi = hiT ? parseNumber(hiT) : null;
      var name = state.byCode[code].short_ru;
      if (loT && isNaN(lo)) { bad = [loI, name + ": нижняя граница — введите число"]; return; }
      if (hiT && isNaN(hi)) { bad = [hiI, name + ": верхняя граница — введите число"]; return; }
      if (lo !== null && hi !== null && lo > hi) { bad = [loI, name + ": нижняя граница больше верхней"]; return; }
      var spec = { unit: state.byCode[code].unit };
      if (lo !== null) { spec.low = lo; }
      if (hi !== null) { spec.high = hi; }
      out[code] = spec;
    });
    if (bad) {
      if (!quiet) { $("labref").open = true; fieldError(bad[0], $("err-labref"), bad[1]); }
      return null;
    }
    return out;
  }
  function parseNumber(text) {
    var t = text.trim().replace(/\s+/g, "").replace(",", ".");
    if (!/^\d+(\.\d+)?$/.test(t)) { return NaN; }
    return parseFloat(t);
  }
  // Пример числа в подсказке к ошибке ввода — середина референса проекта в канонической единице (для Hb — «135»,
  // а не «12,5»: та запись выглядит как г/дл). Нет референса — без примера.
  function exampleFor(code) {
    var d = labrefDefaults()[code];
    if (!d || d[0] === null || d[0] === undefined || d[1] === null || d[1] === undefined) { return ""; }
    var mid = (d[0] + d[1]) / 2;
    return ", например " + num(mid >= 20 ? Math.round(mid) : Math.round(mid * 10) / 10);
  }
  // Текст ошибки возраста — как у сервера (service._FIELD_MESSAGES["age_years"]).
  var AGE_ERROR = "Возраст — целое число от 18 до 120 лет: сервис рассчитан на взрослых.";
  // Собирает запрос из формы. При ошибке ввода показывает её у поля и возвращает null.
  function collect(quiet) {
    var ok = true, values = {};
    $("err-age_years").hidden = true; $("age").removeAttribute("aria-invalid");
    var ageText = $("age").value.trim();
    var age = /^\d{1,3}$/.test(ageText) ? parseInt(ageText, 10) : NaN;
    if (age < 18 || age > 120) { age = NaN; }          // те же границы, что у сервера (schemas.AnalysisInput)
    inputs().slice().reverse().forEach(function (i) {
      var raw = i.value.trim();
      if (raw === "") { return; }
      var v = parseNumber(raw), code = i.getAttribute("data-code");
      if (isNaN(v)) {
        ok = false;
        if (!quiet) { fieldError(i, $("err-" + code), "введите число" + exampleFor(code) + " (десятичный разделитель — запятая или точка)"); }
        return;
      }
      var unit = i.getAttribute("data-unit");
      values[code] = unit ? { value: v, unit: unit } : v;
    });
    if (isNaN(age)) {
      ok = false;
      if (!quiet) { $("age-hint").hidden = true; fieldError($("age"), $("err-age_years"), AGE_ERROR); }
    }
    var refs = collectLabRef(quiet);
    if (refs === null) { ok = false; }
    if (!ok) { return null; }
    var payload = { sex: sex(), age_years: age, values: values, norms: norms(), refer_share: parseFloat($("refer-share").value) };
    if (Object.keys(refs).length) { payload.reference_ranges = refs; }
    if (payload.sex === "F") {
      var status = $("pregnancy").value;
      payload.pregnancy = { status: status };
      if (status === "yes" && $("trimester").value) { payload.pregnancy.trimester = parseInt($("trimester").value, 10); }
    }
    return payload;
  }
  function fillFromInput(input) {
    clearForm();
    document.querySelector('input[name="sex"][value="' + input.sex + '"]').checked = true;
    $("age").value = String(input.age_years);
    var p = input.pregnancy || {};
    $("pregnancy").value = p.status || "no";
    $("trimester").value = p.trimester ? String(p.trimester) : "";
    Object.keys(input.values || {}).forEach(function (code) {
      var v = input.values[code];
      if (v !== null && typeof v === "object") { setValue(code, v.value, v.unit); } else { setValue(code, v); }
    });
    syncPregnancy();
    updateCounts();
  }

  // ---------- расчёт ----------
  // stay — пересчёт из «Настроек для врача» (порог, точка направления, референсы): форма остаётся открытой,
  // страница не прокручивается. Обычный расчёт прячет форму и встаёт на начало результата.
  function calculate(stay) {
    if (state.busy) { return; }
    showAlert("");
    var payload = collect(false);
    if (!payload) { return; }
    state.busy = true;
    $("submit").disabled = true;
    request("POST", "/ui/analyze", payload).then(function (r) {
      if (r.ok && r.data) {
        state.result = r.data;
        state.input = payload;              // пол, возраст, беременность — для сводки и шапки выгружаемого отчёта
        render(r.data, stay === true);
        // Для блока «Сохранить в кабинет» (home-cabinet.js): тот же вход, что ушёл на расчёт, дата и источник.
        document.dispatchEvent(new CustomEvent("jm:result", { detail: { input: payload, result: r.data,
          analysisDate: state.analysisDate, source: state.source } }));
      } else if (r.status === 422 && r.data && r.data.error && r.data.error.field &&
                 showServerFieldError(r.data.error.field, r.data.error.message)) {
        showAlert("");
      } else {
        showAlert(errorText(r));
        revealAlert();
      }
    }).catch(function () {
      showAlert("Нет связи с сервисом. Проверьте соединение и повторите.");
      revealAlert();
    }).then(function () {
      state.busy = false;
      $("submit").disabled = false;
    });
  }

  // ---------- примеры ----------
  function buildExamples() {
    var box = $("examples");
    clear(box);
    state.examples.forEach(function (ex, idx) {
      var b = el("button", { type: "button", class: "ex", title: ex.pain || "", "aria-pressed": "false", "data-id": ex.id }, [
        el("span", { class: "ex__n", text: String(idx + 1), "aria-hidden": "true" }),
        el("span", { class: "ex__t", text: ex.title })
      ]);
      b.addEventListener("click", function () { runExample(ex); });
      box.appendChild(b);
    });
  }
  function markExample(id) {
    Array.prototype.forEach.call(document.querySelectorAll("#examples .ex"), function (b) {
      b.setAttribute("aria-pressed", b.getAttribute("data-id") === id ? "true" : "false");
    });
  }
  function runExample(ex) {
    state.example = ex;
    state.followupDone = false;
    markExample(ex.id);
    fillFromInput(ex.input);
    calculate();
  }
  function renderFollowup() {
    var box = $("followup");
    clear(box);
    var ex = state.example;
    if (!ex || !ex.followup || state.followupDone) { box.hidden = true; return; }
    var b = el("button", { type: "button", class: "btn btn--primary btn--small", text: ex.followup.title || "Следующий шаг" });
    b.addEventListener("click", function () {
      Object.keys(ex.followup.add_values || {}).forEach(function (code) { setValue(code, ex.followup.add_values[code]); });
      updateCounts();
      state.followupDone = true;
      calculate();
    });
    box.appendChild(el("span", { text: "Следующий шаг примера:" }));
    box.appendChild(b);
    box.hidden = false;
  }

  // ---------- отчёт врачу ----------
  var STATUS = {
    low: ["ниже порога", "chip--warn chip--low"], high: ["выше порога", "chip--warn chip--high"],
    normal: ["в пределах", "chip--ok"], borderline: ["пограничное", "chip--warn chip--q"],
    unknown: ["без оценки", "chip--none"]
  };
  var CHECK = {
    excluded: ["исключено", "chip--ok"], suspected: ["под подозрением", "chip--danger chip--bang"],
    not_checked: ["не проверено", "chip--none"]
  };
  var RISK = { usual: "chip--ok", elevated: "chip--warn chip--bang", high: "chip--danger chip--bang" };
  var CONF = { low: "chip--warn chip--q", moderate: "", high: "chip--ok" };

  function bars(items) {
    return el("div", { class: "bars" }, items.map(function (it) {
      var fill = el("span", { class: "bar__fill" });
      fill.style.width = Math.max(0, Math.min(100, it.p * 100)).toFixed(1) + "%";   // CSSOM, не атрибут style
      return el("div", { class: "bar" }, [
        el("span", { class: "bar__name", text: cap(it.name) }),
        el("span", { class: "bar__val", text: pct(it.p) }),
        el("span", { class: "bar__track", "aria-hidden": "true" }, [fill])
      ]);
    }));
  }
  function level1Card(l1) {
    var left = el("p", { class: "big" }, [
      l1.anemia ? "Анемия есть" : "Анемии нет",
      l1.anemia ? chip(l1.severity_ru ? "степень: " + l1.severity_ru : "ниже порога", l1.severity === "severe" ? "chip--danger chip--bang" : "chip--warn chip--low")
                : chip("гемоглобин не ниже порога", "chip--ok")
    ]);
    var right = [el("p", { text: "Гемоглобин " + num(l1.hemoglobin) + " г/л, порог " + num(l1.threshold_g_l) + " г/л." })];
    if (l1.note) { right.push(el("p", { text: l1.note })); }
    right.push(el("p", { class: "source", text: "Источник: " + l1.source + ". Это правило: модель его не меняет." }));
    return card("Уровень 1: анемия по ВОЗ", [el("div", { class: "l1" }, [left, el("div", null, right)])]);
  }
  // Строка из разделов отчёта сервера (reports.<tab>.sections[].lines), подходящая под образец; нет — "".
  function reportLine(r, tab, re) {
    var hit = "";
    ((((r || {}).reports || {})[tab] || {}).sections || []).forEach(function (s) {
      (s.lines || []).forEach(function (t) { if (!hit && re.test(t)) { hit = t; } });
    });
    return hit;
  }
  function level2Card(l2, r) {
    if (!l2.enabled) {
      return card("Уровень 2: класс и причина", [
        el("p", null, [chip("выключен", "chip--none")]),
        el("p", { text: l2.disabled_reason || "Уровень 2 недоступен." })
      ]);
    }
    var rows = [
      // Анемия воспаления: называем класс; «причина: воспаление» рядом с группой «неясного генеза» не пишем.
      [l2.ambiguous ? "Класс (вероятнее всего)" : "Класс", cap(l2.anemia_class_ru)],
      ["Причина", l2.case_group_note_ru ? "" : cap(l2.deficiency_cause_ru)],
      ["Группа кейса", cap(l2.case_group_ru)], ["Полнота панели", l2.completeness_ru],
      ["Режим", l2.mode === "benchmark" ? "бенчмарк" : "клинический"]
    ];
    var dl = el("dl", { class: "kv" });
    rows.forEach(function (r) {
      if (!r[1]) { return; }
      dl.appendChild(el("dt", { text: r[0] }));
      dl.appendChild(el("dd", { text: r[1] }));
    });
    dl.appendChild(el("dt", { text: "Уверенность" }));
    // «Средняя» рядом с 97 % сбивает с толку: словесная уверенность — не только вероятность (engine.py: «высокая» —
    // только при сданном ключевом анализе для класса и без флагов, которые её ограничивают).
    dl.appendChild(el("dd", null, [chip(l2.confidence_ru || "—", CONF[l2.confidence] || ""),
      l2.confidence && l2.confidence !== "high"
        ? el("span", { class: "kv__note", text: "«Высокая» — только если сдан ключевой анализ для класса и нет флагов «На что обратить внимание»." })
        : null]));
    var left = [dl];
    if (l2.class_note_ru) { left.push(el("p", { class: "warn-text", text: l2.class_note_ru })); }
    if (l2.case_group_note_ru) { left.push(el("p", { class: "hint", text: l2.case_group_note_ru })); }
    // Текст — тот же, что в отчёте врачу (DOCX): config/texts_ru/doctor.yaml → sections.level2.ambiguous.
    if (l2.ambiguous) {
      left.push(el("p", { class: "hint", text: reportLine(r, "doctor", /^Вывод неоднозначен/) ||
        "Вывод неоднозначен: вероятность главного варианта невысока — различить помогут анализы из «Что досдать»." }));
    }
    var right = [];
    if (l2.plausible && l2.plausible.length) {
      right.push(el("p", { class: "sub sub--first", text: "Правдоподобные варианты" }));
      right.push(bars(l2.plausible.map(function (c) { return { name: c.name_ru, p: c.p }; })));
      right.push(el("p", { class: "source", text: "Вероятности — оценка модели, обученной на учебном наборе кейса." }));
    }
    return card("Уровень 2: класс и причина", [el("div", { class: "l2" }, [el("div", null, left), el("div", null, right)])]);
  }
  function hiddenCard(h) {
    var kids = [];
    if (!h.applicable) {
      kids.push(el("p", null, [chip("не оценивается", "chip--none")]));
      kids.push(el("p", { text: h.summary_ru }));
      return card("Скрытый дефицит", kids);
    }
    kids.push(el("p", null, [h.detected ? chip("есть сигнал", "chip--warn chip--bang") : chip("сигналов нет", "chip--ok")]));
    if (h.summary_ru) { kids.push(el("p", { text: h.summary_ru })); }
    var s = h.screening;
    if (s && s.applicable) {
      kids.push(el("p", { class: "sub", text: "Скрининг по общему анализу крови" }));
      kids.push(el("p", null, ["Риск дефицита железа: ", chip(s.risk_ru || "—", RISK[s.risk] || "")]));
      kids.push(el("div", { class: "stats" }, [
        // единица — неразрывно с числом: «15 мкг/л» не разрывается на «мкг/» и «л»
        el("div", { class: "stat" }, [el("b", { text: pct(s.p_ferritin_lt15) }), el("span", null, ["ферритин ниже ", el("span", { class: "nowrap", text: "15 мкг/л" })])]),
        el("div", { class: "stat" }, [el("b", { text: pct(s.p_ferritin_lt30) }), el("span", null, ["ферритин ниже ", el("span", { class: "nowrap", text: "30 мкг/л" })])])
      ]));
      if (s.explanation_ru) { kids.push(el("p", { text: s.explanation_ru })); }
      if (s.refer_share) {
        kids.push(el("p", { class: "source", text: "Точка направления на ферритин: верхние " + Math.round(s.refer_share * 100) +
          " % женщин 18–49 лет по вероятности ферритина ниже 15 мкг/л (переключатель — «Настройки для врача»)." }));
      }
      if (s.validated_group === false) { kids.push(el("p", { class: "warn-text", text: "Для этой группы (пол и возраст) модель скрининга не проверялась." })); }
    } else if (s && s.reason_not_applicable) {
      kids.push(el("p", { class: "source", text: "Скрининг по ОАК не применён: " + s.reason_not_applicable }));
    }
    if (h.p_any !== null && h.p_any !== undefined) {
      kids.push(el("p", { class: "sub", text: "Модель кейса (учебный набор кейса)" }));
      var items = [{ name: "любой дефицит", p: h.p_any }];
      var names = (state.ref.class_names || {}).nutrients || {};
      Object.keys(h.by_nutrient || {}).forEach(function (k) { items.push({ name: names[k] || k, p: h.by_nutrient[k] }); });
      kids.push(bars(items));
    }
    if (h.rule_signals && h.rule_signals.length) {
      kids.push(el("p", { class: "sub", text: "Сигналы правил" }));
      kids.push(el("ul", { class: "list" }, h.rule_signals.map(function (r) {
        return el("li", null, [cap(r.text) + ". ", el("span", { class: "source", text: "Источник: " + r.source })]);
      })));
    }
    return card("Скрытый дефицит", kids, "card--hidden");
  }
  function flagsCard(flags) {
    if (!flags || !flags.length) { return null; }
    var nodes = flags.map(function (f) {
      return el("div", { class: "flag" + (f.severity === "info" ? " flag--info" : "") }, [
        el("h4", null, [f.title, " ", chip(f.severity === "info" ? "к сведению" : "внимание", f.severity === "info" ? "chip--none" : "chip--warn chip--bang")]),
        el("p", { text: f.text }),
        el("p", { class: "source", text: "Источник: " + f.source })
      ]);
    });
    return card("На что обратить внимание", nodes.slice(0, 2).concat([moreFold(nodes, 2)]));
  }
  function checklistCard(list) {
    if (!list || !list.length) { return null; }
    return card("Чек-лист исключений", list.map(function (c) {
      var st = CHECK[c.status] || [c.status, ""];
      return el("div", { class: "check" }, [
        el("div", null, [el("div", { class: "check__title", text: c.title }), chip(st[0], st[1])]),
        el("p", { text: c.detail })
      ]);
    }));
  }
  // Ссылки «где сдать» ведут на поиск Яндекс Карт по названию анализа. Координаты пользователя в ссылку не кладём:
  // карты сами предложат определить положение. Открываются в новой вкладке только по нажатию.
  function yandexSearch(name) { return "https://yandex.ru/maps/?text=" + encodeURIComponent("сдать анализ " + String(name).toLowerCase()); }
  function yandexRoute(lat, lon) { return "https://yandex.ru/maps/?rtext=~" + lat + "%2C" + lon + "&rtt=auto"; }
  function extLink(href, text, cls) {
    return el("a", { href: href, target: "_blank", rel: "noopener noreferrer", class: "ext" + (cls ? " " + cls : "") }, [text, el("span", { class: "ext__arrow", "aria-hidden": "true", text: "↗" })]);
  }
  function nextTestsCard(tests) {
    if (!tests || !tests.length) {
      return card("Что досдать", [el("p", { text: "Дополнительные анализы по этим данным не требуются." })]);
    }
    var items = tests.map(function (t, i) {
      return el("li", { class: "test" }, [
        el("span", { class: "test__n", "aria-hidden": "true", text: String(i + 1) }),
        el("div", { class: "test__body" }, [
          el("p", { class: "test__name" }, [t.name_ru, t.available ? null : el("span", { class: "tag", text: "в рознице недоступен" })]),
          el("p", { class: "test__why", text: t.reason }),
          el("p", { class: "source" }, [
            el("span", { class: "tag", text: t.kind === "information" ? "по приросту информации" : "по правилу" }),
            t.source ? " " + t.source : ""
          ])
        ]),
        el("div", { class: "test__side" }, [
          el("span", { class: "test__price", "data-analyte": t.analyte }),
          t.available ? extLink(yandexSearch(t.name_ru), "Где сдать", "test__where") : null
        ])
      ]);
    });
    var go = el("button", { type: "button", class: "btn btn--small test__near", text: "Показать ближайшие пункты" });
    go.addEventListener("click", function () { $("nearby").open = true; $("nearby").scrollIntoView({ behavior: smooth(), block: "start" }); });
    var rest = items.length > 3 ? el("details", { class: "fold fold--more" }, [
      el("summary", { text: "ещё " + (items.length - 3) + " " + plural(items.length - 3, "анализ", "анализа", "анализов") }),
      el("ol", { class: "tests", start: "4" }, items.slice(3))]) : null;
    return card("Что досдать", [el("ol", { class: "tests" }, items.slice(0, 3)), rest, el("div", { class: "actions" }, [go])], "card--tests");
  }
  function whyCard(ex) {
    var kids = [];
    var names = (state.ref.class_names || {}).classes12 || {};
    if (ex.top_class && ex.runner_up) {
      kids.push(el("p", { class: "source", text: "Сравниваются два самых вероятных варианта: «" + (names[ex.top_class] || ex.top_class) + "» и «" + (names[ex.runner_up] || ex.runner_up) + "»." }));
    }
    function side(title, items, word) {
      var list = items.length
        ? el("ul", { class: "list" }, items.map(function (c) {
            return el("li", null, [el("strong", { text: c.name_ru + " " + num(c.value) + " " + c.unit }), " — " + c.text]);
          }))
        : el("p", { class: "muted", text: "Таких показателей нет." });
      return el("div", null, [el("p", { class: "sub", text: title + " (" + word + ")" }), list]);
    }
    var f = ex.items_for || [], a = ex.items_against || [];
    if (f.length || a.length) {
      kids.push(el("div", { class: "proscons" }, [side("За вывод", f, "поддерживают"), side("Против вывода", a, "противоречат")]));
    }
    if (ex.note) { kids.push(el("p", { text: ex.note })); }
    if (!kids.length) { return null; }
    return card("Почему такой вывод", kids);
  }
  function valuesCard(values, notes) {
    var off = 0;
    var body = el("tbody", null, values.map(function (v) {
      var st = STATUS[v.norm.status] || [v.norm.status, ""];
      if (v.norm.status === "low" || v.norm.status === "high") { off += 1; }
      return el("tr", null, [
        el("th", { scope: "row", text: v.name_ru }),
        el("td", { class: "num", "data-label": "Значение", text: num(v.value) + " " + v.unit }),
        el("td", { class: "st st--" + (v.norm.status || "unknown"), "data-label": "Статус" }, [chip(st[0], st[1])]),
        el("td", { "data-label": "Порог и источник" }, [
          v.norm.threshold_text || "",
          v.contribution ? el("span", { class: "source", text: "Вклад в вывод: " + v.contribution }) : null,
          v.warning ? el("span", { class: "warn-text", text: "Проверьте: " + v.warning }) : null
        ])
      ]);
    }));
    var kids = [el("div", { class: "tbl-wrap" }, [el("table", { class: "stack" }, [
      el("thead", null, [el("tr", null, [
        el("th", { scope: "col", text: "Показатель" }), el("th", { scope: "col", class: "num", text: "Значение" }),
        el("th", { scope: "col", text: "Статус" }), el("th", { scope: "col", text: "Порог и источник" })])]),
      body
    ])])];
    if (notes && notes.length) {
      kids.push(el("p", { class: "sub", text: "Замечания к вводу" }));
      kids.push(el("ul", { class: "list" }, notes.map(function (n) { return el("li", { text: n.text }); })));
    }
    return el("details", { class: "card card--fold" }, [
      el("summary", null, [el("h3", { text: "Показатели" }), countBadge(values.length, "показатель", "показателя", "показателей"),
        off ? el("span", { class: "fold__note", text: "вне порога: " + off }) : null]),
      el("div", { class: "fold__body" }, kids)
    ]);
  }
  // Три плитки «с одного взгляда»: анемия по ВОЗ, скрытый дефицит, сколько анализов досдать. Цвет дублируется словом.
  function summaryTiles(r) {
    var l1 = r.level1 || {}, h = r.hidden_deficiency || {}, n = (r.next_tests || []).length;
    var t1 = l1.anemia
      ? ["danger", "Анемия", (l1.severity_ru ? cap(l1.severity_ru) : "Есть"), "Hb " + num(l1.hemoglobin) + " при пороге " + num(l1.threshold_g_l) + " г/л"]
      : ["ok", "Анемия", "Нет", "Hb " + num(l1.hemoglobin) + " при пороге " + num(l1.threshold_g_l) + " г/л"];
    var t2 = !h.applicable ? ["none", "Скрытый дефицит", "Не оценивается", "при анемии — см. класс и причину"]
      : h.detected ? ["warn", "Скрытый дефицит", "Есть сигнал", (h.screening && h.screening.applicable && h.screening.risk_ru) ? "риск по ОАК: " + h.screening.risk_ru : "по сданным анализам"]
      : ["ok", "Скрытый дефицит", "Сигналов нет", "по сданным анализам"];
    var t3 = n ? ["info", "Досдать", n + " " + plural(n, "анализ", "анализа", "анализов"), "список и где сдать — ниже"]
      : ["ok", "Досдать", "Ничего", "по правилам и расчёту"];
    return el("div", { class: "tiles" }, [t1, t2, t3].map(function (t) {
      return el("div", { class: "tile tile--" + t[0] }, [
        el("span", { class: "tile__k", text: t[1] }), el("b", { class: "tile__v", text: t[2] }), el("span", { class: "tile__s", text: t[3] })]);
    }));
  }
  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    return (m10 === 1 && m100 !== 11) ? one : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) ? few : many;
  }
  function renderDoctor(r) {
    var box = $("panel-doctor");
    clear(box);
    // Метка статуса у вывода: срочно / анемия / без анемии — цветом и словом (не только цветом).
    var tone = reportTone(r), headline = r.reports.doctor.headline;
    // Вывод уже начинается со слова метки («Срочно. Тяжёлая анемия…») — метку не повторяем.
    var toneWord = toneTag(tone, headline);
    // Беременность «неизвестно»: какой порог применён — рядом с выводом, а не только в «Подробнее» (уровень 1).
    var preg = ((state.input || {}).pregnancy || {}).status;
    box.appendChild(el("section", { class: "card headline headline--" + tone }, [
      toneWord ? el("span", { class: "headline__tag", text: toneWord }) : null, el("p", { text: headline }),
      preg === "unknown" && r.level1.note ? el("p", { class: "headline__note", text: r.level1.note }) : null]));
    box.appendChild(summaryTiles(r));
    if (r.level1.urgent && r.level1.urgent.length) {
      box.appendChild(el("div", { class: "urgent", role: "alert" }, [
        el("strong", { text: "Почему срочно" }),
        el("ul", null, r.level1.urgent.map(function (u) {
          return el("li", null, [u.text, " ", el("span", { class: "source", text: "Источник: " + u.source })]);
        }))
      ]));
    }
    // Лаконично: на виду — главное для решения (класс при анемии или скрининг без анемии, флаги, что досдать);
    // остальное — под одним «Подробнее», чтобы отчёт не был перегружен.
    var anemia = !!(r.level1 && r.level1.anemia);
    var main = [anemia ? level2Card(r.level2, r) : hiddenCard(r.hidden_deficiency), flagsCard(r.flags), nextTestsCard(r.next_tests),
      valuesCard(r.values || [], r.normalization || [])];
    var more = [level1Card(r.level1), anemia ? hiddenCard(r.hidden_deficiency) : level2Card(r.level2, r), checklistCard(r.checklist),
      whyCard(r.explanation || {})];
    if (r.limitations && r.limitations.length) {
      more.push(card("Ограничения", [el("ul", { class: "list" }, r.limitations.map(function (t) { return el("li", { text: t }); }))]));
    }
    main.forEach(function (c) { if (c) { box.appendChild(c); } });
    var moreBox = el("details", { class: "more-block" }, [el("summary", { text: "Подробнее: уровень 1, вероятности, чек-лист, почему такой вывод, ограничения" })]);
    more.forEach(function (c) { if (c) { moreBox.appendChild(c); } });
    box.appendChild(moreBox);
    var v = r.version;
    box.appendChild(el("p", { class: "meta" }, [
      "Движок " + v.engine + " · модель кейса " + v.case_model + " · скрининг " + v.screen_model + " · пороги " + v.norms +
      " · обработка " + num(Math.round(r.processing_ms)) + " мс. ",
      el("strong", { text: state.ref.disclaimer })
    ]));
  }

  // ---------- отчёт пациенту ----------
  // Шкалы «где значение относительно нормы» — пациенту понятнее таблицы (исследования интерфейсов результатов
  // анализов: горизонтальная полоса нормы + точка значения + слово). Норма — действующий референс: свой из блока
  // «Референсы лаборатории» (те, что ушли в запрос), иначе референс проекта по умолчанию для пола.
  //   • Норма с одной границей (B12 «от 350», СРБ «до 5», рСКФ «от 90») — полоса от границы до края шкалы.
  //   • Границ нет (беременность — референсы проекта не применяются; набор референсов из кабинета, чьих чисел на
  //     странице нет) — строка без полосы: название, значение и слово по статусу сервера; при беременности
  //     показатели, кроме гемоглобина, «оценит врач» (сервис сравнивает с порогом только Hb).
  //   • Статус сервера считается по клиническому порогу и может расходиться с полосой (ферритин 62 при воспалении:
  //     в полосе 30–400, но ниже порога 70 мкг/л) — тогда слово жёлтое: «в пределах нормы, но ниже порога…».
  var STATUS_WORD = { normal: "в норме", low: "ниже нормы", high: "выше нормы", borderline: "на границе нормы", unknown: "оценит врач" };
  function isNum(x) { return typeof x === "number" && isFinite(x); }
  function scaleWord(v, lo, hi, band, pregnant) {
    var st = (v.norm && v.norm.status) || "";
    if (!band) {
      if (pregnant && v.analyte !== "hemoglobin") { return ["оценит врач", "none"]; }
      var w = STATUS_WORD[st] || "оценит врач";
      return [w, st === "normal" ? "ok" : (st === "low" || st === "high") ? "danger" : st === "borderline" ? "warn" : "none"];
    }
    var below = isNum(lo) && v.value < lo, above = isNum(hi) && v.value > hi;
    if ((st === "low" || st === "high") && !below && !above) {
      var infl = /воспален/i.test((v.norm && v.norm.threshold_text) || "") ? " при воспалении" : "";
      return ["в пределах нормы, но " + (st === "low" ? "ниже" : "выше") + " порога" + infl, "warn"];
    }
    if (st === "normal" && (below || above)) { return [below ? "ниже полосы нормы" : "выше полосы нормы", "warn"]; }
    var word = STATUS_WORD[st] && st !== "unknown" ? STATUS_WORD[st] : (below ? "ниже нормы" : above ? "выше нормы" : "в норме");
    return [word, word === "в норме" ? "ok" : (word === "на границе нормы" ? "warn" : "danger")];
  }
  function patientScales(r) {
    var inp = state.input || {}, preg = (inp.pregnancy || {}).status;
    var pregnant = inp.sex === "F" && preg === "yes";
    var defs = labrefDefaultsFor(inp.sex || sex(), preg), sent = inp.reference_ranges || {};
    var refs = r.references || {}, cabinetLab = refs.set === "lab" && !inp.reference_ranges ? (refs.local_analytes || []) : [];
    var rows = [], banded = 0, lab = false, open = false, bare = false;   // lab — граница из «Референсов лаборатории»; open — норма с одной границей
    (r.values || []).forEach(function (v) {
      if (!isNum(v.value)) { return; }
      var lo = null, hi = null, d = defs[v.analyte], s = sent[v.analyte];
      if (d) { lo = isNum(d[0]) ? d[0] : null; hi = isNum(d[1]) ? d[1] : null; }
      if (s) {                                    // свои границы (в канонической единице: collectLabRef)
        if (isNum(s.low)) { lo = s.low; lab = true; }
        if (isNum(s.high)) { hi = s.high; lab = true; }
      } else if (cabinetLab.indexOf(v.analyte) >= 0) { lo = hi = null; }
      if (lo !== null && hi !== null && hi <= lo) { lo = hi = null; }
      var band = lo !== null || hi !== null;
      var wk = scaleWord(v, lo, hi, band, pregnant), word = wk[0], kind = wk[1];
      var unit = v.unit || "", bar;
      if (band) {
        banded += 1;
        // Шкала: обе границы — запас 60 % ширины нормы по краям (не ниже нуля); одна граница — от 0 до удвоенной
        // границы (или чуть дальше значения), полоса нормы — до края шкалы.
        var min, max;
        if (lo !== null && hi !== null) { var span = hi - lo; min = lo - span * 0.6; max = hi + span * 0.6; if (lo >= 0) { min = Math.max(0, min); } }
        else { min = 0; max = Math.max((lo !== null ? lo : hi) * 2, v.value * 1.15); open = true; }
        var pos = function (x) { return Math.max(0, Math.min(100, (x - min) / (max - min) * 100)); };
        var bandEl = el("span", { class: "scale__band" + (hi === null ? " scale__band--to-r" : lo === null ? " scale__band--to-l" : "") });
        var dot = el("span", { class: "scale__dot scale__dot--" + (kind === "none" ? "warn" : kind) });
        bandEl.setAttribute("data-l", (lo === null ? 0 : pos(lo)).toFixed(1));
        bandEl.setAttribute("data-r", (hi === null ? 0 : 100 - pos(hi)).toFixed(1));
        dot.setAttribute("data-x", pos(v.value).toFixed(1));
        var normText = lo !== null && hi !== null ? num(lo) + "–" + num(hi) : lo !== null ? "от " + num(lo) : "до " + num(hi);
        bar = el("span", { class: "scale__bar", role: "img",
          "aria-label": v.name_ru + ": " + num(v.value) + " " + unit + ", норма " + normText + " " + unit + ", " + word }, [bandEl, dot]);
      } else {
        bare = true;
        bar = el("span", { class: "scale__bar scale__bar--none", "aria-hidden": "true" });
      }
      rows.push(el("li", { class: "scale" + (band ? "" : " scale--bare") }, [
        el("span", { class: "scale__name", text: v.name_ru }),
        el("span", { class: "scale__val", text: num(v.value) + " " + unit }),
        bar,
        el("span", { class: "scale__word scale__word--" + kind, text: word })
      ]));
    });
    if (!rows.length) { return null; }
    var list = el("ul", { class: "scales" + (banded ? "" : " scales--bare") }, rows);
    // Положение задаём через CSS-переменные из JS (style-атрибуты в разметке CSP не разрешает, а свойства DOM — можно).
    Array.prototype.forEach.call(list.querySelectorAll(".scale__band"), function (b) {
      b.style.left = b.getAttribute("data-l") + "%"; b.style.right = b.getAttribute("data-r") + "%";
    });
    Array.prototype.forEach.call(list.querySelectorAll(".scale__dot"), function (d) { d.style.left = d.getAttribute("data-x") + "%"; });
    var hints = [];
    if (banded) {
      hints.push(lab || cabinetLab.length
        ? "Зелёная полоса — норма вашей лаборатории (с бланка или из настроек), где она указана; точка — ваше значение."
        : "Зелёная полоса — норма для взрослых вашего пола; точка — ваше значение. Норма лаборатории может немного отличаться.");
    }
    if (open) { hints.push("Полоса до края шкалы — у нормы одна граница («от …» или «до …»)."); }
    if (bare) {
      hints.push(pregnant ? "При беременности нормы другие: сервис сравнивает с порогом только гемоглобин, остальные значения оценит врач."
        : "Без полосы — норма показателя здесь не задана; значение оценит врач.");
    }
    return psection("Ваши показатели", rows.length, true, [list].concat(hints.map(function (t) { return el("p", { class: "hint", text: t }); })));
  }
  // Раздел памятки — раскрывающийся блок с числом пунктов у заголовка.
  function psection(title, n, open, content) {
    return el("details", { class: "psec", open: !!open }, [
      el("summary", null, [el("h3", { text: title }), countBadge(n)]),
      el("div", { class: "psec__body" }, content)
    ]);
  }
  // Пациенту: открыты «Главное» и «Ваши показатели» (и «Когда обращаться срочно», если сервер поставил его первым —
  // так бывает только при срочном случае); остальные разделы свёрнуты.
  function renderPatient(r) {
    var box = $("panel-patient");
    clear(box);
    var rep = r.reports.patient;
    var art = el("article", { class: "card patient" }, [el("p", { class: "patient__head patient__head--" + reportTone(r), text: rep.headline })]);
    var scales = patientScales(r);
    var sections = rep.sections || [];
    var mainIdx = -1;
    sections.forEach(function (s, i) { if (mainIdx < 0 && /^главное/i.test(s.title || "")) { mainIdx = i; } });
    if (scales && mainIdx < 0) { art.appendChild(scales); }
    sections.forEach(function (s, i) {
      var lines = s.lines || [];
      var content = lines.length > 1
        ? el("ul", null, lines.map(function (t) { return el("li", { text: fixText(t) }); }))
        : el("p", { text: fixText(lines[0] || "") });
      art.appendChild(psection(s.title, lines.length, i === 0 || i === mainIdx, [content]));
      if (scales && i === mainIdx) { art.appendChild(scales); }
    });
    box.appendChild(art);
    box.appendChild(el("p", { class: "meta" }, [el("strong", { text: state.ref.disclaimer })]));
  }
  // ---------- результат без формы ----------
  // После расчёта форма скрывается, результат — во всю ширину по центру; сверху строка-сводка
  // «Женщина, 34 года · 5 показателей · [Изменить данные] [Новый анализ]».
  function ruDate(iso) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    return m ? m[3] + "." + m[2] + "." + m[1] : "";
  }
  function personText(i) {
    var parts = [i.sex === "M" ? "мужчина" : "женщина"];
    if (i.age_years) { parts.push(i.age_years + " " + plural(i.age_years, "год", "года", "лет")); }
    var p = i.pregnancy || {};
    if (p.status === "yes") { parts.push("беременность" + (p.trimester ? ", " + ["", "I", "II", "III"][p.trimester] + " триместр" : "")); }
    return parts.join(", ");
  }
  // «введено N»: в блоке «Показатели» их может быть больше — сервер добавляет расчётные (рСКФ по креатинину).
  function renderSummary(r) {
    var i = state.input || {}, n = Object.keys(i.values || {}).length;
    var parts = [cap(personText(i)), "введено " + n + " " + plural(n, "показатель", "показателя", "показателей")];
    if (ruDate(state.analysisDate)) { parts.push("Дата анализа: " + ruDate(state.analysisDate)); }
    $("result-sum-text").textContent = parts.join(" · ");
    renderRefs(r);
  }
  // Расчёт по референсам лаборатории (ответ → references.set == "lab"): плашка в сводке. Без своих границ в запросе
  // это набор врача «по умолчанию» из кабинета (сервер подставил его сам, ADR 0009) — ссылка «изменить в кабинете»;
  // поля «Референсы лаборатории» на странице при этом не трогаем. Имя набора — lab_title / lab_name, если не "request".
  function renderRefs(r) {
    var box = $("result-refs"), refs = (r && r.references) || {};
    clear(box);
    box.hidden = refs.set !== "lab";
    if (box.hidden) { return; }
    var own = !!(state.input || {}).reference_ranges;
    var name = refs.lab_title || refs.lab_name;
    name = name && name !== "request" && name !== "референсы из запроса" ? "«" + name + "»" : "";
    box.appendChild(el("p", { class: "result-sum__refline" }, own
      ? ["Нормы: референсы лаборатории — из «Настроек для врача»" + (name ? " " + name : "")]
      : ["Нормы: референсы лаборатории (" + (name ? "набор " + name + ", " : "") + "ваш набор по умолчанию) · ",
         el("a", { href: "/cabinet#sec-labrefs", text: "изменить в кабинете" })]));
    if (refs.warning) { box.appendChild(el("p", { class: "result-sum__refnote", text: refs.warning })); }
  }
  function setEditing(on) {
    $("layout").classList.toggle("is-editing", !!on);
    $("edit-data").setAttribute("aria-expanded", on ? "true" : "false");
    $("edit-data").textContent = on ? "Скрыть форму" : "Изменить данные";
  }
  // Прокрутка к элементу под закреплённой шапкой. Далеко (больше полутора экранов) — сразу, иначе плавно:
  // долгая плавная прокрутка выглядит как «прыжок».
  function scrollUnderTop(node) {
    var top = document.querySelector(".top");
    var off = top && window.getComputedStyle(top).position === "sticky" ? top.offsetHeight + 14 : 12;
    var target = Math.max(0, node.getBoundingClientRect().top + window.pageYOffset - off);
    var far = Math.abs(target - window.pageYOffset) > window.innerHeight * 1.5;
    window.scrollTo({ top: target, behavior: far ? "instant" : smooth() });
  }
  function render(r, stay) {
    $("empty").hidden = true;
    $("result").hidden = false;
    $("layout").classList.add("has-result");
    if (!stay) { setEditing(false); }
    $("result").classList.remove("is-in"); void $("result").offsetWidth; $("result").classList.add("is-in");   // плавное появление
    renderSummary(r);
    renderFollowup();
    renderDoctor(r);
    renderPatient(r);
    renderNearby(r);
    // После расчёта страница встаёт ровно на начало результата (под закреплённой шапкой), где бы ни была кнопка.
    // Форма скрыта — фокус с кнопки «Рассчитать» ушёл бы на BODY: ставим его на сводку результата (tabindex=-1),
    // а вывод открытой вкладки объявляем через живую область #result-live.
    if (!stay) {
      scrollUnderTop($("result"));             // синхронно: getBoundingClientRect сам пересчитает раскладку
      $("result-sum").focus({ preventScroll: true });
    }
    announce((stay ? "Результат пересчитан. " : "Результат готов. ") + r.reports[activeTab()].headline);
  }
  function announce(text) {
    var live = $("result-live");
    live.textContent = "";
    setTimeout(function () { live.textContent = text; }, 60);   // пауза: одинаковый текст тоже объявляется заново
  }
  // «Новый анализ» и «Очистить»: пустая форма, результата нет.
  function resetAll() {
    clearForm();
    clearChecks();
    clear($("paste-out"));
    // вставленный текст бланка и выбранный файл — тоже от прежнего анализа
    $("paste-text").value = ""; $("paste-file").value = "";
    $("upload-more").open = false;
    $("followup").hidden = true;
    state.example = null; state.result = null; state.followupDone = false;
    markExample(null);
    showAlert("");
    $("result").hidden = true;
    $("empty").hidden = false;
    $("layout").classList.remove("has-result");
    setEditing(false);
  }

  // ---------- где сдать рядом ----------
  // Список пунктов — свой файл (static/labs/labs_msk.json, OpenStreetMap, ODbL; scripts/build_labs.py). Ближайшие
  // считаются здесь, в браузере: координаты пользователя никуда не отправляются (ни на сервер, ни в ссылки).
  // Блок свёрнут, как легенда: регион, цены, геолокация и карта — только после раскрытия. Карта (Leaflet из
  // static/vendor) подгружается только по кнопке. Подложка — картинки tile.openstreetmap.org (ADR 0008), светлая
  // «приглушённая» — фильтром CSS. CARTO light (решение пользователя 04.10) без ключа API с 2026 г. отдаёт вместо
  // карты плитку-водяной знак «API KEY REQUIRED» (проверено 04.10.2026), поэтому пока OSM; для CARTO с ключом
  // достаточно сменить TILES и строку авторства. Надписи в углу карты выключены (attributionControl: false);
  // авторство OpenStreetMap (ODbL) — строкой под картой (#nearby-credit), этого требует ODbL.
  var MSK = [55.7558, 37.6173];
  var TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  var nf1 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
  var near = { labs: null, loading: null, point: null, how: null, map: null, marks: null, me: null, top: null, tests: [] };
  function nearStatus(text) { $("nearby-status").textContent = text || ""; }
  function loadLabs() {
    if (near.labs) { return Promise.resolve(near.labs); }
    if (!near.loading) {
      near.loading = request("GET", "/static/labs/labs_msk.json").then(function (r) {
        if (!r.ok || !r.data || !Array.isArray(r.data.labs)) { near.loading = null; throw new Error("labs"); }
        near.labs = r.data.labs.filter(function (l) { return Array.isArray(l) && isFinite(l[0]) && isFinite(l[1]); });
        return near.labs;
      });
    }
    return near.loading;
  }
  function distKm(a, b) {                       // формула гаверсинусов, км
    var R = 6371, k = Math.PI / 180, dLa = (b[0] - a[0]) * k, dLo = (b[1] - a[1]) * k;
    var h = Math.pow(Math.sin(dLa / 2), 2) + Math.cos(a[0] * k) * Math.cos(b[0] * k) * Math.pow(Math.sin(dLo / 2), 2);
    return 2 * R * Math.asin(Math.sqrt(h));
  }
  function fmtDist(d) { return d < 1 ? (Math.round(d * 100) * 10) + " м" : nf1.format(d) + " км"; }
  var DAYS = { Mo: "Пн", Tu: "Вт", We: "Ср", Th: "Чт", Fr: "Пт", Sa: "Сб", Su: "Вс", PH: "праздники" };
  function hoursRu(h) {
    if (!h) { return ""; }
    if (h === "24/7") { return "круглосуточно"; }
    return h.replace(/\b(Mo|Tu|We|Th|Fr|Sa|Su|PH)\b/g, function (m) { return DAYS[m]; }).replace(/\boff\b/g, "выходной").replace(/;\s*/g, "; ");
  }
  function safeUrl(u) { return /^https?:\/\/[^\s"'<>]+$/i.test(u || "") ? u : ""; }
  function labName(l) { return l[2] || l[3] || "Лаборатория"; }
  function labNode(l, extra) {
    var name = labName(l);
    return [
      el("p", { class: "lab__name" }, [name, (l[3] && name.toLowerCase().indexOf(l[3].toLowerCase()) < 0) ? el("span", { class: "tag", text: l[3] }) : null]),
      // адрес в OSM есть не у всех пунктов: тогда — подсказка открыть маршрут (он ведёт по координатам)
      el("p", { class: "lab__addr" + (l[4] ? "" : " lab__addr--none"), text: l[4] || "адрес не указан — откройте маршрут" }),
      l[5] ? el("p", { class: "lab__hours", text: "Часы: " + hoursRu(l[5]) }) : null,
      el("p", { class: "lab__links" }, [extLink(yandexRoute(l[0], l[1]), "Маршрут"), safeUrl(l[6]) ? extLink(safeUrl(l[6]), "Сайт") : null].concat(extra || []))
    ];
  }
  // ---------- средняя цена в выбранном регионе ----------
  // Цены — config/prices.yaml (медиана по лабораториям Москвы на дату прайса, без взятия крови); регионы — там же
  // (regions: название, рамка bbox [юг, запад, север, восток]). Регион выбирается списком или по выбранной точке.
  var nf0 = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
  function regions() { return ((state.ref && state.ref.prices) || {}).regions || []; }
  function regionFor(p) {
    return regions().filter(function (g) { var b = g.bbox; return b && p[0] >= b[0] && p[0] <= b[2] && p[1] >= b[1] && p[1] <= b[3]; })[0] || null;
  }
  function currentRegion() { var code = $("nearby-region").value; return regions().filter(function (g) { return g.code === code; })[0] || null; }
  function buildRegionSelect() {
    var sel = $("nearby-region");
    if (sel.options.length) { return; }
    regions().forEach(function (g) { sel.appendChild(el("option", { value: g.code, text: g.name_ru })); });
    sel.appendChild(el("option", { value: "", text: "Другой регион" }));
    sel.addEventListener("change", function () { updatePrices(); });
  }
  function priceOf(code) {
    var it = (((state.ref || {}).prices || {}).items || {})[code] || {};
    if (it.price !== null && it.price !== undefined) { return { rub: it.price }; }
    return { text: it.available === false ? "в рознице недоступен" : (it.note || "цену уточните в лаборатории") };
  }
  function updatePrices() {
    var reg = currentRegion(), tests = (state.result && state.result.next_tests) || [];
    $("nearby-region-hint").textContent = reg ? (near.point && !regionFor(near.point) ? "" : (reg.note || "")) : "Для этого региона цен в сервисе нет — уточните в лаборатории.";
    Array.prototype.forEach.call(document.querySelectorAll(".test__price"), function (span) {
      var p = priceOf(span.getAttribute("data-analyte"));
      span.textContent = !reg ? "" : (p.rub !== undefined ? "≈ " + nf0.format(p.rub) + " ₽" : p.text);
      span.title = reg ? "Средняя цена: " + reg.name_ru : "";
    });
    var box = $("nearby-prices");
    clear(box);
    $("nearby-sum").textContent = "";
    if (!reg || !tests.length) { return; }
    var total = 0, rows = [];
    tests.forEach(function (t) {
      var p = priceOf(t.analyte);
      if (p.rub !== undefined) { total += p.rub; }
      rows.push(el("li", null, [el("span", { text: t.name_ru }), el("b", { text: p.rub !== undefined ? "≈ " + nf0.format(p.rub) + " ₽" : p.text })]));
    });
    var bd = ((state.ref.prices || {}).blood_draw) || null;
    box.appendChild(el("p", { class: "prices__title", text: "Средняя цена в регионе «" + reg.name_ru + "»" }));
    box.appendChild(el("ul", { class: "prices__list" }, rows));
    box.appendChild(el("p", { class: "prices__total" }, [
      el("span", { text: "Всего за анализы" }), el("b", { text: "≈ " + nf0.format(total) + " ₽" + (bd ? " + взятие крови " + nf0.format(bd.min) + "–" + nf0.format(bd.max) + " ₽" : "") })]));
    if (total) { $("nearby-sum").textContent = "≈ " + nf0.format(total) + " ₽ в регионе"; $("nearby-sum").title = "Средняя цена: " + reg.name_ru; }
  }
  function renderNearby(r) {
    buildRegionSelect();
    near.tests = (r.next_tests || []).filter(function (t) { return t.available; });
    $("nearby").hidden = !(r.next_tests || []).length;
    var more = $("nearby-more");
    clear(more);
    if (near.tests.length) {
      more.appendChild(document.createTextNode("Нет удобного пункта? Поиск на Яндекс Картах: "));
      near.tests.forEach(function (t, i) {
        if (i) { more.appendChild(document.createTextNode(" · ")); }
        more.appendChild(extLink(yandexSearch(t.name_ru), t.name_ru));
      });
    }
    if (near.point) { setPoint(near.point, near.how); }
    else if (!near.map) { nearStatus(""); }
    updatePrices();
  }
  function setPoint(p, how) {
    near.point = p; near.how = how;
    var reg = regionFor(p);                          // регион цен — по выбранной точке
    $("nearby-region").value = reg ? reg.code : "";
    updatePrices();
    loadLabs().then(function (labs) {
      var top = labs.map(function (l, i) { return { i: i, d: distKm(p, l) }; })
        .sort(function (a, b) { return a.d - b.d; }).slice(0, 5);
      var list = $("nearby-list"), restList = $("nearby-rest-list");
      clear(list); clear(restList);
      // Точка вне региона списка (ближайший пункт дальше 40 км): пункты за сотни километров не показываем и не
      // выгружаем (near.top = null — их нет ни в DOCX, ни в печати); остаются сообщение и ссылки на Яндекс Карты.
      if (!top.length || top[0].d > 40) {
        near.top = null;
        $("nearby").classList.remove("has-list");
        $("nearby-rest").hidden = true;
        nearStatus((top.length ? "Ближайший пункт из нашего списка — в " + fmtDist(top[0].d) + ": " : "") +
          "список пока только по Москве и области. Найдите лабораторию рядом на Яндекс Картах (ссылки ниже).");
        if (near.map) { drawTop(); }
        return;
      }
      near.top = top;
      $("nearby").classList.add("has-list");          // для печати: блок печатается, только когда пункты выбраны
      var restN = Math.max(0, top.length - 3);
      $("nearby-rest").hidden = !restN;
      $("nearby-rest-sum").textContent = restN ? "ещё " + restN + " " + plural(restN, "пункт", "пункта", "пунктов") : "";
      top.forEach(function (t, n) {
        var l = labs[t.i];
        // С клавиатуры пункт открывается кнопкой «На карте» (вложенные ссылки внутри role=button недопустимы);
        // мышью — нажатием на карточку.
        var show = el("button", { type: "button", class: "lab__show", "aria-label": "Показать на карте: " + labName(l), text: "На карте" });
        show.addEventListener("click", function () { showOnMap(t.i); });
        var li = el("li", { class: "lab", "data-i": String(t.i) }, [
          el("span", { class: "lab__n", "aria-hidden": "true", text: String(n + 1) }),
          el("div", { class: "lab__body" }, labNode(l, [show])),
          el("span", { class: "lab__d", text: fmtDist(t.d) })
        ]);
        li.addEventListener("click", function (e) { if (e.target.closest && e.target.closest("a, button")) { return; } showOnMap(t.i); });
        (n < 3 ? list : restList).appendChild(li);
      });
      var where = how === "geo" ? "к вашему положению" : "к выбранной точке";
      nearStatus("Ближайшие " + where + " — " + top.length + " " + plural(top.length, "пункт", "пункта", "пунктов") + ". Расстояние — по прямой." +
        (near.map ? "" : " «На карте» — показать пункт на карте."));
      if (near.map) { drawTop(); }
    }).catch(function () { nearStatus("Не удалось загрузить список пунктов. Обновите страницу."); });
  }
  function locate() {
    if (!navigator.geolocation) { nearStatus("Браузер не определяет положение — выберите точку на карте."); return; }
    nearStatus("Определяем положение…");
    navigator.geolocation.getCurrentPosition(function (pos) {
      setPoint([pos.coords.latitude, pos.coords.longitude], "geo");
    }, function (err) {
      nearStatus(err && err.code === 1 ? "Доступ к положению не разрешён — выберите точку на карте." : "Не удалось определить положение — выберите точку на карте.");
    }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 });
  }
  function loadLeaflet() {
    if (window.L) { return Promise.resolve(window.L); }
    return new Promise(function (resolve, reject) {
      var css = document.createElement("link");
      css.rel = "stylesheet"; css.href = "/static/vendor/leaflet/leaflet.css";
      document.head.appendChild(css);
      var js = document.createElement("script");
      js.src = "/static/vendor/leaflet/leaflet.js";
      js.onload = function () { resolve(window.L); };
      js.onerror = reject;
      document.head.appendChild(js);
    });
  }
  function mapLoading(text) {
    var p = $("nearby-loading");
    p.textContent = text || "";
    p.hidden = !text;
  }
  function openMap() {
    var box = $("nearby-map");
    $("nearby").open = true;
    $("nearby-mapbox").hidden = false; $("nearby-credit").hidden = false;
    if (near.map) { near.map.invalidateSize(); return Promise.resolve(near.map); }
    mapLoading("Загружаем карту…");
    return Promise.all([loadLeaflet(), loadLabs()]).then(function (res) {
      var L = res[0], labs = res[1];
      near.map = L.map(box, { preferCanvas: true, zoomControl: false, attributionControl: false }).setView(near.point || MSK, near.point ? 13 : 10);
      L.control.zoom({ zoomInTitle: "Приблизить", zoomOutTitle: "Отдалить" }).addTo(near.map);   // подписи кнопок — по-русски
      // Подложке — только origin страницы (strict-origin), без пути; «Загружаем карту…» — до первой полной загрузки.
      var tiles = L.tileLayer(TILES, { maxZoom: 19, referrerPolicy: "strict-origin", className: "nearby__tiles" });
      tiles.once("load", function () { mapLoading(""); });
      setTimeout(function () {
        if (!$("nearby-loading").hidden) {
          mapLoading("");
          nearStatus("Подложка карты не загрузилась (нужен интернет). Пункты и список ближайших работают и без неё.");
        }
      }, 15000);
      tiles.addTo(near.map);
      near.marks = L.layerGroup().addTo(near.map);
      // Все пункты — фоном, без нажатия (их сотни рядом): щелчок в любом месте карты выбирает точку поиска.
      // Нажимаются только 5 ближайших — пронумерованные метки (drawTop).
      labs.forEach(function (l) {
        L.circleMarker([l[0], l[1]], { radius: 3.5, weight: 1, color: "#0F4A52", fillColor: "#19D3C5", fillOpacity: 0.7, interactive: false })
          .addTo(near.marks);
      });
      near.map.on("click", function (e) { setPoint([e.latlng.lat, e.latlng.lng], "map"); });
      if (near.point) { setPoint(near.point, near.how); }
      else { nearStatus("Нажмите на карте место, где вам удобно сдать анализ, — покажем 5 ближайших пунктов."); }
      return near.map;
    }).catch(function () {
      mapLoading("");
      nearStatus("Карта не загрузилась (нужен интернет для подложки). Список ближайших работает и без карты — «Определить моё положение».");
    });
  }
  function drawTop() {
    var L = window.L;
    if (!near.map || !L || !near.point) { return; }
    if (near.me) { near.me.remove(); }
    var group = L.layerGroup();
    L.circleMarker(near.point, { radius: 9, weight: 3, color: "#fff", fillColor: "#061019", fillOpacity: 1 }).addTo(group)
      .bindTooltip(near.how === "geo" ? "Вы здесь" : "Выбранная точка", { direction: "top" });
    var bounds = [near.point];
    if (!near.top) { near.me = group.addTo(near.map); near.map.setView(near.point, 10); return; }   // вне региона списка
    near.top.forEach(function (t, n) {
      var l = near.labs[t.i];
      bounds.push([l[0], l[1]]);
      L.marker([l[0], l[1]], { icon: L.divIcon({ className: "pin", html: el("span", { text: String(n + 1) }), iconSize: [28, 28], iconAnchor: [14, 14] }),
        title: l[2] || l[3] || "" })
        .bindPopup(function () { return el("div", { class: "lab lab--pop" }, [el("div", { class: "lab__body" }, labNode(l))]); })
        .addTo(group);
    });
    near.me = group.addTo(near.map);
    near.map.fitBounds(bounds, { padding: [36, 36], maxZoom: 15 });
  }
  function showOnMap(i) {
    openMap().then(function () {
      if (!near.map) { return; }
      var l = near.labs[i];
      near.map.setView([l[0], l[1]], 16);
      window.L.popup().setLatLng([l[0], l[1]]).setContent(el("div", { class: "lab lab--pop" }, [el("div", { class: "lab__body" }, labNode(l))])).openOn(near.map);
      $("nearby-map").scrollIntoView({ behavior: smooth(), block: "nearest" });
    });
  }

  // ---------- выгрузка: DOCX (в браузере, export.js) и PDF (окно печати → «Сохранить как PDF») ----------
  function activeTab() { return $("tab-patient").getAttribute("aria-selected") === "true" ? "patient" : "doctor"; }
  function today() { return new Date().toLocaleDateString("ru-RU"); }
  function personLine() {
    var d = ruDate(state.analysisDate);
    return personText(state.input || {}) + (d ? " · дата анализа: " + d : "");
  }
  // Статус вывода для цветной полосы: срочно / анемия / без анемии (как метка у вывода на странице).
  var TONE_WORD = { urgent: "Срочно", anemia: "Анемия", ok: "Анемии нет" };
  function reportTone(r) { return (r.level1.urgent && r.level1.urgent.length) ? "urgent" : (r.level1.anemia ? "anemia" : "ok"); }
  // Метка у вывода — только если вывод не начинается с того же слова (иначе «СРОЧНО / Срочно. …»).
  function toneTag(tone, headline) {
    var w = TONE_WORD[tone];
    return String(headline || "").toLowerCase().indexOf(w.toLowerCase()) === 0 ? null : w;
  }
  // Текст сервера «блок «Где сдать рядом» на странице сервиса» в памятке и в выгрузке читается вне страницы:
  // говорим «в сервисе Justmedit» (сам текст — config/texts_ru, его правит поток текстов).
  function fixText(t) { return String(t === null || t === undefined ? "" : t).replace(/на странице сервиса/g, "в сервисе Justmedit"); }
  function priceBlocks() {
    var reg = currentRegion(), tests = (state.result && state.result.next_tests) || [];
    if (!reg || !tests.length) { return []; }
    var b = [{ t: "h2", text: "Средняя цена в регионе «" + reg.name_ru + "»" }], total = 0;
    tests.forEach(function (t) {
      var p = priceOf(t.analyte);
      if (p.rub !== undefined) { total += p.rub; }
      b.push({ t: "li", text: t.name_ru + " — " + (p.rub !== undefined ? "≈ " + nf0.format(p.rub) + " ₽" : p.text) });
    });
    var bd = (state.ref.prices || {}).blood_draw;
    b.push({ t: "p", text: "Всего за анализы ≈ " + nf0.format(total) + " ₽" + (bd ? " + взятие крови " + bd.min + "–" + bd.max + " ₽" : "") + "." });
    if (reg.note) { b.push({ t: "small", text: cap(reg.note) + "." }); }
    return b;
  }
  function nearBlocks() {
    if (!near.top || !near.top.length) { return []; }
    var b = [{ t: "h2", text: "Где сдать рядом" }];
    near.top.forEach(function (t, n) {
      var l = near.labs[t.i];
      b.push({ t: "li", text: (n + 1) + ". " + (l[2] || l[3]) + (l[4] ? ", " + l[4] : "") + " — " + fmtDist(t.d) + (l[5] ? "; часы: " + hoursRu(l[5]) : "") +
        "; на карте: " + l[0].toFixed(5) + ", " + l[1].toFixed(5) });
    });
    b.push({ t: "small", text: "Пункты сетей лабораторий по данным OpenStreetMap (© участники OpenStreetMap, ODbL); расстояние — по прямой." });
    return b;
  }
  function reportModel(tab) {
    var r = state.result, rep = r.reports[tab];
    var title = tab === "doctor" ? "Отчёт врачу" : "Памятка пациенту";
    var tone = reportTone(r);
    // Шапка: тёмная полоса с брендом, справа — тип отчёта и дата; строка «пациент»; вывод с цветной полосой по статусу.
    var b = [{ t: "brand", kind: title, date: today() }, { t: "meta", text: "Пациент: " + personLine() },
      { t: "lead", text: rep.headline, tone: tone, tag: tab === "doctor" ? toneTag(tone, rep.headline) : null }];   // пациенту — без метки-диагноза
    var vals = (r.values || []).filter(function (v) { return v.value !== null && v.value !== undefined; });
    // Ячейка статуса окрашивается: ниже — янтарный, выше — розовый, норма — мятный (export.js → TONE_FILL).
    var cellTone = function (v, text) { return { text: text, tone: (v.norm && v.norm.status) || "unknown" }; };
    if (vals.length) {
      b.push({ t: "h2", text: tab === "doctor" ? "Показатели" : "Ваши показатели" });
      if (tab === "doctor") {
        b.push({ t: "table", head: ["Показатель", "Значение", "Статус", "Порог и источник"], widths: [2500, 1500, 1500, 4138],
          rows: vals.map(function (v) { return [v.name_ru, num(v.value) + " " + (v.unit || ""), cellTone(v, (STATUS[v.norm.status] || [v.norm.status])[0]), v.norm.threshold_text || ""]; }) });
      } else {
        b.push({ t: "table", head: ["Показатель", "Значение", "Оценка"], widths: [4200, 2400, 3038],
          rows: vals.map(function (v) { return [v.name_ru, num(v.value) + " " + (v.unit || ""), cellTone(v, STATUS_WORD[v.norm && v.norm.status] || "—")]; }) });
      }
    }
    (rep.sections || []).forEach(function (s) {
      b.push({ t: "h2", text: s.title });
      (s.lines || []).forEach(function (line) { b.push({ t: (s.lines.length > 1 ? "li" : "p"), text: fixText(line) }); });
    });
    b = b.concat(priceBlocks(), nearBlocks());
    // оговорка «прототип, не медицинское изделие» уже есть в разделах отчёта (ограничения / возможна ошибка)
    b.push({ t: "small", text: "Справочная информация, не медицинское заключение. Создано сервисом Justmedit; данные не сохранялись." });
    // Нижний колонтитул на каждой странице: оговорка (из справочника) и номер страницы.
    return { title: "Justmedit — " + title.toLowerCase(), blocks: b, footer: state.ref.disclaimer };
  }
  function fileName(tab, ext) {
    var d = new Date(), pad = function (x) { return (x < 10 ? "0" : "") + x; };
    return "justmedit-" + (tab === "doctor" ? "otchet-vrachu" : "pamyatka-pacientu") + "-" + d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "." + ext;
  }
  function downloadDocx() {
    if (!state.result || !window.JMExport) { return; }
    var tab = activeTab();
    window.JMExport.download(window.JMExport.docx(reportModel(tab)), fileName(tab, "docx"));
  }
  // Шапка печати — как в DOCX: тёмная полоса, «Justmed» + «it» мятным, справа тип отчёта и дата; ниже — пациент.
  function printHead(kind) {
    var box = $("print-head");
    clear(box);
    box.appendChild(el("div", { class: "print-head__bar" }, [
      el("span", { class: "print-head__brand" }, ["Justmed", el("span", { class: "brand__it", text: "it" })]),
      el("span", { class: "print-head__kind" }, [el("b", { text: kind }), el("span", { text: today() })])
    ]));
    box.appendChild(el("p", { class: "print-head__who", text: "Пациент: " + personLine() }));
  }
  function downloadPdf() {
    if (!state.result) { return; }
    var tab = activeTab();
    var title = document.title, opened = [];
    // Свёрнутое (разделы памятки, «Подробнее», «ещё N», «Где сдать рядом») печатается раскрытым; после печати — как было.
    Array.prototype.forEach.call(document.querySelectorAll("#panel-" + tab + " details:not([open]), #nearby:not([open]), #nearby details:not([open])"),
      function (d) { d.open = true; opened.push(d); });
    printHead(tab === "doctor" ? "Отчёт врачу" : "Памятка пациенту");
    document.title = fileName(tab, "pdf").replace(/\.pdf$/, "");      // имя файла по умолчанию в «Сохранить как PDF»
    document.body.classList.add("printing");
    var done = function () {
      document.title = title; document.body.classList.remove("printing");
      opened.forEach(function (d) { d.open = false; });
      window.removeEventListener("afterprint", done);
    };
    window.addEventListener("afterprint", done);
    window.print();
    setTimeout(function () { if (document.body.classList.contains("printing") && !window.matchMedia("print").matches) { done(); } }, 1500);
  }

  // ---------- вкладки ----------
  function selectTab(name, focus) {
    ["doctor", "patient"].forEach(function (n) {
      var on = n === name, tab = $("tab-" + n);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      tab.tabIndex = on ? 0 : -1;
      $("panel-" + n).hidden = !on;
      if (on && focus) { tab.focus(); }
    });
  }
  function initTabs() {
    ["doctor", "patient"].forEach(function (n) {
      $("tab-" + n).addEventListener("click", function () { selectTab(n, false); });
      $("tab-" + n).addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); selectTab(n === "doctor" ? "patient" : "doctor", true); }
        else if (e.key === "Home" || e.key === "End") { e.preventDefault(); selectTab(e.key === "Home" ? "doctor" : "patient", true); }
      });
    });
  }

  // ---------- текст бланка ----------
  function runPaste() {
    var out = $("paste-out");
    clear(out);
    var text = $("paste-text").value;
    if (!text.trim()) { out.appendChild(el("p", { class: "field__error", text: "вставьте текст бланка" })); return; }
    $("paste-run").disabled = true;
    request("POST", "/ui/parse", { text: text }).then(function (r) {
      if (r.ok) { state.source = "file"; }
      showParsed(out, r);
    }).catch(function () {
      out.appendChild(el("p", { class: "field__error", text: "нет связи с сервисом" }));
    }).then(function () { $("paste-run").disabled = false; });
  }

  // Файл бланка (фото, PDF с текстовым слоем, TXT, CSV или XLSX) -> тот же ответ, что и для вставленного текста.
  // Фото распознаётся на сервере (Tesseract); расчёт сам не запускается — только заполняется форма для сверки.
  // Анимация «сканирования» на время распознавания: миниатюра фото (data:-URL — CSP разрешает только data: и self),
  // бегущая линия, сетка, угловые рамки и шаги. Для PDF/TXT/CSV/XLSX — силуэт документа. Файл никуда не сохраняется.
  var SCAN_STEPS = ["Выравниваем снимок", "Читаем строки", "Ищем показатели", "Сверяем единицы"];
  var SCAN_STEPS_DOC = ["Открываем файл", "Читаем строки", "Ищем показатели", "Сверяем единицы"];   // PDF, TXT, CSV, XLSX
  function startScan(out, file) {
    var isImg = /^image\//.test(file.type || "");
    var steps = isImg ? SCAN_STEPS : SCAN_STEPS_DOC;
    var what = isImg ? "Распознаём фото" : (/\.pdf$/i.test(file.name || "") || file.type === "application/pdf" ? "Читаем PDF" : "Читаем файл");
    var frame = el("div", { class: "scan" + (isImg ? "" : " scan--doc"), role: "status", "aria-live": "polite" }, [
      isImg ? el("img", { class: "scan__img", alt: "" }) : el("span", { class: "scan__doc", "aria-hidden": "true" }),
      el("span", { class: "scan__grid", "aria-hidden": "true" }),
      el("span", { class: "scan__line", "aria-hidden": "true" }),
      el("span", { class: "scan__corner scan__corner--tl", "aria-hidden": "true" }),
      el("span", { class: "scan__corner scan__corner--tr", "aria-hidden": "true" }),
      el("span", { class: "scan__corner scan__corner--bl", "aria-hidden": "true" }),
      el("span", { class: "scan__corner scan__corner--br", "aria-hidden": "true" })
    ]);
    var status = el("p", { class: "scan__status", text: what + " · " + steps[0] });
    out.appendChild(el("div", { class: "scan-wrap" }, [frame, status]));
    if (isImg && window.FileReader) {
      var fr = new FileReader();
      fr.onload = function () { var img = frame.querySelector(".scan__img"); if (img) { img.src = fr.result; } };
      fr.readAsDataURL(file);
    }
    var i = 0;
    var timer = setInterval(function () {
      i = (i + 1) % steps.length;
      status.textContent = what + " · " + steps[i];
    }, 900);
    return { stop: function () { clearInterval(timer); } };
  }
  function smooth() { return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"; }
  function hasFiles(e) { return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0); }
  function revealPaste() {
    var r = $("paste-out").getBoundingClientRect();
    if (r.top < 60 || r.top > window.innerHeight - 120) { $("paste").scrollIntoView({ behavior: smooth(), block: "start" }); }
  }
  function runPasteFile(e, inputId) {
    var out = $("paste-out");
    if (state.result) { setEditing(true); }   // кнопки полосы при показанном результате: форма — над результатом
    clear(out);
    var file = $(inputId || "paste-file").files[0];
    if (!file) { out.appendChild(el("p", { class: "field__error", text: "выберите фото бланка или файл PDF, TXT, CSV, XLSX" })); return; }
    var fd = new FormData();
    fd.append("file", file, file.name || "photo.jpg");
    // На время распознавания — без второго файла: поля файлов и кнопки полосы выключены.
    var busy = ["paste-photo", "paste-pdf", "paste-file", "hero-photo", "hero-pdf"];
    busy.forEach(function (id) { $(id).disabled = true; });
    $("paste").classList.add("is-busy");
    var scan = startScan(out, file);
    var t0 = Date.now();
    var minShow = function () { return new Promise(function (res) { setTimeout(res, Math.max(0, 1200 - (Date.now() - t0))); }); };
    fetch("/ui/parse", { method: "POST", body: fd, credentials: "omit", cache: "no-store",
                         headers: { "Accept": "application/json" } }).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        return minShow().then(function () {      // анимация видна хотя бы 1,2 с, даже если сервер ответил быстрее
          scan.stop(); clear(out);
          if (resp.ok) {
            state.source = /^image\//.test(file.type || "") ? "photo" : (/\.pdf$/i.test(file.name || "") || file.type === "application/pdf" ? "pdf" : "file");
          }
          showParsed(out, { status: resp.status, ok: resp.ok, data: data });
        });
      });
    }).catch(function () {
      scan.stop(); clear(out);
      out.appendChild(el("p", { class: "field__error", text: "нет связи с сервисом" }));
    }).then(function () {
      busy.forEach(function (id) { $(id).disabled = false; });
      $("paste").classList.remove("is-busy");
      ["paste-photo", "paste-pdf", "paste-file"].forEach(function (id) { $(id).value = ""; });
    });
  }

  // Пометка «проверьте значение» у поля, значение которого распознано с фото неуверенно.
  function markCheck(code) {
    var input = $("v-" + code);
    if (!input) { return; }
    var f = input.closest(".f");
    f.classList.add("f--check");
    if (!f.querySelector(".f__check")) {
      var id = "chk-" + code;
      f.appendChild(el("p", { class: "f__check", id: id, text: "проверьте значение: распознано с фото неуверенно" }));
      input.setAttribute("aria-describedby", (input.getAttribute("aria-describedby") || "") + " " + id);
    }
  }
  function clearChecks() {
    Array.prototype.forEach.call(document.querySelectorAll("#fields .f--check"), function (f) {
      f.classList.remove("f--check");
      var p = f.querySelector(".f__check");
      if (p) {
        var input = f.querySelector("input[data-code]");
        input.setAttribute("aria-describedby", input.getAttribute("aria-describedby").replace(" " + p.id, ""));
        f.removeChild(p);
      }
    });
  }

  function showParsed(out, r) {
    clearChecks();
    if (!r.ok || !r.data) { out.appendChild(el("p", { class: "field__error", text: errorText(r) })); return; }
    // Новый бланк заменяет форму целиком: значения и единицы прежнего ввода (или примера) не смешиваем с ним.
    // Пол, возраст и беременность из примера — не этого человека: возраст очищаем, просим указать.
    var fromExample = !!state.example;
    inputs().forEach(function (i) { i.value = ""; setUnit(i.getAttribute("data-code"), null); clearFieldError(i); });
    Array.prototype.forEach.call(document.querySelectorAll("#fields details.group"), function (d) { d.open = false; });
    state.example = null; state.followupDone = false;
    markExample(null);
    clear($("followup")); $("followup").hidden = true;
    if (fromExample) {
      $("age").value = ""; $("pregnancy").value = "no"; $("trimester").value = "";
      syncPregnancy();
      $("age-hint").textContent = "Укажите пол и возраст: прежние были из примера.";
      $("age-hint").hidden = false;
    }
    var codes = Object.keys(r.data.values || {}), filled = [];
    codes.forEach(function (code) {
      var v = r.data.values[code];
      if (setValue(code, v.value, v.unit)) { filled.push(state.byCode[code].short_ru); }
    });
    (r.data.check || []).forEach(markCheck);
    updateCounts();
    if (filled.length) { $("manual").open = true; }      // «проверьте значения» — значит, поля должны быть видны
    // Дата анализа с бланка (date, ISO) — в сводку результата и шапку отчёта; нет даты — сбрасываем прежнюю.
    state.analysisDate = ruDate(r.data.date) ? r.data.date : null;
    out.appendChild(el("p", null, [el("strong", { text: "Распознано показателей: " + filled.length + ". " }),
      filled.length ? filled.join(", ") + ". Проверьте значения и единицы, затем нажмите «Рассчитать»." : ""]));
    if (fromExample) { out.appendChild(el("p", { class: "warn-text", text: "Пол, возраст и беременность были из примера — укажите свои вверху формы." })); }
    if (state.analysisDate) { out.appendChild(el("p", { text: "Дата анализа: " + ruDate(state.analysisDate) + "." })); }
    // Референсы с бланка: прежние (от другого бланка) снимаем, новые применяем сразу — галочка включена.
    unapplyBlankRefs();
    var refs = pickBlankRefs(r.data.reference_ranges);
    if (refs) {
      state.blankRefs = { ranges: refs, on: false };
      applyBlankRefs(true);
      out.appendChild(blankRefsNode());
    }
    if (r.data.warnings && r.data.warnings.length) {
      out.appendChild(el("p", { class: "warn-text", text: "Проверьте значения — распознаны неуверенно:" }));
      out.appendChild(el("ul", null, r.data.warnings.map(function (n) { return el("li", { class: "warn-text", text: n }); })));
    }
    // Длинные списки — под раскрытие: замечания, нераспознанные строки, «распознано, но не используется».
    parsedList(out, "Замечания", r.data.notes, 3);
    parsedList(out, "Не распознаны строки", r.data.unrecognized, 3);
    parsedList(out, "Распознано, но сервисом не используется", r.data.ignored, 0);
    if (fromExample) { $("age").focus({ preventScroll: true }); }
  }
  function parsedList(out, title, items, keep) {
    if (!items || !items.length) { return; }
    var ul = el("ul", null, items.map(function (n) { return el("li", { text: n }); }));
    if (items.length <= keep) {
      out.appendChild(el("p", { text: title + ":" }));
      out.appendChild(ul);
      return;
    }
    out.appendChild(el("details", { class: "fold fold--list" }, [el("summary", null, [title + " ", countBadge(items.length, "строка", "строки", "строк")]), ul]));
  }

  // ---------- проверка API ----------
  function apiCheck(withKey) {
    var out = $("api-out");
    var payload = collect(true), note = "";
    if (!payload || payload.values.hemoglobin === undefined) {
      if (!state.examples.length) { return; }
      payload = state.examples[0].input;
      note = " (форма пуста — отправлен пример 1)";
    }
    var headers = {};
    if (withKey) {
      var key = $("api-key").value.trim();
      if (!key) { $("api-key").focus(); addApiLine("С ключом", "—", "введите ключ API в поле слева", "chip--none"); return; }
      headers["X-API-Key"] = key;
    }
    var t0 = performance.now();
    request("POST", "/api/v1/analyze", payload, headers).then(function (r) {
      var ms = Math.round(performance.now() - t0);
      var text;
      if (r.ok && r.data) {
        text = "ответ за " + ms + " мс; анемия по ВОЗ: " + (r.data.level1.anemia ? "есть" : "нет") + note;
      } else {
        text = errorText(r) + " (" + ms + " мс)";
      }
      addApiLine(withKey ? "С ключом" : "Без ключа", String(r.status), text, r.ok ? "chip--ok" : (r.status === 401 || r.status === 403 ? "chip--warn chip--bang" : "chip--danger chip--bang"));
    }).catch(function () { addApiLine(withKey ? "С ключом" : "Без ключа", "—", "нет связи с сервисом", "chip--danger chip--bang"); });

    function addApiLine(kind, code, text, cls) {
      var li = el("li", null, [el("strong", { text: kind + ":" }), chip("код " + code, cls), el("span", { text: text })]);
      out.insertBefore(li, out.firstChild);
      while (out.children.length > 4) { out.removeChild(out.lastChild); }
    }
  }

  // ---------- настройки для врача: порог ферритина и точка направления ----------
  // Порог ферритина: числа и источники — из справочника (norms_ru.yaml → ferritin: who 15 — ВОЗ 2020,
  // ru 30 — КР «Железодефицитная анемия» 2024), в коде страницы их нет.
  function buildNormsCards() {
    var fer = (state.ref.thresholds || []).filter(function (t) { return t.key === "ferritin"; })[0];
    if (!fer || !fer.values || !fer.values.deficiency_below) { return; }
    var d = fer.values.deficiency_below, unit = fer.unit_ru || "";
    var src = function (role) { return ((fer.sources || []).filter(function (x) { return x.role === role; })[0] || {}).title || ""; };
    $("norms-who").textContent = "ВОЗ — " + num(d.who) + " " + unit;
    $("norms-ru").textContent = "Практика РФ — " + num(d.ru) + " " + unit;
    $("norms-who-src").textContent = src("who");
    $("norms-ru-src").textContent = src("ru");
    var infl = fer.values.inflammation_below;
    $("norms-hint").textContent = "Ферритин ниже порога считается дефицитом железа." +
      (infl ? " При воспалении — ниже " + num(infl) + " " + unit + " (" + src("inflammation").split(":")[0] + ")." : "");
  }
  // Точка направления на ферритин (скрининг по ОАК без анемии): три карточки 20/30/40 %. Что даёт каждая на 1000
  // человек — /ui/reference → screening.referral (docs/metrics/screen_metrics.json, тест NHANES 2017–2023).
  // Значение для запроса хранит скрытый select#refer-share (collect читает его, как раньше).
  function ordinalEach(ppv) { return ppv > 0 ? "~" + Math.round(1 / ppv) + "-е" : "—"; }
  function buildReferCards() {
    var scr = state.ref.screening || {}, meta = scr.referral_meta || {};
    if (scr.refer_share) { $("refer-share").value = String(scr.refer_share); }    // по умолчанию — screen_thresholds.yaml
    var rows = {};
    (scr.referral || []).forEach(function (x) { rows[String(x.refer_share)] = x; });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="refer_card"]'), function (i) {
      var x = rows[i.value], d = document.querySelector('[data-refer="' + i.value + '"]');
      i.checked = i.value === $("refer-share").value;
      clear(d);
      if (String(scr.refer_share) === i.value) { i.parentNode.querySelector(".opt-card__t").appendChild(el("span", { class: "opt-card__tag", text: "по умолчанию" })); }
      if (!x) { return; }
      d.appendChild(document.createTextNode("Из 1000 " + (meta.group_ru || "") + " направим " + x.referred_per1000 + ", найдём " +
        x.found_per1000 + " из " + x.cases_per1000 + " с ферритином < " + num(meta.ferritin_below) + " " + ((state.byCode.ferritin || {}).unit_ru || "") + " (чувствительность " + pct(x.sensitivity) +
        "); подтвердится каждое " + ordinalEach(x.ppv) + " направление."));
    });
    $("refer-src").textContent = meta.validation ? "Проверка модели скрининга на данных NHANES (США): " + meta.validation + "." : "";
  }

  // ---------- запуск ----------
  function start() {
    initTabs();
    Promise.all([request("GET", "/ui/reference"), request("GET", "/ui/examples")]).then(function (rs) {
      if (!rs[0].ok || !rs[0].data) { showAlert(errorText(rs[0])); $("fields-loading").textContent = "Не удалось загрузить список показателей. Обновите страницу."; return; }
      state.ref = rs[0].data;
      state.ref.analytes.forEach(function (a) { state.byCode[a.code] = a; });
      state.examples = (rs[1].ok && rs[1].data && rs[1].data.examples) || [];
      buildFields();
      buildLabRef();
      buildExamples();
      buildNormsCards();
      buildReferCards();
      document.body.setAttribute("data-ready", "1");
    }).catch(function () { showAlert("Нет связи с сервисом. Обновите страницу."); });

    $("form").addEventListener("submit", function (e) { e.preventDefault(); calculate(); });
    $("clear").addEventListener("click", function () { resetAll(); $("age").focus({ preventScroll: true }); });
    $("age").addEventListener("input", function () { $("age-hint").hidden = true; });
    // Строка-сводка результата: «Изменить данные» — форма над результатом (значения на месте), «Новый анализ» —
    // пустая форма без результата. Фокус — на возраст (или на первое поле с ошибкой), а не на скрытой кнопке.
    $("edit-data").addEventListener("click", function () {
      var on = !$("layout").classList.contains("is-editing");
      setEditing(on);
      scrollUnderTop(on ? $("form-panel") : $("result"));
      if (on) { (document.querySelector('#form [aria-invalid="true"]') || $("age")).focus({ preventScroll: true }); }
    });
    $("new-analysis").addEventListener("click", function () {
      resetAll();
      $("nearby").open = false;
      scrollUnderTop($("form-panel"));
      $("age").focus({ preventScroll: true });
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="sex"]'), function (i) { i.addEventListener("change", function () { syncPregnancy(); syncLabRef(); }); });
    // Свои границы референсов меняют оценку, как переключатель порога: есть результат — пересчитываем
    // (по завершении правки поля, а не на каждую цифру).
    $("labref-rows").addEventListener("change", function (e) {
      if (e.target && e.target.getAttribute("data-bound") && state.result) { calculate(true); }
    });
    $("labref-reset").addEventListener("click", function () {
      labrefCodes().forEach(function (c) { ["lr-lo-", "lr-hi-"].forEach(function (p) { $(p + c).value = ""; $(p + c).removeAttribute("aria-invalid"); }); });
      $("err-labref").hidden = true;
      labrefCount();
      if (state.result) { calculate(true); }
    });
    $("pregnancy").addEventListener("change", function () { syncPregnancy(); syncLabRef(); $("err-pregnancy").hidden = true; $("pregnancy").removeAttribute("aria-invalid"); });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="norms"]'), function (i) {
      i.addEventListener("change", function () { if (state.result) { calculate(true); } });   // пересчёт с другим порогом
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="refer_card"]'), function (i) {
      i.addEventListener("change", function () {
        $("refer-share").value = i.value;
        if (state.result) { calculate(true); }
      });
    });
    $("paste-run").addEventListener("click", runPaste);
    // Кнопки полосы «Сфотографировать бланк» и «Загрузить PDF» — настоящие <button> (в порядке Tab), открывают выбор файла.
    ["hero-photo", "hero-pdf"].forEach(function (id) {
      $(id).addEventListener("click", function () { $($(id).getAttribute("data-file")).click(); });
    });
    ["paste-photo", "paste-pdf", "paste-file"].forEach(function (id) {
      $(id).addEventListener("change", function (e) {
        if (!$(id).files.length) { return; }
        runPasteFile(e, id);
        revealPaste();                    // кнопка в верхней полосе — показываем, где идёт распознавание
      });
    });
    $("hero-manual").addEventListener("click", function (e) {
      e.preventDefault();
      if (state.result) { setEditing(true); }
      $("manual").open = true;
      $("manual").scrollIntoView({ behavior: smooth(), block: "start" });
      var first = document.querySelector("#fields input");
      if (first) { first.focus({ preventScroll: true }); }
    });
    // Перетаскивание файла бланка в зону загрузки (компьютер): тот же разбор, что у кнопки «Распознать файл».
    var drop = $("paste");
    ["dragenter", "dragover"].forEach(function (t) {
      drop.addEventListener(t, function (e) { if (hasFiles(e)) { e.preventDefault(); drop.classList.add("is-drag"); } });
    });
    ["dragleave", "drop"].forEach(function (t) {
      drop.addEventListener(t, function (e) { if (t === "drop" || !drop.contains(e.relatedTarget)) { drop.classList.remove("is-drag"); } });
    });
    drop.addEventListener("drop", function (e) {
      if (!hasFiles(e)) { return; }
      e.preventDefault();
      try { $("paste-file").files = e.dataTransfer.files; } catch (err) { return; }
      runPasteFile(e, "paste-file");
    });
    // Файл, брошенный мимо зоны, браузер открыл бы вместо страницы — не даём.
    document.addEventListener("dragover", function (e) { if (hasFiles(e)) { e.preventDefault(); } });
    document.addEventListener("drop", function (e) { if (hasFiles(e) && !drop.contains(e.target)) { e.preventDefault(); } });
    $("dl-docx").addEventListener("click", downloadDocx);
    $("dl-pdf").addEventListener("click", downloadPdf);
    $("nearby-geo").addEventListener("click", locate);
    $("nearby-map-btn").addEventListener("click", function () { openMap(); });
    // Карта, созданная в раскрытом блоке, после сворачивания и раскрытия пересчитывает свой размер.
    $("nearby").addEventListener("toggle", function () { if ($("nearby").open && near.map) { near.map.invalidateSize(); } });
    $("api-nokey").addEventListener("click", function () { apiCheck(false); });
    $("api-withkey").addEventListener("click", function () { apiCheck(true); });
    syncPregnancy();
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
})();
