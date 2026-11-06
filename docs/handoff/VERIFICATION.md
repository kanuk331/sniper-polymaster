# VERIFICATION

## 2026-11-06 — доступность API (curl из песочницы)

| Запрос | Результат |
|---|---|
| `GET data-api.polymarket.com/v1/leaderboard`, `/trades` | HTTP 410 `"Data API v1 has been retired"` → миграция на `/v2/` |
| `GET data-api.polymarket.com/v2/leaderboard?time_period=month&sort_by=PNL&limit=3` | HTTP 200, `user_id` = адрес кошелька (42 символа), cursor-пагинация |
| `GET data-api.polymarket.com/v2/trades?limit=2` | HTTP 200, глобальная лента сделок |
| `GET data-api.polymarket.com/v2/activity` без `user` | HTTP 400 `required query param 'user'` |
| `GET data-api.polymarket.com/v2/positions?status=CLOSED,REDEEMABLE` | HTTP 400: статус принимает только одно значение |
| `GET gamma-api.polymarket.com/markets?limit=1&active=true` | HTTP 200 |
| `GET clob.polymarket.com/time` | HTTP 200 |
| `GET polymarket.com/api/geoblock` | `{"blocked":true,"country":"US"}`: из песочницы торговать нельзя |

## 2026-11-06 — проба лидерборда (`research/leaderboard_probe.py --top 100 --period month`)

**Актуальный запуск, 14:03 UTC** (сделки с `taker_only=false`): 32 с, ~600 запросов, exit 0.

| Метрика | Значение |
|---|---|
| Кошельков с ≥1 флагом | 55 / 100 (без флагов: 45) |
| one_bet_concentration | 24 |
| mm_or_arb | 23 |
| near_certainty_grinder | 15 |
| thin_sample | 13 |
| not_profitable_by_economic_pnl | 6 |
| short_crypto_bot | 1 |
| Σ PnL лидерборда против Σ Δeconomic_pnl (`/v2/user-pnl`, 1m) | $78.6M против $42.1M; медиана отношения 0.68; у 43 кошельков < 0.5 |
| №1 лидерборда | $4.67M в лидерборде против ≈$52k Δeconomic_pnl |

**Заменённый запуск, 13:50 UTC:** без `taker_only=false`, поэтому `/v2/trades` по умолчанию отдавал только taker-сделки.
Тогда флаги были у 56/100 (mm_or_arb 21, near_certainty_grinder 22). Результат заменён после того, как выяснилось,
что `taker_only` по умолчанию равен true.

Ограничения: по каждому кошельку берутся последние ≤1000 сделок; у 34 кошельков в каком-то статусе >1000 позиций
(первая страница, поэтому оценки концентрации приблизительные). Эвристики грубые: это проверка идеи, а не модель скоринга.
`python3 -m py_compile research/leaderboard_probe.py`: ok. Unit-тестов нет (исследовательский скрипт).

## 2026-11-06 — перепроверка внешних утверждений из отчётов субагентов

| Утверждение | Как проверено | Итог |
|---|---|---|
| Data API v1 выключен 02.11.2026 | docs migrate/data-api-v1-to-v2 + HTTP 410 | ✅ |
| CLOB V2 с 28.04.2026, залог pUSD, EIP-712 domain "2" | docs v2-migration | ✅ |
| Unified SDK `polymarket-client` / `@polymarket/client` 0.17.0; `py-clob-client` архивирован | GitHub API, PyPI, npm | ✅ |
| Тайпосквоты `py-clob-clients` (PyPI), `polymarket-clob-client` (npm) — malware | OSV MAL-2026-3220, MAL-2026-4643 | ✅ |
| Комиссия тейкера = C·feeRate·p(1−p), ставки 0–0.07, мейкеры не платят | docs trading/fees | ✅ |
| RU, BY, US, GB, DE, FR, PL и др. — close-only на сайте и в API | docs api-reference/geoblock | ✅ |
| Session Key не может выводить средства (бета) | docs trading/session-keys | ✅ |
| 3% умелых, 29% везунчиков забрали 69% прибыли, 44% устойчивость | Yale Insights | ✅ |
| ~25% объёма — вероятный wash trading (Columbia) | Decrypt | ✅ |
| Jia & Sun: живой тест копирования не повторил доходность | arXiv abstract | ✅ (детали: 112 с, убыток — не проверены) |
| `taker_only=true` по умолчанию в `/v2/trades` | OpenAPI + сравнение выборок субагентом | ✅ (учтено в пробе) |
| RTDS activity/trades: весь поток с `proxyWallet`, p50 ≈ 0.3 с | замер субагента на Node | ⚠️ не перепроверено |
| Запрет VPN в ToS, Builder Code of Conduct требует geoblock | отчёт субагента | ⚠️ не перепроверено |

## 2026-11-06 — фаза 1, чекпоинт 1
- `uv sync`, затем `uv run pytest` → **12 passed** (test_http: пагинация, повторная отправка фильтров, max_items,
  ретрай 429 с Retry-After, отсутствие ретрая на 400, лимит ретраев; test_db: идемпотентный upsert, сохранение
  идентичных филлов внутри одной транзакции, вселенная, токены и рынки, окна цен, парсинг времени Gamma).
- `uv run ruff check src tests` → All checks passed.
- Живые проверки API: см. HISTORY (порядок сделок, окна, Gamma `condition_ids` + `closed`, хранение истории цен в Data API и CLOB).

## 2026-11-06 — фаза 1, чекпоинт 2
- `uv run pytest` → **27 passed** (добавлены test_analytics: дисперсия событий, восстановление θ по MLE на симуляции,
  BH/BY, EB-shrinkage, срез as_of и время резолюции, двусторонние ставки, Z для победителя, «одно событие = одна
  ставка», cash-flow PnL с sell/redeem/merge, признаки концентрации и гриндера; test_scoring: флаги, тиры, сквозной
  скоринг на SQLite). `ruff check` → All checks passed.
- Два падения тестов при первом прогоне оказались ошибками тестовых данных, а не кода: в тесте EB было τ²=0, и сжатие
  к среднему полное; `pnl_ex_top3` в тесте равен 0.6, а не ≤0.3. Тесты исправлены, код не менялся.
- После этого найден и исправлен реальный недочёт EB: при τ²=0 апостериорное sd было 0. Теперь учитывается Var(μ̂),
  а μ̂ считается как взвешенное по точности среднее.
- Smoke на живых данных (/tmp/smoke.sqlite, 10 кошельков лидерборда, бэкфилл 4 из них с 2026-09-01, ≤3000 строк):
  universe → 10 кошельков; backfill → 10 817 строк за 11 запросов; enrich → 2253 токена и 1855 рынков за ~30 с;
  score → 4 excluded (hft_bot 3, mm_or_arb 2, near_certainty_grinder 2, concentrated 1), 6 insufficient
  (у 6 кошельков нет бэкфилла).

## 2026-11-06 — фаза 1, чекпоинт 3
- `uv run pytest` → **36 passed** (добавлены test_backtest: PriceBook, спред + комиссия + выплата при резолюции,
  пропуски premium / no_price / extreme_price, окно сигналов против истории для выходов, оценка открытых позиций
  по себестоимости, лимит сигналов, spearman, summarize и выбор кошельков; test_db: merge_windows с лимитом длины).
  `ruff check` → All checks passed.
- Smoke `sniper backtest --split 2026-10-01 --gap-days 3 --horizon-days 20 --latencies 10,300` на /tmp/smoke.sqlite:
  13 окон цен загружено; 1 excluded-кошелёк: 47–48 сделок, ROI копировщика 2.72 (10 с) и 2.74 (300 с) против 2.90
  и 2.87 у бенчмарка лидера, efficiency 0.94–0.95. Выборка из одного кошелька — это проверка механики, а не результат.

## 2026-11-06 — enrich: параллелизация
- `uv run pytest` → 36 passed после изменения `condition_ids_needing_market` (теперь возвращает dict) и enrich.
- Живой enrich: токены ~56 запросов/с (лимит Data API 800/10 с × 0.7), Gamma `/markets` 10 248 запросов.
  Не вернулись 14 039 токенов (2%) и 2 401 рынок: их ставки не участвуют в скоринге (нет резолюции),
  причина не исследована.

## 2026-11-06 — исправления после walk-forward v1
- Тесты: `test_follower_never_buys_cheaper_or_sells_dearer_than_leader` (устаревшая цена 0.10 после покупки лидера
  по 0.60 → копировщик платит 0.60; выход по min(история − спред, цена продажи лидера));
  `test_dersimonian_laird_not_swamped_by_noisy_wallets` (300 из 400 кошельков с SE=3: наивный τ²=0, DL ≈ 0.04).
- `uv run pytest` → **38 passed**; `ruff check` → All checks passed.
- Результаты v1 (`reports/wf-2026-09.json`, не в git) недействительны, см. HISTORY.

## 2026-11-06 — агрегация ордеров
- `uv run pytest` → **39 passed**; `ruff check` → All checks passed.
- Результаты v2 (`reports/wf-2026-09-v2.json`) заменяются v3: v2 завышен из-за копирования отдельных филлов.

## 2026-11-06 — итоговый walk-forward v3
- `uv run sniper backtest --snapshot wf-2026-09 --split 2026-09-01 --gap-days 7 --horizon-days 45
  --latencies 10,60,300,900 --sample-noise 150 --sample-excluded 150` → exit 0, 30 857 окон цен (все из кэша),
  427 кошельков. Таблица и разбор — в `docs/research/2026-11-06-phase1-walkforward.md`; цифры в отчёте сверены
  с `reports/wf-2026-09-v3.json` (ROI по тирам и задержкам, candidate без makeit99, доли пропусков, Spearman).
- Не проверено: исполнимость по реальному стакану (истории стакана нет), устойчивость на других срезах,
  доверительные интервалы.
- `uv run pytest` → 39 passed; `ruff check` → All checks passed.

## 2026-11-06 — итерация 2, чекпоинт 1
- `uv run pytest` → **44 passed** (новые: интервалы покрытия, статус окна complete/missing/capped, миграция sync_state,
  режим комиссий по датам, загрузка плана и проверка непересечения окон оценки). `ruff check` → чисто.
- Smoke `sniper wf collect` на временном плане (2 рынка, лимит 3000 строк): 40 кошельков → 30 complete и 10 capped,
  34 766 строк в окне [2024-11-02, 2025-06-15), 2526 токенов и 2185 рынков обогащено за 7.6 с.
- Проверка обрезки данных фазы 1 (SQL по live.sqlite): 505 кошельков обрезано; у 403 из них первая строка раньше
  2026-08-25, у 102 — позже; среди симулированных insufficient таких 95.

## 2026-11-06 — итерация 2, чекпоинт 2
- `uv run pytest` → **57 passed** (новые: test_metrics — журнал, бутстрэп-ДИ с кластерами, капитал и просадка, CSV-roundtrip
  с потоками, защита holdout; test_backtest — журнал, комиссии по датам, пропуски с бенчмарком, режим position;
  test_copyability — quick exits, пороги флагов, выборка только по разрешённым до среза рынкам). `ruff check` → чисто.
- Регрессия: `research/latency_discrepancy.py` воспроизвёл PnL фазы 1 у .ethguy, makeit99, willsom, tragz до цента;
  у arm1337 старый код на новых окнах цен даёт 1 629.60 / −409.01, то есть столько же, сколько новый (`/tmp/cmp_old.py`).
- Цифры пункта 1 взяты из `reports/latency_discrepancy.json` (бутстрэп 2000 повторов).

## 2026-11-06 — итерация 2, чекпоинт 3
- `uv run pytest` → **132 passed** (из них 72 теста фазы 2: парсинг RTDS, агрегация, стакан и VWAP, риск, paper-движок,
  Telegram через MockTransport, отчёт, runner на фейках; плюс мой `test_position_gate_signals_once_per_position`
  и `test_add_prices_many`). `ruff check` → чисто.
- В runner-тесте агента явно задан `signal_mode="order"`: тест проверяет путь «каждый ордер», а по умолчанию теперь `position`.
- `sniper live run --help` показывает `--signal-mode {position,order}` и `--min-leader-notional`.
- Не проверено: реальная отправка в Telegram (нет токена); выходы по продажам и merge в live — только unit-тесты.

## 2026-11-06 — тред melos, чекпоинт 1
- WIP `dbc52e3` в отдельной копии (`git archive` → /tmp): `uv run pytest` → 132 passed; ruff check/format → чисто.
- После доработки: `uv run pytest` → **133 passed**. Новый `test_report_decision_h1_h2`: два периода; H1 без сделок
  основной стратегии → None; H2 с ROI +20% на 100 событиях → подтверждено на уровне 97.5%; H2 с ROI 0 → не
  подтверждено. `ruff check`, `ruff format --check` → чисто.
- Telegram Bot API (только чтение): `getMe` → ok, @flamered_bot; вебхука нет; `getUpdates` → 0 обновлений,
  chat_id пока неизвестен. Переменные `SNIPER_TELEGRAM_*` в окружении проекта не заданы.
- `git check-ignore`: `.env`, `data/`, `logs/`, `reports/`, `.hoplite/attachments/` игнорируются.

## 2026-11-06 — тред melos: сбор и dev-оценка до заморозки
- Сбор: p2024-10 — 1379 кошельков (1283 полных, 96 activity_cap), 4698 запросов Data API, 254 с; p2025-05 — 1310
  (1178 / 132); p2025-11 — 1865 (1728 / 137); p2025-02 собран.
- Сбой: при `SNIPER_RATE_LIMIT_SHARE=0.85` Data API стал отвечать 429 с HTML-страницей Cloudflare. Это блок IP
  на минуты; 6 ретраев клиента (~1 мин) его не пережили, и `wf collect` p2025-11 и p2025-02 упали на `/tokens`.
  Доля возвращена к 0.6, в скрипт добавлены 3 попытки сбора с паузами 5 и 10 мин. После этого 429 нет.
- CLOB `/prices-history` при 0.85: 2024 ответа 429 с `Retry-After: 0` за оценку p2024-10. Клиент выполняет заголовок,
  общий темп ограничен его token bucket; получено 31 193 из 31 204 окон цен. При 0.6 (p2025-11): 0 ответов 429,
  22 625 из 22 625 окон.
- dev-оценки (код `594e268`, стратегия position, задержка 60 с). Candidate и watch после фильтра быстрых сделок нет
  ни в одном периоде, поэтому пул H2 здесь = noise:

| период | до фильтра: candidate / watch / noise | после фильтра: noise | сделок | ROI | 95% ДИ |
|---|---|---|---|---|---|
| p2024-10 | 1 / 1 / 38 | 8 | 316 | −10.3% | [−18.0%, −1.2%] |
| p2025-05 | 0 / 0 / 58 | 14 | 369 | −5.0% | [−12.3%, +3.2%] |
| p2025-11 | 2 / 0 / 60 | 9 | 228 | +3.9% | [−7.7%, +15.5%] |

  У всех четырёх лидов до фильтра флаг `fast_runaway`, у части также `uncopyable_edge`, `fast_roundtrip` или
  `copy_unverified`. Комиссии 0: до 2026 года taker-комиссий не было. Пропусков `no_price` ≤ 12 на группу.
  DerSimonian–Laird τ² = 0 во всех трёх периодах (Q < df). Excluded при 60 с: −7.7% / −4.0% / −2.3%.
- Ошибок в коде не найдено → заморозка; `git status -- src config` чистый.
