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
