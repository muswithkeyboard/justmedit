# Мини-сайт клиники — пример интеграции DeficitLens

Пациент заполняет форму общего анализа крови на сайте клиники. Браузер отправляет значения только на сервер клиники
(`POST /api/report`, свой origin). Сервер клиники проверяет форму, добавляет ключ `X-API-Key` из переменной
окружения, вызывает `POST /api/v1/analyze` DeficitLens и возвращает странице только `reports.patient` —
отчёт простыми словами, без диагнозов и лекарств.

- Ключ API живёт только на сервере (`DL_API_KEY`), в браузер и в HTML не попадает.
- Страница без inline-скриптов и стилей; строгая CSP (`default-src 'none'; script-src 'self'; connect-src 'self'`);
  ответ выводится через `textContent`.
- Значения и ответы не сохраняются и не логируются (uvicorn запускается с `--no-access-log`).
- Зависимости — только то, что уже есть в образе DeficitLens: Starlette, uvicorn, стандартная библиотека
  (запрос к API — `urllib.request`).

Файлы: `app.py` (Starlette, один файл), `static/index.html`, `static/clinic.js`, `static/clinic.css`.
Тесты: `tests/examples/test_clinic_site.py` (оба сервиса поднимаются в тесте, запросы — `urllib`).

## Через docker compose

Сервис `clinic` (профиль `clinic`) запускается из того же образа, что и API; папка примера монтируется только для
чтения, порт публикуется только на `127.0.0.1:8081`, адрес API — `http://app:8080`:

```bash
docker compose --profile clinic up --build
```

Откройте <http://127.0.0.1:8081>, нажмите «Заполнить примером» → «Расшифровать».
Если сервису задан свой ключ (`DL_API_KEYS=<ключ>`), передайте его и сайту: `DL_CLINIC_API_KEY=<ключ>`.

## Без Docker

1. Поднимите DeficitLens: `make run` (API на `http://127.0.0.1:8080`, демо-ключ `dl-demo-key`).
2. В другом терминале: `make clinic` или

   ```bash
   DL_URL=http://127.0.0.1:8080 DL_API_KEY=dl-demo-key \
     python3 -m uvicorn app:app --app-dir examples/clinic_site --host 127.0.0.1 --port 8081 --no-access-log
   ```

3. Откройте <http://127.0.0.1:8081>.

| Переменная | По умолчанию | Что это |
|---|---|---|
| `DL_URL` | `http://127.0.0.1:8080` (в compose — `http://app:8080`) | адрес API DeficitLens |
| `DL_API_KEY` | `dl-demo-key` | ключ клиники (в реальной клинике — из секрета, не по умолчанию) |

## Ответы сервера клиники

- `200 {"patient": {"headline": "…", "sections": [{"title": "…", "lines": ["…"]}]}}` — как `reports.patient` в API.
- `422 {"error": "…"}` — форма заполнена неверно или DeficitLens отклонил значения (сообщение по-русски).
- `413` — тело больше 16 КБ; `415` — не JSON.
- `502 {"error": "…"}` — DeficitLens недоступен, не принял ключ клиники или ответил ошибкой (пациенту без подробностей).
