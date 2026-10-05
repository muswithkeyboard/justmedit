# Контракты модулей (заморожены координатором)

Менять сигнатуры нельзя; можно добавлять необязательные аргументы и поля. Схемы входа и выхода — `src/deficitlens_core/schemas.py`. Константы — `constants.py`. Конфигурация — `config.py` и `config/*.yaml`.

## normalize.py
```python
@dataclass
class Normalized:
    sex: str                      # "F" | "M"
    age_years: int
    pregnancy_status: str         # "no" | "yes" | "unknown"
    trimester: int | None
    values: dict[str, float]      # канонические коды и единицы; включает производные TSAT и eGFR, если их можно посчитать
    measured: set[str]            # коды, реально присланные пользователем (без производных)
    notes: list[NormalizationNote]
    norms_set: str                # "who" | "ru"

def normalize_input(inp: AnalysisInput, cfg: Config) -> Normalized   # при ошибке: raise InputError(code, message, field)
```

## level1.py
```python
def assess_level1(norm: Normalized, cfg: Config) -> Level1
```

## rules.py
```python
@dataclass
class RulesOutcome:
    flags: list[Flag]
    rule_signals: list[RuleSignal]      # сигналы скрытого дефицита по порогам (заполняются при любом Hb)
    checklist: list[ChecklistItem]      # чек-лист исключений при анемии без найденного дефицита; иначе []
    mandatory_tests: list[dict]         # [{"analyte", "reason", "source"}] в порядке важности, только НЕсданные анализы
    derived: dict[str, float]           # "mentzer", "egfr_calc", "tsat_calc" …

def evaluate_rules(norm: Normalized, level1: Level1, cfg: Config) -> RulesOutcome
```

## values_table.py
```python
def build_values_table(norm: Normalized, cfg: Config, contributions: dict[str, str] | None = None) -> list[ValueRow]
```

## reports/__init__.py
```python
def build_reports(*, norm: Normalized, level1: Level1, level2: Level2, hidden: HiddenDeficiency,
                  flags: list[Flag], checklist: list[ChecklistItem], next_tests: list[NextTest],
                  explanation: Explanation, values: list[ValueRow], cfg: Config) -> Reports
```

## ml/features.py
```python
def to_matrix(records) -> np.ndarray            # list[dict] | DataFrame -> (n, 37) в порядке constants.FEATS; sex "F"/"M" -> 0/1; нет значения -> NaN
def who_anemia(X) -> np.ndarray                 # 0/1 по Hb и полу (ВОЗ: <120 Ж, <130 М)
def who_lock(P, anemia) -> np.ndarray           # обнуляет профили, несовместимые с анемией по Hb, перенормирует
def feats_plus(X) -> np.ndarray                 # X + 6 производных признаков (как в прототипе ensemble_check.py)
def completeness(x_row) -> str                  # "cbc" | "cbc_iron_crp" | "standard" | "extended"
```

## ml/case_model.py
```python
class CaseModel:
    version: str                                 # "case-v1"
    metadata: dict
    @classmethod
    def load(cls, model_dir: str | Path | None = None) -> "CaseModel"      # по умолчанию <models>/case-v1
    def predict_profiles(self, X: np.ndarray, mode: str = "clinical") -> np.ndarray   # (n, 14) после «замка ВОЗ»; mode: "clinical" | "benchmark"
    def contributions(self, x_row: np.ndarray, top_profile: int, runner_profile: int) -> list[dict]
        # [{"analyte": str, "delta": float}] по сданным анализам вне ОАК; delta > 0 — «за» top_profile против runner_profile
    def information_gain(self, x_row: np.ndarray) -> dict[str, float]
        # для каждого НЕсданного анализа вне ОАК — ожидаемое уменьшение энтропии по 14 профилям, бит (клинический режим)
    def hidden_threshold(self, level: str) -> float   # порог P(любой дефицит | нет анемии) при специфичности 0,90 на OOF
```

## ml/screen.py
```python
class ScreenModel:
    version: str                                 # "screen-v1"
    metadata: dict
    @classmethod
    def load(cls, model_dir: str | Path | None = None) -> "ScreenModel"    # по умолчанию <models>/screen-v1
    def predict(self, values: dict[str, float], sex: str, age_years: int, reference: dict | None = None) -> dict
        # {"p_lt15": float, "p_lt30": float, "features": "full_cbc" | "hb_only", "group": "F18-49" | "F50+" | "M",
        #  "thresholds": {"refer": float, "high": float}}   # пороги по p_lt15 для этой группы из metadata
```

## Точки входа CLI (cli.py принадлежит координатору, вызывает эти функции)
```python
deficitlens_core.ml.train_case.train(case_path: str, out_dir: str) -> dict
deficitlens_core.ml.evaluate_case.evaluate(case_path: str, out_json: str | None = None) -> dict
deficitlens_core.io.table.predict_file(in_path: str, out_path: str, mode: str = "auto",
                                       summary_path: str | None = None, mapping_path: str | None = None) -> dict
deficitlens_core.ml.train_screen.train(nhanes_path: str, out_dir: str) -> dict
deficitlens_core.ml.evaluate_screen.evaluate(nhanes_path: str, out_json: str | None = None, case_path: str | None = None) -> dict
deficitlens_core.ml.screen.build_lab_reference(csv_path: str, name: str, write: bool = True) -> dict
```

## engine.py (координатор)
```python
def analyze(inp: AnalysisInput, *, models: Models | None = None, cfg: Config | None = None) -> AnalysisResult
```

---

## Добавления от 04.10 (после сборки ядра)

Необязательные расширения, уже реализованные в коде:

- `CaseModel.hidden_threshold(level, mode="clinical")` — порог зависит от режима; ещё есть `predict_both`, `prior`, `fusion_params`.
- `features.apply_level`, `features.completeness_many`.
- `ScreenModel.predict(..., refer_share=0.30, high_share=0.10)`; в ответе дополнительно `refer_share`, `p_lt30_model`, `reference_fallback`. `ScreenModel.threshold(features, group, share)`.
- `next_test.rank_next_tests(*, norm, hidden, mandatory_tests, case_model, x_row, cfg, allow_information)`.
- Движок сам добавляет флаг `b12_gray_zone` по неуверенности модели, только когда числовой порог B12 не применяется (`norms_ru.yaml → policy`: запись `vitamin_b12` не в `foreign_allowed` и `use_foreign: false`). По решению п. 10 B12 оценивается по NICE NG239, флаг ставят правила.
- `AnalysisInput.reference_ranges` (необязательное): код → {low, high, unit}; `AnalysisResult.references` (необязательное): {set, lab_name, local_analytes, warning}. `Normalized.references` — действующие референсы (`references.py`). `io.table.table_fingerprint(df)`, `io.table.same_as_training(sha, fingerprint, metadata)`.
- Правила-маски и пункты чек-листа «под подозрением» понижают словесную уверенность уровня 2 до «средней» (`class_mapping.yaml → confidence_caps`).
- По одному ОАК `hidden_deficiency.p_any` и `by_nutrient` не выводятся (там это доля классов в файле кейса, а не свойство человека).

## Контракт HTTP (пакет `deficitlens_api`, Starlette)

Порт по умолчанию 8080. Настройки — переменные окружения: `DL_API_KEYS` (ключи через запятую), `DL_UI_RATE_PER_MIN` (30),
`DL_MAX_UPLOAD_MB` (10), `DL_MAX_BATCH` (10000), `DL_HOST` (0.0.0.0), `DL_PORT` (8080). Если `DL_API_KEYS` пуст —
при старте создаётся случайный ключ и печатается в stdout одной строкой `DL_API_KEY=<ключ>`.

Слой сервиса `deficitlens_api/service.py` не зависит от веб-фреймворка (чистые функции: dict → dict), чтобы на
площадке его можно было подключить к FastAPI без переписывания.

| Маршрут | Ключ | Тело запроса | Ответ |
|---|---|---|---|
| `GET /healthz` | нет | — | `{"status":"ok","engine":"…","case_model":"…","screen_model":"…","norms":"…"}` |
| `POST /api/v1/analyze` | да | `AnalysisInput` (JSON) | `AnalysisResult` (JSON) |
| `POST /api/v1/batch` | да | `{"records":[AnalysisInput…],"with_reports":false}` | `{"summary":BatchSummary,"items":[BatchItem…]}` |
| `POST /api/v1/columns` | да | multipart: `file` (CSV / XLSX) | `{"rows":N,"columns":[{"column","kind":"feature\|label\|guess\|unknown\|service\|duplicate","target","unit","example"}…],"targets":[{"code","name_ru","group","units":[{"unit","factor"}]}…],"requirements":{"ok","errors":[…],"warnings":[…],"info":[…]}}` (04.10.2026, своя структура файла) |
| `POST /api/v1/benchmark` | да | multipart: `file` (CSV / XLSX), `mode` = auto \| clinical \| benchmark, необязательно `mapping` — JSON `{"столбец": "код" \| {"code","unit"} \| "ignore"}` (ошибка — 422 `invalid_mapping`) | `{"summary":{…, "requirements":{…}},"preview":[…первые 20 строк…],"csv":"…"}`; с `?format=csv` — файл CSV |
| `POST /api/v1/parse` | да | `{"text":"…"}` или multipart: `file` (фото JPEG / PNG / WEBP, PDF с текстом или скан (OCR), TXT, CSV / XLSX одного анализа) | `{"values":{…},"unrecognized":[…],"notes":[…]}`; для файла ещё `"source"`; `unrecognized` — «строка N: …» без текста бланка; для фото (этап 7, ADR 0007) ещё `"warnings":["строка N: <показатель> — проверьте значение"]`, `"check":[код…]`, `source.format = "photo"`, распознанный текст не возвращается |
| `GET /api/v1/reference` | да | — | пороги с источниками, показатели с единицами, цены, версии |
| `GET /`, `GET /benchmark`, `GET /static/*` | нет | — | страницы и статика (без внешних ресурсов; подложка карты OSM — по кнопке «Выбрать на карте») |
| `GET /ui/examples`, `GET /ui/reference`, `POST /ui/analyze`, `POST /ui/benchmark`, `POST /ui/columns`, `POST /ui/parse` | нет, лимит запросов на адрес | как у `/api/v1/*` | как у `/api/v1/*` |

Тяжёлые маршруты (batch, benchmark, columns, parse — и /api/v1, и /ui): не больше 2 расчётов одновременно на процесс,
иначе `503 service_busy` с `Retry-After`; для /api/v1 — не больше DL_UI_RATE_PER_MIN тяжёлых запросов в минуту
на ключ (429). Файлы: ≤200 столбцов, строк ≤ DL_MAX_BATCH (разбор бланка — 400), XLSX в распакованном виде
≤50 МБ (разбор бланка — 5 МБ) — иначе 422 `too_many_columns` / `too_many_rows` / `file_too_large`; PDF —
422 `bad_pdf` / `no_text_layer` / `pdf_too_complex`; фото — 422 `bad_image` / `heic_unsupported` /
`image_too_large` (> 25 Мп) / `ocr_unavailable` (нет Tesseract) / `ocr_too_slow` (> 20 с) / `no_text` / `ocr_failed`. Файлы разбираются в памяти и на диск не пишутся.
CSV в ответах: все ячейки в кавычках, текст-формула — с апострофом. В журнале путь — через repr.

Ключ передаётся заголовком `X-API-Key`. Нет ключа — `401`, неверный — `403`. Ошибка ввода — `422`
`{"error":{"code","message","field"}}`. Слишком большой файл — `413`. Превышен лимит — `429`. Любая другая ошибка —
`500 {"error":{"code":"internal","message":"Внутренняя ошибка"}}` без трассировки и без значений анализов.
Тела запросов не логируются; в журнале — время, метод, путь, код ответа, длительность.

## Добавления 04.10.2026, вечер (план «кабинет, история, PDF любой лаборатории, API»; ADR 0009). Заморожено координатором

### Ответ разбора бланка `/parse` (поток П2) — новые необязательные поля

```
{"values": {...}, "unrecognized": [...], "notes": [...], "source": {...},          // как раньше
 "date": "2025-04-16" | null,              // дата анализа с бланка (ISO), если найдена
 "reference_ranges": {"hemoglobin": {"low": 120, "high": 150, "unit": "г/л"}, ...},  // нормы с бланка, в
                                           // канонической единице сервиса; low/high могут быть null («< 5»)
 "ignored": ["Средний объём тромбоцитов", ...]   // распознано, но сервисом не используется (без значений)
}
```
ПДн с бланка (ФИО, полис, номер заказа, исполнитель, учреждение) не возвращаются и не логируются. Скан-PDF без текстового
слоя распознаётся OCR (тот же Tesseract, что для фото): тогда `source.format = "pdf_scan"`, есть `warnings`/`check`.

### Анализ истории `deficitlens_core/history.py` (поток П4)

```python
def analyze(records: list[dict], *, cfg: Config | None = None, models: Models | None = None,
            role: str = "patient", region_prices: dict | None = None) -> dict
# records: [{"id": str, "date": "YYYY-MM-DD", "input": AnalysisInput-dict, "result": AnalysisResult-dict | None}]
#   result — посчитанный ранее ответ движка (кеш); None — посчитать engine.analyze.
```
Ответ (dict, JSON-совместимый):
```
{"role": "patient"|"doctor", "n_records": 3, "first_date": "...", "last_date": "...",
 "series": [{"analyte": "hemoglobin", "name_ru": "Гемоглобин", "unit": "г/л",
             "points": [{"date": "...", "value": 118.0, "record_id": "..."}],
             "last": 118.0, "delta": -12.0, "delta_pct": -9.2, "direction": "up"|"down"|"flat"|"single",
             "months": 6.0, "ref": {"low": 120, "high": 150} | null}],
 "events": [{"code": "hb_drop", "severity": "attention"|"info", "date": "...", "title": "...", "text": "...",
             "analytes": ["hemoglobin"]}],                     // тексты — по роли (пациенту без диагнозов и чисел риска)
 "completeness": [{"analyte": "ferritin", "name_ru": "Ферритин", "status": "never"|"stale"|"ok",
                   "last_date": "..." | null, "reason": "...", "priority": 1, "price_rub": 845 | null}],
 "latest": {"record_id": "...", "date": "...", "headline": "..."} | null,
 "summary": "одна строка для шапки кабинета"}
```
Пороги событий и сроки «давно» — `config/norms_ru.yaml → history` (с источником и статусом).

### Хранение и кабинет (поток П3, ADR 0009)

Хранилище — SQLite в `DL_DATA_DIR` (по умолчанию `./data/app`, в Docker — том `/data`); значения анализов, сводки
и референсы зашифрованы (Fernet, ключ `DL_DATA_KEY`; нет ключа — создаётся файл `<DL_DATA_DIR>/key`, 0600).
Новые настройки: `DL_DATA_DIR`, `DL_DATA_KEY`, `DL_COOKIE_SECURE` (0/1), `DL_CORS_ORIGINS` (через запятую).

**Авторизация кабинета:** `Authorization: Bearer <токен>` (внешние сайты, без cookie) или cookie `jm_session`
(сайт; HttpOnly, SameSite=Strict). Изменяющие запросы с cookie — только с заголовком `X-Justmedit: 1` и своим
`Origin`, иначе 403 `csrf`. Нет/просрочен токен — 401 `unauthorized`; не та роль или нет связи — 403 `forbidden`.
Маршруты анализа `/api/v1/analyze|batch|benchmark|parse|columns|reference` принимают `X-API-Key` **или** Bearer-токен.
CORS для `/api/v1/*` — только источники из `DL_CORS_ORIGINS`, без credentials.

| Маршрут | Роль | Тело | Ответ |
|---|---|---|---|
| `POST /api/v1/auth/register` | — | `{"login","password","role":"patient"\|"doctor","clinic"?,"consent":true}` | `201 {"user":User,"token":"…"}` + cookie |
| `POST /api/v1/auth/login` | — | `{"login","password"}` | `{"user":User,"token":"…"}` + cookie |
| `POST /api/v1/auth/logout` | любой | — | `{"ok":true}` |
| `GET /api/v1/me` | любой | — | `User` |
| `DELETE /api/v1/me` | любой | `{"password"}` | `{"deleted":true}` — всё удалено |
| `GET /api/v1/me/tokens`, `POST` (`{"name"}`), `DELETE /api/v1/me/tokens/{id}` | любой | | токены API (значение — только в ответе на создание) |
| `GET /api/v1/me/records` | пациент | — | `{"records":[{"id","date","source","headline","anemia","created"}]}` |
| `POST /api/v1/me/records` | пациент | `{"date":"YYYY-MM-DD","input":AnalysisInput,"source":"form"\|"photo"\|"pdf"\|"file"\|"import"}` | `201 {"record":{…},"result":AnalysisResult}` |
| `GET /api/v1/me/records/{id}` | пациент | — | `{"record":{…,"input"},"result":AnalysisResult}` |
| `DELETE /api/v1/me/records/{id}` | пациент | — | `{"deleted":true}` |
| `GET /api/v1/me/history` | пациент | — | `History` (см. выше, role=patient) |
| `GET /api/v1/me/export?format=json\|csv` | пациент | — | файл |
| `POST /api/v1/me/import` | пациент | JSON экспорта | `{"imported":N}` |
| `GET /api/v1/me/shares`, `POST` (`{"days":1\|7\|30\|90}`), `DELETE /api/v1/me/shares/{id}` | пациент | | `{"shares":[{"id","doctor":{"login","clinic"}\|null,"status":"pending"\|"active"\|"expired"\|"revoked","expires"}]}`; POST → `{"share":{…},"link_token":"…"}` |
| `GET /api/v1/me/access-log` | пациент | — | `{"items":[{"doctor":{"login","clinic"},"what":"history"\|"record","at"}]}` |
| `POST /api/v1/shares/accept` | врач | `{"token"}` | `{"patient":{"id","login"},"expires"}` |
| `GET /api/v1/doctor/patients` | врач | — | `{"patients":[{"id","login","expires","n_records","last_date","last_headline"}]}` |
| `GET /api/v1/doctor/patients/{id}/history` | врач | — | `History` (role=doctor); пишется в журнал доступа |
| `GET /api/v1/doctor/patients/{id}/records/{rid}` | врач | — | `{"record","result"}`; пишется в журнал |
| `GET /api/v1/doctor/lab-refs`, `POST` (`{"name","ranges":{код:{"low","high","unit"}},"default":bool}` или multipart `file` CSV/XLSX «показатель;нижняя;верхняя;единица»), `DELETE /api/v1/doctor/lab-refs/{id}` | врач | | `{"sets":[{"id","name","default","ranges"}]}` |
| `GET /api/v1/openapi.json` | — | — | описание API (поток П6) |

`User = {"id","login","role","clinic","created"}`. Ссылка для врача: `/share#<link_token>` (фрагмент на сервер не
уходит; страница отправляет токен `POST /api/v1/shares/accept`). Токен одноразовый, живёт 48 ч до принятия;
связь — `days` дней с момента принятия. Страницы: `/login`, `/cabinet`, `/share`, `/developers`.

#### Уточнения реализации П3 (сверено с кодом и живыми ответами, поток П6b)

Таблица выше не меняется; здесь — как кабинет отвечает на деле (`cabinet_api.py`, `tests/api/test_cabinet.py`).
`static/openapi.json` описывает те же формы; `tests/api/test_openapi.py` сверяет его примеры с живыми ответами.

| Что | Как в реализации |
|---|---|
| Коды успеха | `201` — только `POST /api/v1/auth/register` и `POST /api/v1/me/records`; остальные успешные ответы — `200`, в том числе `POST /api/v1/me/shares` и `POST /api/v1/me/tokens` |
| `POST /api/v1/me/tokens` | тело необязательно (`{"name"}` до 60 символов, по умолчанию «Токен API») → `{"token":{"id","name","created","expires","last_used","current"},"value":"…"}`; значение — только в этом ответе |
| `GET /api/v1/me/tokens`, `DELETE /api/v1/me/tokens/{id}` | `{"tokens":[{"id","name","created","expires","last_used","current"}]}`; DELETE возвращает оставшиеся токены; токены входа называются «Вход» |
| `POST /api/v1/me/shares` | `{"share":{"id","doctor":null,"status":"pending","expires","days","created","accepted":null},"link_token":"…","link":"/share#…"}`; `expires` до принятия — срок ссылки (48 ч) |
| `GET /api/v1/me/shares`, `DELETE /api/v1/me/shares/{id}` | элементы — ещё с `days`, `created`, `accepted`; DELETE → `{"shares":[…]}` (обновлённый список) |
| `GET /api/v1/me/access-log` | `{"items":[{"doctor":{"login","clinic"}\|null,"what":"history"\|"record","record_id":"…"\|null,"at"}]}`, новые сверху; `doctor` = null — врач удалил учётную запись |
| `POST /api/v1/doctor/lab-refs`, `DELETE /api/v1/doctor/lab-refs/{id}` | `{"sets":[…]}` — все наборы врача; границы — в канонической единице сервиса (`"unit":"g/L"`), не в единице запроса |
| `PATCH /api/v1/doctor/lab-refs/{id}` | врач; тело `{"name"?, "default"?: bool, "ranges"?}` (хотя бы одно поле, иначе `422 missing_field`) → `{"sets":[…]}`; `default: true` снимает отметку с остальных наборов, `false` — с этого; предел числа наборов не затрагивается; чужой набор — `404` |
| `GET /api/v1/doctor/patients/{id}/records` | врач; `{"records":[{"id","date","source","headline","anemia","created"}]}` — `headline` врачу, новые сверху; только при действующей связи (иначе `403`); запрос пишется в журнал доступа пациента: `what = "records"` (`record_id` = null) |
| Токены API | у только что созданного токена `last_used` = null (отметка — с первого запроса этим токеном) |
| `POST /api/v1/me/records` | `date` необязательна: без неё — сегодняшняя (UTC) |
| Тела JSON кабинета | только с `Content-Type: application/json`, иначе `415 unsupported_media_type` |
| Cookie сайта | изменяющие запросы — с заголовком `X-Justmedit: 1` и своим `Origin`, иначе `403 csrf`; с Bearer-токеном заголовок не нужен |
| Ошибки | вход — `401 invalid_credentials` (одинаково для неизвестного логина и неверного пароля); логин занят — `409 login_taken`; ссылка недействительна — `404 share_invalid`; неверный пароль при удалении — `403 wrong_password`; хранилище недоступно — `503 storage_unavailable` |
| id в пути | 32 шестнадцатеричных знака; другой вид или чужой id — `404 not_found` (у врача на чужого пациента — `403 forbidden`) |

#### Уточнения анализа истории после ревизии (04.10.2026, вечер)

- Запись истории может содержать необязательное `"created"` (ISO 8601): записи одного дня упорядочиваются по
  (date, created, порядок во входе); кабинет передаёт `created`. Записи одного дня с одинаковыми значениями
  схлопываются — `n_records` считается после схлопывания; «подряд»/«повторяется» — только по разным датам.
- Новый код события `hb_context_changed` — между анализами сменился контекст оценки (беременность, пол): пороги
  разные, «появилась/ушла анемия» не пишется; оба значения Hb сравниваются с порогом последней записи.
- `summary` учитывает последний анализ: «срочно» — первым, анемия или сигнал скрытого дефицита — «есть отклонения»;
  «спокойная» строка — только без событий «внимание» и без отклонений в последнем анализе.
- Ряды — не больше `history.limits.max_points` (100) последних точек на показатель; изменение и направление
  считаются по показанным точкам; записей — не больше `history.limits.max_records` (500).

#### Безопасность кабинета (ревизия ADR 0009, 04.10.2026, вечер)

**Правило: Bearer-токен не создаёт токены и ссылки без пароля.** Токен API (Bearer) может: анализ
(`/api/v1/analyze|batch|benchmark|parse|columns|reference`), чтение кабинета (`GET` me, me/records, me/history,
me/export, me/shares, me/access-log, doctor/*), запись анализов (`POST`/`DELETE` me/records, me/import), отзыв
токенов и ссылок, принятие ссылки врачом, референсы врача. Создать токен (`POST /api/v1/me/tokens`), ссылку врачу
(`POST /api/v1/me/shares`) и «выйти везде» по Bearer можно только с паролем аккаунта в поле `password` тела
(нет — `403 password_required`, неверный — `403 wrong_password`); с cookie сайта (вход с паролем) пароль не нужен —
формы ответов прежние. Удаление аккаунта и смена пароля — всегда с паролем.

| Маршрут | Роль | Тело | Ответ |
|---|---|---|---|
| `POST /api/v1/me/logout-all` | любой | `{"all"?: bool, "password"?}` (`password` — обязателен по Bearer) | `{"ok":true,"revoked":N}` — отозваны все сессии и токены, кроме текущего входа; `all: true` — и текущий (cookie стирается) |
| `POST /api/v1/me/password` | любой | `{"old","new"}` | `{"ok":true,"revoked":N}` — пароль изменён, все остальные сессии и токены (и именованные) отозваны; неверный `old` — `403 wrong_password` (field = old) |

| Что | Как |
|---|---|
| Токены | вход выдаёт cookie и токен «Вход» с одним сроком (7 дней); выход или истечение cookie закрывают оба. Именованный токен — 30 дней; созданный по Bearer — не дольше токена запроса. Входов — до 20 (старые вытесняются), именованных токенов — до 20 без вытеснения: сверх — `422 too_many_tokens` |
| Запись анализа | показатели в `values` — только известные коды и названия (`422 unknown_analyte`, field = `values`); `patient_ref`, `lab_reference` — до 64 символов; `provenance` не хранится; вход после проверки ≤ 16 КБ (`413 record_too_large`); данных пользователя ≤ 5 МБ (`422 quota_exceeded`); дата — не позже завтрашнего дня (UTC, запас на часовой пояс) |
| Пределы сервиса | база ≤ `DL_DATA_MAX_MB` (512) — иначе `507 storage_full` на новые записи, наборы, ссылки, регистрации; регистраций — ≤ 10 в час с адреса (IPv6 — /64; `429`) и ≤ `DL_MAX_SIGNUPS_PER_DAY` (1000) в сутки (`503 signups_paused`) |
| Вход | кроме лимитов «адрес» и «адрес + логин» — общий счётчик неудач на логин: после 10 за 15 мин — `429` на 1 мин, дальше вдвое дольше, до 15 мин |
| Тяжёлое | `GET /api/v1/me/history` и история пациента у врача — под слотом тяжёлых расчётов (`503 service_busy`); `POST /api/v1/me/import` — ещё и лимит тяжёлых запросов в минуту на пользователя (`429`); записи импорта сначала все проверяются, потом считаются |
| JSON | вложенность глубже 32 — `422 invalid_json` (и у маршрутов анализа) |
| Тексты | клиника, имя набора референсов (и из имени файла), имя токена — без управляющих, форматных и невидимых символов; логин при регистрации — латиница или кириллица, не вперемешку |
| `GET /api/v1/doctor/patients` | `last_headline` — без значений: «Последний анализ: есть признаки анемии.» / «Последний анализ: анемии нет.» |
| `GET /api/v1/me/access-log` | `what`: `history` \| `records` (список записей) \| `record`; повторы «врач — что» в течение часа — одна строка; разные записи за час — `record_id: null` |
| Cookie | при `DL_COOKIE_SECURE=1` — имя `__Host-jm_session` (Secure, Path=/, без Domain), иначе `jm_session` |
| CSRF, CORS | `Origin`/`Referer` сравнивается по схеме, хосту и порту; `X-Forwarded-Proto/Host` и `X-Forwarded-For` — только от `DL_TRUSTED_PROXIES`; CORS разрешает методы GET, POST, PATCH, DELETE |
| Настройки | `DL_TRUSTED_PROXIES`, `DL_DATA_MAX_MB`, `DL_MAX_SIGNUPS_PER_DAY`, `DL_DATA_KEY_FILE` (файл ключа, права 0600, вне тома базы) |

#### Уточнения после UX-ревизии кабинета (05.10.2026)

Формы ответов меняются только добавлением полей; таблицы выше остаются в силе, здесь — что добавлено и как работает.

| Что | Как |
|---|---|
| `POST /api/v1/me/import` | `{"imported":N,"skipped":M}`: запись файла, у которой та же дата, пол, возраст, беременность и те же значения после пересчёта единиц (нормализация движка), что у сохранённой, не загружается второй раз (`skipped`). Повторы — как мультимножество: в кабинете один такой анализ, в файле два — загружается один; выгрузка в пустой кабинет переносится целиком. Предел 500 считается по новым записям |
| `POST /api/v1/analyze`, `/ui/analyze` (врач) | набор врача по умолчанию подписан в ответе: `references.lab_name = "doctor_default"`, `references.lab_title` — имя набора, `references.lab_id` — его id; в таблице значений вместо «референсы из запроса» — «ваш набор «имя»». Референсы из запроса — по-прежнему `lab_name = "request"` (без `lab_title`) |
| `GET /api/v1/me` | cookie сайта есть, но недействительна (выход везде, смена пароля, срок) — `401 unauthorized` и `Set-Cookie`, стирающий её (без cookie и с Bearer — 401 без Set-Cookie) |
| `GET /api/v1/me/tokens` и ответы токенов | у токена ещё `"kind":"login"\|"named"` (токен входа или именованный токен приложения); `current` с cookie сайта — токен «Вход» этой сессии (по Bearer — как раньше, сам токен запроса) |
| `GET /api/v1/me/access-log`, `GET /api/v1/me/shares` | врач удалил аккаунт: `doctor = {"login","clinic","deleted":true}` — снимок на момент просмотра или принятия ссылки (хранится зашифрованно, `enc_doctor`); его связи с пациентами не стираются, а закрываются (`status = "revoked"`) и остаются у пациента в «Прошлых». `doctor = null` у журнала — строка записана до снимков |
| `GET /api/v1/me/export` | необязательный `?day=ГГГГ-ММ-ДД` — местная дата страницы для имени файла `justmedit-<day>.json\|csv` (в пределах суток от даты сервера, иначе — дата сервера) |
| `POST /api/v1/me/records` | `source` — ещё `text` (вставленный текст бланка) |
| История и слот | `GET /api/v1/me/history` и история пациента у врача занимают слот тяжёлых расчётов, только если кеш ответов движка устарел (сменилась версия движка, моделей или порогов) и записи пересчитываются; слот ждут до 3 с (без блокировки цикла событий), затем `503 service_busy` с `Retry-After`. Загрузка выгрузки ждёт слот так же. Страница кабинета на 503 один раз сама повторяет запрос через `Retry-After` секунд |
| `n_records` | в истории — число разных анализов (одинаковые записи одного дня считаются один раз); в `GET /api/v1/doctor/patients` — число сохранённых записей (без схлопывания: расшифровывать все записи ради списка пациентов дорого); кабинет врача, открыв историю пациента, показывает оба числа, если они различаются |

#### Уточнения после ревизии надёжности (05.10.2026)

- `GET /ui/reference`, `GET /ui/examples` — без лимита запросов, `Cache-Control: public, max-age=300`, `ETag` (повтор — `304`).
  Лимит `DL_UI_RATE_PER_MIN` (30 в коде, 120 в `docker-compose.yml`) — только на `POST /ui/*`.
- Регистраций с одного адреса — `DL_SIGNUP_IP_PER_HOUR` в час (по умолчанию 30) вместо фиксированных 10.
- Тяжёлые расчёты: 2 на процесс и 1 на адрес клиента (второй запрос того же адреса ждёт до 6 с, затем `503`);
  разбор вставленного текста (`parse` с JSON) слот не занимает; `analyze` — не больше 4 расчётов одновременно (очередь).
- PDF бланка больше 5 МБ — `413 pdf_too_large`; разбор PDF — не дольше 4 с.
- Страницы: `/cabinet` без cookie сессии — `303` на `/login?next=/cabinet`; неизвестный адрес в браузере
  (`Accept: text/html`, не `/api`, `/ui`, `/static`) — страница 404; `/favicon.ico` — значок.
- Ответы: статика с `?v=` — `public, max-age=31536000, immutable`; сжатие gzip — статика, страницы, `openapi.json`,
  справочник; ответы кабинета и расчётов не сжимаются. При `DL_COOKIE_SECURE=1` — `Strict-Transport-Security`.
