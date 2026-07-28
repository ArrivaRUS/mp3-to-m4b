# Архитектор #2 (GPT-5.6 Sol) — ранний нудж (D17)

Движок: codex-cli 0.144.6 · `-m gpt-5.6-sol` · `model_reasoning_effort=xhigh` · `-s read-only`
Вызов: один `codex exec` из корня репо, `< /dev/null`. EXIT=0 · 180 371 токен · ~9.5 мин.
Дата: 2026-07-28. Сырая трасса: `arch/early-nudge-codex.raw.txt`.

---

## 1. Целевая картина по шагам

1. `run_scan` проходит access gate, обходит каталог, собирает список mp3 и вычисляет `source_rev_for` без ffprobe.

2. Для новых/изменённых книг `_files_are_stable` проверяет не только размер/mtime уже найденных файлов, но и неизменность состава каталога. После debounce список mp3 и `source_rev` вычисляются повторно. До прохождения этого гейта манифест и окно не появляются.

3. Для всех стабильных новых книг одним проходом записываются скелетные манифесты. `confirm_token` создаётся здесь и больше не меняется до конца этой попытки подготовки.

4. `run_scan` строит `state.json` со статусом `discovering` и атомарно публикует его. Только после этого `_publish_showcase_and_maybe_open` фиксирует ledger и вызывает `_nudge_app`. Поэтому холодно запущенное приложение уже видит книгу.

5. Окно открывается примерно через 0,8 с: название из имени папки, число и имена файлов, плейсхолдеры длительностей/автора/обложки. «Собрать» конструктивно недоступна; «Пропустить» и «Позже» можно оставить активными.

6. Агент продолжает тот же `run_scan`:

   - `_probe_book` заполняет главы, длительности, теги и `source_samplerate`;
   - метаданные атомарно дописываются в манифест, затем обновляется `state.json`;
   - embedded extraction, `cover.search_web` и `cover.generate_variants` завершают обложку; готовые компоненты также могут публиковаться отдельными ревизиями фазы `discovering`.

7. Перед финализацией агент повторно получает список файлов и `source_rev_for`. Если входы изменились, старый результат подготовки не становится buildable.

8. Полный манифест одним атомарным replace переходит в `manifest_phase=ready`, `status=pending-confirm` и получает отдельный `build_token`. После этого публикуется `state.json`, что будит существующий `DispatchSourceFileSystemObject`.

9. Swift переключает отдельный skeleton-view на обычный confirm-view. Теперь создаётся типизированная build-authorisation и появляется «Собрать».

10. `confirm-build` проходит серверный гейт только по полностью готовому манифесту. Дальнейшие переходы `converting → done/error` остаются прежними.

---

## 2. ПРОТОКОЛ МАНИФЕСТА — фазы, поля, атомарность, переходы

### Форма schema v2

Общие неизменяемые в рамках одной подготовки поля:

- `manifest_schema: 2`
- `book_id`, `src_dir`
- `source_rev`, `source_rev_v`
- `confirm_token` — identity нуджа; один и тот же у скелета и полной версии
- `generation_id` — корреляция манифеста с showcase, не build-capability
- `manifest_seq` — монотонный номер атомарной версии
- `processed_keys`
- `discovered_at`, `ts`

Скелет:

- `manifest_phase: "discovering"`
- `status: "discovering"`
- `discovery.file_count`
- `discovery.files` с индексом и именем файла
- `preparation.metadata/cover` со значениями `pending|ready|failed`
- `params` с безопасными дефолтами
- нет `build_token`
- build-поля `chapters`, `source_samplerate`, `cover_options` либо отсутствуют, либо считаются только display-checkpoint, но не build payload

Полная версия:

- `manifest_phase: "ready"`
- `status: "pending-confirm"`
- `preparation.metadata == ready`
- `preparation.cover == ready`
- непустой структурно корректный `chapters`
- `title`, `author`, `total_duration_ms`, `source_samplerate`
- завершённые `cover_options` и `cover_selected`
- `build_token` — случайная capability, создаваемая только в финальном атомарном commit

`confirm-build` должен нести и прежние `source_rev`/`confirm_token`, и новый `build_token`.

### Почему нужен отдельный `build_token`

Одного `manifest_phase` недостаточно. Ошибочная версия приложения может записать команду по скелету; пока команда ждёт drain, агент успеет заменить скелет полной версией с теми же `source_rev` и `confirm_token`. Проверка только текущей фазы тогда примет старую команду.

У скелета `build_token` физически отсутствует. Поэтому команда, созданная до готовности, останется невалидной даже после финализации.

### Переходы

- Нет манифеста / изменился `source_rev` → после E10 записать новый `discovering`.
- `discovering` с теми же `source_rev + confirm_token` → продолжить подготовку, а не сработать текущим short-circuit.
- `discovering → discovering` → атомарные metadata/cover checkpoints, тот же token, `manifest_seq + 1`.
- `discovering → ready` → только после готовности обязательных компонентов и финального source fence.
- `ready` с неизменным `source_rev`, `force=False` → вернуть без записи.
- Любая версия с изменившимся `source_rev` → новая подготовка; старый worker не имеет права финализировать её.
- `force=True` → новая generation и новые `confirm_token/build_token`, даже при прежнем `source_rev`; `processed_keys=[]`.
- `discovering|pending-confirm|error → skipped` — допустимый skip. Финализатор проверяет текущие generation/status и не может перезаписать `skipped`.
- Неустранимая ошибка подготовки → `manifest_phase=failed`, `status=error`, без `build_token`; скелет не висит бесконечно как будто работа продолжается.

### Кто пишет и читает

- `agent/scan.py::_write_manifest` владеет `discovering`, checkpoints и `ready`.
- `agent/dispatcher.py` меняет статус только у `ready`-манифеста.
- Swift только читает.
- `build_m4b` получает только объект, прошедший readiness-gate.

### Атомарность и порядок публикации

Каждая версия манифеста и `state.json` записывается через `state.write_json_atomic`: temp в той же директории, `fsync`, `os.replace`. Следует добавить best-effort `fsync` родительской директории после rename. Читатель увидит старую или новую целую JSON-версию, но не середину файла.

Манифест и `state.json` не являются одной транзакцией, поэтому порядок обязателен:

1. атомарно записать манифест;
2. атомарно записать соответствующий `state.json`;
3. записать notified-ledger;
4. вызвать нудж.

Если процесс умрёт между 1 и 2, UI останется на более старом, безопасном состоянии. Следующий тик перепроецирует готовый манифест. Обратный порядок запрещён: `state.json` не должен объявлять готовность раньше манифеста.

Showcase-row должен нести `manifest_phase`, `generation_id`, `manifest_seq` и readiness компонентов. Это позволяет Swift обнаружить межфайловый skew и никогда не создать build-authorisation для чужой generation.

### Crash/restart

- `recover_interrupted` продолжает обрабатывать только `status=converting`.
- `discovering` не является прерванной сборкой и не переводится в `error: interrupted`.
- После recovery обычный `run_scan` видит тот же `source_rev`, сохраняет `confirm_token` и возобновляет enrichment.
- Если скелет остался, а источник исчез, он удаляется/помечается abandoned только после успешного `ACCESS_OK`-скана. При denied/missing ничего не чистится.
- Если полная версия записана, а state — нет, следующий тик только перепубликует showcase. Существующий `StartInterval=300` не даст состоянию остаться таким навсегда.
- Старые полные манифесты без `manifest_phase` мигрируются в `ready` только после проверки обязательной формы; им добавляется `build_token`, но сохраняются `status`, `confirm_token` и ledger. Пустой/сомнительный legacy-манифест не «благословляется» как ready.

### Два агента

Нужно превратить утверждение «агент — single writer» в проверяемое свойство: process-wide advisory lock в `agent/state.py`, удерживаемый `agent/__main__.py` от `recover_interrupted` до конца drain. Проигравший процесс не пишет манифесты, state или ledger.

Дополнительно каждый checkpoint перед replace перечитывает `(generation_id, source_rev, confirm_token, manifest_seq, status)`. Несовпадение означает, что результат устарел и должен быть отброшен.

---

## 3. Инварианты + чем каждый проверяется

| Инвариант | Проверка |
|---|---|
| До E10 нет скелета, state и нуджа | Расширить E10 в `selfcheck_reliability`: изменение размера и добавление нового mp3 во время debounce |
| Первый нудж происходит после доступного skeleton-state | `selfcheck_nudge`: recorder при вызове читает `state.json` и манифест, ожидает `discovering` |
| Один logical arm имеет один `confirm_token` и один ledger-key во всех фазах | `selfcheck_nudge`: сравнить ключ после skeleton, metadata checkpoint и ready |
| Одна публикация вызывает не более одного нуджа; две книги в batch — один нудж | Существующий rapid-fire в `selfcheck_nudge`, расширенный на skeleton batch |
| Команда, созданная по скелету, никогда не строит — даже если drain начался после ready | Новый race-тест в `selfcheck_reliability`: команда без/с неверным `build_token`, ноль `confirm_accepted/build_started` |
| Ready всегда содержит непустой build payload | Unit-проверка readiness-validator плюс `selfcheck_reliability` с forged `ready` и пустыми `chapters` |
| Изменение источника во время web timeout не финализирует старую ревизию | `selfcheck_reliability`: барьер в cover search, изменение файла, затем проверка отсутствия ready для старого rev |
| Crash после skeleton возобновляет подготовку с тем же token и без второго нуджа | Crash-injection в `selfcheck_nudge` |
| Crash после ready-manifest, но до ready-state, исправляется следующим `run_scan` | Новый crash-point в `selfcheck_reliability` |
| `skip` с тем же `source_rev` не пробует, не создаёт skeleton и не нуджит | Расширить `selfcheck_skip`, инструментировав `_probe_book`/cover |
| Skip во время `discovering` нельзя перетереть поздним enrichment | Race-тест в `selfcheck_skip` |
| `force=True` сохраняет прежний `source_rev`, но создаёт новую generation/token и очищает dedup | Расширить `selfcheck_reconvert` |
| `refresh_showcase` показывает `discovering`, а не «готовую книгу с 0 глав» | Python projection-test плюс `app/selfcheck_routing.swift` |
| Два процесса дают один manifest-generation и не более одного нуджа | Subprocess contention test в `selfcheck_reliability` |
| Reader никогда не получает частично записанный JSON | Stress reader/writer test в `selfcheck_reliability` |
| Переход skeleton → ready действительно обновляет SwiftUI, не оставляя начальные `@State` | `app/selfcheck_routing.swift`: discovering → metadata → ready для одного `book_id` |

---

## 4. Milestone по зависимостям

1. **Контракт schema v2.** Зафиксировать фазы, `build_token`, generation/seq, showcase-поля и migration legacy. Подготовить JSON fixtures.

2. **Fail-closed фундамент.** В `agent/dispatcher.py` добавить readiness-validator и build-token до `_already_processed`; в `_real_build` — защитный precondition. В `EngineClient.swift` заменить приём сырого `BookManifest` на `ReadyBookManifest`, который создаётся только валидатором `StateModel.swift`.

3. **Persistence/concurrency.** Усилить `state.write_json_atomic`, добавить process lock и generation guard. Этот milestone предшествует включению скелетов.

4. **Scanner state machine.** Рефакторить `_write_manifest`, `run_scan`, `_publish_showcase_and_maybe_open`, `_edge_keys`, `build_state`, `_collect_showcase_manifests`, `refresh_showcase`; расширить `_files_are_stable` проверкой состава каталога; добавить source fence и crash resume.

5. **Swift UI — параллельно с milestone 4 после фиксации schema.** Добавить фазовые модели, отдельные Preparing/Ready views и phase-aware строки в `QueueView.swift`. В `main.swift` изменить только routing/watch-baseline для `discovering`; не трогать `WindowPresentation.swift`, `presentWindow` и недавно принятый focus ladder.

6. **Recovery и watchdog.** `recover_interrupted` явно игнорирует/resume-маркирует `discovering`. В `agent/__main__.py` отделить deadline access/discovery от enrichment: медленная сеть не должна публиковать ложный `folder_access=blocked`.

7. **Гейты.** Расширить четыре указанные selfcheck-suite, добавить Swift routing cases, затем прогнать общий gate и измерить отдельно `skeleton state written → nudge invoked`.

Scanner и Swift можно делать параллельно после milestone 1. Раннюю публикацию нельзя включать раньше серверного и клиентского fail-closed milestone 2.

---

## 5. Как сняты риски 1–6

1. **Сборка по недочитанному манифесту.** Скелет имеет другой `status`, не имеет `build_token` и не преобразуется в `ReadyBookManifest`. Dispatcher проверяет phase, обязательный payload и build-token до idempotency. Ранняя команда остаётся невалидной после финализации.

2. **Книга ещё копируется.** `_files_are_stable` остаётся перед первой публикацией и нуджем. Проверяется также состав каталога, после debounce заново строятся список mp3 и `source_rev`.

3. **Один ledger-key.** `confirm_token` чеканится в skeleton commit и сохраняется во всех checkpoints и ready. Ключ остаётся ровно `book:<book_id>:<source_rev[:16]>:<confirm_token[:16]>`. Enrichment-публикации state не создают нового edge.

4. **Skip.** Классификация existing manifest и сравнение `source_rev` происходят до debounce/probe. `skipped + same rev` возвращается без записи и нуджа. Изменившийся rev и presence re-drop по-прежнему реармят книгу.

5. **Reconvert.** `force=True` создаёт новую generation и token при прежнем `source_rev`, очищает `processed_keys`, проходит тот же resumable pipeline и получает новый `build_token`. Детерминированный idempotency key остаётся прежним, поэтому существующий selfcheck действительно проверяет reset ledger.

6. **`refresh_showcase`.** Скелет проецируется как `discovering` с `discovery.file_count`, а не как `pending-confirm` с нулём глав. `StateModel.swift` и `QueueView.swift` показывают loading-row/скелет; build-action отсутствует. Каждая checkpoint-запись манифеста сопровождается state-публикацией, потому существующий watcher получает обновление.

---

## 6. ЧТО УПУСТИТ ПЛАН, НАПИСАННЫЙ «В ЛОБ»

- **Проверить текущую фазу недостаточно.** Команда может быть записана по скелету, подождать в очереди и валидироваться уже после ready. Это закрывает только отдельный `build_token`, отсутствующий у скелета.

- **Текущий unchanged-rev short-circuit заморозит скелет навсегда.** Он должен возвращать без работы только `ready`/терминальные манифесты; `discovering` с тем же rev означает resume.

- **Стабильность списка не равна стабильности файлов.** Нынешний `_files_are_stable(mp3s)` не замечает новый mp3, появившийся после формирования аргумента. Можно получить «стабильные» первые 20 файлов из копируемых 56.

- **Атомарный JSON не даёт транзакцию manifest+state.** Если публиковать state первым, UI сможет включить сборку до появления полного манифеста. Если после manifest не публиковать state, watcher вообще не узнает об обновлении.

- **SwiftUI сохранит старые `@State`.** `ConfirmView` для того же `book_id` может сохранить пустые title/cover/params, несмотря на новый manifest. Надёжнее разные Preparing/Ready view-типы, а не `onChange` с россыпью присваиваний.

- **Watcher наблюдает `state/`, не `queue/books/`.** Одной финальной записи манифеста недостаточно; после каждого полезного checkpoint нужен atomic state publish.

- **Медленная сеть расширяет окно гонки входов.** После 12 секунд поиска обложки исходники могут уже отличаться от тех, которые пробовали. Нужен финальный `source_rev` fence.

- **Поздний worker может перетереть `skipped`, `converting` или новую generation.** Перед каждым commit нужны generation/status/seq guard и process lock. После `ready` scanner больше не делает поздних payload-записей.

- **Два агента создадут два token и два нуджа.** Атомарный replace защищает файл от половины JSON, но не от lost update. Нужен lock на весь recover→scan→drain.

- **Ledger-before-nudge имеет сознательное окно потери уведомления.** Если процесс умрёт после записи ledger, но до `/usr/bin/open`, повторного нуджа не будет. Это цена строгого at-most-once; менять порядок нельзя — иначе crash даст двойной нудж.

- **Сетевой timeout нельзя считать `folder_access=blocked`.** Нынешний phase watchdog охватывает весь `run_scan`; несколько offline-книг могут превысить 150 секунд и показать ложную TCC-проблему. Deadline подготовки должен иметь другую диагностику.

- **Старые манифесты нельзя автоматически считать ready только из-за отсутствия phase.** Иначе старый пустой/повреждённый manifest обойдёт новый гейт.

- **Cover-файлы тоже имеют generation.** Старые web/generated workers не должны писать в те же `<book_id>-web-*` пути, что новая подготовка. Нужны generation-scoped staging/names и очистка abandoned generation.

- **Публикация скелетов по одному ломает rapid-fire.** `run_scan` сначала должен собрать и записать batch стабильных skeletons, затем сделать одну nudge-capable публикацию.

---

## 7. Открытые вопросы человеку

Один продуктовый выбор: должна ли «Собрать» ждать завершения web-поиска, или её можно включить после метаданных и гарантированной embedded/generated-обложки, отказавшись от ещё не пришедших web-кандидатов?

Без отдельного решения безопасный вариант плана — `ready` только после завершения всей cover-chain; окно и метаданные появляются раньше, но кнопка остаётся недоступной во время сетевых таймаутов.
