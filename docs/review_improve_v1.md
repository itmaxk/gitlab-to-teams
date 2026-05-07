# Review Improve V1

## Цель

Улучшить AI-review в `/review` для проекта AdInsure implementation так, чтобы модель проверяла не только измененные файлы, но и связанные элементы конфигурационного конструктора.

Проект работает как набор взаимосвязанных конфигураций: `dataSource`, `dataProvider`, `etlService`, `sinkGroup`, `route`, `document`, `component`, `view`, `printoutRelation` и другие сущности часто ломаются не внутри одного файла, а на границе связей.

## Базовые настройки V1

- Целевой проект для профиля: `D:\Projects\adinsure-impl\implementation-master`.
- Основная зона анализа: `configuration/@config-rgsl`.
- SQL target по умолчанию: `PostgreSQL 17.5+`.
- SQL target должен редактироваться в настройках проекта, если позже потребуется другая совместимость.
- Multi-db compatibility в V1 не проверяется как обязательное требование.
- Глубина графового контекста по умолчанию: 1 hop от измененного файла.

## Найденные закономерности конструктора

### DataSource и DataProvider

- `dataSource/<CodeName>/configuration.json` использует `dataProvider.codeName`, `dataProvider.type`, `dataProvider.version`.
- Для `DatabaseDataProvider` важен `query.postgres.handlebars`.
- Для review одного `dataSource` нужно подтягивать `inputSchema.json`, `resultSchema.json`, `inputMapping.js`, `resultMapping.js`, связанный `dataProvider` и postgres query.
- Для review одного `dataProvider` нужно находить потребляющие его `dataSource`.

### View и DataExport

- `view` и `dataExport` часто используют поле `dataSource`.
- UI-фильтры, result content и export должны согласовываться с `resultSchema.json` и `resultMapping.js` соответствующего `dataSource`.
- При изменении `view` или `dataExport` нужно добавлять связанный `dataSource`.

### EtlService

- `etlService` использует `mainDataSource`.
- `sourceMappings/<mainDataSource>` должен соответствовать `mainDataSource`.
- `sinks`, `completionSinks`, `errorSinks` используют имена sink-ов, для которых должны существовать `sinkMappings/<sink.name>`.
- `sinks[*].ref` указывает на `sinkGroup`.
- `sinks[*].fetch.configuration.name` указывает на `dataSource`.
- `document`, `masterEntity`, `documentTransition`, `class`, `database`, `api`, `notification`, `sequence` внутри sink-ов являются отдельными типами связей, которые нужно давать модели как контекст.

### Route, IntegrationService и SinkGroup

- `route`, `integrationService` и `sinkGroup` используют такой же sink-паттерн, как `etlService`.
- `route.condition.documentStates` должен быть согласован с состояниями документа.
- `documentTransition.transition.configurationName`, `configurationVersion`, `transitionName` должны проверяться против конфигураций документов и маршрутов.
- `fetch.configuration.name` должен ссылаться на существующий `dataSource`.
- `ref` должен ссылаться на существующий `sinkGroup`.

### Document, MasterEntity и Component

- `document` и `masterEntity` связываются с `components`, `UI`, `validation`, `enrichment`, `flowRules`, `translation`.
- `document.states` и `document.transitions` должны учитываться при проверке `route` и `documentTransition`.
- `component` часто имеет `ClientAction`, `validation`, `enrichment`, `additionalContext`, `translation`, `lib`.
- При изменении component/client action нужно подтягивать владельцев component через `document`, `masterEntity` или `view`.

### PrintoutRelation, Printout и Notification

- `printoutRelation` связывает `sourceConfigurationName/sourceConfigurationType/sourceConfigurationVersion` с `targetPrintout/targetPrintoutVersion`.
- `printoutRelation.additionalDataSources` требует проверки `sourceMappings/<DataSource>`.
- `printout` связан с templates/assets и rendering settings.
- `notification.channel.templates.subject/content` должны указывать на существующие template-файлы.

### Package и JS imports

- В `configuration/@config-rgsl/*/package.json` есть зависимости на другие `@config-rgsl/*` и `@config-system/*` пакеты.
- JS-файлы используют `require('@config-rgsl/...')` и `require('@config-system/...')`.
- При изменении common helper в `lib` нужно подтягивать ключевых потребителей или хотя бы показывать модели список затронутых импортов.

## Правила AI-review V1

- Проверять битые ссылки между конфигурациями.
- Проверять несовпадения `sink.name` и директорий `sinkMappings/<name>`.
- Проверять несовпадения `mainDataSource` и `sourceMappings/<mainDataSource>`.
- Проверять `dataSource.dataProvider.codeName` против существующих `dataProvider`.
- Проверять `fetch.configuration.name` против существующих `dataSource`.
- Проверять schema/mapping drift: поля `inputMapping.js`, `resultMapping.js`, `inputSchema.json`, `resultSchema.json`.
- Проверять SQL-параметры в `query.postgres.handlebars` против `inputMapping.js` и `inputSchema.json`.
- Проверять states/transitions для `route`, `document`, `documentTransition`.
- Проверять наличие notification templates, printout source mappings и translations там, где они ожидаются соглашением проекта.

## Таблица выполнения

| Блок | Что нужно сделать | Статус |
|---|---|---|
| Исследование проекта | Найти основные типы конфигураций и связи конструктора | Выполнено |
| DataSource/DataProvider | Зафиксировать связь `dataSource.configuration.dataProvider -> dataProvider/query.postgres.handlebars` | Выполнено |
| ETL/Sinks | Зафиксировать связи `mainDataSource`, `sourceMappings`, `sinks`, `sinkMappings`, `ref -> sinkGroup` | Выполнено |
| Route/Integration/SinkGroup | Зафиксировать общий sink-паттерн: `fetch`, `document`, `documentTransition`, `class`, `database`, `ref` | Выполнено |
| UI/Document/Component | Зафиксировать связи `document/masterEntity/view/component` с UI, validation, enrichment, translations | Выполнено |
| Printout/Notification | Зафиксировать связи `printoutRelation`, `sourceMappings`, `targetPrintout`, notification templates | Выполнено |
| Package imports | Учесть `package.json` и JS `require('@config-rgsl/...')` как связи контекста | Выполнено |
| Project profile | Добавить редактируемые настройки проекта в `/review` | Выполнено |
| Graph resolver | Реализовать индекс конфигураций и расширение контекста на 1 hop | Выполнено |
| Prompt rubric | Добавить в AI prompt секцию Constructor Graph Checks | Выполнено |
| UI transparency | Показать в `/review`, какие связанные файлы попали в контекст | Выполнено |
| Tests | Добавить unit/regression tests для resolver и review context | Выполнено |
| Verification | Запустить targeted tests и зафиксировать результат | Выполнено |

## Журнал выполнения

| Дата | Действие | Проверка |
|---|---|---|
| 2026-05-07 | Зафиксирован V1-план улучшения `/review` и таблица выполнения | Документ создан в `docs/review_improve_v1.md` |
| 2026-05-07 | Реализованы project profile, graph resolver, prompt rubric и UI transparency для `/review` | `pytest tests/test_review_project_context.py tests/test_review_batching.py tests/test_review_settings.py tests/test_review_jobs.py` |
| 2026-05-07 | Проведена общая регрессионная проверка после реализации V1 | `pytest` |

## Как обновлять статус

- Использовать значения: `Не выполнено`, `В работе`, `Выполнено`, `Блокер`.
- После каждого крупного шага обновлять таблицу выполнения.
- В журнал добавлять дату, действие и проверку.
- Не добавлять секреты, значения из `.env` или приватные credentials.
