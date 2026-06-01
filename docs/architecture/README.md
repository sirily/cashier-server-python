# Архитектура автономной книги Cashier PWA

Эта папка описывает проект следующего изменения серверной границы формирования снимка после диагностики потери точного смысла цены `@@` в рабочей среде.

## Документы

1. [`standalone-pwa-ledger-snapshot.md`](./standalone-pwa-ledger-snapshot.md) — целевая архитектура формирования одного автономного `main.bean` для принятого Stage 1 read-only sync.
2. [`standalone-pwa-ledger-implementation-plan.md`](./standalone-pwa-ledger-implementation-plan.md) — этапы реализации, регрессионные и контейнерные проверки Stage 1.
3. [`production-plugin-export-policy-audit.md`](./production-plugin-export-policy-audit.md) — обязательный аудит поведения production Python-плагинов перед безопасной сборкой снимка.
4. [`stage-2-manual-transaction-writeback.md`](./stage-2-manual-transaction-writeback.md) — Stage 2: узкий writeback ручных PWA-транзакций через `POST /api/xact` в единственный rw-файл `/workspace/manual_transactions.bean`.

## Зафиксированный вывод

Текущий PWA-движок `@rustledger/wasm@0.14.1` корректно разбирает исходные проводки Beancount с `@@`. Ошибка в рабочей среде возникает потому, что сервер повторно выводит исходные записи после разбора Python Beancount с потерей исходной формы и округляет вычисленную форму цены за единицу.

Поэтому следующее серверное решение должно выдавать один автономный файл, сохраняя исходную семантику пользовательских записей и добавляя только доказанно корректные результаты серверных плагинов.