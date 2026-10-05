/* Justmedit — страница «Проверить на своём файле».
   Шаги: 1) файл (кнопка или перетаскивание); 2) столбцы — /ui/columns возвращает заголовок, что сервис понял в каждом
   столбце, догадки для своих названий («HGB, г/дл») и проверку, хватает ли данных; пользователь правит сопоставление
   и единицы, предупреждения пересчитываются сразу; 3) режим и расчёт — /ui/benchmark с полем mapping.
   Числа метрик берутся только из ответа сервера (кросс-валидация — из docs/metrics/case_metrics.json, поле cv_metrics).
   Браузер ничего не хранит; содержимое файла вставляется в страницу только как текст. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var csvText = null;

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
  function card(title, children, cls) { return el("section", { class: "card" + (cls ? " " + cls : "") }, [el("h3", { text: title })].concat(children)); }

  var nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 3 });
  var nf3 = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  var nf1 = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  function share(x) { return (x === null || x === undefined) ? "—" : nf1.format(x * 100) + " %"; }
  function f1(x) { return (x === null || x === undefined) ? "—" : nf3.format(x); }
  function shortName(s) { return s ? s.split(" (")[0] : s; }
  var MODE = { clinical: "клинический", benchmark: "бенчмарк", auto: "авто" };
  // Превью результата — по-русски: названия столбцов файла результата и коды значений (в CSV остаются коды).
  var PREVIEW_HEAD = { patient_id: "Строка", anemia: "Анемия по ВОЗ", case_group: "Группа кейса", anemia_class: "Класс анемии",
    deficiency_cause: "Причина", confidence: "Уверенность модели", hidden_deficiency: "Скрытый дефицит", mode: "Режим",
    status: "Статус", reject_reason: "Почему отклонена" };
  var STATUS_RU = { done: "рассчитана", rejected: "отклонена" };
  var REJECT = {
    not_a_number: "значение не является числом", out_of_hard_range: "значение вне допустимого диапазона",
    age_missing: "возраст не указан", age_below_18: "возраст меньше 18 лет", age_above_120: "возраст больше 120 лет",
    sex_unrecognized: "пол не распознан", hemoglobin_missing: "гемоглобин не указан"
  };

  function showAlert(text) { var a = $("bench-alert"); a.textContent = text; a.hidden = !text; }
  function stat(value, label) { return el("div", { class: "stat" }, [el("b", { text: value }), el("span", { text: label })]); }
  function table(head, rows, cls) {
    return el("div", { class: "tbl-wrap" }, [el("table", { class: cls || "" }, [
      el("thead", null, [el("tr", null, head.map(function (h) { return el("th", { scope: "col", class: h.num ? "num" : null, text: h.text }); }))]),
      el("tbody", null, rows)
    ])]);
  }

  function metricsRows(m, level1) {
    var rows = [];
    function row(name, s) {
      if (!s) { return; }
      rows.push(el("tr", null, [el("th", { scope: "row", text: name }), el("td", { class: "num", text: nf.format(s.n) }),
        el("td", { class: "num", text: share(s.accuracy) }), el("td", { class: "num", text: f1(s.macro_f1) })]));
    }
    row("Уровень 1: анемия по ВОЗ", level1);
    row("5 групп кейса", m.groups5);
    row("5 групп, только строки с анемией", m.groups5 && m.groups5.anemic_rows);
    row("12 классов", m.classes12);
    row("Причина", m.cause);
    return rows;
  }
  var METRIC_HEAD = [{ text: "Что сравнивается" }, { text: "Строк", num: true }, { text: "Точность", num: true }, { text: "Macro-F1", num: true }];

  function cvBanner(cv) {
    var kids = [el("h3", { text: "На этом файле модель училась: цифры на экране завышены" })];
    if (!cv) {
      kids.push(el("p", { text: "Честная оценка — кросс-валидация; файл с её результатами (docs/metrics/case_metrics.json) не найден." }));
      return el("div", { class: "banner", role: "note" }, kids);
    }
    kids.push(el("p", { text: "Честная оценка — кросс-валидация: " + (cv.scheme || "") + "; строк: " + nf.format(cv.n_rows) + ". Среднее по тестовым фолдам ± стандартное отклонение." }));
    var rows = [];
    ["benchmark", "clinical"].forEach(function (mode) {
      var m = cv.modes[mode];
      if (!m) { return; }
      function cell(s) { return el("td", { class: "num", text: share(s.accuracy) + " ± " + nf1.format(s.accuracy_sd * 100) + " · F1 " + f1(s.macro_f1) }); }
      rows.push(el("tr", null, [el("th", { scope: "row", text: "Режим: " + MODE[mode] }), cell(m.classes12), cell(m.groups5), cell(m.cause)]));
    });
    kids.push(table([{ text: "Кросс-валидация" }, { text: "12 классов", num: true }, { text: "5 групп", num: true }, { text: "Причина", num: true }], rows));
    kids.push(el("p", { class: "source", text: "Источник чисел: " + cv.source + "." }));
    return el("div", { class: "banner", role: "note" }, kids);
  }

  function confusion(conf, names) {
    var labels = conf.labels.map(function (c) { return shortName(names[c] || c); });
    var head = [{ text: "Истина ↓ / прогноз →" }].concat(labels.map(function (l) { return { text: l, num: true }; }));
    var rows = conf.matrix.map(function (r, i) {
      return el("tr", null, [el("th", { scope: "row", text: labels[i] })].concat(r.map(function (v, j) {
        return el("td", { class: "num" + (i === j ? " diag" : (v ? " miss" : "")), text: nf.format(v) });
      })));
    });
    return table(head, rows, "confusion");
  }


  // ---------- итог расчёта ----------
  function tile(value, label, kind) {
    return el("div", { class: "tile tile--" + (kind || "none") }, [el("span", { class: "tile__k", text: label }), el("b", { class: "tile__v", text: value })]);
  }
  function reqCard(req) {
    if (!req) { return null; }
    var items = [];
    (req.errors || []).forEach(function (t) { items.push(el("li", { class: "req__item req__item--error", text: t })); });
    (req.warnings || []).forEach(function (t) { items.push(el("li", { class: "req__item req__item--warn", text: t })); });
    if (!items.length) { return null; }
    return card("Чего не хватало для анализа", [el("ul", { class: "req__list" }, items)], "card--req");
  }
  function codeName(code) { var t = state.targets[code]; return t ? (t.short_ru && t.short_ru !== t.name_ru ? t.name_ru + " (" + t.short_ru + ")" : t.name_ru) : code; }
  function yesNo(v) { return v === 1 || v === "1" || v === true ? "да" : v === 0 || v === "0" || v === false ? "нет" : v; }
  // Ячейка превью: коды классов, групп и причин — словами из labels_ru ответа; «hemoglobin: …» — названием показателя.
  function previewCell(col, v, labels) {
    if (v === null || v === undefined || v === "") { return ""; }
    if (col === "case_group") { return (labels.groups5 || {})[v] || v; }
    if (col === "anemia_class") { return (labels.classes12 || {})[v] || v; }
    if (col === "deficiency_cause") { return (labels.causes11 || {})[v] || v; }
    if (col === "anemia" || col === "hidden_deficiency") { return String(yesNo(v)); }
    if (col === "mode") { return MODE[v] || v; }
    if (col === "status") { return STATUS_RU[v] || v; }
    if (col === "confidence" && typeof v === "number") { return share(v); }
    if (col === "reject_reason") {
      return String(v).replace(/(^|;\s*)([A-Za-z_0-9]+):/g, function (m, pre, code) { return pre + (state.targets[code] ? codeName(code) : code) + ":"; });
    }
    return typeof v === "number" ? nf.format(v) : String(v);
  }
  function render(data) {
    var out = $("bench-out");
    clear(out);
    var s = data.summary, labelsRu = data.labels_ru || {}, names = labelsRu.groups5 || {};
    csvText = data.csv;

    if (s.metrics && s.metrics.same_as_training_data) { out.appendChild(cvBanner(data.cv_metrics)); }

    var modeText = MODE[s.mode] || s.mode;
    var dl = el("button", { type: "button", class: "btn btn--dl", text: "Скачать CSV" });
    dl.addEventListener("click", download);
    var head = el("div", { class: "bench-result__head" }, [
      el("div", null, [el("h2", { class: "bench-result__title", text: "Результат" }),
        el("p", { class: "hint", text: "Режим: " + modeText + ". " + ((s.auto && s.auto.reason) ? "Почему: " + s.auto.reason + "." : "Режим задан явно.") })]),
      dl
    ]);
    var predicted = s.predicted || {};
    var tiles = el("div", { class: "tiles tiles--4" }, [
      tile(nf.format(s.rows_total), "строк в файле", "info"),
      tile(nf.format(s.rows_done), "рассчитано", "ok"),
      tile(nf.format(s.rows_rejected), "отклонено", s.rows_rejected ? "warn" : "ok"),
      predicted.anemia !== undefined ? tile(nf.format(predicted.anemia), "анемия по ВОЗ", predicted.anemia ? "danger" : "ok") : null,
      predicted.hidden_deficiency !== undefined ? tile(nf.format(predicted.hidden_deficiency), "скрытый дефицит без анемии", predicted.hidden_deficiency ? "warn" : "ok") : null,
      tile(nf.format(s.seconds) + " с", "время обработки", "none")
    ]);
    out.appendChild(el("section", { class: "card bench-result" }, [head, tiles]));
    var rq = reqCard(s.requirements);
    if (rq) { out.appendChild(rq); }

    var rejected = Object.keys(s.reject_reasons || {});
    if (rejected.length) {
      out.appendChild(card("Почему строки отклонены", [el("ul", { class: "list" }, rejected.map(function (k) {
        return el("li", { text: (REJECT[k] || k) + " — " + nf.format(s.reject_reasons[k]) });
      })), el("p", { class: "source", text: "Значения не исправляются: причина по каждой строке — в столбце reject_reason файла результата." })]));
    }

    var m = s.metrics;
    if (m && m.by_mode && m.by_mode[s.mode]) {
      var chosen = m.by_mode[s.mode];
      var left = card("Метрики по меткам файла · режим: " + modeText, [table(METRIC_HEAD, metricsRows(chosen, m.level1))]);
      var otherMode = s.mode === "benchmark" ? "clinical" : "benchmark";
      if (m.by_mode[otherMode]) {
        left.appendChild(el("details", { class: "more" }, [
          el("summary", { text: "Те же метрики в режиме «" + MODE[otherMode] + "»" }),
          table(METRIC_HEAD, metricsRows(m.by_mode[otherMode], null))
        ]));
      }
      var grid = el("div", { class: "grid2" }, [left]);
      if (chosen.groups5 && chosen.groups5.confusion) {
        grid.appendChild(card("Матрица ошибок: 5 групп кейса", [
          confusion(chosen.groups5.confusion, names),
          el("p", { class: "source", text: "Строки — группа по меткам файла, столбцы — прогноз. На диагонали — совпадения." })
        ]));
      }
      out.appendChild(grid);
    }

    if (data.preview && data.preview.length) {
      var cols = data.columns || Object.keys(data.preview[0]);
      out.appendChild(card("Первые строки результата (" + data.preview.length + ")", [table(
        cols.map(function (c) { return { text: PREVIEW_HEAD[c] || c }; }),
        data.preview.map(function (r) {
          return el("tr", null, cols.map(function (c) { return el("td", { text: previewCell(c, r[c], labelsRu) }); }));
        }), "preview"),
        el("p", { class: "source", text: "В скачанном CSV — те же столбцы с кодами (patient_id, anemia_class, …) для обработки программой." })]));
    }

    var notes = (s.notes || []).slice();
    var c = s.columns || {};
    if (c.ignored_service && c.ignored_service.length) { notes.push("Служебные столбцы и столбцы-ответы в модель не попадают: " + c.ignored_service.join(", ") + "."); }
    if (c.analytes_absent && c.analytes_absent.length) { notes.push("Показателей нет в файле: " + c.analytes_absent.map(function (k) { var t = state.targets[k]; return t ? (t.short_ru || t.name_ru) : k; }).join(", ") + "."); }
    if (!(m && m.by_mode)) { notes.push("В файле нет столбцов с метками (anemia, anemia_class, deficiency_cause): метрики не считаются, предсказания — в таблице и в CSV."); }
    if (notes.length) { out.appendChild(card("Замечания", [el("ul", { class: "list" }, notes.map(function (n) { return el("li", { text: n }); }))])); }
    out.scrollIntoView({ behavior: smooth(), block: "start" });
  }

  function download() {
    if (csvText === null) { return; }
    var url = URL.createObjectURL(new Blob(["﻿" + csvText], { type: "text/csv;charset=utf-8" }));
    var a = el("a", { href: url, download: "justmedit_pred.csv" });
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }
  function smooth() { return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"; }

  // ---------- шаги 1–2: файл и столбцы ----------
  var state = { file: null, cols: null, rows: 0, targets: {}, groups: {} };
  var unitNames = {};          // код показателя -> [{unit (по-русски), factor}] из /ui/reference
  var refReady = Promise.resolve();
  var KIND = { feature: ["распознан", "chip--ok"], label: ["распознан", "chip--ok"], guess: ["предложено — проверьте", "chip--warn chip--q"],
               unknown: ["не распознан", "chip--none"], service: ["служебный", "chip--none"], duplicate: ["повтор", "chip--warn"] };
  var REQUIRED = [["sex", "пол"], ["age_years", "возраст"], ["hemoglobin", "гемоглобин"]];
  var CBC_CORE = ["MCV", "MCH", "RDW", "RBC"];
  function steps(n) {
    [1, 2, 3].forEach(function (i) { $("bs-" + i).className = i < n ? "is-done" : (i === n ? "is-on" : ""); });
  }
  function fmtSize(b) { return b < 1024 * 1024 ? Math.max(1, Math.round(b / 1024)) + " КБ" : nf1.format(b / 1048576) + " МБ"; }
  function setFile(file) {
    if (!file) { return; }
    state.file = file;
    var name = $("bench-file-name");
    clear(name);
    name.appendChild(el("span", { class: "bench-file__name", text: file.name }));
    name.appendChild(el("span", { class: "bench-file__size", text: fmtSize(file.size) }));
    name.hidden = false;
    $("bench-out").textContent = "";
    loadColumns();
  }
  function loadColumns() {
    showAlert("");
    $("step-cols").hidden = true; $("step-run").hidden = true;
    var fd = new FormData();
    fd.append("file", state.file, state.file.name);
    $("bench-file-label").textContent = "Читаю файл…";
    fetch("/ui/columns", { method: "POST", body: fd, credentials: "omit", cache: "no-store" }).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = JSON.parse(text); } catch (err) { data = null; }
        if (resp.ok && data) { return refReady.then(function () { buildMapping(data); }); }   // подписи единиц — из справочника
        steps(1);
        showAlert((data && data.error && data.error.message) || (resp.status === 413 ? "Файл слишком большой." :
          resp.status === 429 ? "Слишком много запросов. Повторите через минуту." : "Сервис не ответил (код " + resp.status + ")."));
      });
    }).catch(function () { showAlert("Нет связи с сервисом. Повторите."); })
      .then(function () { $("bench-file-label").textContent = "Выбрать другой файл"; });
  }
  function targetSelect(row) {
    var sel = el("select", { "data-col": row.column, "aria-label": "Что означает столбец «" + row.column + "»" });
    sel.appendChild(el("option", { value: "ignore", text: "— не использовать —" }));
    var groups = [["person", "Пациент"], ["cbc", "Общий анализ крови"], ["labs", "Анализы вне ОАК"], ["label", "Метки и идентификатор"]];
    groups.forEach(function (g) {
      var og = el("optgroup", { label: g[1] });
      (state.groups[g[0]] || []).forEach(function (t) {
        og.appendChild(el("option", { value: t.code, text: t.name_ru + (t.short_ru && t.short_ru !== t.name_ru ? " (" + t.short_ru + ")" : "") }));
      });
      sel.appendChild(og);
    });
    sel.value = (row.target && state.targets[row.target]) ? row.target : "ignore";
    return sel;
  }
  function unitSelect(code, chosen) {
    var t = state.targets[code], sel = el("select", { class: "unit-sel", "aria-label": "Единица" });
    if (!t || !t.units || t.units.length < 2) {
      sel.appendChild(el("option", { value: "", text: t && t.unit_ru ? t.unit_ru : "—" }));
      sel.disabled = true;
      return sel;
    }
    t.units.forEach(function (u) {
      sel.appendChild(el("option", { value: u.factor === 1 ? "" : u.unit,
        text: u.factor === 1 ? (t.unit_ru || u.unit) + " — как в сервисе" : unitRu(code, u) + " → ×" + nf.format(u.factor) }));
    });
    sel.value = chosen && t.units.some(function (u) { return u.unit === chosen && u.factor !== 1; }) ? chosen : "";
    return sel;
  }
  // Название единицы по-русски: /ui/reference → analytes[].unit_choices (по одной на множитель); нет — как в ответе.
  function unitRu(code, u) {
    var ch = (unitNames[code] || []).filter(function (c) { return Math.abs(c.factor - u.factor) < 1e-9; })[0];
    return ch ? ch.unit : u.unit;
  }
  function buildMapping(data) {
    state.cols = data.columns;
    state.rows = data.rows;
    state.targets = {}; state.groups = {};
    data.targets.forEach(function (t) { state.targets[t.code] = t; (state.groups[t.group] = state.groups[t.group] || []).push(t); });
    var body = $("map-rows");
    clear(body);
    data.columns.forEach(function (row) {
      var st = KIND[row.kind] || KIND.unknown;
      var sel = targetSelect(row);
      var unitCell = el("td", { "data-label": "Единица" }, [unitSelect(sel.value, row.unit)]);
      var tr = el("tr", { class: "map-row map-row--" + row.kind }, [
        el("th", { scope: "row" }, [el("span", { class: "map-col", text: row.column }), el("span", { class: "chip " + st[1], text: st[0] })]),
        el("td", { "data-label": "Пример", class: "map-ex", text: row.example || "—" }),
        el("td", { "data-label": "Что это" }, [sel]),
        unitCell
      ]);
      sel.addEventListener("change", function () {
        clear(unitCell); unitCell.appendChild(unitSelect(sel.value, ""));
        tr.classList.toggle("is-off", sel.value === "ignore");
        checkRequirements();
      });
      tr.classList.toggle("is-off", sel.value === "ignore");
      body.appendChild(tr);
    });
    $("cols-count").textContent = "· " + data.columns.length + " " + plural(data.columns.length, "столбец", "столбца", "столбцов") +
      ", " + nf.format(data.rows) + " " + plural(data.rows, "строка", "строки", "строк");
    $("step-cols").hidden = false; $("step-run").hidden = false;
    steps(2);
    checkRequirements();
    $("step-cols").scrollIntoView({ behavior: smooth(), block: "start" });
  }
  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    return (m10 === 1 && m100 !== 11) ? one : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) ? few : many;
  }
  function selections() {
    return Array.prototype.map.call(document.querySelectorAll("#map-rows tr"), function (tr) {
      var sel = tr.querySelector("select[data-col]"), unit = tr.querySelector(".unit-sel");
      return { column: sel.getAttribute("data-col"), code: sel.value, unit: unit && !unit.disabled ? unit.value : "" };
    });
  }
  // Те же проверки, что на сервере (service.requirements): пересчитываются при каждой правке сопоставления.
  function checkRequirements() {
    var chosen = {}, dup = [], ignored = 0;
    selections().forEach(function (s) {
      if (s.code === "ignore") { ignored += 1; return; }
      if (chosen[s.code]) { dup.push(s.column); } else { chosen[s.code] = s.column; }
    });
    var errors = [], warnings = [], info = [];
    if (!state.rows) { errors.push("В файле нет строк с данными: под строкой заголовка должны быть строки с анализами."); }
    REQUIRED.forEach(function (r) { if (!chosen[r[0]]) { errors.push("Нет столбца «" + r[1] + "»: без него каждая строка будет отклонена. Выберите его в списке «Что это»."); } });
    var miss = CBC_CORE.filter(function (c) { return !chosen[c]; });
    if (miss.length) { warnings.push("Нет показателей общего анализа крови: " + miss.join(", ") + ". Скрининг скрытого дефицита и класс анемии будут менее надёжны."); }
    if (!(state.groups.labs || []).some(function (t) { return chosen[t.code]; })) {
      warnings.push("Нет анализов вне общего анализа крови (ферритин, железо, B12, фолаты и др.): причину анемии по одному ОАК надёжно не определить.");
    }
    if (dup.length) { warnings.push("Несколько столбцов выбраны как один показатель — будет использован первый: " + dup.join(", ") + "."); }
    if (!chosen.anemia && !chosen.anemia_class && !chosen.cause) { info.push("Нет столбцов с метками: метрики не считаются, будут только предсказания."); }
    if (ignored) { info.push("Не используются: " + ignored + " " + plural(ignored, "столбец", "столбца", "столбцов") + "."); }
    if (!state.rows) { errors = errors.slice(0, 1); warnings = []; info = []; }   // пустой файл: остальное — шум
    var box = $("req");
    clear(box);
    var ok = !errors.length;
    box.appendChild(el("p", { class: "req__lead req__lead--" + (ok ? (warnings.length ? "warn" : "ok") : "error"),
      text: ok ? (warnings.length ? "Можно считать, но данных не хватает для полного анализа:" : "Данных достаточно для анализа.")
        : (state.rows ? "Не хватает обязательных столбцов:" : "Расчёт невозможен:") }));
    var list = el("ul", { class: "req__list" });
    errors.forEach(function (t) { list.appendChild(el("li", { class: "req__item req__item--error", text: t })); });
    warnings.forEach(function (t) { list.appendChild(el("li", { class: "req__item req__item--warn", text: t })); });
    info.forEach(function (t) { list.appendChild(el("li", { class: "req__item req__item--info", text: t })); });
    box.appendChild(list);
    $("bench-run").disabled = !ok;
    $("bench-run").title = ok ? "" : (state.rows ? "Сначала сопоставьте пол, возраст и гемоглобин" : "В файле нет строк с данными");
    if (ok) { steps(3); } else { steps(2); }
  }

  // ---------- шаг 3: расчёт ----------
  function submit(e) {
    e.preventDefault();
    showAlert("");
    if (!state.file) { showAlert("Выберите файл CSV или XLSX."); return; }
    var mapping = {};
    selections().forEach(function (s) { mapping[s.column] = s.code === "ignore" ? "ignore" : (s.unit ? { code: s.code, unit: s.unit } : s.code); });
    var fd = new FormData();
    fd.append("mode", document.querySelector('input[name="mode"]:checked').value);
    fd.append("mapping", JSON.stringify(mapping));
    fd.append("file", state.file, state.file.name);
    var btn = $("bench-run");
    btn.disabled = true; btn.textContent = "Считаю…";
    fetch("/ui/benchmark", { method: "POST", body: fd, credentials: "omit", cache: "no-store" }).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = JSON.parse(text); } catch (err) { data = null; }
        if (resp.ok && data) { render(data); return; }
        clear($("bench-out"));
        showAlert((data && data.error && data.error.message) || (resp.status === 413 ? "Файл слишком большой." :
          resp.status === 429 ? "Слишком много запросов. Повторите через минуту." : "Сервис не ответил (код " + resp.status + ")."));
      });
    }).catch(function () { showAlert("Нет связи с сервисом. Повторите."); })
      .then(function () { btn.disabled = false; btn.textContent = "Рассчитать"; });
  }
  function hasFiles(e) { return !!(e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0); }

  function start() {
    refReady = fetch("/ui/reference", { credentials: "omit", cache: "no-store", headers: { "Accept": "application/json" } }).then(function (resp) {
      return resp.ok ? resp.json() : null;
    }).then(function (ref) {
      ((ref && ref.analytes) || []).forEach(function (a) { unitNames[a.code] = a.unit_choices || []; });
    }).catch(function () { /* без справочника — подписи единиц как в ответе /ui/columns */ });
    $("bench-form").addEventListener("submit", submit);
    $("bench-file").addEventListener("change", function () { setFile($("bench-file").files[0]); });
    $("bench-reset").addEventListener("click", function () { $("bench-file").value = ""; $("bench-file").click(); });
    var drop = $("bench-drop");
    ["dragenter", "dragover"].forEach(function (t) { drop.addEventListener(t, function (e) { if (hasFiles(e)) { e.preventDefault(); drop.classList.add("is-drag"); } }); });
    ["dragleave", "drop"].forEach(function (t) { drop.addEventListener(t, function (e) { if (t === "drop" || !drop.contains(e.relatedTarget)) { drop.classList.remove("is-drag"); } }); });
    drop.addEventListener("drop", function (e) { if (hasFiles(e)) { e.preventDefault(); setFile(e.dataTransfer.files[0]); } });
    document.addEventListener("dragover", function (e) { if (hasFiles(e)) { e.preventDefault(); } });
    document.addEventListener("drop", function (e) { if (hasFiles(e) && !drop.contains(e.target)) { e.preventDefault(); } });
    document.body.setAttribute("data-ready", "1");
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
})();
