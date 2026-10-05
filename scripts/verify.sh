#!/usr/bin/env bash
# Полный чистый прогон DeficitLens БЕЗ Docker:
#   1) тесты; 2) загрузка моделей и версия scikit-learn; 3) docs/norms.md соответствует конфигурации;
#   4) запуск сервера на свободном порту; 5) запросы: healthz, analyze без ключа (401), с неверным ключом (403),
#      с ключом (200), batch, benchmark с файлом, ошибка ввода (422); 6) в журнале сервера нет значений анализов;
#   7) остановка сервера.
# Печатает таблицу PASS / FAIL / SKIP. Код возврата 1, если есть хотя бы один FAIL.
#
# Запуск из корня репозитория:  uv run bash scripts/verify.sh   (или make verify, или bash scripts/verify.sh)
# Интерпретатор: переменная PYTHON; иначе $ROOT/.venv/bin/python, если окружение создано (uv sync --extra dev);
# иначе python3 из PATH. Без зависимостей сервиса скрипт сразу говорит, что поставить, а не падает на тестах.
# Файл кейса нужен только части тестов и шагу benchmark «на файле кейса»; без него эти проверки получают SKIP.
# Положить файл:  data/case/deficiency_anemia.csv  или  export DL_CASE_DATA=/абсолютный/путь/deficiency_anemia.csv
# Причины SKIP печатаются те, что назвали сами тесты (нет файла кейса, NHANES, tesseract и т. п.).
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
export PYTHONPATH="$ROOT/src"
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi
TMP="$(mktemp -d)"
SERVER_PID=""
RESULTS="$TMP/results.tsv"
: > "$RESULTS"

cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

record() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$RESULTS"; printf '%-5s %s — %s\n' "$1" "$2" "$3"; }

echo "== DeficitLens: проверка без Docker =="
echo "Python: $PY ($($PY --version 2>&1)); каталог: $ROOT"
echo

# ---------------------------------------------------------------------------------------------
# 0. Зависимости: без них каждый тест упал бы с ImportError — вместо сотни FAIL одна понятная строка
# ---------------------------------------------------------------------------------------------
if ! $PY -c "import yaml, sklearn, starlette, jinja2" > "$TMP/deps.log" 2>&1; then
  record FAIL "зависимости" "у $PY нет пакетов сервиса ($(tail -1 "$TMP/deps.log")); установите зависимости: uv sync --extra dev — и запустите uv run bash scripts/verify.sh (или задайте PYTHON=/путь/к/python)"
  echo
  echo "== Итог: PASS 0, FAIL 1, SKIP 0 =="
  exit 1
fi

# ---------------------------------------------------------------------------------------------
# 1. Тесты
# ---------------------------------------------------------------------------------------------
echo "-- 1. Тесты (scripts/run_tests.py)"
$PY scripts/run_tests.py > "$TMP/tests.log" 2>&1
TEST_RC=$?
SUMMARY="$(grep -E '^Итог:' "$TMP/tests.log" | tail -1)"
if [ "$TEST_RC" = "0" ]; then
  record PASS "тесты" "${SUMMARY:-завершились без ошибок}"
else
  record FAIL "тесты" "${SUMMARY:-run_tests.py завершился с ошибкой}"
  grep -E '^FAIL' "$TMP/tests.log" | head -20
fi
if grep -q '^SKIP' "$TMP/tests.log"; then
  # Причины — из самого журнала тестов («SKIP  путь::тест — причина»), сгруппированные с числом тестов
  SKIP_REASONS="$($PY - "$TMP/tests.log" <<'PYEOF'
import collections, sys
reasons = collections.Counter()
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    if line.startswith("SKIP"):
        reasons[line.split(" — ", 1)[1].strip() if " — " in line else "причина не указана"] += 1
print("; ".join(f"{r} — {n}" for r, n in reasons.most_common()))
PYEOF
)"
  record SKIP "тесты, которым нужны данные или программы" "$(grep -c '^SKIP' "$TMP/tests.log") пропущено: $SKIP_REASONS"
fi

# ---------------------------------------------------------------------------------------------
# 2. Модели
# ---------------------------------------------------------------------------------------------
echo "-- 2. Модели"
$PY - > "$TMP/models.log" 2>&1 <<'PYEOF'
import json, sys
from pathlib import Path
import sklearn
from deficitlens_core.config import models_dir
from deficitlens_core.engine import load_models
m = load_models()
ok = m.case is not None and m.screen is not None
print(f"case: {getattr(m.case, 'version', None)}; screen: {getattr(m.screen, 'version', None)}")
for name in ("case-v1", "screen-v1"):
    meta = json.loads((Path(models_dir()) / name / "metadata.json").read_text(encoding="utf-8"))
    trained = meta.get("versions", {}).get("scikit-learn")
    same = trained == sklearn.__version__
    print(f"{name}: обучена на scikit-learn {trained}; установлена {sklearn.__version__}; {'совпадает' if same else 'НЕ СОВПАДАЕТ'}")
    ok = ok and same
sys.exit(0 if ok else 1)
PYEOF
if [ $? = 0 ]; then
  record PASS "модели" "$(head -1 "$TMP/models.log")"
else
  record FAIL "модели" "не загружены или версия scikit-learn другая: $(tr '\n' ' ' < "$TMP/models.log" | tail -c 300)"
fi

# ---------------------------------------------------------------------------------------------
# 3. Документ порогов соответствует конфигурации
# ---------------------------------------------------------------------------------------------
echo "-- 3. docs/norms.md"
if $PY scripts/gen_norms_doc.py --check > "$TMP/norms.log" 2>&1; then
  record PASS "docs/norms.md" "соответствует config/norms_ru.yaml"
else
  record FAIL "docs/norms.md" "устарел: запустите python3 scripts/gen_norms_doc.py"
fi

# ---------------------------------------------------------------------------------------------
# 4. Сервер
# ---------------------------------------------------------------------------------------------
echo "-- 4. Сервер"
if ! $PY -c "import deficitlens_api.app" > "$TMP/import.log" 2>&1; then
  record FAIL "импорт deficitlens_api.app" "$(tail -1 "$TMP/import.log")"
  record SKIP "запросы к серверу" "сервер не запущен"
else
  PORT="$($PY -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  KEY="verify-$($PY -c 'import secrets; print(secrets.token_hex(8))')"
  DL_API_KEYS="$KEY" $PY -m uvicorn deficitlens_api.app:app --host 127.0.0.1 --port "$PORT" --no-access-log --no-server-header --no-proxy-headers \
    > "$TMP/server.log" 2>&1 &
  SERVER_PID=$!
  UP=0
  for _ in $(seq 1 60); do
    if $PY -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=2)" 2>/dev/null; then
      UP=1; break
    fi
    kill -0 "$SERVER_PID" 2>/dev/null || break
    sleep 0.5
  done
  if [ "$UP" != "1" ]; then
    record FAIL "запуск сервера" "не ответил на /healthz за 30 с: $(tail -3 "$TMP/server.log" | tr '\n' ' ')"
    record SKIP "запросы к серверу" "сервер не запущен"
  else
    record PASS "запуск сервера" "порт $PORT, ключ задан через DL_API_KEYS"

    # -----------------------------------------------------------------------------------------
    # 5–6. Запросы (стандартная библиотека Python, без curl и requests)
    # -----------------------------------------------------------------------------------------
    echo "-- 5. Запросы"
    CASE_FILE="${DL_CASE_DATA:-$ROOT/data/case/deficiency_anemia.csv}"
    BASE="http://127.0.0.1:$PORT" KEY="$KEY" CASE_FILE="$CASE_FILE" SERVER_LOG="$TMP/server.log" \
      $PY - >> "$RESULTS.http" 2> "$TMP/http.err" <<'PYEOF'
import json, os, time, urllib.error, urllib.request, uuid
from pathlib import Path

BASE, KEY = os.environ["BASE"], os.environ["KEY"]
CANARY_REF = "CANARY-PATIENT-7391"      # метка строки, которой не должно быть в журнале
CANARY_VALUE = 123.4567                 # значение гемоглобина, которого не должно быть в журнале


def call(method, path, body=None, key=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def multipart(fields, filename, content):
    b = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f'Content-Type: text/csv\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={b}"


def out(status, name, detail):
    print(f"{status}\t{name}\t{detail}")


def js(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {}


CBC = {"hemoglobin": 124, "RBC": 4.59, "hematocrit": 37.7, "MCV": 82, "MCH": 27.0, "MCHC": 329, "RDW": 15.2,
       "platelets": 310, "WBC": 6.1}
ONE = {"sex": "F", "age_years": 34, "values": CBC}

s, raw, h = call("GET", "/healthz")
d = js(raw)
ok = s == 200 and d.get("status") == "ok"
out("PASS" if ok else "FAIL", "GET /healthz", f"HTTP {s}; движок {d.get('engine')}, модели {d.get('case_model')} и "
    f"{d.get('screen_model')}, пороги {d.get('norms')}")

s, raw, _ = call("POST", "/api/v1/analyze", ONE)
out("PASS" if s == 401 else "FAIL", "analyze без ключа", f"HTTP {s} (ожидался 401)")

s, raw, _ = call("POST", "/api/v1/analyze", ONE, key="wrong-" + KEY)
out("PASS" if s == 403 else "FAIL", "analyze с неверным ключом", f"HTTP {s} (ожидался 403)")

t = time.perf_counter()
s, raw, h = call("POST", "/api/v1/analyze", ONE, key=KEY)
ms = 1000 * (time.perf_counter() - t)
d = js(raw)
scr = (d.get("hidden_deficiency") or {}).get("screening") or {}
nxt = [x.get("analyte") for x in d.get("next_tests", [])]
ok = (s == 200 and d.get("level1", {}).get("anemia") is False and "reports" in d
      and scr.get("risk") in ("elevated", "high") and nxt[:1] == ["ferritin"])
out("PASS" if ok else "FAIL", "analyze с ключом (пример 1)",
    f"HTTP {s}; анемии нет; риск по скринингу: {scr.get('risk_ru')}; первым предложен: {nxt[:1]}; {ms:.0f} мс")

s, raw, _ = call("POST", "/api/v1/analyze", {"sex": "F", "age_years": 12, "values": {"hemoglobin": 118}}, key=KEY)
d = js(raw)
ok = s == 422 and "error" in d and "message" in d["error"]
out("PASS" if ok else "FAIL", "ошибка ввода (возраст 12 лет)", f"HTTP {s} (ожидался 422); код: {d.get('error', {}).get('code')}")

batch = {"with_reports": False, "records": [
    {"patient_ref": CANARY_REF, "sex": "F", "age_years": 40, "values": {"hemoglobin": CANARY_VALUE, "MCV": 88}},
    {"patient_ref": "row-2", "sex": "M", "age_years": 61, "values": {"hemoglobin": 104, "MCV": 79, "ferritin": 62, "CRP": 28}},
    {"patient_ref": "row-3", "sex": "F", "age_years": 40, "values": {"MCV": 88}},
]}
s, raw, _ = call("POST", "/api/v1/batch", batch, key=KEY)
d = js(raw)
sm = d.get("summary", {})
ok = s == 200 and sm.get("total") == 3 and sm.get("done") == 2 and sm.get("rejected") == 1
out("PASS" if ok else "FAIL", "POST /api/v1/batch", f"HTTP {s}; всего {sm.get('total')}, посчитано {sm.get('done')}, "
    f"отклонено {sm.get('rejected')} (ожидалось 3 / 2 / 1)")

small = ("patient_id,sex,age_years,hemoglobin,RBC,hematocrit,MCV,MCH,MCHC,RDW,platelets,WBC,ferritin,CRP\n"
         "demo-1,F,34,124,4.59,37.7,82,27.0,329,15.2,310,6.1,,\n"
         "demo-2,M,61,104,4.10,32.4,79,25.4,321,16.0,340,8.9,62,28\n"
         "demo-3,F,58,96,3.35,30.5,91,28.7,315,19.5,280,5.2,8,\n").encode()
body, ctype = multipart({"mode": "auto"}, "example.csv", small)
s, raw, _ = call("POST", "/api/v1/benchmark", raw=body, ctype=ctype, key=KEY)
d = js(raw)
sm = d.get("summary", {})
ok = s == 200 and sm.get("rows_total") == 3 and sm.get("rows_done") == 3 and len(d.get("preview", [])) == 3 and "csv" in d
out("PASS" if ok else "FAIL", "benchmark: файл из трёх придуманных строк",
    f"HTTP {s}; строк {sm.get('rows_total')}, принято {sm.get('rows_done')}, режим {sm.get('mode')}")

s, raw, h = call("POST", "/api/v1/benchmark?format=csv", raw=body, ctype=ctype, key=KEY)
ok = s == 200 and raw.decode("utf-8-sig", "replace").lstrip('"').startswith("patient_id")  # ячейки в кавычках
out("PASS" if ok else "FAIL", "benchmark?format=csv", f"HTTP {s}; тип ответа: {h.get('content-type') or h.get('Content-Type')}")

s, raw, _ = call("POST", "/api/v1/benchmark", raw=body, ctype=ctype)
out("PASS" if s == 401 else "FAIL", "benchmark без ключа", f"HTTP {s} (ожидался 401)")

case = Path(os.environ.get("CASE_FILE", ""))
if case.is_file():
    body2, ctype2 = multipart({"mode": "auto"}, "deficiency_anemia.csv", case.read_bytes())
    t = time.perf_counter()
    s, raw, _ = call("POST", "/api/v1/benchmark", raw=body2, ctype=ctype2, key=KEY)
    sec = time.perf_counter() - t
    d = js(raw)
    sm = d.get("summary", {})
    m = sm.get("metrics") or {}
    l1 = m.get("level1", {})
    ok = (s == 200 and sm.get("rows_rejected") == 0 and l1.get("match") == sm.get("rows_done")
          and m.get("same_as_training_data") is True)
    out("PASS" if ok else "FAIL", "benchmark: файл кейса",
        f"HTTP {s}; строк {sm.get('rows_total')}, режим {sm.get('mode')}, уровень 1 совпал в {l1.get('match')} строках, "
        f"пометка «файл совпадает с обучающим»: {m.get('same_as_training_data')}, {sec:.2f} с")
else:
    out("SKIP", "benchmark: файл кейса", "нет data/case/deficiency_anemia.csv (или DL_CASE_DATA)")

s, raw, _ = call("GET", "/api/v1/reference", key=KEY)
d = js(raw)
ok = s == 200 and len(d.get("analytes", [])) > 0 and len(d.get("thresholds", [])) > 0
out("PASS" if ok else "FAIL", "GET /api/v1/reference", f"HTTP {s}; показателей {len(d.get('analytes', []))}, "
    f"записей порогов {len(d.get('thresholds', []))}")

s, raw, h = call("GET", "/")
hl = {k.lower(): v for k, v in h.items()}
ok = s == 200 and "content-security-policy" in hl and hl.get("x-content-type-options") == "nosniff"
out("PASS" if ok else "FAIL", "страница демо и заголовки безопасности",
    f"HTTP {s}; CSP: {'есть' if 'content-security-policy' in hl else 'нет'}; nosniff: {hl.get('x-content-type-options')}")

time.sleep(0.3)
log = Path(os.environ["SERVER_LOG"]).read_text(encoding="utf-8", errors="replace")
leaks = [x for x in (CANARY_REF, "123.4567", "123,4567", KEY) if x in log]
lines = [ln for ln in log.splitlines() if "/api/v1/batch" in ln]
if leaks:
    out("FAIL", "журнал сервера без значений анализов, меток и ключа", f"в журнале найдено: {len(leaks)} из 4 проверочных строк")
elif not lines:
    out("SKIP", "журнал сервера без значений анализов, меток и ключа", "строк о запросах в журнале нет: проверять нечего")
else:
    out("PASS", "журнал сервера без значений анализов, меток и ключа",
        f"запрос с проверочными значениями в журнале есть ({len(lines)} строка), самих значений, метки и ключа нет")
PYEOF
    HTTP_RC=$?
    if [ -s "$RESULTS.http" ]; then
      while IFS=$'\t' read -r st name detail; do record "$st" "$name" "$detail"; done < "$RESULTS.http"
    fi
    if [ "$HTTP_RC" != "0" ]; then
      record FAIL "скрипт запросов" "$(tail -2 "$TMP/http.err" | tr '\n' ' ')"
    fi

    # -----------------------------------------------------------------------------------------
    # 7. Остановка
    # -----------------------------------------------------------------------------------------
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      record FAIL "остановка сервера" "процесс не завершился"
    else
      record PASS "остановка сервера" "процесс завершён"
    fi
    SERVER_PID=""
  fi
fi

# ---------------------------------------------------------------------------------------------
# Итог
# ---------------------------------------------------------------------------------------------
N_PASS="$(grep -c '^PASS' "$RESULTS")"; N_FAIL="$(grep -c '^FAIL' "$RESULTS")"; N_SKIP="$(grep -c '^SKIP' "$RESULTS")"
echo
echo "== Итог: PASS $N_PASS, FAIL $N_FAIL, SKIP $N_SKIP =="
echo "Docker этим скриптом не проверяется. Сборку образа проверьте отдельно: docker compose up --build"
# Подсказки — только по строкам SKIP (в строках PASS тоже бывает «файл кейса»)
grep '^SKIP' "$RESULTS" > "$TMP/skips.tsv"
if grep -q 'файла кейса\|файл кейса\|deficiency_anemia' "$TMP/skips.tsv"; then
  echo "Чтобы убрать SKIP по файлу кейса, положите его в data/case/deficiency_anemia.csv или задайте DL_CASE_DATA=/абсолютный/путь"
fi
if grep -q 'NHANES' "$TMP/skips.tsv"; then
  echo "Файл NHANES: data/nhanes/nhanes_deficiency_real.csv или DL_NHANES_DATA=/абсолютный/путь"
fi
if grep -q 'tesseract' "$TMP/skips.tsv"; then
  echo "Распознавание фото (tesseract) проверяется в контейнере: docker compose run --rm verify"
fi
[ "$N_FAIL" = "0" ]
