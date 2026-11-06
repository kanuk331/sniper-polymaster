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
