# sniper.polymaster

Polymarket Sniper — система, которая ищет игроков с устойчивым преимуществом, проверяет «навык или удача»,
шлёт алерты в Telegram и помогает копировать сделки с встроенным риск-менеджментом.

**Статус:** фаза исследования, кода продукта пока нет.
- Итоги исследования и рекомендации: [docs/research/2026-11-06-polymarket-sniper.md](docs/research/2026-11-06-polymarket-sniper.md)
- Текущее состояние и следующие шаги: [docs/handoff](docs/handoff)

## Проба данных

```bash
python3 research/leaderboard_probe.py --top 100 --period month --out /tmp/probe.json
```

Скрипт обращается только к публичным read-only эндпоинтам Data API v2 и не требует ключей.
Нужен Python 3.10+, сторонних зависимостей нет.

> ⚠️ Репозиторий публичный: никогда не коммитьте приватные ключи, API-ключи и `.env`.
