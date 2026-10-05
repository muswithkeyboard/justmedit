# Примеры интеграции

Четыре примера работы с API Justmedit и сценарий с токеном пользователя. Описание маршрутов и форматов —
`docs/integration.md`; консоль запросов и справочник — страница `/developers`; машиночитаемое описание —
`GET /api/v1/openapi.json` (OpenAPI 3.0).

Все значения анализов в примерах придуманы. Персональных данных в примерах нет.

## Что нужно

1. Запущенный сервис. По умолчанию — `http://127.0.0.1:8080`.
2. Ключ API в переменной окружения `DL_API_KEY`.

```bash
export DL_API_KEY=dl-demo-key                 # демо-ключ из docker-compose.yml; для своего запуска — свой ключ
export DL_BASE_URL=http://127.0.0.1:8080      # необязательно: это значение по умолчанию
```

Откуда взять ключ: это одно из значений переменной `DL_API_KEYS` на стороне сервиса. Если её не задавали,
сервис при старте печатает строку `DL_API_KEY=<ключ>`.

Для примеров на Python нужна библиотека `requests`: `pip install requests`. В образ сервиса она не входит.

## `curl.sh` — все маршруты через curl

```bash
bash examples/curl.sh                  # файл для шага 6 создаётся сам: три придуманные строки
bash examples/curl.sh my_file.csv      # или свой файл в схеме кейса
```

Что делает по шагам:

| Шаг | Запрос | Ожидаемый ответ |
|---|---|---|
| 1 | `GET /healthz` | 200, состояние и версии |
| 2 | `POST /api/v1/analyze` без ключа | 401 |
| 3 | `POST /api/v1/analyze` с неверным ключом | 403 |
| 4 | `POST /api/v1/analyze` с ключом | 200, полный ответ |
| 5 | `POST /api/v1/batch` — три записи, одна с ошибкой | 200, сводка «посчитано 2, отклонена 1» |
| 6 | `POST /api/v1/benchmark` с файлом | 200, сводка, первые строки, CSV |
| 7 | `POST /api/v1/benchmark?format=csv` | 200, готовый CSV |
| 8 | `GET /api/v1/reference` | 200, показатели, пороги, цены |

Скрипт печатает `PASS` или `FAIL` для каждого шага и начало ответа. Код возврата 1, если хотя бы один ответ
пришёл не с тем кодом. Поэтому скрипт годится и как быстрая проверка после развёртывания.

## `python_client.py` — клиент на Python

```bash
python3 examples/python_client.py                 # шаги 1–6
python3 examples/python_client.py my_file.csv     # и шаг 7: файл в схеме кейса
```

В файле класс `DeficitLensClient` с методами `health`, `analyze`, `batch`, `benchmark`, `parse`, `reference`.
Ошибка сервиса превращается в исключение `DeficitLensError` с полями `status`, `code`, `message`, `field`.

Пример использования из своего кода (каталог `examples/` должен быть в пути поиска модулей):

```python
from python_client import DeficitLensClient

client = DeficitLensClient("http://127.0.0.1:8080", api_key="dl-demo-key")
res = client.analyze("F", 34, {"hemoglobin": 124, "MCV": 82, "RDW": 15.2})
print(res["level1"]["anemia"], res["hidden_deficiency"]["summary_ru"])
for test in res["next_tests"]:
    print(test["name_ru"], "—", test["reason"])
```

## `lis_batch.py` — выгрузка ЛИС пакетами

```bash
python3 examples/lis_batch.py --in lis_export.csv --out result.csv \
    --id-column "Номер заказа" --map "Гемоглобин=hemoglobin" --map "Ферритин=ferritin"
```

Вход — CSV с заголовком. Разделитель — запятая, точка с запятой или табуляция. Кодировка utf-8 или cp1251.
Десятичная запятая допускается. Обязательные столбцы: пол (`sex`, `пол`, `gender`) и возраст
(`age_years`, `age`, `возраст`). Столбцы анализов называются кодами показателей или сопоставляются через `--map`.
Значения — в канонических единицах.

Выход — CSV: номер строки, локальный идентификатор, статус, анемия по ВОЗ, тяжесть, группа, класс, причина,
уверенность, сигнал скрытого дефицита, риск по скринингу, рекомендован ли ферритин, флаги, что досдать,
текст ошибки. На экран печатается сводка: сколько строк посчитано, сколько с анемией, скольким рекомендован ферритин.

Как сделано с персональными данными:

- на сервер уходят только пол, возраст и значения показателей из белого списка;
- остальные столбцы (ФИО, дата рождения, полис, телефон) не читаются и не передаются;
- идентификатор из `--id-column` на сервер не уходит: вместо него идёт номер строки. В выходной файл
  идентификатор копируется на вашей машине;
- значения анализов в выходной файл и на экран не выводятся;
- строка с нечисловым значением отклоняется на месте, значение в сообщение об ошибке не попадает.

Параметр `--chunk` задаёт число записей в одном запросе (по умолчанию 500). Сервис принимает до 10 000 записей
и до 10 МБ в одном запросе.

## `clinic_site/` — мини-сайт клиники

Пациент заполняет форму общего анализа крови на сайте клиники и получает отчёт простыми словами.
Браузер обращается только к серверу клиники; сервер клиники добавляет ключ `X-API-Key`, вызывает
`POST /api/v1/analyze` и отдаёт странице только `reports.patient`. Ключ в браузер не попадает.

```bash
docker compose --profile clinic up --build        # сервис на 8080 и сайт клиники на http://127.0.0.1:8081
make clinic                                       # без Docker, рядом с `make run`
```

Starlette и стандартная библиотека (`urllib`), без `requests`. Подробно — `clinic_site/README.md`.
Тесты — `tests/examples/test_clinic_site.py`.

## Токен пользователя (Bearer): кабинет и свой сайт

Ключ `X-API-Key` — только «сервер к серверу»: он не привязан к человеку, и в коде страницы его увидит любой
посетитель. Для браузера, мобильного приложения и чужого сайта нужен токен пользователя: он приходит в ответе
входа, принадлежит одному человеку и отзывается в кабинете. Токен передаётся заголовком
`Authorization: Bearer <токен>`; маршруты анализа (`/api/v1/analyze` и другие) принимают и его, и ключ.

```bash
BASE=${DL_BASE_URL:-http://127.0.0.1:8080}

# 1. Регистрация пациента (логин — не ФИО и не почта; без согласия consent — 422)
curl -sS -X POST "$BASE/api/v1/auth/register" -H "Content-Type: application/json" \
  -d '{"login": "anna.demo", "password": "придумайте-длинный-пароль", "role": "patient", "consent": true}'

# 2. Вход: токен — в переменную окружения, не в файл и не в адрес страницы
export JUSTMEDIT_TOKEN=$(curl -sS -X POST "$BASE/api/v1/auth/login" -H "Content-Type: application/json" \
  -d '{"login": "anna.demo", "password": "придумайте-длинный-пароль"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')

# 3. Расшифровка с токеном вместо ключа — тот же ответ, что с X-API-Key
curl -sS -X POST "$BASE/api/v1/analyze" -H "Authorization: Bearer $JUSTMEDIT_TOKEN" \
  -H "Content-Type: application/json" -d '{"sex": "F", "age_years": 34, "values": {"hemoglobin": 124, "MCV": 82}}'

# 4. Сохранить анализ в историю (значения хранятся зашифрованными) и получить динамику
curl -sS -X POST "$BASE/api/v1/me/records" -H "Authorization: Bearer $JUSTMEDIT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-04-16", "source": "form", "input": {"sex": "F", "age_years": 34, "values": {"hemoglobin": 124}}}'
curl -sS "$BASE/api/v1/me/history" -H "Authorization: Bearer $JUSTMEDIT_TOKEN"

# 5. Доступ врачу на 30 дней: в ответе link_token, ссылка для врача — $BASE/share#<link_token>
curl -sS -X POST "$BASE/api/v1/me/shares" -H "Authorization: Bearer $JUSTMEDIT_TOKEN" \
  -H "Content-Type: application/json" -d '{"days": 30}'

# 6. Выйти (токен отзывается); удалить учётную запись и все записи — DELETE /api/v1/me с паролем
curl -sS -X POST "$BASE/api/v1/auth/logout" -H "Authorization: Bearer $JUSTMEDIT_TOKEN"
```

Чтобы страница вашего сайта обращалась к API из браузера, добавьте её источник в `DL_CORS_ORIGINS` на стороне
сервиса (через запятую, со схемой): `DL_CORS_ORIGINS=https://clinic.example`. Cookie сайта Justmedit чужой странице
не передаются — только заголовок `Authorization`. Готовая форма расшифровки на HTML и JavaScript — на странице
`/developers`, раздел «Встраивание в свой сайт». Кабинет — в разделе «Добавления 04.10.2026, вечер»
`docs/CONTRACTS.md` (ADR 0009).

## Чего в примерах нет

- Примера обмена в формате FHIR.
- Повторных попыток при сбое сети: при ошибке `lis_batch.py` останавливается, выходной файл не пишется.
