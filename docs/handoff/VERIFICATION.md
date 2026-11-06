# VERIFICATION

## 2026-11-06 — проверки доступности API (curl из песочницы)
| Запрос | Результат |
|---|---|
| `GET data-api.polymarket.com/v1/leaderboard`, `/trades` | HTTP 410 `"Data API v1 has been retired"` → миграция на `/v2/` |
| `GET data-api.polymarket.com/v2/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=3` | HTTP 200, поля `rank,user_id,pnl,volume,user_name`, cursor-пагинация |
| `GET data-api.polymarket.com/v2/trades?limit=2` | HTTP 200, глобальная лента сделок (`proxy_wallet, side, price, size, timestamp, transaction_hash`) |
| `GET data-api.polymarket.com/v2/activity` без `user` | HTTP 400 `required query param 'user'` |
| `GET gamma-api.polymarket.com/markets?limit=1&active=true` | HTTP 200 |
| `GET clob.polymarket.com/time` | HTTP 200 |
| `GET polymarket.com/api/geoblock` | `{"blocked":true,"country":"US"}`: из песочницы торговать нельзя |

Тестов кода пока нет: код ещё не написан.

## 2026-11-06 13:50 UTC — `python3 research/leaderboard_probe.py --top 100 --period month`
Выполнено за 37 с, ~600 запросов к публичному Data API v2, без ошибок.
Эвристики: short_crypto_bot (≥50% сделок в up/down-рынках), mm_or_arb (≥30% рынков с покупкой обоих исходов
или ≥25% месячного PnL из ребейтов и наград), near_certainty_grinder (≥50% объёма покупок по цене ≥0.95),
one_bet_concentration (лучшая позиция ≥50% суммарного PnL позиций окна), thin_sample (<20 рынков за 30 дней),
not_profitable_by_economic_pnl (Δeconomic_pnl за месяц ≤0).

| Метрика | Значение |
|---|---|
| Кошельков с ≥1 флагом | 56 / 100 (без флагов: 44) |
| one_bet_concentration | 24 |
| near_certainty_grinder | 22 |
| mm_or_arb | 21 |
| thin_sample | 13 |
| not_profitable_by_economic_pnl | 6 |
| short_crypto_bot | 1 |
| Σ PnL лидерборда против Σ Δeconomic_pnl (`/v2/user-pnl`, 1m) | $78.6M против $42.0M (медиана отношения 0.68; у 44 кошельков < 0.5) |

Ограничения: по каждому кошельку берутся только последние ≤1000 сделок; у 34 кошельков в каком-то из статусов
>1000 позиций (первая страница, поэтому оценки концентрации приблизительные). Эвристики грубые: это проверка
идеи, а не модель скоринга. Причина расхождения PnL лидерборда и `user-pnl` в документации не описана (не проверена).
