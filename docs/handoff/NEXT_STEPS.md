# NEXT_STEPS

1. `src/sniper/ingest.py` + `src/sniper/cli.py`: `universe leaderboard|markets`, `backfill`, `enrich`.
2. Аналитика и скоринг (`analytics.py`, `scoring.py`) с unit-тестами.
3. Бэктест копирования + walk-forward (`backtest.py`), цены из CLOB `/prices-history?fidelity=1`.
4. Живой прогон, отчёт в `docs/research/`, обновить README.
5. Фаза 2 (после фазы 1): Telegram-бот (aiogram), RTDS-поток, paper-портфель.
