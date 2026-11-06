# sniper.polymaster

Polymarket Sniper ищет игроков с устойчивым преимуществом, проверяет, навык это или удача, и моделирует,
сколько заработал бы копировщик. Позже система будет слать алерты в Telegram и вести paper trading.

**Статус:** фаза 1 (данные, скоринг, бэктест копирования). Только чтение публичных API: без ключей и без торговли.
- Исследование и обоснование методики: [docs/research/2026-11-06-polymarket-sniper.md](docs/research/2026-11-06-polymarket-sniper.md)
- Результаты фазы 1 (walk-forward копирования): [docs/research/2026-11-06-phase1-walkforward.md](docs/research/2026-11-06-phase1-walkforward.md)
- Текущее состояние и следующие шаги: [docs/handoff](docs/handoff)

## Установка

```bash
uv sync                 # Python ≥ 3.11
cp .env.example .env    # необязательно: путь к БД, доля лимитов API
uv run pytest           # unit-тесты
```

## Пайплайн

```bash
# 1. Вселенная кандидатов
#    a) из рынков, закрытых в окне: отбор без взгляда на результат, подходит для бэктестов
uv run sniper universe markets --end-min 2026-03-01T00:00:00Z --end-max 2026-08-25T00:00:00Z --markets 150
#    b) из лидербордов: смещена в пользу победителей, годится только для live-watchlist
uv run sniper universe leaderboard --periods month,all --categories overall,politics,sports --limit 200

# 2. Активность кошельков (сделки, включая maker-сделки, а также merge, redeem, conversion, ребейты)
uv run sniper backfill --since 2026-01-01 --max-items 20000

# 3. Резолюции токенов, события, neg-risk, категории и комиссии рынков
uv run sniper enrich

# 4. Скоринг: фильтры архетипов → Z-тест против цены входа (кластеры событий) → BH/BY → shrinkage → тир
uv run sniper score --out reports/scores.csv

# 5. Walk-forward: скоринг до среза, симуляция копирования после него при разных задержках
uv run sniper backtest --split 2026-09-01 --gap-days 7 --horizon-days 45 --latencies 10,60,300,900
```

Тиры: `candidate` (BH q ≤ 0.05 и 20-й перцентиль θ̃ > 0), `watch` (q ≤ 0.10, θ̃ > 0), `noise`,
`insufficient` (меньше 30 событий или меньше 90 дней истории), `excluded` (бот, маркетмейкер или арбитражёр,
скупка «почти наверняка», прибыль от одного события).

## Структура

| Модуль | Назначение |
|---|---|
| `src/sniper/http.py` | async HTTP: token bucket по маршрутам, ретраи 429/5xx, cursor-пагинация |
| `src/sniper/polymarket.py` | клиенты Data API v2, Gamma, CLOB (минутная история цен) |
| `src/sniper/db.py` | SQLite-хранилище (идемпотентные upsert) |
| `src/sniper/ingest.py` | вселенная, бэкфилл, обогащение, окна цен |
| `src/sniper/analytics.py` | ставки, PnL, признаки, статистика навыка, FDR, empirical Bayes |
| `src/sniper/scoring.py` | флаги архетипов, тиры, CSV и JSON |
| `src/sniper/backtest.py` | симуляция копировщика, walk-forward, сводки |
| `research/leaderboard_probe.py` | первая быстрая проба лидерборда (исследование) |

> ⚠️ Репозиторий публичный: никогда не коммитьте приватные ключи, API-ключи и `.env`.
