# План реализации и проверки автономного снимка PWA

Этот документ превращает архитектурное решение из [`standalone-pwa-ledger-snapshot.md`](./standalone-pwa-ledger-snapshot.md) в последовательность работ. Он не является разрешением сразу менять рабочую логику: каждый этап должен быть подтверждён тестом и проверкой на фактических плагинах книги.

## 1. Принцип разработки

Работа выполняется по правилу:

1. Сначала регрессионный тест, воспроизводящий обнаруженную потерю смысла.
2. Затем минимально необходимая реализация очередного архитектурного слоя.
3. Затем тесты этого слоя и полный контейнерный барьер.
4. Только после зелёного Docker image и проверки RustLedger — развёртывание и production QA.

Недопустимы доказательства, основанные только на локальном виртуальном окружении, вручную дополненном образе или проверке Python parser вместо PWA parser boundary.

## 2. Предлагаемая структура модулей

Точные имена могут быть уточнены при реализации, но обязанности должны быть разделены.

```text
cashier_snapshot/
  source_index.py          # чтение исходных файлов, директивные блоки, происхождение
  materialize.py           # запуск загрузчика Beancount и Python-плагинов
  plugin_policies.py       # зарегистрированные правила вывода поддерживаемых plugins
  reconcile.py             # retained/removed/generated/transformed/unsupported
  printer.py               # вывод только generated/transformed entries
  builder.py               # сборка автономного main.bean
  validation.py            # проверки инвариантов снимка и диагностические отчёты
```

Endpoint в `main.py` должен вызывать высокоуровневый builder, а не содержать сложную логику сопоставления внутри HTTP handler.

## 3. Этап 0: зафиксировать текущий дефект тестом

### 3.1. Расширить fixture

Существующий `tests/fixtures/plugin_ledger` должен получить операции с реальной проблемной семантикой:

- округлённая валютная конверсия с `@@`, которая становится несбалансированной при выводе округлённого `@`;
- ещё одна операция с крупной разницей, которую нельзя правдоподобно скрыть tolerance;
- настоящий исходная проводка с `@`, который должен остаться проводка с ценой за единицу.

Fixture обязан сохранять действующие production-like Python plugins, включая `beancount_lazy_plugins.filter_map`; отдельный упрощённый файл без плагинов не докажет correctness всей границы.

### 3.2. Красный контейнерный тест

Тест должен идти через существующий рабочий путь:

```text
Docker image из PR
-> server container с fixture workspace mounted read-only
-> GET /infrastructure?file_path=main.bean
-> @rustledger/wasm@0.14.1
```

До реализации ожидается:

- endpoint может вернуть `200`;
- итоговая RustLedger проверка падает на тестовый случай с округлённой валютной конверсией через `@@`;
- диагностическая проверка показывает, что исходник содержит `@@`, а выданный файл потерял точную семантику.

После реализации этот же тест обязан стать зелёным, не за счёт tolerances, а за счёт корректного снимка.

## 4. Этап 1: индекс исходной книги

### Задача

Создать `SourceLedgerIndex`, который позволяет обращаться к исходной текстовой директиве по происхождению и строить порядок flattening.

### Необходимые свойства

- Путь каждого файла нормализован относительно корня книги и не выходит за него.
- Include glob раскрывается в стабильном порядке.
- Каждый блок директивы сохраняется как исходный текст.
- Многострочные `plugin` directives распознаются целиком и могут быть исключены целым блоком.
- Transaction block сохраняет posting строки дословно, включая `@@`.
- Индекс не использует распечатанные parsed entries как источник исходного текста.

### Тесты

- одиночный include;
- glob include;
- вложенный include;
- path traversal/выход из корня запрещён;
- многострочный plugin config исключается без обломков;
- transaction с `@@` сохраняется дословно;
- transaction с `@` сохраняется дословно.

## 5. Этап 2: инвентаризация поведения плагинов

До общего алгоритма reconciliation необходимо получить доказанное поведение каждого production plugin на малых fixtures.

### Формат результата аудита

Для каждого плагина добавить тест и таблицу политики:

| Поле | Содержание |
|---|---|
| Plugin name | Полное имя Python plugin |
| Input directive types | Что он читает/потребляет |
| Output action | Добавляет / изменяет / удаляет / заменяет |
| Provenance | Как связать output с input |
| Snapshot rule | Что выводить в автономный файл |
| Unsupported cases | В каких случаях builder должен отказать |

### Приоритет аудита

1. `pad` / `balance` / `group_pad_transactions`, так как они уже потребовали server canonicalization.
2. `filter_map`, потому что он изменяет transactions/metadata и обязателен в production smoke.
3. `share` и `effective_date`, потому что они могут преобразовывать пользовательские transactions.
4. `recur` и `split`.
5. Plugins, добавляющие цены/аккаунты/valuation entries.

### Правило безопасности

До регистрации политики plugin считается неподдерживаемым автономным сборщиком. Это лучше, чем тихо выдать неверную бухгалтерскую книгу.

## 6. Этап 3: сопоставление исходного и материализованного результата

### Базовый алгоритм

`DirectiveReconciler` получает:

- `SourceLedgerIndex`;
- итоговые entries после Python loader/plugins;
- реестр правил активных plugins.

Результат — последовательность export decisions:

```text
retain_source(source_block)
omit_source(source_block, reason)
print_generated(entry, policy)
print_transformed(entry, source_block, policy)
reject(entry_or_source, reason)
```

### Правила сопоставления

- Основной идентификатор исходной директивы: подтверждённое происхождение по `filename`, `lineno` и типу.
- Если plugin создаёт новую запись без исходного блока, она может быть `generated` только согласно политике этого plugin.
- Если итоговая запись ссылается на исходный блок, но её значимые поля изменились, она не может быть `retained`; требуется `transformed` policy.
- Если исходная запись исчезла и policy не объясняет удаление, сборка падает.
- Если появились неизвестные output entries, сборка падает.

### Смысловые поля transaction

Для определения, является ли transaction изменённой, учитывать как минимум:

- date, flag, payee, narration, tags, links;
- postings: account, units, currency, cost, price, posting metadata;
- entry metadata, кроме служебных provenance полей.

## 7. Этап 4: безопасный вывод материализованного результата

### Для `retained`

Вывести исходный текст без повторного форматирования.

### Для `generated`

Печатать специально предназначенным printer-ом. Проверять, что вывод повторно разбирается RustLedger и не теряет баланс.

### Для `transformed`

Печатать изменённую запись на основании политики plugin. Если исходный posting с `@@` семантически сохранился, его точная price-аннотация должна быть перенесена в вывод преобразованной записи.

### Запрещённый запасной режим

Нельзя в случае неопределённости просто печатать итоговый entry стандартным `EntryPrinter` и надеяться, что RustLedger его примет. Текущий production дефект возник именно из-за такого поведения.

## 8. Этап 5: сборщик и endpoint

`StandaloneSnapshotBuilder` заменяет только способ формирования root-файла при запросе:

```http
GET /infrastructure?file_path=main.bean
```

Сохраняются:

- текущая JSON-форма `{ "content": "..." }`;
- обработка errors через HTTP `422`, если книгу нельзя безопасно материализовать;
- безопасность разрешения путей;
- read-only режим.

В diagnostic logging должны быть доступны счётчики:

- source directives retained;
- include/plugin directives omitted;
- operational directives consumed;
- generated entries emitted;
- transformed entries emitted;
- unsupported entries/plugins;
- final artifact size.

В HTTP response эти внутренние сведения по умолчанию добавлять не требуется, чтобы не менять контракт PWA.

## 9. Проверки корректности

### 9.1. Модульные тесты

- Блоки исходного текста и flattening.
- Policies каждого plugin.
- Reconciliation решений.
- Сохранение `@@`/`@`.
- Вывод generated metadata.
- Отказ на неизвестном изменяющем plugin.

### 9.2. Integration tests endpoint

- Root возвращает автономный файл.
- В нём отсутствуют `include` и Python `plugin`.
- `pad` не выполняется повторно.
- Созданные plugin entries присутствуют.
- Rounded FX `@@` не потеряны.

### 9.3. Контейнерный барьер

Существующий Docker acceptance workflow должен быть расширен, а не заменён:

- образ собирается из текущего PR;
- container запускается без установки зависимостей внутрь него;
- fixture монтируется read-only;
- API выдаёт автономный файл;
- `@rustledger/wasm@0.14.1` разбирает результат с нулём ошибок;
- fixture содержит `filter_map` и rounded FX `@@` случаи.

### 9.4. Проверка в рабочей среде после deploy

Обязательный результат:

```text
GET /api/infrastructure?file_path=main.bean -> 200
Ledger files to OPFS                         ✓
Root book selected                           ✓ main.bean
Full ledger parsed                           ✓
Parse errors                                 0
```

После этого повторить проверку локальной offline transaction persistence и реального multi-currency dashboard/vendor flow.

## 10. Разбиение будущей реализации на PR

Эта документационная ветка не реализует поведение. Для кода рекомендуется последовательность:

### PR A: красная регрессия и аудит

- Добавить rounded FX fixture и падающий/помеченный ожидаемым дефектом Docker boundary test.
- Добавить документированные тесты поведения production plugins.
- Не пытаться маскировать ошибку tolerance options.

### PR B: source index и безопасное flattening

- Реализовать чтение/индексацию source directives и удаление инфраструктурных directives.
- Без включения нового builder в рабочий endpoint, пока plugin reconciliation не доказан.

### PR C: policy-driven reconciliation и автономный builder

- Реализовать реестр политик и сборщик.
- Переключить root endpoint на новую выдачу.
- Сделать Docker/RustLedger rounded-FX gate зелёным.

### PR D: deploy и production verification

- Развернуть серверный образ.
- Выполнить полный `/sync` QA и задокументировать результат.

Разбиение может быть объединено, если реализация остаётся проверяемой, но запрещено мерджить изменение endpoint без зелёного контейнерного RustLedger барьера на realistic fixture.
