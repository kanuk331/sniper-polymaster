# NEXT_STEPS

1. Сбор идёт фоном: `research/iteration2_pipeline.sh`, прогресс в `logs/status.log` и `logs/collect-<период>.log`.
   Порядок: dev p2024-10 → p2025-05 → p2025-11 → holdout p2025-02 → p2025-08 → p2026-02 → p2026-07 →
   dev p2026-05 → p2026-09. Dev-периоды оцениваются сразу после сбора (`logs/eval-<период>.log`). После перезапуска
   песочницы запустить скрипт снова: готовые периоды пропускаются, бэкфилл продолжается по покрытию.
2. Проверить dev-оценки p2024-10, p2025-05, p2025-11 на ошибки (только исправления, без подбора порогов) →
   коммит = заморозка кода.
3. После заморозки не менять `src/` и `config/`, пока не оценены все holdout-периоды:
   `uv run sniper wf evaluate --role holdout --periods <p> --confirm-holdout` для каждого собранного периода.
4. `uv run sniper wf report --role dev` и `--role holdout` → `docs/research/2026-11-06-iteration2-results.md`,
   README, handoff.
5. Telegram: владелец пишет @flamered_bot /start → `getUpdates` даёт chat_id → `SNIPER_TELEGRAM_CHAT_ID` в `.env` →
   тестовое сообщение через `TelegramNotifier`.
6. После итогов: `wf watchlist` на сегодня и paper-прогон с алертами на сервере 24/7 (сервер за владельцем).
