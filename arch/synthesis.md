# Архитектура — синтез (суд Юрки, гейт G4)

> Сведение двух независимых планов в один. **Дата:** 2026-06-28.
> Источники: `arch/plan-claude.md` (Архитектор #1, Opus) · `arch/plan-codex.md` (Архитектор #2, GPT-5.5
> `xhigh`, + сырой `arch/plan-codex.raw.md`). Гейт двойного архитектора пройден (оба ответили независимо).

## Рубрика суда
Оба плана сильные и **сходятся в фундаменте**: клонируем каркас соседа fb2-to-epub (SwiftUI-приложение-
читатель + launchd-агент-писатель + DMG ad-hoc + installer/build/runner-паттерны), движок `fb2→epub`
(Calibre) → `mp3→m4b` (**только ffmpeg/ffprobe**), агент — **единственный писатель** авторитетного
состояния, приложение только читает витрину и **роняет команды**, всплытие на rising-edge, идемпотентность.

Расхождения и решение по каждому:

| # | Развилка | Claude #1 | Codex #2 | **Решение (синтез)** |
|---|---|---|---|---|
| 1 | Форма реверс-канала подтверждения | books[] в `state.json` + `jobs/` (поднятый apply-jobs соседа) | command-log (app пишет) + per-book манифесты (агент пишет) + `confirm_token`/`source_rev`/`idempotency_key` | **Гибрид → ближе к Codex.** Агент — единственный писатель; app пишет ТОЛЬКО команды; жёсткие токены/ревизии. Витрину Status берём у соседа. (См. ниже.) |
| 2 | Язык движка | bash-линия соседа | **Python** (bash лишь lock/launchd) | **Python** — у mp3→m4b логики кратно больше (протокол с токенами, накопительная математика глав, FFMETADATA, нарезка, цепочка обложки). bash был бы хрупок (кавычки/массивы/float). Тонкий bash-`runner` сохраняем как стабильную FDA-цель → exec python. |
| 3 | Генерация запасной обложки | нативный WKWebView (0 доп-зависимостей) | Python | **Python + Pillow** (рисуем градиент+текст напрямую, системный «зелёный» шрифт .ttf) — держит движок в агенте (app=читатель), и **обходит cairosvg-ловушку кириллицы** вовсе. WKWebView — запасной вариант, если качество PIL-текста не устроит на QA. |

**Почему Codex перевесил в (1)/(2):** его главное предостережение верное — нельзя давать приложению
писать в общий `state.json`/ставить `confirmed=true` (гонка читатель↔писатель + дыра «случайно
подтвердили»). Это ровно тот «катастрофический» класс ошибок, ради которого держим второго архитектора.
Claude независимо пришёл к «агент — единственный писатель», но Codex дал более строгую защиту самого канала.

## Синтез-план (что строим)

### A. Процессная модель (от соседа)
- **SwiftUI-приложение** — читатель/заказчик: экраны Setup · Status · Окно подтверждения · Очередь ·
  Настройки. Читает `state/` + `queue/books/`, пишет ТОЛЬКО `queue/commands/`. **Не трогает файлы книг → не нужен FDA.**
- **launchd-агент** (тонкий `runner.sh` = стабильная codesigned FDA-цель → `exec python3 -m agent`) —
  единственный владелец: скан папки, манифесты, `state.json`, ffmpeg, выход `.m4b`.

### B. ★ Протокол app↔agent (ядро, новизна vs fb2)
- **Вниз (агент пишет, app читает):**
  - `state/state.json` — **витрина** (как у соседа): `batch{active,total,done}`, `totals`, `recent`,
    + лёгкий список книг с `book_id`+`status`. Атомарно tmp→rename. Единственный писатель — агент.
  - `queue/books/<book_id>.json` — **per-book манифест** (агент пишет): распознанные автор/название/
    главы+длительности, под-состояние обложки + кандидаты/превью, дефолт-параметры, `source_rev`,
    `confirm_token`, прогресс. Отдельные файлы → нет переписывания «толстого» state на каждый чих.
- **Вверх (app пишет, агент читает):**
  - `queue/commands/<cmd_id>.json` — **app-owned команда**, атомарно tmp→rename: `action`
    (`confirm-build` | `grouping-choice` | `cover-choice` | `cancel` | `skip` | `apply-to-all`(P1)),
    `book_id`, правленые метаданные/параметры/нарезка, **`source_rev` + `confirm_token` + `idempotency_key`**.
  - `WatchPaths` = отслеживаемая папка **+ `queue/commands/`** (чтобы «Собрать» будила агента без нового mp3).
- **Гарантии:**
  - **I2 структурно:** ffmpeg-сборка живёт ИСКЛЮЧИТЕЛЬНО в обработчике `confirm-build` после валидации
    (`status==pending-confirm` && `source_rev` совпал && `confirm_token` верный). Сканер НИКОГДА не зовёт build.
  - Идемпотентность: `book_id`=sha256(пути), `source_rev`=fingerprint(relpath,size,mtime_ns,duration);
    устаревшая правка → `confirm_rejected_stale`; дубль команды → по `idempotency_key`.
  - Сбои: malformed json → `queue/commands/bad/` (без сборки); рестарт в `converting` без живого pid →
    `error: interrupted` + чистка temp; gate-тест в `events.jsonl`: нет `build_started` без `confirm_accepted`.

### C. Движок (Python, `agent/` модули, по research)
`probe.py` (ffprobe: длительности/теги/детект обложки) · `metadata.py` (приоритет ID3→имя папки/файлов,
натуральная сортировка) · `cover.py` (извлечь из mp3 → веб-поиск urllib DuckDuckGo/Yandex+квадрат-фильтр
→ генерация Pillow) · `build_m4b.py` (concat filter+aformat→AAC; FFMETADATA-главы 1/1000; attached_pic;
`-f ipod +faststart`; ffmpeg argv-массивами) · `split.py` (stream-copy `-ss/-to` + обяз. `-map_chapters 1`,
per-part обложка+track) · атомарный temp→rename, отмена kill+cleanup. Митигация лимита дескрипторов:
`filter_complex_script` + порог → fallback на нормализованный concat demuxer (research §1a).

### D. Установка/упаковка (клон соседа)
`installer.sh` (детект ffmpeg как Calibre; plist через `plutil`; стабильный bundle id
`com.arrivarus.mp3tom4b(.agent)`; идемпотентно/миграция); каталоги
`~/Library/Application Support/mp3-to-m4b/{state,queue/{books,commands,commands/bad},covers,bin,venv}`;
`build-app.sh` (swiftc universal + codesign-ретрай против гонки FinderInfo — урок соседа); `make-dmg`;
иконка из `branding/icon-app.svg` (full-bleed 1024). Python-движок — в venv проекта (pillow; urllib — stdlib).

## Майлстоны (validation-first; объединил M0-разбивку Codex с M0/M1 Claude)
- **M0 — протокол-скелет (риск №1 первым).** Агент видит mp3 → `pending-confirm` (манифест); app читает,
  всплывает; «Собрать» пишет command; **fake-engine** ставит `done`. **Валидируем САМ ПРОТОКОЛ:** без
  command нет `converting`; stale `source_rev` отклоняется; двойной клик = один build; malformed→bad/;
  I1/I2/G5.
- **M0.5 — реальная разведка.** ffprobe-probe + метаданные (главы, имена, длительности) + детект/превью
  обложки из mp3.
- **M1 — вертикаль MVP.** Всплытие→правка→`confirm-build`→**реальный `.m4b`** (главы+обложка, `-f ipod`)→
  Status. Затем слоями: цепочка обложки (веб→генерация Pillow→выбор) · группировка (sheet D1) · окно
  подтверждения целиком (все состояния) · очередь (много pending) · Status (кольцо/счётчики) ·
  надёжность (нет сети/места, битый mp3, идемпотентность). **Нарезка — последней (P1/v1.1).**
- **Упаковка/QA:** LaunchAgent + FDA + DMG с **живым рендер/клик-тестом** (уроки соседа: рендер DMG глазами,
  cap высоты окна на максимуме глав, `.contentShape` кликабельность).

## Топ-риски (объединённые) и митигации
1. **Протокол подтверждения** (потеря/двоение команды, гонка) → rand-суффикс cmd_id, удаление команды
   только после обработки, идемпотентность по `book_id`/`idempotency_key`, **валидируем первым в M0**.
2. **Гонки `state`↔команды** → один писатель state; команда несёт стабильный `book_id`+`source_rev`, не индекс.
3. **Лимит дескрипторов** concat filter на сотнях глав → `filter_complex_script` + fallback demuxer.
4. **Apple Books без `stik`** → опц. AtomicParsley по итогам QA (D из tech-defaults).
5. **Python-движок ≠ клон bash соседа** → тонкий bash-runner сохраняет FDA-стабильность; движок крыт тестами с M0.

## Решения синтеза, вынесенные человеку на G4 (приняты Юркой, обратимы на M0)
- **R-S1.** Реверс-канал = command-log + per-book манифесты + агент-единственный-писатель + `confirm_token`/`source_rev` (по Codex).
- **R-S2.** Движок агента — **Python** (тонкий bash-runner как FDA-цель), не bash-клон соседа.
- **R-S3.** Запасная обложка — **Python+Pillow** (без cairosvg, без WKWebView), WKWebView в запасе.
Все три внутренние/невидимые пользователю и **обратимы на M0** (валидируем дёшево до объёмного кода).
Если человек не возразит на G4 — фиксируем и идём в execution-pack.
