# NEXT_STEPS

1. Дождаться сбора dev-периодов (/tmp/wf_collect.log), прогнать `sniper wf evaluate --role dev`, затем `sniper wf report --role dev`.
2. Принять результат фазы 2 от агента: ревью, подключить `sniper live` в CLI, тесты, live smoke со снимками стакана.
3. Заморозить код (коммит), прогнать `sniper wf evaluate --role holdout --confirm-holdout`, затем `sniper wf report --role holdout`.
4. Итоговый отчёт итерации 2 в `docs/research/`, обновить README и handoff.
