// Форма ОАК → POST /api/report (свой сервер клиники) → отчёт пациенту.
// Ключа API здесь нет: его добавляет сервер клиники. Ответ выводится только через textContent, без innerHTML.
const NUMERIC = ["age_years", "hemoglobin", "RBC", "hematocrit", "MCV", "MCH", "MCHC", "RDW", "platelets", "WBC", "ferritin"];
// Придуманный пример (не данные пациента).
const EXAMPLE = { sex: "F", age_years: "34", hemoglobin: "104", RBC: "4,21", hematocrit: "33", MCV: "78", MCH: "24,7",
  MCHC: "315", RDW: "16,1", platelets: "310", WBC: "6,1" };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}

function show(out, nodes) {
  out.replaceChildren(...nodes);
}

function collect(form) {
  const fd = new FormData(form);
  const body = { sex: fd.get("sex") || null, pregnant: fd.get("pregnant") === "on" };
  for (const name of NUMERIC) {
    const raw = String(fd.get(name) || "").trim().replace(",", ".");
    if (raw === "") continue;
    const v = Number(raw);
    if (!Number.isFinite(v)) return { error: `Поле «${name}»: введите число` };
    body[name] = v;
  }
  if (!body.sex) return { error: "Выберите пол" };
  if (body.age_years === undefined) return { error: "Укажите возраст" };
  if (body.hemoglobin === undefined) return { error: "Укажите гемоглобин" };
  return { body };
}

// Отчёт пациенту DeficitLens: { headline, sections: [{ title, lines: [строка] }] }.
function renderReport(p) {
  const nodes = [el("h2", null, "Ваш анализ крови")];
  if (p.headline) nodes.push(el("p", "main", p.headline));
  for (const s of p.sections || []) {
    const sec = el("section", "rsec");
    sec.append(el("h3", null, s.title || ""));
    const ul = el("ul");
    for (const line of s.lines || []) ul.append(el("li", null, line));
    sec.append(ul);
    nodes.push(sec);
  }
  nodes.push(el("p", "muted", "Это не диагноз. Решение принимает врач."));
  return nodes;
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("cbc-form");
  const out = document.getElementById("out");
  const btn = document.getElementById("submit");

  document.getElementById("fill-example").addEventListener("click", () => {
    for (const [k, v] of Object.entries(EXAMPLE)) form.elements[k].value = v;
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const { body, error } = collect(form);
    if (error) {
      show(out, [el("p", "alert", error)]);
      return;
    }
    btn.disabled = true;
    show(out, [el("p", "muted", "Расшифровываем…")]);
    try {
      const r = await fetch("/api/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.patient) show(out, renderReport(data.patient));
      else show(out, [el("p", "alert", data.error || `Ошибка ${r.status}`)]);
    } catch {
      show(out, [el("p", "alert", "Нет связи с сервером клиники")]);
    } finally {
      btn.disabled = false;
    }
  });
});
