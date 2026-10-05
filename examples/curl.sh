#!/usr/bin/env bash
# Примеры запросов к API DeficitLens через curl. Заодно это быстрая проверка: скрипт завершается с кодом 1,
# если какой-то ответ пришёл не с тем кодом HTTP, который ожидается.
#
#   export DL_API_KEY=dl-demo-key                 # ключ API (в docker-compose.yml по умолчанию dl-demo-key)
#   export DL_BASE_URL=http://127.0.0.1:8080      # необязательно; это значение по умолчанию
#   bash examples/curl.sh [файл.csv|файл.xlsx]    # файл в схеме кейса для шага 6; без него берётся пример из трёх строк
#
# Все значения анализов в запросах придуманы. Персональных данных в запросах нет и быть не должно.
set -u

BASE="${DL_BASE_URL:-http://127.0.0.1:8080}"
KEY="${DL_API_KEY:?Задайте ключ: export DL_API_KEY=<ключ>}"
FILE="${1:-}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
FAILED=0

# Один анализ: женщина 34 лет, гемоглобин в норме, RDW повышен (пример 1 из демо).
ANALYZE='{
  "sex": "F",
  "age_years": 34,
  "values": {
    "hemoglobin": 124, "RBC": 4.59, "hematocrit": 37.7, "MCV": 82, "MCH": 27.0,
    "MCHC": 329, "RDW": 15.2, "platelets": 310, "WBC": 6.1
  }
}'

# Пакет: три записи. Вторая — с единицей измерения (г/дл пересчитается в г/л), третья — с ошибкой (возраст 12 лет).
BATCH='{
  "with_reports": false,
  "records": [
    {"patient_ref": "row-1", "sex": "F", "age_years": 34, "values": {"hemoglobin": 124, "MCV": 82, "RDW": 15.2}},
    {"patient_ref": "row-2", "sex": "M", "age_years": 61,
     "values": {"hemoglobin": {"value": 10.4, "unit": "g/dL"}, "MCV": 79, "ferritin": 62, "CRP": 28}},
    {"patient_ref": "row-3", "sex": "F", "age_years": 12, "values": {"hemoglobin": 118}}
  ]
}'

# check <название> <ожидаемый код> <аргументы curl...>
check() {
  local title="$1" want="$2"; shift 2
  local code
  code="$(curl -sS -o "$TMP/body" -w '%{http_code}' "$@")" || code="000"
  if [ "$code" = "$want" ]; then
    echo "PASS  $title — HTTP $code"
  else
    echo "FAIL  $title — HTTP $code, ожидался $want"
    FAILED=1
  fi
  head -c 600 "$TMP/body"; echo; echo
}

echo "Сервис: $BASE"; echo

# 1. Проверка живости. Ключ не нужен.
check "1. GET /healthz" 200 "$BASE/healthz"

# 2. Запрос без ключа — отказ 401.
check "2. POST /api/v1/analyze без ключа" 401 \
  -X POST "$BASE/api/v1/analyze" -H 'Content-Type: application/json' -d "$ANALYZE"

# 3. Запрос с неверным ключом — отказ 403.
check "3. POST /api/v1/analyze с неверным ключом" 403 \
  -X POST "$BASE/api/v1/analyze" -H 'Content-Type: application/json' -H 'X-API-Key: wrong-key' -d "$ANALYZE"

# 4. Запрос с ключом — полный ответ: уровень 1, уровень 2, скрытый дефицит, следующий анализ, два отчёта.
check "4. POST /api/v1/analyze с ключом" 200 \
  -X POST "$BASE/api/v1/analyze" -H 'Content-Type: application/json' -H "X-API-Key: $KEY" -d "$ANALYZE"

# 5. Пакет записей: у каждой статус done или rejected; сводка — в поле summary.
check "5. POST /api/v1/batch" 200 \
  -X POST "$BASE/api/v1/batch" -H 'Content-Type: application/json' -H "X-API-Key: $KEY" -d "$BATCH"

# 6. Файл в схеме кейса: CSV или XLSX -> предсказания и сводка (если в файле есть метки — ещё и метрики).
if [ -z "$FILE" ]; then
  FILE="$TMP/example.csv"
  cat > "$FILE" <<'CSV'
patient_id,sex,age_years,hemoglobin,RBC,hematocrit,MCV,MCH,MCHC,RDW,platelets,WBC,ferritin,CRP
demo-1,F,34,124,4.59,37.7,82,27.0,329,15.2,310,6.1,,
demo-2,M,61,104,4.10,32.4,79,25.4,321,16.0,340,8.9,62,28
demo-3,F,58,96,3.35,30.5,91,28.7,315,19.5,280,5.2,8,
CSV
fi
check "6. POST /api/v1/benchmark (JSON: summary, preview, csv)" 200 \
  -X POST "$BASE/api/v1/benchmark" -H "X-API-Key: $KEY" -F "file=@$FILE" -F "mode=auto"

# 7. Тот же файл, ответ — готовый CSV.
check "7. POST /api/v1/benchmark?format=csv" 200 \
  -X POST "$BASE/api/v1/benchmark?format=csv" -H "X-API-Key: $KEY" -F "file=@$FILE" -F "mode=clinical"

# 8. Справочник: показатели и единицы, пороги с источниками, цены, версии.
check "8. GET /api/v1/reference" 200 "$BASE/api/v1/reference" -H "X-API-Key: $KEY"

if [ "$FAILED" = "0" ]; then echo "Итог: все ответы совпали с ожидаемыми."; else echo "Итог: есть расхождения (FAIL)."; fi
exit "$FAILED"
