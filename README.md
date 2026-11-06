# sniper.polymaster

Polymarket Sniper ищет игроков с устойчивым преимуществом, проверяет, навык это или удача, моделирует, сколько
заработал бы копировщик, и в реальном времени шлёт алерты в Telegram с paper trading по живым стаканам.
**Реальной торговли нет и не будет без отдельного решения:** код не размещает ордера и не работает с ключами.

Статус и документы:
- Исследование и методика: [docs/research/2026-11-06-polymarket-sniper.md](docs/research/2026-11-06-polymarket-sniper.md)
- Фаза 1 (один срез): [docs/research/2026-11-06-phase1-walkforward.md](docs/research/2026-11-06-phase1-walkforward.md)
- Итерация 2: [предрегистрация](docs/research/2026-11-06-iteration2-preregistration.md),
  [разбор «10 с против 60 с»](docs/research/2026-11-06-latency-discrepancy.md),
  [результаты](docs/research/2026-11-06-iteration2-results.md). Итог: на 4 holdout-периодах (2025-02 … 2026-07)
  копирование отобранных игроков не прибыльно, ROI пула −3.7%, 97.5% ДИ [−8.5%, +0.9%].
- Текущее состояние и следующие шаги: [docs/handoff](docs/handoff)

## Установка

```bash
uv sync                 # Python ≥ 3.11
cp .env.example .env    # необязательно: путь к БД, доля лимитов API, Telegram
uv run pytest           # unit-тесты
```

## Walk-forward на нескольких периодах

Периоды и роли (`dev` / `holdout`) задаются в `config/walkforward.toml`; правила решения — в предрегистрации.

```bash
export SNIPER_DB_PATH=data/live.sqlite
uv run sniper wf collect --role dev                  # вселенная, бэкфилл по окнам, обогащение (часы: лимиты API)
uv run sniper wf evaluate --role dev                 # скоринг до среза + фильтр быстрых сделок → симуляция
uv run sniper wf report --role dev                   # reports/walkforward/report-dev.md
# holdout — один раз, на закоммиченном коде:
uv run sniper wf evaluate --role holdout --confirm-holdout
uv run sniper wf report --role holdout
```

Что считается для каждой группы (тир × стратегия × задержка, стиль трейдера, категория рынка, отдельный трейдер):
число сигналов и сделок, пропуски по причинам и упущенный PnL лидера, вложенный капитал и его пик, комиссии,
проскальзывание к цене лидера, PnL, ROI с 95% доверительным интервалом (бутстрэп по событиям), максимальная просадка.

Стратегии: `position` (основная; копируется вход лидера в позицию ≥ $100) и `baseline` (каждый ордер, как в фазе 1).

## Paper trading и Telegram (фаза 2)

```bash
uv run sniper wf watchlist --out reports/watchlist.csv     # свежий отбор на сегодня по замороженным правилам
uv run sniper live run --scores reports/watchlist.csv --track-tiers candidate,watch --control-sample 30 \
    --delays 0,10,60,300 --paper-db data/paper.sqlite
uv run sniper live report --paper-db data/paper.sqlite --out reports/paper.json
```

- Поток сделок RTDS → агрегация филлов в ордера → для каждой задержки (0/10/60/300 с) снимок стакана CLOB →
  решение по реальному стакану (премия к лидеру, глубина, минимальный размер, риск-лимиты) → paper-позиции,
  выходы вслед за лидером, расчёт при резолюции, mark-to-market.
- Алерты уходят в Telegram, если заданы `SNIPER_TELEGRAM_BOT_TOKEN` и `SNIPER_TELEGRAM_CHAT_ID` (секреты проекта,
  не репозиторий). Без них — dry-run в `data/alerts.log`. Команды бота: `/status`, `/portfolio`, `/watchlist`,
  `/pause`, `/resume`, `/report`; отвечает только своему чату.
- Риск по умолчанию: виртуальный банк $10 000 на каждую задержку, ставка $100 (или ¼ Kelly), лимиты 2% на рынок,
  5% на событие, 15% на лидера, 30% на категорию, 50% всего; дневной стоп −3%, недельный −7%, kill-switch −15% от пика.

## Структура

| Модуль | Назначение |
|---|---|
| `src/sniper/http.py`, `polymarket.py` | async-клиенты Data API v2, Gamma, CLOB с лимитами и ретраями |
| `src/sniper/db.py`, `ingest.py` | SQLite, покрытие активности по окнам, бэкфилл, обогащение, кэш цен |
| `src/sniper/analytics.py`, `scoring.py` | ставки, PnL, Z-тест с кластерами, BH/BY, shrinkage, флаги, тиры |
| `src/sniper/copyability.py` | строгий фильтр быстрых сделок |
| `src/sniper/backtest.py`, `metrics.py`, `fees.py` | симуляция копировщика, журнал сделок, метрики, комиссии по датам |
| `src/sniper/plan.py`, `walkforward.py` | план периодов, сбор, оценка, отчёты, защита holdout |
| `src/sniper/live/` | фаза 2: RTDS, стакан, сигналы, paper-движок, риск, Telegram, отчёт |
| `research/` | разовые исследовательские скрипты |

> ⚠️ Репозиторий публичный: никогда не коммитьте приватные ключи, токены и `.env`.
