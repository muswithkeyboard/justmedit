/* Justmedit — страница «API для встраивания» (/developers).
   Описание маршрутов берётся из /api/v1/openapi.json (написано вручную, тест сверяет его с маршрутами app.py).
   Из него строятся консоль (маршрут, авторизация, параметры, тело-пример), примеры кода и справочник.
   Консоль шлёт запросы со страницы через fetch с credentials: "omit" — cookie сайта не уходят, авторизация только та,
   что выбрана в консоли. Ключ и токен живут в памяти вкладки: ни localStorage, ни cookie, ни адрес страницы;
   в примеры кода вместо них подставляются $JUSTMEDIT_TOKEN, $JUSTMEDIT_API_KEY или <ваш ключ>. id из ответов кабинета
   (запись, доступ врачу, токен, пациент, набор референсов) тоже держатся только в памяти и подставляются в параметры
   пути следующих запросов. Тела кабинета уходят как JSON с Content-Type: application/json (иначе сервер ответит 415).
   Всё, что пришло от сервера, выводится только как текст (textContent); JSON подсвечивается классами CSS.
   Примеры файлов для консоли (CSV, TXT) — придуманные строки, без персональных данных.
   Пароли: в теле-примере консоли пароль пустой (свой вводит пользователь), логин регистрации — dev-<6 случайных
   знаков>: в демо не появляется общий аккаунт с паролем из документации. В примерах кода вместо пароля —
   переменные окружения $JUSTMEDIT_PASSWORD (и $JUSTMEDIT_NEW_PASSWORD для смены пароля). */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

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
  function show(node, on) { node.hidden = !on; }

  var ORIGIN = (location.origin && location.origin !== "null") ? location.origin : "";
  var PLACEHOLDER_ORIGIN = "https://justmedit.example";
  var GROUPS = ["Вход и токены", "Анализ", "Файлы", "Кабинет пациента", "Врач", "Служебное"];
  var METHODS = ["get", "post", "put", "patch", "delete"];
  var STATUS_TEXT = {
    200: "OK", 201: "создано", 204: "без содержимого", 400: "некорректный запрос", 401: "нет авторизации",
    403: "доступ запрещён", 404: "не найдено", 405: "метод не поддерживается", 409: "конфликт", 413: "слишком большой запрос",
    415: "не тот тип тела", 422: "ошибка ввода", 429: "лимит запросов", 500: "внутренняя ошибка", 503: "сервис занят",
    507: "хранилище заполнено"
  };
  var SHOW_HEADERS = ["content-type", "content-length", "content-disposition", "cache-control", "retry-after",
    "www-authenticate", "access-control-allow-origin"];
  var MAX_SHOW = 200000;            // символов ответа на экране: дальше — обрезка (подсветка огромного JSON тормозит)
  var AUTH_LABEL = { none: "без авторизации", key: "X-API-Key", bearer: "Bearer-токен" };
  var KEY_PLACEHOLDER = "<ваш ключ>";
  var TOKEN_PLACEHOLDER = "<токен пользователя>";

  // Примеры файлов для маршрутов с полем file: значения придуманы (как в examples/curl.sh).
  var SAMPLE_FILES = {
    table: { name: "example.csv", type: "text/csv", text:
      "patient_id,sex,age_years,hemoglobin,RBC,hematocrit,MCV,MCH,MCHC,RDW,platelets,WBC,ferritin,CRP\n" +
      "demo-1,F,34,124,4.59,37.7,82,27.0,329,15.2,310,6.1,,\n" +
      "demo-2,M,61,104,4.10,32.4,79,25.4,321,16.0,340,8.9,62,28\n" +
      "demo-3,F,58,96,3.35,30.5,91,28.7,315,19.5,280,5.2,8,\n" },
    blank: { name: "blank.txt", type: "text/plain", text:
      "Дата взятия 16.04.2026\nГемоглобин 118 г/л 120-150\nЭритроциты 4,1 10^12/л 3,8-5,1\nMCV 78 фл 80-100\n" +
      "Ферритин 9 мкг/л 10-120\n" },
    labrefs: { name: "lab_refs.csv", type: "text/csv", text:
      "показатель;нижняя;верхняя;единица\nhemoglobin;117;155;г/л\nMCV;81;100;фл\n" }
  };
  var SAMPLE_FOR = { parse: "blank", columns: "table", benchmark: "table", labRefsCreate: "labrefs" };
  var ACCEPT_FOR = { parse: ".jpg,.jpeg,.png,.webp,.pdf,.txt,.csv,.xlsx", columns: ".csv,.xlsx,.txt",
    benchmark: ".csv,.xlsx,.txt", labRefsCreate: ".csv,.xlsx" };

  var spec = null;
  var ops = [];                      // [{id, method, path, op, group}]
  var byId = {};
  var cur = null;                    // выбранный маршрут
  var secrets = { key: "", bearer: "" };   // только в памяти вкладки
  var lastToken = null;              // токен из последнего ответа входа / регистрации / создания токена
  // id из ответов кабинета (запись, доступ, токен, пациент, набор референсов) — подставляются в параметры пути
  // следующих запросов вместо примера: GET /api/v1/me/records/{id} после POST /api/v1/me/records. Только в памяти.
  var seenIds = {};
  var chosenFile = null;             // выбранный файл (File) — только в памяти
  // Поля тела с паролем: в примере консоли — пустые, в примерах кода — переменная окружения.
  var SECRET_FIELDS = { password: "JUSTMEDIT_PASSWORD", old: "JUSTMEDIT_PASSWORD", "new": "JUSTMEDIT_NEW_PASSWORD" };
  var devLogin = null;               // логин dev-xxxxxx этой вкладки: регистрация и вход в консоли — один и тот же
  var lang = "curl";
  var inFlight = false;

  function announce(text) { var n = $("dev-live"); n.textContent = ""; window.setTimeout(function () { n.textContent = text; }, 30); }

  // ---------------------------------------------------------------------------------------------
  // OpenAPI: ссылки $ref, примеры
  // ---------------------------------------------------------------------------------------------
  function resolve(obj) {
    var guard = 0;
    while (obj && obj.$ref && guard++ < 10) {
      var parts = obj.$ref.replace(/^#\//, "").split("/");
      obj = parts.reduce(function (o, p) { return o ? o[p] : undefined; }, spec);
    }
    return obj || {};
  }
  function bodyContent(op) {
    var rb = op.requestBody ? resolve(op.requestBody) : null;
    return rb && rb.content ? rb.content : {};
  }
  function jsonExample(media) {
    if (!media) { return undefined; }
    if (media.example !== undefined) { return media.example; }
    var s = media.schema ? resolve(media.schema) : null;
    return s && s.example !== undefined ? s.example : undefined;
  }
  function firstSuccess(op) {
    var codes = Object.keys(op.responses || {}).filter(function (c) { return /^2/.test(c); }).sort();
    if (!codes.length) { return null; }
    var r = resolve(op.responses[codes[0]]);
    var types = Object.keys(r.content || {});
    var type = types[0] || null;
    return { code: codes[0], type: type, example: type ? jsonExample(r.content[type]) : undefined, description: r.description || "" };
  }
  function securityModes(op) {
    var sec = op.security || spec.security || [];
    var names = [];
    sec.forEach(function (req) { Object.keys(req).forEach(function (n) { names.push(n); }); });
    return names;
  }
  function needsUserToken(op) {
    var s = securityModes(op);
    return s.indexOf("BearerAuth") >= 0 && s.indexOf("ApiKeyAuth") < 0;
  }
  function authText(op) {
    var s = securityModes(op);
    if (!s.length) { return "не нужна"; }
    var parts = [];
    if (s.indexOf("ApiKeyAuth") >= 0) { parts.push("X-API-Key"); }
    if (s.indexOf("BearerAuth") >= 0) { parts.push("Authorization: Bearer"); }
    if (s.indexOf("CookieAuth") >= 0) { parts.push("cookie сайта"); }
    return parts.join(" или ");
  }

  // ---------------------------------------------------------------------------------------------
  // Подсветка JSON: узлы span с классами, текст — только textContent
  // ---------------------------------------------------------------------------------------------
  function jsonInto(target, value) {
    var frag = document.createDocumentFragment();
    var PAD = "  ";
    function tok(cls, text) { frag.appendChild(cls ? el("span", { class: cls, text: text }) : document.createTextNode(text)); }
    function walk(v, ind) {
      if (v === null) { tok("j-n", "null"); return; }
      if (Array.isArray(v)) {
        if (!v.length) { tok("j-p", "[]"); return; }
        tok("j-p", "[");
        v.forEach(function (x, i) {
          tok(null, "\n" + ind + PAD); walk(x, ind + PAD);
          if (i < v.length - 1) { tok("j-p", ","); }
        });
        tok(null, "\n" + ind); tok("j-p", "]");
        return;
      }
      if (typeof v === "object") {
        var keys = Object.keys(v);
        if (!keys.length) { tok("j-p", "{}"); return; }
        tok("j-p", "{");
        keys.forEach(function (k, i) {
          tok(null, "\n" + ind + PAD); tok("j-k", JSON.stringify(k)); tok("j-p", ": "); walk(v[k], ind + PAD);
          if (i < keys.length - 1) { tok("j-p", ","); }
        });
        tok(null, "\n" + ind); tok("j-p", "}");
        return;
      }
      if (typeof v === "string") { tok("j-s", JSON.stringify(v)); return; }
      if (typeof v === "number") { tok("j-num", String(v)); return; }
      if (typeof v === "boolean") { tok("j-b", String(v)); return; }
      tok(null, String(v));
    }
    walk(value, "");
    clear(target);
    target.appendChild(frag);
  }
  function exampleInto(target, example) {
    if (typeof example === "string") { target.textContent = example; }
    else { jsonInto(target, example); }
  }

  // ---------------------------------------------------------------------------------------------
  // Консоль: выбор маршрута
  // ---------------------------------------------------------------------------------------------
  function methodBadge(method) { return el("span", { class: "method method--" + method, text: method.toUpperCase() }); }

  function fillRoutes() {
    var sel = $("c-route");
    clear(sel);
    GROUPS.concat(["Другое"]).forEach(function (g) {
      var list = ops.filter(function (o) { return o.group === g; });
      if (!list.length) { return; }
      var og = el("optgroup", { label: g });
      list.forEach(function (o) {
        og.appendChild(el("option", { value: o.id, text: o.method.toUpperCase() + " " + o.path + " — " + (o.op.summary || "") }));
      });
      sel.appendChild(og);
    });
    sel.disabled = false;
  }

  function setAuthMode(mode) {
    Array.prototype.forEach.call(document.querySelectorAll('input[name="c-auth"]'), function (r) { r.checked = r.value === mode; });
    updateAuthUI();
  }
  function authMode() {
    var r = document.querySelector('input[name="c-auth"]:checked');
    return r ? r.value : "none";
  }
  function updateAuthUI() {
    var mode = authMode();
    show($("c-secret-box"), mode !== "none");
    var input = $("c-secret");
    if (mode === "key") {
      $("c-secret-label").textContent = "Ключ API (заголовок X-API-Key)";
      input.placeholder = "например, dl-demo-key (демо)";
      input.value = secrets.key;
      $("c-secret-hint").textContent = cur && needsUserToken(cur.op)
        ? "Этот маршрут не принимает ключ API: нужен токен пользователя."
        : "Ключ — только для запросов с вашего сервера. На своём сайте в браузере используйте токен пользователя.";
    } else if (mode === "bearer") {
      $("c-secret-label").textContent = "Токен пользователя (Authorization: Bearer)";
      input.placeholder = "token из ответа /api/v1/auth/login";
      input.value = secrets.bearer;
      $("c-secret-hint").textContent = "Токен уйдёт заголовком Authorization: Bearer <токен>. Страница его не сохраняет.";
    }
  }

  function selectOp(id, opts) {
    var o = byId[id] || ops[0];
    if (!o) { return; }
    cur = o;
    $("c-route").value = o.id;
    var m = $("c-method");
    m.className = "method method--" + o.method;
    m.textContent = o.method.toUpperCase();
    $("c-path").textContent = o.path;
    var desc = $("c-desc");
    clear(desc);
    desc.appendChild(document.createTextNode((o.op.description || o.op.summary || "") + " "));
    if (o.op["x-role"]) { desc.appendChild(el("b", { text: "Кто может вызывать: " + o.op["x-role"] + "." })); }
    show($("c-hint"), needsUserToken(o.op));

    var s = securityModes(o.op);
    setAuthMode(!s.length ? "none" : (s.indexOf("ApiKeyAuth") >= 0 ? (secrets.key || !secrets.bearer ? "key" : "bearer") : "bearer"));

    buildParams(o);
    buildBody(o);
    resetResponse();
    renderCode();
    if (opts && opts.focus) { $("c-route").focus(); }
  }

  // ---- id из ответов для параметров пути ----
  // Вид id по шаблону пути: records, shares, tokens (кабинет пациента), patients, lab-refs (врач), patientRecord ({rid}).
  function idKind(path, name) {
    if (name === "rid") { return "patientRecord"; }
    var m = /^\/api\/v1\/(?:me\/(records|shares|tokens)|doctor\/(patients|lab-refs))\//.exec(path);
    return m ? (m[1] || m[2]) : null;
  }
  function rememberIds(path, data) {
    var doctor = /^\/api\/v1\/doctor\//.test(path);
    function put(kind, v) { if (typeof v === "string" && /^[0-9a-f]{32}$/.test(v)) { seenIds[kind] = v; } }
    function first(list) { return Array.isArray(list) && list.length ? list[0] : {}; }
    if (data.record) { put(doctor ? "patientRecord" : "records", data.record.id); }
    put("records", first(data.records).id);
    if (data.latest) { put(doctor ? "patientRecord" : "records", data.latest.record_id); }
    if (data.share) { put("shares", data.share.id); }
    put("shares", first(data.shares).id);
    if (data.token && typeof data.token === "object") { put("tokens", data.token.id); }   // только новый токен API
    if (data.patient) { put("patients", data.patient.id); }
    put("patients", first(data.patients).id);
    if (Array.isArray(data.sets) && data.sets.length) { put("lab-refs", data.sets[data.sets.length - 1].id); }
  }

  // ---- параметры пути и строки запроса ----
  function buildParams(o) {
    var box = $("c-params");
    clear(box);
    (o.op.parameters || []).map(resolve).forEach(function (p) {
      if (p.in !== "path" && p.in !== "query") { return; }
      var id = "c-p-" + p.in + "-" + p.name;
      var schema = p.schema ? resolve(p.schema) : {};
      var input;
      if (schema.enum) {
        input = el("select", { id: id, "data-param": p.name, "data-in": p.in });
        if (!p.required) { input.appendChild(el("option", { value: "", text: "не передавать" })); }
        schema.enum.forEach(function (v) { input.appendChild(el("option", { value: String(v), text: String(v) })); });
        if (p.required && p.example !== undefined) { input.value = String(p.example); }
      } else {
        input = el("input", { id: id, type: "text", autocomplete: "off", spellcheck: "false", "data-param": p.name, "data-in": p.in });
        var seen = p.in === "path" ? seenIds[idKind(o.path, p.name)] : undefined;
        input.value = seen || (p.in === "path" && p.example !== undefined ? String(p.example) : "");
      }
      input.addEventListener("input", renderCode);
      input.addEventListener("change", renderCode);
      var where = p.in === "path" ? "в пути" : "в строке запроса";
      box.appendChild(el("div", { class: "console__param" }, [
        el("label", { for: id, class: "console__label", text: p.name + " · " + where + (p.required ? "" : " · необязательно") }),
        input,
        p.description ? el("p", { class: "hint", text: p.description }) : null
      ]));
    });
  }

  // ---- тело: JSON или файл ----
  function hasJson(o) { return !!bodyContent(o.op)["application/json"]; }
  function bodyRequired(o) { return !!(o.op.requestBody && resolve(o.op.requestBody).required); }
  function hasForm(o) { return !!bodyContent(o.op)["multipart/form-data"]; }
  function bodyKind() {
    if (!cur) { return "none"; }
    var j = hasJson(cur), f = hasForm(cur);
    if (j && f) {
      var r = document.querySelector('input[name="c-ctype"]:checked');
      return r && r.value === "file" ? "form" : "json";
    }
    return j ? "json" : (f ? "form" : "none");
  }
  function randomLogin() {
    var abc = "abcdefghijklmnopqrstuvwxyz0123456789", out = "", buf = new Uint8Array(6);
    (window.crypto || window.msCrypto).getRandomValues(buf);
    for (var i = 0; i < buf.length; i++) { out += abc.charAt(buf[i] % abc.length); }
    return "dev-" + out;
  }
  // Пример тела для консоли: пароли пустые (вводит пользователь), логин регистрации и входа — dev-xxxxxx этой
  // вкладки. Так из документации нельзя случайно создать общий аккаунт с известным паролем.
  function exampleText(o) {
    var ex = jsonExample(bodyContent(o.op)["application/json"]);
    if (ex === undefined) { return "{}"; }
    if (ex && typeof ex === "object" && !Array.isArray(ex)) {
      ex = JSON.parse(JSON.stringify(ex));
      Object.keys(SECRET_FIELDS).forEach(function (k) { if (typeof ex[k] === "string") { ex[k] = ""; } });
      if (typeof ex.login === "string" && /^\/api\/v1\/auth\/(register|login)$/.test(o.path)) {
        devLogin = devLogin || randomLogin();
        ex.login = devLogin;
      }
    }
    return JSON.stringify(ex, null, 2);
  }
  function buildBody(o) {
    var j = hasJson(o), f = hasForm(o);
    show($("c-body-box"), j || f);
    show($("c-ctype"), j && f);
    Array.prototype.forEach.call(document.querySelectorAll('input[name="c-ctype"]'), function (r) { r.checked = r.value === "json"; });
    chosenFile = null;
    $("c-body").value = j ? exampleText(o) : "";
    markBody(null);
    buildForm(o);
    switchBody();
  }
  function switchBody() {
    var kind = bodyKind();
    show($("c-body"), kind === "json");
    show($("c-reset"), kind === "json");
    show($("c-form"), kind === "form");
    if (kind !== "json") { markBody(null); }
    $("c-body-label").textContent = kind === "form" ? "Тело запроса · multipart/form-data" : "Тело запроса · JSON";
  }
  function formSchema(o) {
    var media = bodyContent(o.op)["multipart/form-data"];
    return media && media.schema ? resolve(media.schema) : null;
  }
  function buildForm(o) {
    var box = $("c-form");
    clear(box);
    var schema = formSchema(o);
    if (!schema) { return; }
    var props = schema.properties || {};
    Object.keys(props).forEach(function (name) {
      var p = resolve(props[name]);
      var id = "c-f-" + name;
      if (p.format === "binary") {
        var input = el("input", { id: id, type: "file", class: "vh", accept: ACCEPT_FOR[o.id] || null });
        var fname = el("span", { class: "console__file-name", id: "c-file-name", text: "файл не выбран" });
        input.addEventListener("change", function () {
          chosenFile = input.files && input.files[0] ? input.files[0] : null;
          fname.textContent = chosenFile ? chosenFile.name + " · " + fmtBytes(chosenFile.size) : "файл не выбран";
          renderCode();
        });
        var row = [el("label", { for: id, class: "btn btn--small", text: "Выбрать файл" })];
        var sample = SAMPLE_FILES[SAMPLE_FOR[o.id]];
        if (sample) {
          var btn = el("button", { type: "button", class: "btn btn--small", text: "Взять пример: " + sample.name });
          btn.addEventListener("click", function () {
            chosenFile = new File([sample.text], sample.name, { type: sample.type });
            fname.textContent = chosenFile.name + " · " + fmtBytes(chosenFile.size) + " · придуманные значения";
            renderCode();
          });
          row.push(btn);
        }
        box.appendChild(el("div", { class: "console__file" }, [
          el("span", { class: "console__label", text: name + (isRequired(schema, name) ? "" : " · необязательно") }),
          input,
          el("div", { class: "console__file-row" }, row),
          fname,
          p.description ? el("p", { class: "hint", text: p.description }) : null
        ]));
        return;
      }
      var field;
      if (p.enum) {
        field = el("select", { id: id, "data-field": name });
        if (!isRequired(schema, name)) { field.appendChild(el("option", { value: "", text: "не передавать" })); }
        p.enum.forEach(function (v) { field.appendChild(el("option", { value: String(v), text: String(v) })); });
        if (p["default"] !== undefined) { field.value = String(p["default"]); }
      } else {
        field = el("input", { id: id, type: "text", autocomplete: "off", spellcheck: "false", "data-field": name });
      }
      field.addEventListener("input", renderCode);
      field.addEventListener("change", renderCode);
      box.appendChild(el("div", { class: "console__param" }, [
        el("label", { for: id, class: "console__label", text: name + (isRequired(schema, name) ? "" : " · необязательно") }),
        field,
        p.description ? el("p", { class: "hint", text: p.description }) : null
      ]));
    });
  }
  function isRequired(schema, name) { return (schema.required || []).indexOf(name) >= 0; }
  function fmtBytes(n) { return n < 1024 ? n + " Б" : (n < 1048576 ? (n / 1024).toFixed(1).replace(".", ",") + " КБ" : (n / 1048576).toFixed(1).replace(".", ",") + " МБ"); }
  function markBody(message) {
    var e = $("c-body-error");
    $("c-body").setAttribute("aria-invalid", message ? "true" : "false");
    e.textContent = message || "";
    show(e, !!message);
  }

  // ---------------------------------------------------------------------------------------------
  // Запрос из состояния консоли. withSecrets=false — для примеров кода (вместо ключа — заглушки).
  // ---------------------------------------------------------------------------------------------
  function buildRequest(withSecrets) {
    var r = { method: cur.method, path: cur.path, query: [], headers: [], auth: authMode(), kind: bodyKind(),
      json: undefined, jsonText: null, fields: [], file: null, fileName: null, error: null };
    var missing = [];
    Array.prototype.forEach.call($("c-params").querySelectorAll("[data-param]"), function (input) {
      var name = input.getAttribute("data-param"), v = input.value.trim();
      if (input.getAttribute("data-in") === "path") {
        if (!v) { missing.push(name); }
        r.path = r.path.split("{" + name + "}").join(v ? encodeURIComponent(v) : "{" + name + "}");
      } else if (v) {
        r.query.push([name, v]);
      }
    });
    if (missing.length) { r.error = "Заполните параметр пути: " + missing.join(", ") + "."; }
    r.url = r.path + (r.query.length ? "?" + r.query.map(function (q) {
      return encodeURIComponent(q[0]) + "=" + encodeURIComponent(q[1]);
    }).join("&") : "");

    if (r.auth === "key") {
      r.headers.push(["X-API-Key", withSecrets ? secrets.key.trim() : null]);
      if (withSecrets && !secrets.key.trim() && !r.error) { r.error = "Введите ключ API или выберите другую авторизацию."; }
    } else if (r.auth === "bearer") {
      r.headers.push(["Authorization", withSecrets ? "Bearer " + secrets.bearer.trim() : null]);
      if (withSecrets && !secrets.bearer.trim() && !r.error) { r.error = "Вставьте токен пользователя или выберите другую авторизацию."; }
    }

    if (r.kind === "json" && !$("c-body").value.trim() && !bodyRequired(cur)) {
      r.kind = "none";                 // тело необязательно (POST /api/v1/me/tokens): пустое поле — запрос без тела
    }
    if (r.kind === "json") {
      r.jsonText = $("c-body").value;
      try { r.json = JSON.parse(r.jsonText); }
      catch (e) { r.json = undefined; if (withSecrets && !r.error) { r.error = jsonErrorText(r.jsonText); r.bodyError = true; } }
      r.codeJson = r.json;
      if (r.json && typeof r.json === "object" && !Array.isArray(r.json)) {
        var empty = Object.keys(SECRET_FIELDS).filter(function (k) { return r.json[k] === ""; });
        if (withSecrets && empty.length && !r.error) {
          r.error = "Впишите свой пароль в поле «" + empty[0] + "» (от 8 символов): пример пароля консоль не подставляет.";
          r.bodyError = true;
        }
        // в примерах кода — не значение пароля, а переменная окружения
        r.codeJson = {};
        Object.keys(r.json).forEach(function (k) {
          r.codeJson[k] = (SECRET_FIELDS[k] && typeof r.json[k] === "string") ? "__JM_ENV_" + SECRET_FIELDS[k] + "__" : r.json[k];
        });
      }
      // Кабинет принимает JSON только с этим заголовком (иначе 415): так простую HTML-форму с чужого сайта не отправить
      r.headers.push(["Content-Type", "application/json"]);
    } else if (r.kind === "form") {
      Array.prototype.forEach.call($("c-form").querySelectorAll("[data-field]"), function (f) {
        var v = f.value.trim();
        if (v) { r.fields.push([f.getAttribute("data-field"), v]); }
      });
      r.file = chosenFile;
      r.fileName = chosenFile ? chosenFile.name : (SAMPLE_FILES[SAMPLE_FOR[cur.id]] || { name: "analyses.csv" }).name;
      if (withSecrets && !chosenFile && !r.error) { r.error = "Выберите файл или возьмите пример."; }
    }
    return r;
  }

  // ---------------------------------------------------------------------------------------------
  // Отправка и ответ
  // ---------------------------------------------------------------------------------------------
  // Ошибка разбора JSON своими словами (сообщение браузера — по-английски и у всех разное): строка и столбец.
  function jsonErrorText(text) {
    var pos = -1;
    try { JSON.parse(text); } catch (e) {
      var m = /position (\d+)/i.exec(e.message || "");
      if (m) { pos = parseInt(m[1], 10); }
      var lc = /line (\d+) column (\d+)/i.exec(e.message || "");
      if (lc) { return "Тело запроса — не JSON: ошибка в строке " + lc[1] + ", столбце " + lc[2] + ". Проверьте кавычки, запятые и скобки."; }
    }
    if (pos >= 0) {
      var before = text.slice(0, pos).split("\n");
      return "Тело запроса — не JSON: ошибка в строке " + before.length + ", столбце " + (before[before.length - 1].length + 1) + ". Проверьте кавычки, запятые и скобки.";
    }
    return "Тело запроса — не JSON. Проверьте кавычки (только двойные), запятые и скобки.";
  }

  function send() {
    if (!cur || inFlight) { return; }
    var r = buildRequest(true);
    markBody(r.bodyError ? r.error : null);
    // ошибка тела — один раз, под полем тела; остальные — в блоке ответа
    if (r.error) { resetResponse(); if (r.bodyError) { $("c-body").focus(); } else { showError(null, r.error, null); } return; }
    var headers = {};
    r.headers.forEach(function (h) { headers[h[0]] = h[1]; });
    var init = { method: r.method.toUpperCase(), headers: headers, credentials: "omit", cache: "no-store" };
    if (r.kind === "json") { init.body = r.jsonText; }
    else if (r.kind === "form") {
      var fd = new FormData();
      r.fields.forEach(function (f) { fd.append(f[0], f[1]); });
      fd.append("file", r.file, r.file.name);
      init.body = fd;
    }
    inFlight = true;
    var btn = $("c-send");
    btn.disabled = true;
    btn.textContent = "Отправляем…";
    var t0 = performance.now();
    fetch(r.url, init).then(function (res) {
      return res.text().then(function (text) { showResponse(res, text, performance.now() - t0); });
    }).catch(function () {
      resetResponse();
      showError(null, "Ответа нет: сервер недоступен или запрос заблокирован браузером (сеть, CORS). Проверьте адрес и повторите.", null);
    }).then(function () {
      inFlight = false;
      btn.disabled = false;
      btn.textContent = "Отправить";
    });
  }

  function resetResponse() {
    show($("c-res-empty"), true);
    show($("c-status"), false);
    $("c-time").textContent = "";
    show($("c-error"), false);
    show($("c-token"), false);
    show($("c-headers"), false);
    show($("c-res"), false);
    show($("c-res-copy"), false);
    show($("c-res-note"), false);
  }

  function showError(code, message, extra) {
    var box = $("c-error");
    clear(box);
    if (code) { box.appendChild(el("b", { text: code })); }
    box.appendChild(el("span", { text: message }));
    if (extra) { box.appendChild(el("p", { class: "hint", text: extra })); }
    show(box, true);
    show($("c-res-empty"), false);
  }

  var CODE_HINTS = {
    invalid_credentials: "Проверьте логин и пароль. Аккаунта ещё нет — POST /api/v1/auth/register.",
    login_taken: "Логин занят (регистр не важен): выберите другой или войдите через POST /api/v1/auth/login.",
    unsupported_media_type: "Тело кабинета — JSON с заголовком Content-Type: application/json.",
    share_invalid: "Ссылка одноразовая и живёт 48 ч: попросите пациента создать новую (POST /api/v1/me/shares).",
    password_required: "По токену API это действие (новый токен, ссылка врачу, «выйти везде») — только с паролем аккаунта: добавьте в тело поле \"password\".",
    csrf: "Запрос с cookie требует заголовок X-Justmedit: 1 — с токеном он не нужен.",
    consent_required: "Регистрация — только с согласием на хранение анализов: \"consent\": true.",
    too_many_tokens: "Именованных токенов не больше 20: отзовите ненужный (DELETE /api/v1/me/tokens/{id}) и создайте новый.",
    quota_exceeded: "Данных одного пользователя — не больше 5 МБ: удалите старые записи (DELETE /api/v1/me/records/{id}).",
    record_too_large: "Вход одной записи — не больше 16 КБ: уберите лишние показатели, референсы или patient_ref.",
    storage_full: "Хранилище сервиса заполнено: новые данные не принимаются, чтение, выгрузка и удаление работают. Повторите позже.",
    signups_paused: "Достигнут суточный предел новых аккаунтов (DL_MAX_SIGNUPS_PER_DAY): повторите позже — вход работает.",
    unknown_analyte: "Показатель не из справочника: коды и названия — GET /api/v1/reference. В кабинете хранятся только известные показатели.",
    missing_field: "Укажите, что изменить: хотя бы одно из полей name, default или ranges."
  };
  // wrong_password — по маршруту: что именно не сделано из-за неверного пароля
  var WRONG_PASSWORD_HINTS = {
    "delete /api/v1/me": "Нужен текущий пароль этого аккаунта. Ничего не удалено.",
    "post /api/v1/me/password": "Поле old — текущий пароль. Пароль не изменён.",
    "post /api/v1/me/shares": "Нужен текущий пароль аккаунта. Ссылка врачу не создана.",
    "post /api/v1/me/tokens": "Нужен текущий пароль аккаунта. Токен не создан.",
    "post /api/v1/me/logout-all": "Нужен текущий пароль аккаунта. Входы не закрыты."
  };
  function errorHint(status, data) {
    var mode = authMode();
    var code = data && data.error ? data.error.code : "";
    if (code === "wrong_password") {
      return WRONG_PASSWORD_HINTS[cur.method + " " + cur.path] || "Нужен текущий пароль этого аккаунта: действие не выполнено.";
    }
    if (CODE_HINTS[code]) { return CODE_HINTS[code]; }
    if (status === 401) {
      return mode === "none" ? "Выберите авторизацию: X-API-Key для маршрутов анализа или Bearer-токен для кабинета."
        : "Ключ или токен не передан либо токен просрочен: войдите заново через POST /api/v1/auth/login.";
    }
    if (status === 403) { return "Проверьте ключ, роль пользователя (пациент или врач) и срок доступа к пациенту."; }
    if (status === 404) { return "Нет такой записи или пути: id — 32 шестнадцатеричных знака из ответа сервера (консоль подставляет id из прошлых ответов)."; }
    if (status === 429 || status === 503) { return "Повторите позже: через сколько секунд — в заголовке Retry-After."; }
    if (status === 413) { return "Уменьшите файл или разбейте пакет на части."; }
    if (status === 507) { return "Хранилище сервиса заполнено: повторите позже."; }
    return null;
  }

  function showResponse(res, text, ms) {
    show($("c-res-empty"), false);
    var st = $("c-status");
    st.className = "status status--" + String(res.status).charAt(0) + "xx";
    st.textContent = res.status + " " + (STATUS_TEXT[res.status] || res.statusText || "");
    show(st, true);
    $("c-time").textContent = Math.round(ms) + " мс";

    var dl = $("c-headers");
    clear(dl);
    SHOW_HEADERS.forEach(function (h) {
      var v = res.headers.get(h);
      if (v !== null) { dl.appendChild(el("dt", { text: h })); dl.appendChild(el("dd", { text: v })); }
    });
    show(dl, !!dl.firstChild);

    var ctype = (res.headers.get("content-type") || "").toLowerCase();
    var data = null;
    if (ctype.indexOf("json") >= 0 && text.length <= MAX_SHOW) {
      try { data = JSON.parse(text); } catch (e) { data = null; }
    }
    var code = $("c-res-code");
    var note = $("c-res-note");
    show(note, false);
    if (data !== null) {
      jsonInto(code, data);
    } else if (!text) {
      code.textContent = "(пустое тело)";
    } else {
      code.textContent = text.length > MAX_SHOW ? text.slice(0, MAX_SHOW) : text;
      if (text.length > MAX_SHOW) {
        note.textContent = "Показано " + MAX_SHOW.toLocaleString("ru-RU") + " символов из " + text.length.toLocaleString("ru-RU") + ".";
        show(note, true);
      }
    }
    show($("c-res"), true);
    show($("c-res-copy"), true);
    $("c-res").scrollTop = 0;

    show($("c-error"), false);
    if (!res.ok) {
      var err = data && data.error ? data.error : null;
      showError(err ? err.code + (err.field ? " · поле " + err.field : "") : null,
        err ? err.message : "Сервер вернул ошибку " + res.status + ".", errorHint(res.status, data));
    }

    lastToken = null;
    if (res.ok && data && typeof data === "object" && !Array.isArray(data)) {
      if (typeof data.token === "string") { lastToken = data.token; }
      else if (typeof data.value === "string" && /\/me\/tokens$/.test(cur.path)) { lastToken = data.value; }
      if (cur.method === "delete") {
        var gone = idKind(cur.path, "id");            // удалённый id больше не подставляется
        if (gone) { delete seenIds[gone]; }
      } else {
        rememberIds(cur.path, data);
      }
    }
    show($("c-token"), !!lastToken);
    announce("Ответ " + res.status + " за " + Math.round(ms) + " мс");
  }

  // ---------------------------------------------------------------------------------------------
  // Примеры кода: curl, JavaScript fetch, Python requests — без настоящих ключей
  // ---------------------------------------------------------------------------------------------
  function shq(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'"; }
  // Есть ли в теле поля с паролем (r.codeJson со вставками __JM_ENV_<ПЕРЕМЕННАЯ>__) и какие переменные нужны.
  function envVars(r) {
    var text = r.codeJson !== undefined ? JSON.stringify(r.codeJson) : "", found = [], re = /__JM_ENV_([A-Z_]+)__/g, m;
    while ((m = re.exec(text))) { if (found.indexOf(m[1]) < 0) { found.push(m[1]); } }
    return found;
  }
  function envComment(vars) {
    return vars.map(function (v) { return "# пароль — из переменной окружения, не из кода: read -rs " + v + "; export " + v; });
  }
  function fullUrl(r) { return (ORIGIN || PLACEHOLDER_ORIGIN) + r.url; }
  function prettyBody(r) {
    if (r.codeJson !== undefined) { return JSON.stringify(r.codeJson, null, 2); }
    return r.jsonText || "";
  }
  function expectsText(r) {
    return r.query.some(function (q) { return q[0] === "format" && q[1] === "csv"; });
  }

  function curlCode(r) {
    var parts = ["curl -sS" + (r.method === "get" ? "" : " -X " + r.method.toUpperCase()) + " \"" + fullUrl(r) + "\""];
    if (r.auth === "key") { parts.push("-H \"X-API-Key: $JUSTMEDIT_API_KEY\""); }
    if (r.auth === "bearer") { parts.push("-H \"Authorization: Bearer $JUSTMEDIT_TOKEN\""); }
    var tail = "";
    var vars = envVars(r);
    if (r.kind === "json") {
      parts.push("-H \"Content-Type: application/json\"");
      var compact = r.codeJson !== undefined ? JSON.stringify(r.codeJson) : (r.jsonText || "").trim();
      if (vars.length) {
        // пароль подставит оболочка: heredoc без кавычек раскрывает $JUSTMEDIT_PASSWORD
        parts.push("--data-binary @- <<JSON");
        tail = "\n" + prettyBody(r).replace(/__JM_ENV_([A-Z_]+)__/g, "$$$1") + "\nJSON";
      } else if (compact.length <= 72 && compact.indexOf("\n") < 0) { parts.push("-d " + shq(compact)); }
      else { parts.push("--data-binary @- <<'JSON'"); tail = "\n" + prettyBody(r) + "\nJSON"; }
    } else if (r.kind === "form") {
      parts.push("-F " + shq("file=@" + r.fileName));
      r.fields.forEach(function (f) { parts.push("-F " + shq(f[0] + "=" + f[1])); });
    }
    if (expectsText(r)) { parts.push("-o result.csv"); }
    var head = [];
    if (r.auth === "key") { head.push("# ключ — только на сервере: export JUSTMEDIT_API_KEY=" + KEY_PLACEHOLDER); }
    if (r.auth === "bearer") { head.push("# токен пользователя: export JUSTMEDIT_TOKEN=" + TOKEN_PLACEHOLDER + "  (POST /api/v1/auth/login → token)"); }
    head = head.concat(envComment(vars));
    return (head.length ? head.join("\n") + "\n" : "") + parts.join(" \\\n  ") + tail;
  }

  function indentLines(text, pad) { return text.split("\n").map(function (l, i) { return i ? pad + l : l; }).join("\n"); }

  function jsCode(r) {
    var L = [], vars = envVars(r);
    var JS_NAME = { JUSTMEDIT_PASSWORD: "PASSWORD", JUSTMEDIT_NEW_PASSWORD: "NEW_PASSWORD" };
    vars.forEach(function (v) {
      L.push("const " + JS_NAME[v] + " = process.env." + v + "; // Node.js; на странице — из поля формы, не из кода");
    });
    if (r.auth === "bearer") { L.push("const TOKEN = \"" + TOKEN_PLACEHOLDER + "\"; // ответ POST /api/v1/auth/login, поле token"); }
    if (r.auth === "key") {
      L.push("// Ключ API — только на сервере (Node.js 18+): в коде страницы его увидит любой посетитель.");
      L.push("const API_KEY = \"" + KEY_PLACEHOLDER + "\";");
    }
    if (r.kind === "form") {
      if (L.length) { L.push(""); }
      L.push("const form = new FormData();");
      L.push("form.append(\"file\", fileInput.files[0]); // <input type=\"file\" id=\"fileInput\">: " + r.fileName);
      r.fields.forEach(function (f) { L.push("form.append(" + JSON.stringify(f[0]) + ", " + JSON.stringify(f[1]) + ");"); });
    }
    if (L.length) { L.push(""); }
    var opts = [];
    if (r.method !== "get") { opts.push("  method: \"" + r.method.toUpperCase() + "\""); }
    var hs = [];
    if (r.auth === "key") { hs.push("    \"X-API-Key\": API_KEY"); }
    if (r.auth === "bearer") { hs.push("    \"Authorization\": `Bearer ${TOKEN}`"); }
    if (r.kind === "json") { hs.push("    \"Content-Type\": \"application/json\""); }
    if (hs.length) { opts.push("  headers: {\n" + hs.join(",\n") + "\n  }"); }
    if (r.kind === "json") {
      opts.push(r.codeJson !== undefined ? "  body: JSON.stringify(" + indentLines(JSON.stringify(r.codeJson, null, 2), "  ")
        .replace(/"__JM_ENV_([A-Z_]+)__"/g, function (_, v) { return JS_NAME[v] || v; }) + ")"
        : "  body: " + JSON.stringify(r.jsonText || ""));
    } else if (r.kind === "form") {
      opts.push("  body: form");
    }
    L.push("const res = await fetch(\"" + fullUrl(r) + "\"" + (opts.length ? ", {\n" + opts.join(",\n") + "\n}" : "") + ");");
    if (expectsText(r)) {
      L.push("if (!res.ok) throw new Error(`HTTP ${res.status}`);");
      L.push("const csv = await res.text();");
      L.push("console.log(csv);");
    } else {
      L.push("const data = await res.json();");
      L.push("if (!res.ok) throw new Error(data.error.message); // {\"error\": {\"code\", \"message\", \"field\"}}");
      L.push("console.log(data);");
    }
    return L.join("\n");
  }

  function pyLit(v, ind) {
    var PAD = "    ";
    if (v === null || v === undefined) { return "None"; }
    if (v === true) { return "True"; }
    if (v === false) { return "False"; }
    if (typeof v === "number") { return String(v); }
    if (typeof v === "string") { return JSON.stringify(v); }
    var flat;
    if (Array.isArray(v)) {
      if (!v.length) { return "[]"; }
      flat = "[" + v.map(function (x) { return pyLit(x, ""); }).join(", ") + "]";
      if (flat.length + ind.length <= 84 && flat.indexOf("\n") < 0) { return flat; }
      return "[\n" + v.map(function (x) { return ind + PAD + pyLit(x, ind + PAD); }).join(",\n") + ",\n" + ind + "]";
    }
    var keys = Object.keys(v);
    if (!keys.length) { return "{}"; }
    flat = "{" + keys.map(function (k) { return JSON.stringify(k) + ": " + pyLit(v[k], ""); }).join(", ") + "}";
    if (flat.length + ind.length <= 84 && flat.indexOf("\n") < 0) { return flat; }
    return "{\n" + keys.map(function (k) { return ind + PAD + JSON.stringify(k) + ": " + pyLit(v[k], ind + PAD); }).join(",\n") + ",\n" + ind + "}";
  }

  function pyCode(r) {
    var L = [], vars = envVars(r);
    if (r.auth !== "none" || vars.length) { L.push("import os"); L.push(""); }
    L.push("import requests");
    L.push("");
    if (r.auth === "key") { L.push("# ключ — только на сервере: export JUSTMEDIT_API_KEY=" + KEY_PLACEHOLDER); }
    if (r.auth === "bearer") { L.push("# токен пользователя: export JUSTMEDIT_TOKEN=" + TOKEN_PLACEHOLDER + "  (POST /api/v1/auth/login → token)"); }
    L = L.concat(envComment(vars));
    var args = ["    " + JSON.stringify(fullUrl(r).split("?")[0])];
    if (r.auth === "key") { args.push("    headers={\"X-API-Key\": os.environ[\"JUSTMEDIT_API_KEY\"]}"); }
    if (r.auth === "bearer") { args.push("    headers={\"Authorization\": f\"Bearer {os.environ['JUSTMEDIT_TOKEN']}\"}"); }
    if (r.query.length) {
      var q = {};
      r.query.forEach(function (p) { q[p[0]] = p[1]; });
      args.push("    params=" + pyLit(q, "    "));
    }
    if (r.kind === "json") {
      args.push(r.codeJson !== undefined ? "    json=" + pyLit(r.codeJson, "    ").replace(/"__JM_ENV_([A-Z_]+)__"/g, "os.environ[\"$1\"]")
        : "    data=" + JSON.stringify(r.jsonText || "") + ".encode()");
    } else if (r.kind === "form") {
      args.push("    files={\"file\": open(" + JSON.stringify(r.fileName) + ", \"rb\")}");
      if (r.fields.length) {
        var d = {};
        r.fields.forEach(function (f) { d[f[0]] = f[1]; });
        args.push("    data=" + pyLit(d, "    "));
      }
    }
    args.push("    timeout=60");
    L.push("r = requests." + r.method + "(\n" + args.join(",\n") + ",\n)");
    if (expectsText(r)) {
      L.push("r.raise_for_status()");
      L.push("print(r.text)");
    } else {
      L.push("if not r.ok:");
      L.push("    raise RuntimeError(r.json()[\"error\"][\"message\"])  # {\"error\": {\"code\", \"message\", \"field\"}}");
      L.push("print(r.json())");
    }
    return L.join("\n");
  }

  var codeTimer = null;
  function renderCode() {
    if (codeTimer) { window.clearTimeout(codeTimer); }
    codeTimer = window.setTimeout(function () {
      codeTimer = null;
      if (!cur) { return; }
      var r = buildRequest(false);
      var text = lang === "js" ? jsCode(r) : (lang === "py" ? pyCode(r) : curlCode(r));
      $("code-out-code").textContent = text;
    }, 60);
  }

  function selectLang(next, focus) {
    lang = next;
    Array.prototype.forEach.call(document.querySelectorAll(".codebox__tabs [role=tab]"), function (t) {
      var on = t.getAttribute("data-lang") === next;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      if (on) { $("code-out").setAttribute("aria-labelledby", t.id); if (focus) { t.focus(); } }
    });
    renderCode();
  }

  // ---------------------------------------------------------------------------------------------
  // Справочник
  // ---------------------------------------------------------------------------------------------
  function requestExampleNode(o) {
    var content = bodyContent(o.op);
    if (content["application/json"]) {
      var ex = jsonExample(content["application/json"]);
      if (ex !== undefined) {
        var c = el("code");
        jsonInto(c, ex);
        var nodes = [el("pre", { class: "code code--small", tabindex: "0" }, [c])];
        if (content["multipart/form-data"]) { nodes.push(el("p", { class: "hint", text: "Или multipart/form-data с полем file." })); }
        return nodes;
      }
    }
    if (content["multipart/form-data"]) {
      var schema = resolve(content["multipart/form-data"].schema || {});
      var lines = ["multipart/form-data"];
      Object.keys(schema.properties || {}).forEach(function (n) {
        var p = resolve(schema.properties[n]);
        lines.push(n + "=" + (p.format === "binary" ? "@файл" : (p["default"] !== undefined ? p["default"] : "…")) +
          (isRequired(schema, n) ? "" : "   (необязательно)"));
      });
      return [el("pre", { class: "code code--small", tabindex: "0" }, [el("code", { text: lines.join("\n") })])];
    }
    return [el("p", { class: "op__none", text: "Без тела: только " + o.method.toUpperCase() + " " + o.path + "." })];
  }

  function opBody(o) {
    var params = (o.op.parameters || []).map(resolve).filter(function (p) { return p.in === "path" || p.in === "query"; });
    var meta = el("dl", { class: "op__meta" }, [
      el("dt", { text: "Кто может вызывать" }), el("dd", { text: o.op["x-role"] || "—" }),
      el("dt", { text: "Авторизация" }), el("dd", { text: authText(o.op) })
    ]);
    if (params.length) {
      meta.appendChild(el("dt", { text: "Параметры" }));
      meta.appendChild(el("dd", { text: params.map(function (p) {
        return p.name + (p.in === "path" ? " (в пути)" : " (в строке запроса)") + (p.description ? " — " + p.description : "");
      }).join("; ") }));
    }
    if (o.op["x-heavy"]) {
      meta.appendChild(el("dt", { text: "Лимиты" }));
      meta.appendChild(el("dd", { text: "тяжёлый: не больше 2 расчётов одновременно (503), лимит в минуту на ключ или токен (429)" }));
    }
    var ok = firstSuccess(o.op);
    var resNode;
    if (ok && ok.example !== undefined) {
      var c = el("code");
      exampleInto(c, ok.example);
      resNode = el("pre", { class: "code code--small", tabindex: "0" }, [c]);
    } else {
      resNode = el("p", { class: "op__none", text: ok ? ok.description : "—" });
    }
    var tryBtn = el("button", { type: "button", class: "btn btn--small", text: "Попробовать в консоли" });
    tryBtn.addEventListener("click", function () {
      selectOp(o.id);
      $("console").scrollIntoView({ block: "start" });
      $("c-route").focus({ preventScroll: true });
    });
    return [
      el("p", { class: "op__desc", text: o.op.description || "" }),
      meta,
      el("div", { class: "op__ex" }, [
        el("div", null, [el("p", { class: "op__ex-t", text: "Пример запроса" })].concat(requestExampleNode(o))),
        el("div", null, [el("p", { class: "op__ex-t", text: "Пример ответа · " + (ok ? ok.code : "") + (ok && ok.type ? " · " + ok.type : "") }), resNode])
      ]),
      tryBtn
    ];
  }

  function renderReference() {
    var box = $("ref-list");
    clear(box);
    var tags = {};
    (spec.tags || []).forEach(function (t) { tags[t.name] = t.description || ""; });
    GROUPS.concat(["Другое"]).forEach(function (g) {
      var list = ops.filter(function (o) { return o.group === g; });
      if (!list.length) { return; }
      var group = el("section", { class: "ref-group", "aria-label": g }, [
        el("div", { class: "ref-group__head" }, [el("h3", { class: "ref-group__t", text: g }),
          el("span", { class: "ref-group__n", text: list.length + " " + plural(list.length, "маршрут", "маршрута", "маршрутов") })]),
        tags[g] ? el("p", { class: "ref-group__d", text: tags[g] }) : null
      ]);
      var ul = el("div", { class: "ref-list" });
      list.forEach(function (o) {
        var body = el("div", { class: "op__body" });
        var det = el("details", { class: "op", id: "op-" + o.id }, [
          el("summary", null, [
            methodBadge(o.method),
            el("code", { class: "op__path", text: o.path }),
            el("span", { class: "op__sum", text: o.op.summary || "" }),
            el("span", { class: "op__role" }, [el("span", { text: o.op["x-role"] || "" })])
          ]),
          body
        ]);
        det.addEventListener("toggle", function () {     // примеры строятся при первом раскрытии
          if (det.open && !body.firstChild) { opBody(o).forEach(function (n) { body.appendChild(n); }); }
        });
        ul.appendChild(det);
      });
      group.appendChild(ul);
      box.appendChild(group);
    });
  }
  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) { return one; }
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) { return few; }
    return many;
  }

  // ---------------------------------------------------------------------------------------------
  // Копирование
  // ---------------------------------------------------------------------------------------------
  function copyText(text, btn) {
    var label = btn.textContent;
    function done(ok) {
      btn.textContent = ok ? "Скопировано" : "Выделите и скопируйте";
      announce(ok ? "Скопировано в буфер обмена" : "Не удалось скопировать");
      window.setTimeout(function () { btn.textContent = label; }, 1600);
    }
    function fallback() {
      var ta = el("textarea", { class: "vh", readonly: true, "aria-hidden": "true", tabindex: "-1" });
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(ta);
      done(ok);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
    } else {
      fallback();
    }
  }

  // ---------------------------------------------------------------------------------------------
  // Запуск
  // ---------------------------------------------------------------------------------------------
  function init(data) {
    spec = data;
    ops = [];
    Object.keys(spec.paths || {}).forEach(function (path) {
      METHODS.forEach(function (m) {
        var op = spec.paths[path][m];
        if (!op) { return; }
        var group = (op.tags && op.tags[0] && GROUPS.indexOf(op.tags[0]) >= 0) ? op.tags[0] : "Другое";
        var o = { id: op.operationId || (m + " " + path), method: m, path: path, op: op, group: group };
        ops.push(o);
        byId[o.id] = o;
      });
    });
    fillRoutes();
    renderReference();
    $("c-send").disabled = false;
    selectOp(byId.analyze ? "analyze" : ops[0].id);
  }

  function wire() {
    $("c-route").addEventListener("change", function () { selectOp($("c-route").value); });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="c-auth"]'), function (r) {
      r.addEventListener("change", function () { updateAuthUI(); renderCode(); });
    });
    $("c-secret").addEventListener("input", function () {
      var mode = authMode();
      if (mode === "key" || mode === "bearer") { secrets[mode] = $("c-secret").value; }
    });
    Array.prototype.forEach.call(document.querySelectorAll('input[name="c-ctype"]'), function (r) {
      r.addEventListener("change", function () { switchBody(); renderCode(); });
    });
    $("c-body").addEventListener("input", function () {
      if ($("c-body").getAttribute("aria-invalid") === "true") {
        try { JSON.parse($("c-body").value); markBody(null); } catch (e) { /* ошибка останется до исправления */ }
      }
      renderCode();
    });
    $("c-body").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
    });
    $("c-reset").addEventListener("click", function () { if (cur) { $("c-body").value = exampleText(cur); markBody(null); renderCode(); } });
    $("c-send").addEventListener("click", send);
    $("c-go-login").addEventListener("click", function () { if (byId.authLogin) { selectOp("authLogin", { focus: true }); } });
    $("c-use-token").addEventListener("click", function () {
      if (!lastToken) { return; }
      secrets.bearer = lastToken;
      setAuthMode("bearer");
      renderCode();
      announce("Токен подставлен в авторизацию консоли");
      $("c-use-token").textContent = "Токен подставлен";
      window.setTimeout(function () { $("c-use-token").textContent = "Использовать токен"; }, 1600);
    });

    var tabs = Array.prototype.slice.call(document.querySelectorAll(".codebox__tabs [role=tab]"));
    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () { selectLang(t.getAttribute("data-lang")); });
      t.addEventListener("keydown", function (e) {
        var j = e.key === "ArrowRight" ? i + 1 : (e.key === "ArrowLeft" ? i - 1 : (e.key === "Home" ? 0 : (e.key === "End" ? tabs.length - 1 : null)));
        if (j === null) { return; }
        e.preventDefault();
        j = (j + tabs.length) % tabs.length;
        selectLang(tabs[j].getAttribute("data-lang"), true);
      });
    });

    Array.prototype.forEach.call(document.querySelectorAll("button.copy[data-copy]"), function (b) {
      b.addEventListener("click", function () {
        var target = $(b.getAttribute("data-copy"));
        if (target) { copyText(target.textContent, b); }
      });
    });

    // Статичные примеры (встраивание, кабинет) — с адресом этого сервера вместо https://justmedit.example
    if (ORIGIN) {
      Array.prototype.forEach.call(document.querySelectorAll("code[data-origin]"), function (c) {
        c.textContent = c.textContent.split(PLACEHOLDER_ORIGIN).join(ORIGIN);
      });
    }
  }

  function start() {
    wire();
    fetch("/api/v1/openapi.json", { credentials: "omit", cache: "no-cache" }).then(function (res) {
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      return res.json();
    }).then(init).catch(function () {
      var a = $("dev-load");
      a.textContent = "Не удалось загрузить описание API (/api/v1/openapi.json). Обновите страницу.";
      a.hidden = false;
      $("c-route").disabled = true;
      $("code-out-code").textContent = "Описание API не загрузилось.";
      $("ref-list").textContent = "Описание API не загрузилось.";
    });
  }

  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", start); } else { start(); }
}());
