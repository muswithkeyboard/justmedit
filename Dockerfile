# DeficitLens — два образа из одного файла:
#   target app    — веб-сервис и командная строка (deficitlens:0.1.0);
#   target verify — тот же образ плюс тесты, скрипты и dev-зависимости: `bash scripts/verify.sh`.
# Сборка и запуск: docker compose up --build  (см. docker-compose.yml).
#
# Предсказуемые версии:
#   - Python 3.13.16 (на нём обучены модели, см. models/*/metadata.json), образ закреплён по дайджесту;
#   - зависимости — строго по uv.lock (`uv sync --frozen`), uv закреплён по версии.
# Файлов кейса и NHANES в образах нет (см. .dockerignore); verify получает их томами только для чтения.

ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
ARG UV_VERSION=0.12.19

# ---------------------------------------------------------------------------------------------
# builder: виртуальное окружение /opt/venv по uv.lock. uv остаётся только в этой стадии.
# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
ARG UV_VERSION
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
RUN pip install "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Только зависимости (сам пакет не ставится: код берётся из /app/src через PYTHONPATH,
# пути к config/ и models/ считаются от /app — см. src/deficitlens_core/config.py).
RUN uv sync --frozen --no-install-project --no-dev

# Зависимости для тестов (pytest, httpx2, requests, ruff, matplotlib) — только для образа verify.
# scripts/run_tests.py понимает pytest Skipped, поэтому pytest в образе не мешает SKIP без данных.
FROM builder AS builder-dev
RUN uv sync --frozen --no-install-project --extra dev

# ---------------------------------------------------------------------------------------------
# app: сервис. Без uv, без компилятора, без root.
# ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS app
# PYTHONDONTWRITEBYTECODE — файловая система контейнера только для чтения, .pyc писать некуда
# (байт-код зависимостей уже скомпилирован при сборке: UV_COMPILE_BYTECODE).
# HOME и JOBLIB_TEMP_FOLDER — единственное место для записи: /tmp (в docker-compose.yml это tmpfs, то есть память).
# OMP_NUM_THREADS=1 — предсказание бустинга в один поток: одинаковое время ответа и без лишних потоков в контейнере.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    JOBLIB_TEMP_FOLDER=/tmp \
    MPLCONFIGDIR=/tmp \
    OMP_NUM_THREADS=1 \
    DL_HOST=0.0.0.0 \
    DL_PORT=8080
# Пользователь без прав root (uid:gid 10001:10001); файлы приложения принадлежат root, пользователю — только чтение.
# Команда deficitlens — обёртка над `python -m deficitlens_core.cli` (пакет не устанавливается, код — в /app/src).
# Tesseract — локальное распознавание фото бланка (ADR 0007): движок и языки rus, eng из Debian, без рекомендуемых
# пакетов. Наружу при работе не ходит; фото и временные файлы — только в /tmp (tmpfs).
# tini — PID 1: убирает процессы-«сироты» (tesseract, убитый по таймауту вместе с дочерним процессом), иначе они
# остаются зомби у uvicorn и копятся до pids_limit.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng tini \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
 && tesseract --version 2>&1 | head -1 && tesseract --list-langs 2>&1 | tail -n +2
# /data — каталог базы личного кабинета (ADR 0009, DL_DATA_DIR): владелец — пользователь сервиса; права 0711 —
# другие пользователи не видят список файлов, но cli с DL_UID=$(id -u) проходит к своим /data/in и /data/out.
# Файлы базы и ключа создаются с правами 0600. Именованный том (jm-data в docker-compose.yml) при первом
# подключении получает владельца и права этого каталога.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
 && printf '#!/bin/sh\nexec python -m deficitlens_core.cli "$@"\n' > /usr/local/bin/deficitlens \
 && chmod 0755 /usr/local/bin/deficitlens \
 && mkdir -p /data && chown 10001:10001 /data && chmod 0711 /data
ENV DL_DATA_DIR=/data
# Байт-код стандартной библиотеки: официальный образ python удаляет все .pyc, а файловая система контейнера только
# для чтения — без этого каждый процесс (uvicorn, дочерние процессы разбора PDF и фото на каждый файл, healthcheck)
# заново компилирует dataclasses, inspect, typing, subprocess… (~0,15 с на дочерний процесс).
RUN python -m compileall -q -j 0 --invalidation-mode unchecked-hash \
      "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
# В образ идут только код, конфигурация, обученные модели, демо-примеры и сводные метрики.
COPY src ./src
# Байт-код кода сервиса — при сборке: файловая система контейнера только для чтения (PYTHONDONTWRITEBYTECODE),
# и без .pyc каждый процесс компилировал бы /app/src заново — в том числе дочерний процесс разбора PDF и фото на
# каждый файл. unchecked-hash: .pyc верен независимо от времени изменения файлов в слое образа.
# Байт-код зависимостей в /opt/venv уже скомпилирован в builder (UV_COMPILE_BYTECODE=1).
# Вместе с байт-кодом stdlib (выше) разбор PDF-бланка: 0,49 → 0,17 с, старт сервиса: 2,5 → 1,8 с (замер 05.10.2026).
RUN python -m compileall -q --invalidation-mode unchecked-hash /app/src
COPY config ./config
COPY models ./models
COPY data/demo ./data/demo
COPY docs/metrics ./docs/metrics
USER 10001
EXPOSE 8080
# Проверка живости: /healthz отвечает без ключа. curl в образе нет — используется сам Python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]
# --no-access-log: журнал uvicorn выключен, чтобы в логи не попадали строка запроса и адрес клиента.
# Сервис ведёт свой журнал: время, метод, путь, код ответа, длительность.
# --no-proxy-headers: X-Forwarded-For / -Proto / -Host разбирает сам сервис и только от адресов из DL_TRUSTED_PROXIES
# (по умолчанию пусто — не доверять никому); uvicorn по умолчанию доверял бы им от 127.0.0.1.
ENV DL_TRUSTED_PROXIES=""
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "deficitlens_api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-server-header", "--no-proxy-headers"]

# ---------------------------------------------------------------------------------------------
# verify: образ app + тесты, скрипты, мини-сайт клиники (его тесты) и docs/norms.md (сверка с config).
# Запуск: docker compose run --rm verify  → таблица PASS / FAIL / SKIP; без файлов кейса и NHANES — SKIP.
# ---------------------------------------------------------------------------------------------
FROM app AS verify
# Шрифт DejaVu — для синтетического фото бланка в тестах распознавания (tests/helpers.py::photo_blank).
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
USER 10001
COPY --from=builder-dev /opt/venv /opt/venv
COPY tests ./tests
COPY scripts ./scripts
COPY examples/clinic_site ./examples/clinic_site
COPY examples/lis_batch.py ./examples/lis_batch.py
COPY docs/pitch ./docs/pitch
COPY docs/facts/reference_table.md ./docs/facts/reference_table.md
COPY docs/norms.md ./docs/norms.md
COPY docs/CONTRACTS.md ./docs/CONTRACTS.md
HEALTHCHECK NONE
CMD ["bash", "scripts/verify.sh"]
