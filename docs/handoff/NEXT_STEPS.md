# NEXT_STEPS

1. Не менять `src/` и `config/`, пока в `logs/holdout-status.log` нет строки `holdout done`: holdout-оценка требует
   замороженного кода. Сбор идёт через `research/iteration2_pipeline.sh` (`logs/status.log`), holdout-оценка через
   `research/iteration2_holdout.sh` (`logs/holdout-status.log`). После перезапуска песочницы запустить оба скрипта
   снова: готовые периоды пропускаются, уже оценённый holdout не повторяется.
2. `uv run sniper wf report --role holdout` → решение по H1/H2 (`reports/walkforward/report-holdout.md`).
3. После оценки dev p2026-05 и p2026-09: `uv run sniper wf report --role dev`.
4. Итог в `docs/research/2026-11-06-iteration2-results.md`, README, handoff.
5. Telegram: владелец пишет @flamered_bot /start → `getUpdates` даёт chat_id → `SNIPER_TELEGRAM_CHAT_ID` в `.env` →
   тестовое сообщение через `TelegramNotifier`.
6. После итогов: `wf watchlist` на сегодня и paper-прогон с алертами на сервере 24/7 (сервер за владельцем).
