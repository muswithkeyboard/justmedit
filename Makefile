# DeficitLens — частые команды. Запуск из корня репозитория.
# Цели run/test/train/evaluate/verify/clinic работают без Docker; цели docker-* — через docker compose.
# Интерпретатор: окружение проекта .venv (uv sync --extra dev), если оно есть, иначе python3 из PATH.
PY      ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PORT    ?= 8080
HOST    ?= 127.0.0.1
DL_API_KEYS ?= dl-demo-key
# Пакетная расшифровка в Docker: файл кладётся в data/in, результат появляется в data/out.
IN      ?= x.csv
OUT     ?= pred.csv
COMPOSE ?= docker compose
comma   := ,
export PYTHONPATH := src

.PHONY: figures help run clinic test train evaluate norms perf verify \
        docker-build docker-up docker-verify docker-predict docker-clinic docker-down

help:
	@echo "Без Docker:"
	@echo "  make run            — веб-сервис на http://$(HOST):$(PORT) (ключ API: $(DL_API_KEYS))"
	@echo "  make clinic         — мини-сайт клиники на http://127.0.0.1:8081 (нужен запущенный make run)"
	@echo "  make test           — тесты (без pytest)"
	@echo "  make train          — обучить модели (нужны data/case и data/nhanes или DL_CASE_DATA / DL_NHANES_DATA)"
	@echo "  make evaluate       — пересчитать метрики в docs/metrics (нужны те же файлы)"
	@echo "  make norms          — пересобрать docs/norms.md из config/norms_ru.yaml"
	@echo "  make perf           — замер скорости, результат в docs/perf.md"
	@echo "  make verify         — полный прогон: тесты, модели, сервер, запросы"
	@echo "Docker:"
	@echo "  make docker-build   — собрать образы app и verify"
	@echo "  make docker-up      — docker compose up --build (http://127.0.0.1:8080)"
	@echo "  make docker-verify  — тесты и проверка сервиса в контейнере (без data/case и data/nhanes — SKIP)"
	@echo "  make docker-predict IN=x.csv OUT=pred.csv — data/in/IN → data/out/OUT"
	@echo "  make docker-clinic  — сервис и мини-сайт клиники (http://127.0.0.1:8081)"
	@echo "  make docker-down    — остановить всё"

run:
	DL_API_KEYS=$(DL_API_KEYS) $(PY) -m uvicorn deficitlens_api.app:app --host $(HOST) --port $(PORT) --no-access-log --no-server-header --no-proxy-headers

clinic:
	DL_URL=http://$(HOST):$(PORT) DL_API_KEY=$(firstword $(subst $(comma), ,$(DL_API_KEYS))) \
	  $(PY) -m uvicorn app:app --app-dir examples/clinic_site --host 127.0.0.1 --port 8081 --no-access-log --no-server-header

test:
	$(PY) scripts/run_tests.py

train:
	$(PY) -m deficitlens_core.cli train

evaluate:
	$(PY) -m deficitlens_core.cli evaluate

norms:
	$(PY) scripts/gen_norms_doc.py

figures:
	$(PY) scripts/make_figures.py

perf:
	$(PY) scripts/perf.py

verify:
	PYTHON=$(PY) bash scripts/verify.sh

docker-build:
	$(COMPOSE) --profile tools build app verify

docker-up:
	$(COMPOSE) up --build

docker-verify:
	$(COMPOSE) run --rm --build verify

# На Linux файлы в data/out создаются от вашего пользователя (на macOS и Windows это не нужно, но не мешает).
docker-predict:
	mkdir -p data/in data/out
	DL_UID=$$(id -u) DL_GID=$$(id -g) $(COMPOSE) run --rm cli predict --in /data/in/$(IN) --out /data/out/$(OUT)

docker-clinic:
	$(COMPOSE) --profile clinic up --build

docker-down:
	$(COMPOSE) --profile tools --profile clinic down
