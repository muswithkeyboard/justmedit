# Прототипы DeficitLens (референс для переноса в продукт)

Созданы 3–4 октября 2026 г. во время хакатона MedITron. Положить в `research/prototypes/` и не менять: по ним сверяются цифры продукта.

| Файл | Что это |
|---|---|
| `run_experiment.py` | Сравнение движков на файле кейса (840 строк): наивный бустинг, правила, слияние без утечки (`fit_v5` / `predict_v5`), маскирование. 5 фолдов × 3 повтора |
| `ensemble_check.py` | Ансамбль «бустинг × слияние» + замок ВОЗ — режим бенчмарка; использует функции из `run_experiment.py` |
| `RESULTS_case.md`, `RESULTS_ensemble.md` | Таблицы результатов по кейсу |
| `nhanes_screen.py` | Скрининг скрытого дефицита железа по ОАК на NHANES: временная проверка, стандартизация внутри лаборатории, таблица направлений |
| `RESULTS_nhanes.md`, `nhanes_metrics.csv`, `nhanes_referral.json` | Результаты по NHANES |
| `lab_reference_default.json` | Справочник ОАК по умолчанию (медиана и квартили по полу, NHANES 2017–2023) |
| `sensitivity_v6.py` | Проверка, что вариант с маскированием плох не из-за гиперпараметров (в продукт не идёт) |

Запуск (нужны Python 3.12+, numpy, pandas, scikit-learn, joblib):

```
DL_CASE_DATA=/abs/path/deficiency_anemia.csv python3 run_experiment.py          # ~3 мин на 2 ядрах
DL_CASE_DATA=/abs/path/deficiency_anemia.csv python3 ensemble_check.py          # ~2 мин
DL_NHANES_DATA=/abs/path/nhanes_deficiency_real.csv DL_CASE_DATA=/abs/path/deficiency_anemia.csv python3 nhanes_screen.py   # ~30 с
```

Что переносить: `to_matrix`, `who_anemia`, `who_lock`, `fit_tables`, `group_loglik`, `v5_posterior`, `fit_v5`, `predict_v5` → `ml/fusion.py`; `feats_plus`, `geo` и вариант `ENS_V7b+V5(noleak)` → `ml/ensemble.py`; расчёт стандартизации, обучение и таблицу направлений → `ml/screen.py`. Вариант V6 (маскирование) не переносить.

Ограничения: гиперпараметры бустинга фиксированы заранее и не подбирались; файл кейса синтетический; NHANES — данные США; это прототип, не медицинское изделие.
