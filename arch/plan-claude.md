# Архитектура и порядок сборки — mp3-to-m4b (Архитектор #1, Claude/Opus)

> Дата: 2026-06-28. Источники правды: `prd/PRD.md` (F1–F13, §6 автомат, §9 edge),
> `design/spec.md` + `design/tokens.json` + `design/flows.md`, `research/m4b-toolchain.md`
> (ffmpeg-рецепты, high), `decisions/log.md` (D1–D11). Прообраз каркаса:
> `../2026.06 fb2-to-epub/` (изучен: `bin/fb2-to-epub-watcher.sh`, `app/*.swift`,
> `packaging/installer.sh`, `packaging/fb2-to-epub-runner.sh`, `build/build-app.sh`,
> `launchd/*.plist.template`, `.patches/005,007,010,011,013`).
>
> Один независимый план. Юрка сравнит его с Архитектором #2 (Codex) и синтезирует.

---

## Итог (3–5 строк)

Клонируем проверенный каркас соседа (SwiftUI-приложение-читатель + фоновый launchd-агент-
писатель + `state.json` атомарно + DMG ad-hoc), меняем движок `fb2→epub`(Calibre) на
`mp3→m4b`(ffmpeg). **Новизна — обязательное подтверждение перед сборкой (D4/I2).** Реализуем
его НЕ новым IPC, а **повышением уже существующего у соседа паттерна apply-jobs**: агент
открывает книгу до состояния `pending-confirm` (распознал метаданные + обложку, НЕ собрал),
приложение показывает/правит и роняет job `confirm-build` в watched-каталог `jobs/`; агент по
`WatchPaths` просыпается, дренирует jobs и только тогда собирает. Инвариант I2 = «нет
`confirm-build` job → ffmpeg не запускается» держится структурно: разведка и сборка — два
разных прохода агента, разделённые человеком. Validation-first: M0 — тончайший сквозной
скелет (установка→обнаружение→окно→сборка одной книги→валидный `.m4b`), M1 — полный MVP.

---

## 1. Процессная модель и зоны ответственности

Два процесса, как у соседа, **звезда вокруг `state.json`** (агент — единственный писатель
снапшота; приложение — читатель; обратная связь — через `jobs/`-каталоги, см. §2).

### 1.1 Приложение (SwiftUI/AppKit, unsandboxed, Foundation-only)
Роль — **читатель состояния и заказчик работ**. Никогда сам не трогает исходные mp3 и не
запускает ffmpeg. Экраны (карта — `design/spec.md §2`, флоу — `flows.md`):

| Экран | Файл (клон-аналог соседа) | Роль |
|---|---|---|
| Setup (S1) | `SetupView.swift` | детект ffmpeg/ffprobe, выбор папки, запуск installer |
| Status (S2) | `StatusView.swift` | дом: кольцо-прогресс, счётчики, очередь-вход, последние |
| **Окно подтверждения (S3) ★** | `ConfirmView.swift` (новый) | ядро D4: показать/править → job `confirm-build` |
| Состояния обложки | внутри ConfirmView + `CoverPanel.swift` | 6 под-состояний (§4 spec) |
| Группировка (S4) | `GroupingSheet.swift` (новый) | sheet D1 → job `grouping-choice` |
| Очередь (S5) | `QueueView.swift` (новый; аналог `CoverSelectView`) | pending-confirm / в работе |
| Настройки (S6) | `SettingsView.swift` | дефолты пресета/нарезки, FDA, версия (Could) |

Общие для всех экранов: `Tokens.swift` (1-в-1 имена под соседа, `design/spec §1`),
`StateModel.swift` (декод `state.json`), `EngineClient.swift` (мост к installer/launchctl),
`JobWriter.swift` (новый — атомарная запись job'ов, §2), `CoverGenerator.swift`
(нативный рендер запасной обложки через offscreen WKWebView — переносим механизм соседа,
но КВАДРАТНЫЕ 1:1 шаблоны).

### 1.2 Агент (bash + python3, движок ffmpeg/ffprobe)
Роль — **писатель состояния и исполнитель**. Под Full Disk Access (через runner — §1.3).
Делает ВСЁ, что трогает файлы: обнаружение, ffprobe-разведка метаданных, чтение/детект
встроенной обложки, веб-поиск обложки, генерация запасной обложки, сборка `.m4b`, нарезка,
отмена. Запускается launchd на изменение watched-каталогов; за один fire выполняет **две фазы**:
дренаж jobs (исполнение решений человека) → разведка новых книг (наполнение `pending-confirm`).

Скрипты (в `bin/`, клон-структура соседа):
| Скрипт | Аналог соседа | Назначение |
|---|---|---|
| `mp3-to-m4b-watcher.sh` | `fb2-to-epub-watcher.sh` | главный цикл: jobs-дренаж + разведка |
| `mp3-to-m4b-build.sh` (или функция внутри watcher) | (нет; новое) | конвейер ffmpeg: concat→AAC, главы, обложка, контейнер, нарезка |
| `mp3-to-m4b-probe.py` | (нет; новое) | ffprobe-разведка: метаданные книги/глав/длительности/детект APIC → JSON |
| `mp3-to-m4b-cover-finder.py` | `fb2-to-epub-cover-finder.py` | веб-поиск обложки (квадрат 1:1), `--json` режим |
| `mp3-to-m4b-runner.sh` | `fb2-to-epub-runner.sh` | стабильный FDA-таргет, exec watcher |

> **Решение арх (граница языка):** разведка метаданных (`probe.py`) и поиск/генерация обложек
> — Python (JSON + urllib, как у соседа). Оркестрация, ffmpeg-вызовы, jobs-дренаж, атомарные
> записи state — bash + встроенные python-heredoc'и (как у соседа: bash не парсит JSON сам).
> Сборку `.m4b` (длинная ffmpeg-командная строка с `filter_complex`) собирает Python-хелпер,
> печатающий argv, или bash-функция — **выбор уточняется в M0** по читаемости (filter_complex
> на N входов проще генерировать в Python). По умолчанию: Python генерирует argv → bash
> запускает (контролируем процесс/отмену из bash).

### 1.3 FDA-граница (как у соседа, дословно)
TCC привязывает право доступа к файлам к **исполняемому в `ProgramArguments`**, не к скрипту.
Поэтому `ProgramArguments` = стабильный `mp3-to-m4b-runner.sh` по фиксированному пути в App
Support; человек даёт Full Disk Access именно ему; грант переживает обновления, пока путь и
байты раннера стабильны (installer переустанавливает runner только при изменении — паттерн
соседа). Приложение FDA НЕ требует (оно читает свой App Support + пишет в свой App Support
jobs — это не TCC-зона). **Все правки исходных/выходных файлов — только агент под FDA.**

---

## 2. ★ Протокол app↔agent: `state.json` (вниз) + jobs (вверх) — САМОЕ ВАЖНОЕ

Это новизна vs fb2. Сосед уже имеет ОБА направления (state вниз + `covers/jobs/` вверх для
re-pick обложки **постфактум**). Мы **повышаем jobs из косметики в центральный гейт**: тем же
механизмом, но job `confirm-build` стоит ПЕРЕД сборкой, а не после.

### 2.1 Поток (звезда, hub = диск)
```
agent  ──(пишет атомарно tmp→rename)──►  state.json  ──(file-watch + focus)──►  app (читает)
app    ──(пишет атомарно tmp→rename)──►  jobs/<id>.json  ──(launchd WatchPaths)──►  agent (дренирует, удаляет)
```
- Приложение НИКОГДА не пишет в `state.json` (владелец — агент). Свои app-only маркеры (как
  «очистить историю» у соседа `state/recent-cleared-at`) — отдельными файлами, не в снапшоте.
- Агент НИКОГДА не ждёт приложение синхронно: дренаж jobs идемпотентен и best-effort (ошибка
  job не валит остальной прогон — паттерн соседа), job всегда удаляется после обработки (даже
  при ошибке), чтобы не зациклить агента.

### 2.2 Расширенная схема `state.json` (надстройка над снапшотом соседа)
Сохраняем поля соседа (`schema`, `agent.watch_dir`, `totals`, `recent`, `batch{active,total,
done}`, `last_conversion`) — Status и кольцо переиспользуют их как есть. **Добавляем** массив
книг-заданий `books[]` (новизна — у соседа книги не имели «ожидающего» состояния):

```jsonc
{
  "schema": 2,
  "agent": { "watch_dir": "/Users/…/Desktop/mp3-to-m4b" },
  "totals": { "converted_total": N, "today": N, "failed_today": N },
  "batch":  { "active": true, "total": 5, "done": 2 },     // кольцо Status (как у соседа)
  "books": [                                               // ← НОВОЕ: очередь заданий
    {
      "book_id": "<sha256(out_path)[:16]>",                // стабильный id (как cover book_id соседа)
      "status": "pending-confirm",                         // detected|grouping-ask|pending-confirm|converting|done|error|cancelled
      "src_dir": "/…/Толстой - Война и мир",               // папка-источник (или корень для одиночек)
      "out_path": "/…/Война и мир.m4b",                    // целевой файл (рядом, I1)
      "group_kind": "folder",                              // folder|loose-merge|loose-single
      "meta": {                                            // распознано probe.py (F3), всё правимо в окне
        "author": "Толстой", "title": "Война и мир",
        "author_source": "tag|folder|empty",              // для метрики G3 / отладки
        "chapters": [ {"idx":1,"src":"01.mp3","name":"Глава 1","dur_ms":725000}, … ],
        "total_ms": 51600000
      },
      "cover": {                                           // под-состояние обложки (§4 spec / F6)
        "state": "from-mp3|searching|candidates|not-found|generated|user-picked|user-replaced",
        "current_path": "/…/covers/<book_id>/chosen.jpg", // что вшивать (всегда непусто к «Собрать»)
        "candidates": [ {"id","rank","source","url","preview_path","square":true} ],
        "best_candidate_id": "…|null"
      },
      "params": {                                          // дефолты пресета (D2), правятся в окне (F5)
        "bitrate_kbps": 192, "channels": "stereo", "sample_rate": 44100,
        "split": { "enabled": false, "threshold_mb": 300 }  // D6 дефолт — выкл
      },
      "progress": { "done_ch": 0, "total_ch": 12 },        // прогресс converting (по главам)
      "error": null,                                       // {kind:"corrupt|enospc|engine", file?, message}
      "ts": "2026-06-28T…Z"
    }
  ],
  "last_conversion": { … }                                 // как у соседа
}
```
Декод в Swift — **defensive, как `StateModel.swift` соседа**: отсутствующий `books` → `[]`;
неизвестные ключи игнорируются; частичная запись → пустая-но-валидная модель, не краш.
Версия схемы поднимается до `2`; приложение читает `>=1` (forward-compatible).

> **Гонки и атомарность (инвариант):** `state.json` пишется ТОЛЬКО агентом через
> sibling-tmp+`os.replace` (атомарно, паттерн `record_conversion`/`batch_state` соседа) — читатель
> никогда не видит полузаписи. Несколько мутаций за один fire (разведка нескольких книг +
> тики прогресса) композируются через load-mutate-replace поверх существующего снапшота
> (как `batch_state` соседа расширяет, а не клоберит). Один-писатель снимает классические
> гонки JSON. См. также §8.

### 2.3 Job'ы (приложение → агент). Каталог `jobs/`, атомарная запись, в `WatchPaths`
Все job'ы — маленькие JSON `{action, book_id, …, ts}`, имя `jobs/<book_id>-<action>-<rand>.json`
(rand чтобы повторный job той же книги не перезаписал необработанный — урок соседа). Пишутся
tmp→rename (`JobWriter.swift`). Агент за fire: `glob jobs/*.json` (кроме `.tmp`) → диспетчер
по `action` → исполнить → **удалить job**. Best-effort: сбой одного не валит прогон.

| action | Когда | Поля | Что делает агент |
|---|---|---|---|
| **`confirm-build`** ★ | человек нажал «Собрать» | `book_id`, `params{bitrate,channels,sr,split}`, `meta{author,title,chapters[name override]}`, `cover_choice{kind:"path", path}` | I2-гейт снят: запускает конвейер сборки (§3) для этой книги; `status→converting`→`done`/`error` |
| **`grouping-choice`** | человек выбрал в S4 | `book_id`(временный id группы), `choice:"merge"\|"separate"` | merge → одна книга из N глав → `pending-confirm`; separate → N книг, каждая → `pending-confirm` |
| **`cover-choice`** | выбрал кандидата/свой файл/«искать ещё» | `book_id`, `kind:"candidate"\|"file"\|"research"\|"generate"`, (`candidate_id`\|`path`\|`query`,`exclude[]`\|`template`) | обновляет `cover.current_path`/`candidates`/`state` в `state.json` (НЕ собирает) — как research/apply у соседа, но до сборки |
| **`cancel`** | «Отмена» в converting | `book_id` | убивает ffmpeg этой книги (pidfile, §3), чистит temp, `status→cancelled` |
| **`skip`** | «Пропустить» | `book_id` | снимает книгу с обработки (помечает, чтобы разведка не вернула её снова), исходники целы |
| **`apply-to-all`** (P1) | «Применить параметры ко всем» | `params`, `scope:"pending"` | проставляет params всем `pending-confirm` (обложку НЕ трогает — индивидуальна) |

### 2.4 Инвариант I2 (структурно, не флагом)
Сборка живёт ИСКЛЮЧИТЕЛЬНО в обработчике `confirm-build`. Разведка (наполнение
`pending-confirm`) и сборка — **разные ветки кода в разных фазах одного fire**, разделённые
человеком: разведка НЕ вызывает сборку ни при каких условиях; единственный вход в ffmpeg-
конвейер — пришедший `confirm-build` job. Нет job → нет сборки. Это делает I2 свойством
структуры графа вызовов, а не проверкой во время выполнения, которую можно забыть.

### 2.5 Идемпотентность (G5) и rising-edge всплытия
- **G5:** перед тем как поднять книгу в `pending-confirm`, разведка проверяет: рядом есть
  `out_path` (.m4b) **новее всех** исходных mp3 этой книги → книга актуальна, тихо пропустить
  (НЕ всплывать, НЕ плодить запись). Точный аналог `[[ "$dst" -nt "$src" ]]` соседа, но «новее
  ВСЕХ глав» (max mtime入ходов). При нарезке — проверка по части-1 / маркеру (уточнить в M1).
- **Всплытие (rising-edge):** при появлении НОВОЙ книги в `pending-confirm` агент один раз за
  пачку делает `open -b com.arrivarus.mp3tom4b` (как сосед на batch start) — поднимает/
  открывает приложение. Приложение само определяет rising-edge по diff `state.json` и
  поднимает окно подтверждения поверх (паттерн соседа; защита от «влетающих» элементов —
  `.patches/011`: стабильная identity + opacity, не if/else).

---

## 3. Движок сборки (bin/, bash+python, только ffmpeg) — research-рецепты

Все рецепты ниже **проверены Researcher локально** (`research/m4b-toolchain.md`, high).

### 3.1 Разведка метаданных — `mp3-to-m4b-probe.py` (вызывается в фазе detected)
Вход: путь книги (папка или список одиночек). Для каждого mp3 — `ffprobe -show_entries
format=duration:format_tags=title,track,album,artist,album_artist` + детект APIC (`-select_streams
v -show_entries stream=index` → count≥1). Выход — JSON для `state.books[].meta`:
- **Автор/Название** (F3/I4, research §5): `album`→title, `album_artist`/`artist`→author; пусто/
  мусор → парс имени папки «Автор - Название» (разделитель ` - `); не разобрать → title=имя
  папки, author="" (`author_source` фиксируется для G3).
- **Порядок глав:** тег `track` → иначе **натуральная** сортировка имён (`01,02,…,10`, не
  лексикографическая — research §5).
- **Имя главы:** `title` → иначе/«Track NN» → имя файла без расширения и ведущего номера
  (`^\d+[\s._-]+`).
- **Длительности:** накопительная сумма → `total_ms` и per-chapter (для START/END FFMETADATA).
- **Обложка:** первый mp3 с APIC → `cover.state=from-mp3`, извлечь `ffmpeg -i in.mp3 -an -c:v
  copy out.jpg`; иначе → `searching` (фоновый поиск, не блокирует окно).

### 3.2 Цепочка обложки (F6, research §4/6/7; D7) — НЕ блокирует «Собрать»
1. **mp3 (APIC):** извлечь как есть (§3.1). Под-состояние `from-mp3`.
2. **Веб-поиск** (`mp3-to-m4b-cover-finder.py`, клон finder соседа + аудио-уклон): запрос
   «<автор> <название> аудиокнига [обложка]», источники DuckDuckGo/Yandex Images (осн.) + опц.
   litres + OpenLibrary (запас). **Фильтр формы — КВАДРАТ 1:1** (`looks_like_cover`: целевой
   aspect ≈ 1.0, не 1.5 как у fb2 — research §6). `--json` → кандидаты с локальными превью под
   `covers/<book_id>/`. Только stdlib `urllib` (D-A4).
3. **Генерация** (если пусто/нет сети): **нативный `CoverGenerator.swift`** (offscreen
   WKWebView, механизм соседа) по **новым КВАДРАТНЫМ 1:1 шаблонам** (viewBox ~1500×1500;
   зелёный список шрифтов кириллицы из research §7 / `.patches/012` соседа: Georgia,
   Baskerville, Helvetica, Gill Sans, Hoefler Text; НЕ Didot/Futura/Avenir; курсив — Georgia
   italic, не Baskerville). Под-состояние `generated`, сетка 2×2 вариантов.
   > Решение арх: генерацию делает **приложение** нативно (как сосед — без python/cairo на
   > машине пользователя), кладёт PNG на диск, и через `cover-choice{kind:"generate"…}` /
   > прямой выбор передаёт путь агенту в `confirm-build.cover_choice.path`. Запасной путь
   > генерации в python (cairosvg+pillow в venv) — только если нативный рендер недоступен.
4. **Выбор/замена:** пейджер кандидатов, «искать ещё» (свой запрос, exclude показанных —
   паттерн research-job соседа), «заменить файлом» (drag/выбор, авто-кроп к квадрату).
- **G4:** к моменту «Собрать» `cover.current_path` ВСЕГДА непуст (нет сети/пусто → сразу
  генерация). Пустых обложек не бывает.

### 3.3 Сборка `.m4b` — `confirm-build` обработчик (research §1, high)
Конвейер для подтверждённой книги:
1. **FFMETADATA** (research §1b): `;FFMETADATA1` + book-теги (`title`,`artist`,`album_artist`,
   `album`,`genre=Audiobook`) + `[CHAPTER]` блоки `TIMEBASE=1/1000`, START/END из §3.1. `stik`
   НЕ ставим (v1, тех-дефолт; открытый вопрос §11 PRD — по итогам QA).
2. **Склейка+кодек** (research §1a — **concat filter с `aformat`**, не demuxer): каждый вход
   `[i:a]aformat=sample_rates=<sr>:channel_layouts=<ch>[ai]` → `concat=n=N:v=0:a=1[aout]` →
   `-c:a aac -b:a <bitrate>k`. Детерминированно, без дрейфа границ на разнородных входах.
3. **Обложка** (research §1c): `-map <cover>:v -c:v copy -disposition:v attached_pic` (jpeg, без
   перекода картинки).
4. **Контейнер** (research §1d): **`-f ipod -movflags +faststart`**, формат задаём ЯВНО (не по
   расширению), файл `.m4b`. Имя из названия (санитизация недопустимых символов — E11).
5. **Атомарность** (I1/I5): сборка во временный файл рядом → `mv -f tmp final` (нет полу-`.m4b`
   при сбое). Исходники не трогаются. Прогресс по главам → тики в `state.books[].progress` +
   `batch tick` (как сосед).
6. **Отмена** (F11): pid ffmpeg в `<book_id>.pid`; `cancel` job → `kill` + `rm tmp` →
   `cancelled`. Гонка с финальным rename: финал создан → `done` (atomic boundary), иначе
   `cancelled` (E7).

### 3.4 Нарезка на части (F8/D6, research §3) — P1
- Дефолт выкл (один файл). Вкл: оценка размера части = `bitrate_bps/8 × dur_s` + обложка +
  overhead; идём по главам, копим, превысили порог → закрываем часть на границе предыдущей
  главы (никогда не режем главу).
- Нарезка готовой книги **stream-copy** `-ss/-to -c copy` + **обязательный `-map_chapters`**
  (главы ТОЛЬКО из per-part FFMETADATA — иначе дублирование, research §3 грабли) + per-part
  обложка + `title="…, Часть N из M"`, `track=N/M`, общий `album`.
- Edge E17: порог < одной главы → часть = глава, предупредить (UI — `design/spec §6`).

### 3.5 Edge-cases движка (PRD §9)
- E3 битый mp3: ffprobe падает → `error{kind:corrupt,file}` + выбор «собрать без него/отменить»
  (повторный `confirm-build` с урезанным списком глав).
- E5 нет места: проверка перед/обработка ENOSPC → `error{kind:enospc}`, temp удалён (I1).
- E8 сотни глав: concat filter держит входы открытыми; при упоре в лимит дескрипторов →
  fallback на concat demuxer с предварительным приведением (research §1a note). См. §8 риск.
- E18 краш: при следующем fire temp-файлы (`*.m4b.tmp`/`mktemp`) подчищаются; финала нет →
  книга снова в `pending-confirm`.

---

## 4. Слежение / установка (клон installer.sh соседа + плановые отличия)

### 4.1 launchd plist (генерится `plutil`, не sed — для путей с пробелами/кириллицей)
`ProgramArguments=[runner]`; **`WatchPaths`** = `[ WATCH_DIR, jobs/ ]` (минимум; обоснование
ниже); `EnvironmentVariables` = `{ WATCH_DIR, PATH=/usr/bin:/bin:/usr/sbin:/sbin, FFMPEG,
FFPROBE, PYTHON3 }`; `RunAtLoad=true`, `ThrottleInterval=5`, лог в `~/Library/Logs/
mp3-to-m4b.log`. Bundle id агента `com.arrivarus.mp3tom4b.agent` (стабильный).
> **Решение арх (WatchPaths):** хватает ДВУХ путей — `WATCH_DIR` (новые mp3 → разведка) и
> `jobs/` (решения человека → дренаж). Отдельные каталоги под каждый тип job не нужны: один
> `jobs/` + диспетчер по `action` проще и меньше движущихся частей (у соседа один `covers/jobs`).

### 4.2 installer.sh (ответственность — вся логика установки, как у соседа)
- **Детект ffmpeg/ffprobe** как Calibre у соседа: `command -v ffmpeg ffprobe` / явные пути →
  нет → сообщение `brew install ffmpeg` + exit 1 (UI показывает инструкцию, F1).
- **Детект python3** (для probe/finder) — паттерн соседа (`/usr/bin/python3` → homebrew → PATH).
- **WATCH_DIR** (arg/env, дефолт `~/Desktop/mp3-to-m4b`), нормализация `~`, mkdir, `cd&&pwd`.
- **Копирование** скриптов в `App Support/mp3-to-m4b/bin` (`install -m 0755`); runner —
  переустанавливать ТОЛЬКО при изменении (`cmp -s`), чтобы не сбросить FDA-грант (урок соседа).
- **plist через plutil** (skeleton → `plutil -replace/-insert` типизированными значениями) →
  tmp → `mv -f` → `plutil -lint`.
- **(Re)load идемпотентно:** `bootout || true` → `bootstrap` → `enable` → `kickstart`. Миграция
  легаси-лейблов — как у соседа (если когда-то был другой id).
- **FDA-подсказка** при WATCH_DIR в Desktop/Documents/Downloads (путь runner + Cmd-Shift-G).
- **venv запасной обложки** (cairosvg/pillow) — best-effort (как build соседа); провал → UI
  предупреждает «запасная генерация недоступна, поиск/из mp3 работают» (F1, не блок).

### 4.3 Каталоги данных (App Support `mp3-to-m4b/`)
```
~/Library/Application Support/mp3-to-m4b/
  state/state.json                 # снапшот (агент пишет атомарно, app читает)
  state/events.jsonl               # журнал (как у соседа, для отладки/метрик §12 PRD)
  bin/{watcher,build,runner}.sh, probe.py, cover-finder.py   # установленные скрипты
  covers/<book_id>/{chosen.jpg, cand-1.jpg…, gen-1.png…}     # обложки/превью/генерёжка
  jobs/<book_id>-<action>-<rand>.json                        # app→agent (в WatchPaths)
  queue/                           # (опц.) рабочие temp пачки, если понадобится
~/Library/Logs/mp3-to-m4b.log
~/Library/LaunchAgents/com.arrivarus.mp3tom4b.agent.plist
```

---

## 5. Сборка / упаковка (клон build/ соседа)

- **`build/build-app.sh`** (клон): `xcrun swiftc -O` для arm64 + x86_64 → `lipo` в universal
  `Contents/MacOS/mp3-to-m4b`; копирование `installer.sh`+скриптов+КВАДРАТНЫХ cover-templates в
  `Contents/Resources`; иконка из `branding/icon-app.svg` (cairosvg→png→`sips`→`iconutil`);
  `Info.plist` from scratch (`CFBundleIdentifier=com.arrivarus.mp3tom4b` — стабильный, иначе
  ломает TCC на каждой пересборке — `.patches/002` соседа); **ad-hoc codesign + strict verify в
  retry-цикле** (iCloud FinderInfo-гонка — `.patches/003` соседа). `LSMinimumSystemVersion`
  под WKWebView (генерёжка) — 11.0 как у соседа.
- **`build/make-dmg.sh`** (клон): DMG из `branding/dmg-background`, проверка реального рендера
  (`.patches/001` соседа: не preview, а реальный рендер); `.metadata_never_index` в repo +
  очистка собранных `.app/.dmg` из индексации Spotlight (memory: build-artifacts→Spotlight dup).
- Список Swift-исходников в build-app.sh пополняется новыми файлами (`ConfirmView`,
  `GroupingSheet`, `QueueView`, `CoverPanel`, `JobWriter`).

---

## 6. Карта файлов проекта (что клонируем ≈как есть / что новое)

| Область | Клон соседа (минимальная правка: имена/движок) | Новое (под D4-подтверждение / m4b) |
|---|---|---|
| **Приложение** | `Tokens.swift`, `StateModel.swift`(+`books[]`), `EngineClient.swift`(ffmpeg вместо Calibre), `StatusView.swift`, `SetupView.swift`, `SettingsView.swift`, `CoverGenerator.swift`(квадрат), `main.swift`, `UpdateChecker.swift` | **`ConfirmView.swift`** ★, **`GroupingSheet.swift`**, **`QueueView.swift`**, **`CoverPanel.swift`**, **`JobWriter.swift`** |
| **Агент** | `*-runner.sh`(дослов.), `*-watcher.sh`(скелет цикла+jobs-дренаж+atomic state из соседа), `*-cover-finder.py`(аудио-уклон, квадрат) | **`*-probe.py`** (ffprobe-разведка), **`*-build.sh`/build-функция** (ffmpeg-конвейер), новые квадратные cover-шаблоны |
| **Установка/сборка** | `installer.sh`(ffmpeg-детект), `launchd/*.plist.template`, `build/build-app.sh`, `build/make-dmg.sh`, `build/dmg-settings.py`, `install.sh`/`uninstall.sh` | — (только переименование id/строк) |
| **Готово в проекте** | — | `branding/*`, `design/*`(tokens/mockups/refs/flows/spec), `prd/*`, `research/*`, `decisions/*` — используем как вход |
| **Execution-pack** | — | `arch/plans.md` (синтез Юрки), `arch/status.md`, `arch/test-plan.md` (фаза execution-pack) |

> **Принцип (memory: check-siblings-proven-impl-first):** максимально переиспользуем
> отлаженный код соседа; новое пишем только там, где подтверждение/m4b принципиально иные.
> НЕ переусложнять (memory: over-engineering — `.patches/006` соседа).

---

## 7. Майлстоны по зависимостям (validation-first)

Каждый майлстоун кончается **наблюдаемым результатом**, который Юрка верифицирует в браузере/
на реальном Mac (visual-verify + GUI-interaction, memory: render≠interaction).

### M0 — Сквозной скелет (тончайшая вертикаль «всё связано»)
Цель: доказать, что каркас+протокол+движок состыкованы end-to-end на ОДНОЙ книге.
1. **Каркас** (клон): build-app.sh собирает пустое окно; installer ставит агент; ffmpeg-детект
   в Setup; DMG ставится (memory: GUI > CLI — собрать DMG для ручного теста человеком).
2. **Разведка → state:** агент на drop папки с mp3 пишет `state.books[]` одну книгу в
   `pending-confirm` с минимальными метаданными (probe.py: author/title/chapters/dur). G5-skip.
3. **Окно (минимум):** приложение читает `state`, на rising-edge всплывает ConfirmView,
   показывает автор/название/главы + дефолт-параметры + плейсхолдер-обложку; кнопка «Собрать».
4. **Job → сборка:** «Собрать» роняет `confirm-build` job; агент дренирует, собирает
   `.m4b` минимальным конвейером (concat filter→AAC + FFMETADATA-главы + контейнер ipod, БЕЗ
   обложки/нарезки/веб), atomic rename; `status→done`.
**Валидируется:** одна книга-сборник → `Война и мир.m4b` рядом, открывается в Apple Books,
главы кликабельны, START/END верны (ffprobe), исходники целы (I1); БЕЗ `confirm-build` ничего
не собралось (I2); повторный drop не пересобрал (G5). Это режет главные риски (протокол +
ffmpeg-контейнер) раньше всего.

### M1 — Полный MVP (26 Must)
Надстройка на M0 по зависимостям:
1. **Обложка целиком** (F6/D7): from-mp3 → веб-поиск (квадрат) → нативная генерация (квадрат) →
   выбор/замена/«искать ещё» (`cover-choice` jobs); вшивание `attached_pic` в `confirm-build`.
   G4 (обложка для 100%, в т.ч. офлайн).
2. **Группировка** (F2/D1): одиночки в корне → `grouping-ask` → S4 sheet → `grouping-choice`
   job → merge/separate. Натуральная сортировка, игнор мусора, throttle недокопированных (E10).
3. **Окно полностью** (F4/F5): правка автор/название/имён глав, бокс качества (пресеты/каналы/
   SR), валидация (пустое название → «Собрать» disabled + подсказка), все состояния (§3 spec:
   disabled/converting/error). `.contentShape` на кастомных тап-таргетах (`.patches/010`).
4. **Очередь + прогресс** (F9): несколько книг → пейджер/`QueueView`, `batch{}` кольцо,
   переживание закрытия приложения. apply-to-all (P1).
5. **Status целиком** (F12/D8): кольцо, счётчики, последние книги, вход в очередь, live-обновление.
6. **Надёжность/edge** (F10/F11 + §9): идемпотентность строго, отмена (`cancel` job + pidfile),
   все edge E1–E18, 0 необработанных ошибок в логе (G6).
7. **Нарезка** (F8/D6, P1 — последней): stream-copy + map_chapters, per-part обложка/нумерация,
   предпросмотр в окне.
**Валидируется:** эталонный набор ≥10 реальных книг (с тегами/без/с APIC/без/разные SR) — все
открываются в Apple Books + ≥1 стороннем плеере (G1), 0 без обложки (G4), 0 сборок без «ок»
(G2), ≤2 действия на типовой книге (G3), 0 лишних пересборок (G5), 0 необработанных ошибок (G6).

> **Окно высоты — жёсткое требование во ВСЕХ экранах M1** (memory:
> native-window-cap-height + `.patches/013` соседа): content-sized, но cap по
> `screen.visibleFrame`, переменная секция (список глав / очередь) скроллится; тестировать на
> МАКСИМУМЕ (книга с десятками глав, очередь из многих) — иначе нижние кнопки за экраном.

---

## 8. Риски и узкие места

| # | Риск | Где бьёт | Митигация |
|---|---|---|---|
| R1 | **Протокол подтверждения (новизна).** confirm-build job не доходит / теряется / обрабатывается дважды | ядро D4/I2 | rand-суффикс имени job (нет перезаписи необработанного); удаление job ТОЛЬКО после обработки; идемпотентность по `book_id` (повторный confirm-build на `done`/`converting` — no-op); валидация существования `out_dir`/исходников на входе обработчика (урок `.patches/007` — файлы пользователя живут своей жизнью). Это первое, что валидируем в M0. |
| R2 | **Гонки state.json ↔ jobs.** Агент пишет state, пока приложение роняет job по устаревшему снапшоту | весь протокол | Один писатель state (агент) + atomic tmp→replace. Job несёт `book_id` (стабильный), не индекс; обработчик ре-читает актуальное состояние книги перед действием. launchd сериализует fire'ы (lock-dir соседа: `mkdir lock || exit`), параллельных агентов нет. App-only маркеры — отдельные файлы, не в снапшоте. |
| R3 | **rising-edge всплытие** «влетает»/двоится | UX окна | Стабильная view-identity + opacity-тумблер, `value:`-скоуп анимаций (`.patches/011`); `open -b` один раз за пачку (как сосед), не на каждый тик. |
| R4 | **FDA-граница.** Грант слетает на обновлении / агент не видит файлы в Desktop | вся работа агента | Стабильный runner-путь+байты, переустановка только при `cmp` (урок соседа); FDA-подсказка в Setup/Settings; приложение FDA не требует. Проверка TCC (memory: TCC verify/reset) при отладке. |
| R5 | **Лимит дескрипторов concat filter** на сотнях глав (E8) | очень длинные книги | research §1a note: fallback на concat demuxer с предварительным приведением входов (aformat по одному → промежуточные), либо батч-склейка. Детект по числу входов (порог ~неск. сотен) → авто-выбор стратегии. Замерить на синтетике в M1. |
| R6 | **Перекод обязателен (I3)** — сборка дорогая по времени на больших книгах | converting UX | Прогресс по главам (не зависает — E8); отмена доступна; кодек встроенный `aac` (research: хватает 96–192k). Время — ожидаемо, показываем оценку в окне до старта. |
| R7 | **Квадратная обложка** — портретные шаблоны соседа нельзя ресайзить; кириллица tofu в cairosvg | F6 генерация | Новые 1:1 шаблоны (viewBox ~1500), пересчёт зон текста; зелёный список шрифтов + fallback-стек (research §7); ОБЯЗАТЕЛЬНАЯ проверка рендером (PIL/глаза) каждого шрифта на ы/й/ё (`.patches/012`). Нативный рендер (WKWebView) обходит cairo-tofu вовсе — предпочтителен. |
| R8 | **Веб-поиск обложек** нестабилен/мусор/нет сети (G4) | F6 | Фильтр квадрат 1:1 отбрасывает портреты/логотипы; пусто/нет сети → сразу генерация (книга НИКОГДА без обложки); поиск не блокирует «Собрать». Только stdlib urllib (D-A4). |
| R9 | **Apple Books без `stik`** не кладёт в «Аудиокниги»/не помнит позицию | приёмка G1 | v1 полагается на `.m4b`+`-f ipod` brand `M4A` (research: главы/обложка/воспроизведение ОК). Открытый вопрос §11 PRD — QA проверит; если нужно → точечный AtomicParsley (core, одна строка), уже заложен «в запас». Не блокер M0/M1-движка. |
| R10 | **Оверинжиниринг протокола** (соблазн схему на каждый чих) | сроки/сложность | Один `jobs/` + диспетчер по action, один писатель state, переиспользование batch/state соседа. Минимум движущихся частей (memory: `.patches/006` — не строить с нуля + не переусложнять). |

---

## Артефакты
- План: `/Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-claude.md` (этот файл).

## Вопросы к человеку
Нет блокирующих для проектирования. Все ранее открытые вопросы (`stik`, кодер aac vs aac_at,
набор пресетов/порог нарезки, источники веб-поиска) уже зафиксированы как тех-дефолты в
PRD §11 / decisions D7–D11 и не мешают архитектуре; первый из них (`stik`) — к решению по
итогам QA, заложен запасной путь (R9). Один момент на ратификацию архитектором #2/синтезом:
**где собирать ffmpeg-argv** (Python-хелпер vs чистый bash) — предложено Python-генерация argv
+ bash-запуск (§1.2); финал — по читаемости на M0.
