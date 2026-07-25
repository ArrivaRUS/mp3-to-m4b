# План v2: бинарный раннер mp3-to-m4b (релиз 1.0) — ЕДИНЫЙ источник правды

> Дата: 2026-07-25. Сводит `arch/plan-binrunner-mp3-claude.md` (Архитектор #1) и
> `arch/plan-binrunner-mp3-codex.md` (адверсариальный разбор GPT-5.6 Sol: 5 BLOCKER ·
> 12 MAJOR · 2 MINOR) с решениями Юрки Р1–Р6. **Заменяет оба предыдущих документа как
> источник правды.** Они остаются рядом как обоснование — здесь на них ссылки, а не
> пересказ. Донор: `../2026.06 fb2-to-epub` (схема обкатана в бою).

## Цель и контекст

На macOS 26 (Tahoe) tccd атрибутирует TCC/FDA-запрос launchd-агента к **Mach-O-образу**
процесса `ProgramArguments[0]`. У нас PA0 — shebang-скрипт `bin/runner.sh`, его образ —
`/bin/bash` (platform binary) ⇒ грант, который просит наш же установщик, **мёртв как
класс**. Watch-папка по умолчанию `~/Desktop/mp3-to-m4b` — TCC-зона. Диагноз донора:
`<донор>/.patches/020-tahoe-fda-script-grant-dead-real-not-panel.md`.

Релиз 1.0 = пять кусков:

1. **Замороженный Mach-O helper** как PA0 (порт донора, байты — валюта гранта).
2. **Транзакционный установщик** с независимым golden SHA, install generation и
   отдельным offline-режимом починки.
3. **Онбординг доступа**: probe в агенте → `state.json` → карточка в приложении →
   кнопка «путь в буфер» → фактическая перепроверка.
4. **StartInterval** как safety-reconciliation (наш фикс, у донора его нет).
5. **Живучесть под сигналами**: bash остаётся жив, Python гасит свои ffmpeg.

Три вещи, которые эта конструкция обязана держать одновременно:
**(а)** грант живёт, пока стабильны путь И байты helper'а;
**(б)** UI никогда не показывает путь/карточку, которые не соответствуют РЕАЛЬНО
загруженному job'у (fail-closed);
**(в)** отказ доступа не разрушает состояние библиотеки (ledgers, `skip`-пометки).

## Оглавление

1. [Решения Юрки Р1–Р6](#1-решения-юрки-р1р6-принято)
2. [Трассировка находок Codex (все 19)](#2-трассировка-находок-codex)
3. [Milestones и зависимости](#3-milestones-и-зависимости)
4. [T0-протокол (гейт, блокирует всё)](#4-t0-протокол)
5. [Изменения по файлам (мутируемо / заморожено)](#5-изменения-по-файлам)
6. [Миграция v0.9 и тупик `.failed`](#6-миграция-v09-и-тупик-failed)
7. [Тест-план](#7-тест-план)
8. [Риски и открытые вопросы](#8-риски-и-открытые-вопросы-к-человеку)

---

## 1. Решения Юрки Р1–Р6 (принято)

**Р1. Топология процессов — форма донора 1-в-1, без `exec`-девиации.**
`runner.sh` больше НЕ делает `exec "$PYTHON3" -m agent`. Он запускает python **в фоне**
и `wait`'ит его, с trap'ами на TERM/INT/HUP, пересылая сигнал дочернему python.
Итоговое дерево: `launchd → mp3-to-m4b-agent (helper, жив) → /bin/bash runner.sh (жив)
→ python3 -m agent → ffmpeg`. Это ровно донорская цепочка (`helper → bash watcher →
python3/Calibre`), обкатанная в бою, а не вариация. Helper НЕ меняется.
T0 всё равно обязателен — но теперь он подтверждает известную форму, а не гипотезу.

> ⚠️ **Фактическая поправка к обоснованию Р1(б).** «Bash жив → есть кому убрать ffmpeg»
> — неверно как механизм. Bash не знает pid'а ffmpeg (ffmpeg — его ВНУК), а Python по
> умолчанию на SIGTERM умирает, не трогая детей. Живой bash даёт **место для trap'а и
> гарантию порядка**, но реальное гашение ffmpeg обязан делать **обработчик сигналов в
> Python** (машинерия уже есть: `_terminate_ffmpeg_many` + подметание temp +
> `recover_interrupted`). У донора аналог — EXIT-trap вотчера, который снимает ЛОК, а не
> убивает внуков. Поэтому MAJOR-9 закрывается связкой Р1 + Python-handler, см. M3.
> Вторая деталь той же формы: bash-`wait` при трапнутом сигнале возвращается сразу
> (>128) — trap обязан **пере-`wait`'ить в цикле**, иначе runner.sh выйдет раньше python,
> и launchd прибьёт остаток дерева SIGKILL'ом.

**Р2. Принимаем ВСЕ 5 BLOCKER'ов Codex** (раскрытие — в трассировке и milestone'ах):
T0 с корреляцией PID/msgID и обязательным негативным контролем · отдельный **offline**
`installer.sh --repair-launchd-only` · **install generation** (UUID в
`EnvironmentVariables` → агент переносит в `state.json`; поверхность доступа разрешена
только при `disk PA0 == helper && state.generation == receipt.generation`; receipt пишется
последним; кнопка fail-closed) · **транзакционность** установщика (межпроцессный lock +
single-flight в Swift + долгий preflight ДО касания боевых файлов + откат plist/job) ·
**независимый golden SHA** helper'а, вшитый в installer (`src == expected` до записей,
`dst == expected` после; сверка src↔dst остаётся только как проверка копирования).

**Р3. `denied` не перевзводит библиотеку.** При `denied` — ранний выход **ДО** скана и
**ДО** `_reconcile_presence`; ledgers (`presence.json`, `notified.json`, манифесты,
**пометка `skip`**) не трогаются вообще. `missing` считается transient несколько тиков.
Подтверждено чтением кода: при TCC-deny `Path.is_dir()` глотает EPERM → `scan_watch_folder`
возвращает `([], None)` → `_manifest_source_alive` ложно для всех книг → `_reconcile_presence`
помечает всё absent → следующий успешный скан видит `absent→present` + `done` и **re-arm'ит
всю библиотеку** (`scan.py:1233`). Для пользователя — «все книги всплыли как новые».

**Р4. `StartInterval` = 300 с** (не 60) — safety-reconciliation, быстрый путь остаётся за
`WatchPaths`. До финального выбора — замер `powermetrics`/wall/CPU на 0/100/1000 файлов.
«Проверить снова» обязана различать `job busy` и `probe failed` и говорить «проверим после
текущей сборки», а не показывать timeout при корректно выданном гранте.

**Р5. Rollback НИКОГДА не перепойнчивает PA0 обратно на `runner.sh`** — это ровно та
конструкция, которую мы чиним. Откатываются только мутируемые части (`runner.sh`, пакет
`agent/`). Плюс правило `bundled >= installed` по receipt, чтобы downgrade не считался
апдейтом.

**Р6. Журналы.** `events.jsonl` — источник gate-инвариантов, поэтому ротация допустима
только вместе с правкой читателя. Дизайн: ротация **только на старте процесса** (единственный
писатель + launchd не запускает второй экземпляр label ⇒ конкуренции нет), `read_events()`
читает `.1` затем текущий как одну последовательность. Для `StandardOutPath` — сначала
**тихий холостой ран**, ротация лога тем же стартовым правилом с честной оговоркой про fd.

**Свежее (вне этого плана, но учтено):** команда агента **`skip`** («Пропустить» в окне
подтверждения: книга снимается с обработки, исходники целы, разведка её больше не поднимает)
делается Developer'ом параллельно. Здесь она фигурирует только как состояние, которое Р3
обязан не сломать, плюс три инварианта в §5. Правило «окно всегда на экране» живёт в
`app/WindowGeometry.swift` (покрыто юнитами) — любые новые поверхности (карточка доступа,
диагностический блок `.failed`) обязаны проходить через него, а не заводить свои клэмпы.

---

## 2. Трассировка находок Codex

Все 19 находок. Ни одна не «принята к сведению» — у каждой есть решение и адрес.

### BLOCKER (5)

| # | Находка | Решение | Где реализуется |
|---|---|---|---|
| B1 | A1 (атрибуция через `exec`) — неизвестно, а не «принято» | Убираем сам вопрос: по Р1 `exec` в цепочке больше нет, топология = донорская. T0 доказывает её на этой машине с корреляцией `accessing.pid` / `responsible_path` / `AUTHREQ_SUBJECT` по одному msgID + негативный PA0-контроль | **M0** (T0-протокол, §4) · `bin/runner.sh` (M3) |
| B2 | «PA0-only ремонт за 1–2 с» уходит в сеть (`pip install --upgrade pip` без timeout, `installer.sh:226`) | Новый режим `installer.sh --repair-launchd-only`: строго offline — проверить уложенные файлы + golden SHA, перепечь plist, reload/verify, receipt. Ни ffmpeg-поиска, ни venv, ни pip. Полный installer остаётся асинхронным (экран `.updating`) | **M1** `packaging/installer.sh` · **M5** `app/main.swift` (синхронный вызов только этого режима) |
| B3 | Правильный plist на диске ≠ launchd запустил новый PA0 | **Install generation**: installer генерит UUID → `EnvironmentVariables.MP3TOM4B_INSTALL_GENERATION` → агент переносит его в `state.agent.install_generation`; installer после bootstrap проверяет `launchctl print` и пишет **receipt последним**. Поверхность доступа рендерится только при `disk PA0 == helper && state.generation == receipt.generation`; при несовпадении — ДРУГАЯ поверхность «агент не запустился» с диагностикой и кнопкой починки | **M1** installer · **M4** `agent/scan.py`, `agent/config.py` · **M5** `app/main.swift`, `app/StateModel.swift` |
| B4 | Installer не транзакционен и не защищён от двух запусков (`rm -rf` + два bootstrap; в Настройках два независимых `InstallPhase`, `main.swift:403/418`) | Межпроцессный lock (`mkdir $APP_SUPPORT/.install.lock` + pid, перехват протухшего) + single-flight координатор в Swift (один флаг на модели, все три вызывателя через него). Порядок под локом: **отказ, если идёт сборка** → preflight/pip ДО боевых файлов → stage → validate → bootout → атомарная замена пакета → publish plist (с backup) → bootstrap → verify → receipt. Ошибка ⇒ вернуть prev plist + пакет, receipt не писать, exit≠0 | **M1** installer · **M5** `app/main.swift` (`InstallCoordinator`) |
| B5 | `sha src↔dst` не ловит испорченный source (два одинаково битых файла равны) | Вшитый в installer неизменяемый `EXPECTED_HELPER_SHA256` из PROVENANCE: `src == expected` **до любых записей**, `dst == expected` **после установки**. src↔dst остаётся только как проверка качества копирования. Три независимых теста: битый src / битый dst / оба одинаково битые | **M1** installer · **M0** `PROVENANCE.md` · тест-план §7 |

### MAJOR (12)

| # | Находка | Решение | Где реализуется |
|---|---|---|---|
| M1f | T0 смешивает scratch-path и боевой FDA-path (`$BIN_DIR` производен от `MP3TOM4B_SUPPORT_DIR`) | Разводим `HELPER_PATH` и `STATE_ROOT`: T0-харнес **не использует installer**, кладёт свой helper в собственный T0-каталог и печёт plist руками; `MP3TOM4B_SUPPORT_DIR` = только scratch-state. Перед каждым kickstart печатаем `realpath(PA0)`, SHA и loaded PA0. Боевой грант (R1b) — отдельный шаг в M7 | **M0** (§4) · опц. `MP3TOM4B_BIN_DIR`-override в installer (**M1**) |
| M2f | `currentWatchDir()` может откатить папку к старой (state старше plist) | Источник истины — **receipt последней успешно загруженной generation**; при его отсутствии — валидный plist раньше state; state — последним и ТОЛЬКО если `state.generation == receipt.generation`. Строгий fail-closed (без дефолта) сохраняется | **M5** `app/main.swift:4057` |
| M3f | Байт-страж стоит слишком рано (после codesign, а дальше `ditto` в `build/dist` и DMG) | Golden SHA проверяется на **четырёх границах**: repo → подписанный staging `.app` → `build/dist/*.app` после `ditto` → `.app`, извлечённый из смонтированного финального DMG. Плюс `.gitattributes` (`binary -filter`) и правило релиза «SHA изменился ⇒ `requires_fda_regrant=true` в release notes» | **M2** `build/build-app.sh`, `build/make-dmg.sh`, `build/build-dmg.sh` |
| M4f | Сигнал helper'а не доходит до ffmpeg (orphan пишет в удалённый temp) | Р1 даёт живой bash и trap-точку; **гашение делает Python**: handler на TERM/INT/HUP ставит флаг shutdown, который читает уже существующий poll-цикл энкодера (~3×/с) → `_terminate_ffmpeg_many` → подметание temp → манифест в `error: interrupted` → выход `128+sig`. Тест: bootout посреди encode ⇒ ни одного потомка через 5 с | **M3** `bin/runner.sh` + `agent/__main__.py`, `agent/build_m4b.py` |
| M5f | Recheck и StartInterval не работают в обещанный срок во время сборки (тик пропускается, `kickstart` без `-k` — no-op) | «Проверить снова» больше не полагается на kickstart: кнопка кладёт команду `recheck-access` в `queue/commands/` (эта папка уже в `WatchPaths` ⇒ агент просыпается сам), затем ждёт смены `folder_access_ts` ≤10 с. Если ts не сменился И `launchctl print` показывает `state = running` И есть книга в `converting` ⇒ честное «проверим сразу после текущей сборки» (не timeout); карточка растворится сама, когда пост-сборочный скан обновит probe | **M4** `agent/dispatcher.py` · **M6** `app/FolderAccessCard.swift`, `app/EngineClient.swift` |
| M6f | Цена минутного polling сильно занижена (`ffmpeg -version` каждый ран; state/presence/notified переписываются всегда) | `StartInterval = 300` (Р4) + четыре фикса цены: (а) `FFMPEG_VERSION` кладёт installer в plist-env, агент не спавнит ffmpeg на холостом ране; (б) `state.json` пишется только при **семантическом** изменении (сравнение без `ts`); (в) `presence.json` / (г) `notified.json` — только при изменении содержимого. Перед финальным выбором интервала — замер `powermetrics` на 0/100/1000 файлов | **M4** `agent/scan.py`, `agent/state.py` · **M1** installer (env) · **M7** замер |
| M7f | Ротация `events.jsonl` ломает читателя и gate-инварианты; `StandardOutPath` — fd открыт launchd'ом | Р6: ротация **только на старте процесса** (до первого append), одна копия `.1`; `read_events()` читает `.1`+текущий как одну последовательность и терпит смену inode. Для stdout — сначала тихий холостой ран, затем то же стартовое правило (текущий ран допишет в `.1`, следующий откроет новый файл — launchd открывает путь на КАЖДЫЙ запуск job) | **M4** `agent/state.py`, `agent/__main__.py` |
| M8f | `denied`/`missing` превращает библиотеку в «повторно добавленные книги» | Р3: `denied` ⇒ ранний выход ДО `scan_watch_folder` и ДО `_reconcile_presence`; витрина переносится из prev state как есть; ledgers и `skip`-пометки не трогаются. `missing` — transient: разрушительный reconcile только после ≥2 подряд сканов И ≥10 мин с первого `missing`. Тест «done → denied/missing → ok не re-arm'ит книгу» | **M4** `agent/scan.py:1476` (`run_scan`) |
| M9f | Причина мёртвого `WatchPaths` не установлена (Apple сам пишет, что он race-prone) | Диагноз из плана #1 **снимается** как утверждение. `StartInterval` объявляется safety-reconciliation, а не фиксом первопричины. В M7 — дешёвая диагностика: canonical `realpath`, `dev/inode` до и после логина/iCloud-гидрации, сравнение WatchPaths на локальной и iCloud-папке. Долгоживущий FSEvents-агент — **отдельный трек, не 1.0** | **M7** (диагностика) · план 1.x |
| M10f | Предложенный rollback возвращает исходный Tahoe-баг | Р5: rollback НИКОГДА не меняет PA0. Frozen helper остаётся; откатываются только `runner.sh` и пакет `agent/`. Downgrade до v0.9 на Tahoe — **unsupported**, явно в release notes | **M7** release notes · §6 |
| M11f | Downgrade считается «апдейтом» и вернёт PA0 на `runner.sh` (`freshness` не сравнивает версии) | Receipt со `schema`/`engine_version`; автоустановка только при `bundled >= installed`, неизвестный downgrade требует явного подтверждения. После installer — проверка сходимости всех частей + runtime generation; mismatch после «успеха» = терминальная ошибка, не бесконечный авто-ран. Для уже выпущенной v0.9 защиты нет — только release notes | **M1** installer (receipt) · **M5** `app/SetupView.swift:263`, `app/main.swift` |
| M12f | Миграция без запуска приложения не существует; `.failed`-экран — тупик | Признаём открытым: авто-апдейтера в проекте нет (Sparkle — отдельный трек). Меры: release notes «после обновления обязательно откройте приложение один раз» + README + DMG-подсказка. `.failed` получает выходы: «Повторить», «Выбрать папку…», «Открыть Настройки», «Продолжить без обновления» и раскрывающийся диагностический блок (фактический PA0, путь plist, receipt, watch_dir из каждого источника, хвост stderr) | **M5** `app/main.swift:1364` (`AgentUpdatingView`) · **M7** тексты |

### MINOR (2)

| # | Находка | Решение | Где реализуется |
|---|---|---|---|
| m1 | `PermissionError` ≠ «чини FDA» (chmod/ACL) | Статус в state остаётся `denied` (одно значение — слияние EPERM/EACCES нарочно, чтобы chmod-тест вёл себя как боевой TCC), но **текст различает** Swift по признаку «папка в защищённой зоне»: в зоне — FDA как основной шаг; вне зоны — «нет доступа к папке», подсказка проверить права в Finder (Cmd+I). Chmod-тест доказывает ветку UI, не TCC-механику | **M4** (state) · **M6** `app/FolderAccessCard.swift` |
| m2 | SHA-256 достаточен только против независимого golden | Закрыт B5 (golden как основной identity-гейт). Дополнительно на релизе: `codesign --verify --strict`, DR и cdhash обоих `--arch`; installer **запрещает symlink** на helper/`BIN_DIR` и сверяет ожидаемый путь с `realpath`; смена HOME/пути App Support — документированный re-grant (допущение A6 перестаёт быть безусловным) | **M1** installer · **M2** релизная проверка · **M0** PROVENANCE |

---

## 3. Milestones и зависимости

```
M0  T0-ГЕЙТ ⛔ ─┬─► M1 installer ──┬─► M5 Swift: истина+починка ──┐
  (блокирует     ├─► M2 build/DMG ──┘                             ├─► M6 UI доступа ──► M7 релиз ⛔
   ВСЁ)          ├─► M3 runner+сигналы ────────────────────────────┤
                 └─► M4 агент: probe/ledgers/журналы ──────────────┘
```

**Гейт: пока M0 не GREEN, не начинается ни один из M1–M7.** Причина — если T0 покажет
субъект ≠ helper, вся конструкция (и половина работ M1–M6) бессмысленна. Это самое дешёвое
опровержение, оно стоит ~час и один тумблер человека.

### M0. Замороженный helper + T0-гейт ⛔ (блокирует всё)

- Порт донорского `.c` → `packaging/agent-src/mp3-to-m4b-agent.c`: имя, сосед `runner.sh`,
  `_NSGetExecutablePath + realpath`, **spawn+wait (не exec)**, форвардинг TERM/INT/HUP,
  зеркалирование exit-кода. Микрофикс окна pid-reuse (`if (w == pid) { g_child = 0; break; }`)
  — донорский `PROVENANCE.md:23-41` прямо предписывает учесть его при новой сборке; наши
  байты в любом случае новые (другие строки), так что «отклонение от боевых байтов» тут
  ложная тревога: боевым является ДИЗАЙН и цепочка.
- `build-once.sh` (страж `MP3TOM4B_AGENT_REBUILD_I_UNDERSTAND=1`, clang universal
  `arm64 + x86_64`, `-mmacosx-version-min=11.0`, strip, `codesign -s -`), **однократная**
  сборка, `PROVENANCE.md` с фактическими sha256/size/cdhash обоих слайсов/DR/окружением.
- Артефакт `packaging/mp3-to-m4b-agent` коммитится байт-в-байт; `.gitattributes` с
  `binary -filter` (M3f).
- **T0-протокол целиком (§4)**, включая негативный контроль. ⛔ Субъект ≠ helper — СТОП,
  к человеку.

### M1. Installer: транзакционность, golden SHA, generation, offline-починка

Зависит от M0 (нужен замороженный артефакт и его golden SHA).
`AGENT_BIN_DST` · `find_agent_bin()` · cmp-preserve · golden-SHA-гейты (B5) · запрет symlink
+ `realpath`-сверка (m2) · межпроцессный lock + порядок с откатом (B4) · отказ при активной
сборке · `--repair-launchd-only` (B2) · generation UUID + `launchctl print`-verify + receipt
последним (B3) · `gen_plist`: PA0 = helper (ОДИН элемент), `StartInterval 300`,
`FFMPEG_VERSION`, `MP3TOM4B_INSTALL_GENERATION` · текст FDA-подсказки → путь helper'а.

### M2. Сборка приложения и DMG: страж на четырёх границах

Зависит от M0; параллелен M1. `build-app.sh`: helper → `Contents/Resources/`; golden-SHA
страж после codesign И после `ditto` в `build/dist`; `make-dmg.sh`/`build-dmg.sh`: проверка
helper'а внутри смонтированного финального образа. Любое несовпадение — release-blocking.

### M3. runner.sh (форма донора) + гашение ffmpeg по сигналу

Зависит от M0 (форма подтверждена T0); параллелен M1/M2.
`bin/runner.sh`: резолв python (без изменений) → `"$PYTHON3" -m agent &` → `CHILD=$!` →
trap TERM/INT/HUP: `kill -s "$sig" "$CHILD"` → **цикл `wait`** до фактического выхода →
`exit 128+sig`/код ребёнка. Шапка переписывается: файл **демотирован** из FDA-цели в
обычного ребёнка, FDA-цель — helper.
`agent/__main__.py`: установка обработчиков сигналов → флаг shutdown;
`agent/build_m4b.py`: poll-цикл энкодера читает флаг наравне с `_cancel_requested`
→ `_terminate_ffmpeg_many` → подметание temp → `error: interrupted`.

### M4. Агент: probe, Р3, generation, журналы, цена тика

Зависит от M0 концептуально, кодово независим (работает и под старым PA0 — поля честно
скажут `denied`). Параллелен M1–M3.
`probe_watch_dir_access()` (`os.listdir`, read-only, EPERM/EACCES слиты нарочно) · вызов в
начале `run_scan` **до** скана и **до** reconcile · ранний выход при `denied` с переносом
витрины и ledgers (Р3, M8f) · transient-`missing` · `agent.folder_access` /
`folder_access_ts` (ISO-8601 UTC, суб-секунды, opaque-токен) · `agent.install_generation`
из env · `refresh_showcase` переносит поля без probe · edge-pop через существующий
`_nudge_app` на фронте `≠denied → denied` · команда `recheck-access` в dispatcher (M5f) ·
экономия записей state/presence/notified (M6f) · ротация журналов + `read_events` (M7f) ·
тихий холостой ран.

### M5. Swift: истина о PA0/generation, самопочинка, `.failed` без тупика

Зависит от M1 (семантика receipt/generation/offline-режима).
`installedHelperPath` (корень — `supportRoot`) · `installedRunnerIsHelper()` через
`plutil -extract ProgramArguments.0 raw` **без фолбэка** · расширение условия автоапдейта
на байты helper'а и `runner.sh` · синхронный `--repair-launchd-only` до UI для случая
«PA0-only» · `InstallCoordinator` (single-flight, B4) · порядок источников watch-dir
receipt → plist → state-той-же-generation (M2f) · правило `bundled >= installed` (M11f) ·
`.failed`-экран с четырьмя выходами и диагностикой (M12f) · поверхность «агент не
запустился» при generation-mismatch. Все новые экраны — через `app/WindowGeometry.swift`.

### M6. Swift: онбординг доступа (UI)

Зависит от M4 (поля в state) и M5 (истина PA0 + подавление).
`FolderAccess` enum + толерантный декод в `AgentInfo` · чистые `FolderRecheck.evaluate` /
`terminalRecheckDissolves` · роутер приоритетов
`agentRepair > agentNotRunning > folderAccess > normal` · `FolderAccessCard` в наших
`Tokens` (4 подачи: blocker / banner / setupStep / settingsRow) · кнопка «Открыть настройки
и скопировать путь» (живое чтение PA0 в момент нажатия, fail-closed, верификация клипборда
+ ack) · «Проверить снова» с различением busy/failed (M5f) · тексты по m1.

### M7. Миграция, замеры, релизный гейт ⛔

Зависит от всего. R1b (боевой грант человека, ~5 мин) · репетиция апгрейда v0.9→1.0 на живой
установке · негативный контроль на боевом пути · ребут-тест `StartInterval` · замер
`powermetrics` и финальное подтверждение 300 с · диагностика WatchPaths (M9f) ·
release notes (новый грант; downgrade unsupported; «откройте приложение один раз») ·
README RU/EN · DMG. ⛔ СТОП-точка перед PR.

---

## 4. T0-протокол

Урок донора (patch 020): **панель System Settings врёт** — свежие записи не отрисовывает.
Верим только (а) фактическому действию (probe реально прочитал маркер) и (б) логу tccd с
корреляцией по одному msgID. `launchctl print` доказывает **loaded PA0**, но НЕ TCC-subject;
`codesign`/`csops` доказывают образ, но НЕ responsible-chain.

### 4.1 Разведение HELPER_PATH и STATE_ROOT (M1f)

Codex прав: в installer `$BIN_DIR` производен от `MP3TOM4B_SUPPORT_DIR`, поэтому «грант
конкретному пути» и «изоляция состояния в scratch» через один рычаг невозможны. Поэтому
**T0-харнес не использует installer вообще** и разводит две оси явно:

```
HELPER_PATH  = $T0_ROOT/bin/mp3-to-m4b-agent-t0     # субъект гранта, задан ЯВНО в PA0
T0_RUNNER    = $T0_ROOT/bin/runner.sh               # сосед, которого helper ищет по имени
STATE_ROOT   = $T0_ROOT/state                       # MP3TOM4B_SUPPORT_DIR, только состояние
WATCH        = $HOME/Desktop/mp3tom4b-t0-probe      # TCC-зона + marker.txt
LABEL        = com.arrivarus.mp3tom4b.t0
PLIST        = $HOME/Library/LaunchAgents/$LABEL.plist   # иначе launchd его не увидит
T0_ROOT      = $HOME/Library/Application Support/mp3-to-m4b-t0   # НЕ /tmp
```

Три следствия, которые нужно держать в голове:

- **Имя T0-копии — `mp3-to-m4b-agent-t0`, не `mp3-to-m4b-agent`.** В панели FDA видно ИМЯ
  файла, а не путь; два одинаковых имени спровоцируют «уже включено» (урок донора №1). Байты
  — те же самые (это копия артефакта), путь и имя — другие. Поиск соседа `runner.sh` идёт по
  `dirname(self)`, поэтому переименование T0-копии ничего не ломает.
- **T0-грант НЕ является боевым.** Запись TCC ключуется путём + DR, поэтому боевой грант
  (R1b, M7) остаётся отдельным шагом — и это хорошо: негативный контроль T0.4 портит байты
  по T0-пути, не трогая боевую запись.
- `$T0_ROOT` — не `/tmp` (симлинк `/private/tmp`, чистка системой), и боевое дерево
  `~/Library/Application Support/mp3-to-m4b/` в T0 не участвует ни одним файлом.

### 4.2 Что именно доказываем

Топология по Р1: `launchd → helper (PA0, жив) → /bin/bash runner.sh (жив) → python3 → …`

> ⚠️ **Поправка к рецепту Codex.** Его T0 писался под `exec`-вариант и требовал
> `BASH_PID == PY_PID` («доказывает, что проверен именно exec»). Под Р1 критерий
> **инвертируется**: pid'ы обязаны РАЗЛИЧАТЬСЯ, иначе мы измерили не ту форму. Если
> прогнать рецепт Codex дословно, он напечатает `NOT EXEC: pid changed` — это ОЖИДАЕМЫЙ
> результат, а не провал. Харнес обязан печатать обе величины и явную строку
> `FORM=donor(background+wait)` / `FORM=exec`.

`t0_probe.py` до `os.listdir()` пишет `os.getpid()`, `os.getppid()`, `sys.executable` в
`$STATE_ROOT/python.json`; `runner.sh` пишет `$$` в `$STATE_ROOT/bash.pid`; helper-pid берём
из `launchctl print`.

### 4.3 Шаги

| Шаг | Что делаем | GREEN-критерий | Нужен человек |
|---|---|---|---|
| **T0.1** Субъект без гранта | bootstrap t0-plist (PA0 = HELPER_PATH, один элемент) → kickstart | `folder_access: denied` в scratch-state И в логе tccd для ОДНОГО msgID: `accessing.pid == PY_PID`, `accessing.binary_path` = venv/base python, `responsible_path` и `AUTHREQ_SUBJECT`/`Sub` = **HELPER_PATH**; `/bin/bash` и python НЕ являются subject/responsible. Плюс `BASH_PID != PY_PID` (форма донора) | нет |
| **T0.2** Негативный PA0-контроль | тот же харнес, PA0 = `$T0_RUNNER` (shebang) | субъект = `/bin/bash`, deny. **Обязателен**: без него T0.1 не отличим от «повезло» | нет |
| **T0.3** Функциональный allow | человек добавляет `mp3-to-m4b-agent-t0` в FDA → повторный kickstart | `folder_access: ok` И листинг реально вернул `marker.txt`. Панель не скриншотим и не трактуем | **да, ~2 мин** |
| **T0.4** Негативный контроль байтов | пересобрать helper во временную копию (другой cdhash), подменить по тому же пути → probe → вернуть замороженные байты → probe | подмена ⇒ `denied`; возврат ⇒ `ok`. Доказывает и «грант пришпилен к байтам», и что T0.3 не даёт ложный GREEN | **да, ~2 мин** |
| **T0.5** Контроль формы `exec` (не гейт) | тот же харнес с `exec`-вариантом runner'а | знание про запас: сохраняется ли субъект через exec. Результат в PROVENANCE, на решение 1.0 не влияет | нет |
| **T0.6** Среда | зафиксировать версию macOS/сборку в PROVENANCE; харнес положить в `packaging/agent-src/t0/` | переиспользуемый смок после мажорных апдейтов ОС | нет |

Команда сверки лога (по `--start` от засечки перед kickstart):
`log show --start "$START" --info --debug --style compact --predicate 'process == "tccd" AND subsystem == "com.apple.TCC"'`
→ грепать `AUTHREQ_(ATTRIBUTION|SUBJECT|RESULT)`, `Sub:`, `Resp:`, `responsible_path`,
`pid=$PY_PID`, `mp3-to-m4b-agent-t0`, `/bin/bash`, `python`.

**⛔ Гейт:** T0.1 + T0.2 + T0.3 + T0.4 зелёные ⇒ M1–M7 разрешены. Иначе — СТОП и разговор с
человеком (варианты отступления: helper сам `posix_spawn`'ит python по фиксированному
`PYTHON3` из env — дороже, замораживает контракт env/ошибок; либо ждать Developer ID и
SMAppService — отдельный трек).

### 4.4 Уборка

T0-каталог, t0-plist и папка `~/Desktop/mp3tom4b-t0-probe` удаляются скриптом в конце;
строку `mp3-to-m4b-agent-t0` в панели FDA человек снимает вручную (в чеклист M7, чтобы в
панели не осталось мусора рядом с боевой строкой).

---

## 5. Изменения по файлам

### 5.1 Заморожено навсегда (менять = убить грант у всех)

| Сущность | Почему заморожена |
|---|---|
| `packaging/mp3-to-m4b-agent` (байты артефакта) | ad-hoc DR = cdhash этих байтов. Любая пересборка — новый cdhash — молчаливая смерть гранта у КАЖДОГО пользователя |
| `packaging/agent-src/mp3-to-m4b-agent.c` | де-факто заморожен: правка ⇒ пересборка ⇒ см. выше. Правится только осознанным решением + `requires_fda_regrant` в release notes |
| Путь установки `~/Library/Application Support/mp3-to-m4b/bin/mp3-to-m4b-agent` | часть identity гранта (путь + байты). Переезд App Support = re-grant всем (допущение A6 — **не** безусловное, см. m2) |
| Имя файла `mp3-to-m4b-agent` | это строка, которую человек видит в панели FDA; смена = «непонятная новая запись» |
| Контракт «helper ищет `runner.sh` рядом с собой» | зашит в замороженные байты; переименование `runner.sh` ломает запуск навсегда |

### 5.2 Свободно мутируемое

`bin/runner.sh` (демотирован в обычного ребёнка — правится в каждом релизе безнаказанно) ·
`agent/*.py` · `packaging/installer.sh` · `build/*.sh` · `app/*.swift` · plist (перепекается
каждой установкой).

### 5.3 Таблица правок

| Файл | Статус | Что меняется |
|---|---|---|
| `packaging/agent-src/mp3-to-m4b-agent.c` | НОВЫЙ · заморожен | порт донорского `.c`: сосед `runner.sh`, свои строки-префиксы, микрофикс pid-reuse |
| `packaging/agent-src/build-once.sh` | НОВЫЙ · мутируем | страж `MP3TOM4B_AGENT_REBUILD_I_UNDERSTAND=1`, clang universal, strip, `codesign -s -`, печать sha/cdhash |
| `packaging/agent-src/PROVENANCE.md` | НОВЫЙ | identity артефакта (sha256, size, cdhash обоих слайсов, DR), DO-NOT-REBUILD, окружение сборки, версия macOS T0, результат T0.5 |
| `packaging/agent-src/t0/` | НОВЫЙ · мутируем | харнес T0 (§4): свой plist, `runner.sh`, `t0_probe.py`, сборщик лога, уборка |
| `packaging/mp3-to-m4b-agent` | НОВЫЙ · **заморожен** | universal Mach-O, коммитится байт-в-байт |
| `.gitattributes` | НОВЫЙ | `packaging/mp3-to-m4b-agent binary -filter -diff -merge` (M3f) |
| `packaging/installer.sh` | мутируем | `AGENT_BIN_DST` · `find_agent_bin()` · cmp-preserve · `EXPECTED_HELPER_SHA256` (src до записей, dst после) · запрет symlink + `realpath` · межпроцессный lock + порядок с откатом · отказ при активной сборке · `--repair-launchd-only` · generation UUID + `launchctl print`-verify · receipt последним · `gen_plist`: PA0 = helper (1 элемент), `StartInterval 300`, `FFMPEG_VERSION`, `MP3TOM4B_INSTALL_GENERATION` · опц. `MP3TOM4B_BIN_DIR` · текст FDA → путь helper'а |
| `bin/runner.sh` | мутируем | **форма донора** (Р1): python в фоне + trap TERM/INT/HUP + пере-`wait` в цикле + зеркалирование кода; шапка «демотирован, FDA-цель — helper»; резолв python без изменений |
| `build/build-app.sh` | мутируем | helper → `Contents/Resources/`; golden-SHA страж после codesign И после `ditto` (release-blocking) |
| `build/make-dmg.sh`, `build/build-dmg.sh` | мутируем | проверка helper'а внутри смонтированного финального образа (4-я граница) |
| `agent/scan.py` | мутируем | `probe_watch_dir_access()` · probe в начале `run_scan` ДО скана и ДО reconcile · ранний выход при `denied` (Р3) · transient-`missing` · поля `folder_access`/`_ts`/`install_generation` в `build_state` · carry-forward в `refresh_showcase` · edge-pop через `_nudge_app` · запись state/presence/notified только при семантическом изменении · `FFMPEG_VERSION` из env |
| `agent/state.py` | мутируем | ротация `events.jsonl` на старте процесса (одна копия `.1`); `read_events()` читает `.1`+текущий |
| `agent/__main__.py` | мутируем | обработчики TERM/INT/HUP → флаг shutdown; тихий холостой ран; вызов ротации до первого append |
| `agent/build_m4b.py` | мутируем | poll-цикл энкодера читает флаг shutdown наравне с `_cancel_requested` → `_terminate_ffmpeg_many` + подметание temp + `error: interrupted` |
| `agent/dispatcher.py` | мутируем | команда `recheck-access` (probe + безусловное обновление `folder_access_ts`); `skip` — зона Developer'а, здесь только инварианты §5.4 |
| `agent/config.py` | мутируем | путь receipt (`install-receipt.json` в корне App Support, НЕ в `state/` — чтобы не дёргать файловый вотчер приложения) |
| `app/EngineClient.swift` | мутируем | `fdaTargetPath()` — фактический PA0 через `plutil` (фолбэк только на `<supportRoot>/bin/mp3-to-m4b-agent`); `agentJobState()` — `launchctl print` (running/pid) для различения busy; запись команды `recheck-access` |
| `app/EngineClient+Status.swift` | мутируем | `installedHelperPath`; `installedRunnerIsHelper()` через `plutil -extract` **без фолбэка**; байт-диффы bundled↔installed для helper и `runner.sh`; чтение receipt |
| `app/main.swift` | мутируем | синхронное решение `needsRepair` до landing; синхронный `--repair-launchd-only`; `InstallCoordinator` (single-flight); порядок источников watch-dir receipt→plist→state (M2f); `.failed` с 4 выходами + диагностика; поверхность «агент не запустился»; роутер приоритетов; copy/open/recheck |
| `app/StateModel.swift` | мутируем | `FolderAccess` enum (неизвестное → nil ⇒ нет поверхности); `AgentInfo` += `folderAccess`/`folderAccessTs`/`installGeneration` (толерантный декод); `FolderRecheckOutcome`/`FolderRecheck`; `StatusSurface`-роутер |
| `app/FolderAccessCard.swift` | НОВЫЙ · мутируем | 4 подачи на наших `Tokens`; литерал шага 3 = `mp3-to-m4b-agent`; автомат `denied/checking/stillDenied/busy/timeout`; тексты по m1; ack `justCopied` |
| `app/StatusView.swift`, `app/SetupView.swift` | мутируем | рендер blocker/banner по роутеру; `setupStep`-подача; `bundled >= installed` в блоке свежести |
| `app/WindowGeometry.swift` | мутируем | **не дублировать**: новые поверхности проходят через существующие клэмпы «окно всегда на экране» (юниты уже есть) |
| `agent/selfcheck_fda.py` | НОВЫЙ | см. §7 |
| `agent/selfcheck_agent_helper.py` | НОВЫЙ | см. §7 |
| `agent/selfcheck_installer_repoint.py` | мутируем | расширение: PA0/interval/preserve/heal/golden-SHA/lock/generation/repair-only |
| `agent/selfcheck_all.py` | мутируем | +2 сьюты **плоско** (без вложенных прогонов — урок `selfcheck-no-nested-regression`) |

### 5.4 Инварианты стыка с командой `skip` (делает Developer параллельно)

Три правила, которые план v2 обязан не сломать и которые Developer обязан соблюсти:

1. **`denied` ⇒ ни одного разрушающего действия над состоянием.** Ранний выход Р3 не
   трогает `presence.json`, `notified.json`, манифесты и пометку `skip`. Пропущенная книга
   не должна «размориться» из-за отказа доступа.
2. **Re-arm по presence разрешён ТОЛЬКО для `status == done` И не-`skipped`.** Если `skip`
   кодируется отдельным статусом — условие выполняется само (`scan.py:1233` смотрит на
   `done`); если флагом на манифесте — нужна явная проверка.
3. **`skipped` книга не даёт edge-key** (`_book_edge_key`) — иначе нудж поднимет приложение
   для книги, которую человек только что убрал с глаз. И при заморозке витрины на `denied`
   её строка переносится как есть.

---

## 6. Миграция v0.9 и тупик `.failed`

### 6.1 Кто мигрирует

Живая установка человека (v0.9: PA0 = `runner.sh`, watch = `~/Desktop/mp3-to-m4b` на
iCloud-Desktop) плюс внешние установки (repo и DMG публичны).

### 6.2 Счастливый путь (приложение запустили)

1. Запуск нового `.app` → синхронный детект: байты `agent/*.py`/helper протухли или
   `PA0 ≠ helper`.
2. Ветка **«протухли байты»** (обычный апдейт) — существующий асинхронный экран `.updating`
   + полный installer (venv/pip могут занять десятки секунд — блокировать окно нельзя).
   Ветка **«PA0-only»** (байты совпали, PA0 кривой) — **синхронный
   `--repair-launchd-only`** до UI: offline, ≤1–2 с, гарантированно без сети (B2).
3. Installer перепекает plist: PA0 = helper, `StartInterval 300`, новый generation → bootout
   → bootstrap → `launchctl print`-verify → receipt.
4. Агент фирится, переносит generation в state, делает probe → `denied` (грант новому
   субъекту ещё не выдан) → edge-pop поднимает приложение → карточка доступа.
5. Человек жмёт «Открыть настройки и скопировать путь» → включает `mp3-to-m4b-agent` →
   probe через `recheck-access` → `ok` → карточка растворяется.

**Один новый грант неизбежен**: старый субъект был `/bin/bash`, переносить нечего.

### 6.3 Инвариант поверхности доступа (fail-closed, B3)

```
показываем FolderAccess  ⇔  state.agent.folder_access == "denied"
                         ∧  disk PA0 == installedHelperPath        (plutil, без фолбэка)
                         ∧  state.agent.install_generation == receipt.generation
                         ∧  updatePhase ∉ {running, failed}
```

Кнопка «скопировать путь» дополнительно перечитывает PA0 **живьём в момент нажатия** и
отказывается копировать при несовпадении — мёртвый путь не может попасть в буфер даже при
баге роутера. Если generation не сходится (или отсутствует дольше ~15 с после «успешной»
установки) — показываем **другую** поверхность: «агент не запустился» с диагностикой
(фактический PA0, receipt-generation, state-generation, `launchctl print`) и кнопкой
«Починить» → `--repair-launchd-only`. Приоритет: `agentRepair > agentNotRunning >
folderAccess > normal`.

### 6.4 «Приложение не запускали»

Признаём **открытым**, а не «поведенчески закрытым» (M12f): авто-апдейтера в проекте нет
(`main.swift:1119` — только ссылка на GitHub), Sparkle — отдельный трек, не 1.0. Меры:

- release notes / README / текст на DMG: **«после обновления обязательно откройте приложение
  один раз»**;
- честный аргумент, снижающий остроту: у человека на v0.9 подбор из Desktop и так почти
  наверняка мёртв (грант скрипту не работает на Tahoe), поэтому «старый агент живёт неделями»
  — не регресс, а статус-кво (проверяется ручным пунктом §7.4);
- `StartInterval 300` в НОВОЙ установке гарантирует, что после первого запуска приложения
  система сама себя подтягивает без ручного bootstrap.

### 6.5 Rollback и downgrade (Р5, M10f/M11f)

- **Rollback НИКОГДА не трогает PA0.** Frozen helper остаётся на месте; откатываются только
  `runner.sh` и пакет `agent/`. Отдельный rollback-пакет для разработчика — тоже
  helper-preserving.
- **Downgrade до v0.9 на Tahoe — unsupported** и явно запрещён в release notes: старый
  installer безусловно вернёт PA0 = `runner.sh` (`installer.sh:270`), и папка снова станет
  недоступной. Восстановление возможно (повторная установка 1.0 чинит PA0, а грант на helper
  жив, потому что путь и байты не менялись) — это тоже пишем.
- Автоустановка только при `bundled >= installed` по receipt; неизвестный downgrade требует
  явного подтверждения человека, а не тихого «outdated ⇒ переустановить».

### 6.6 Тупик `.failed` (M12f)

Сегодня `AgentUpdatingView.failed` (`main.swift:1364`) показывает только «Повторить», а текст
ошибки при неизвестной папке отправляет «обновите через Настройки», куда с этого экрана
попасть нельзя. Правим:

| Действие | Поведение |
|---|---|
| «Повторить» | как сейчас |
| «Выбрать папку…» | NSOpenPanel → повтор установки с явно выбранной папкой (закрывает случай `currentWatchDir() == nil`) |
| «Открыть Настройки» | `navigate(to: .settings)` — выход из тупика |
| «Продолжить без обновления» | normal landing; агент старый, приложение не кирпич. Поверхность FolderAccess при этом подавлена инвариантом §6.3 — это корректно |
| Диагностика (раскрывающийся блок) | фактический PA0, путь plist, путь receipt, watch_dir из каждого источника (receipt/plist/state), хвост stderr установщика |

Экран проходит через `app/WindowGeometry.swift` (он стал выше — клэмп «окно всегда на
экране» обязателен, ручных высот не заводим).

---

## 7. Тест-план

Изоляция везде: `MP3TOM4B_NO_LAUNCHCTL=1` + временный `MP3TOM4B_LABEL` +
`MP3TOM4B_SUPPORT_DIR` в scratch (паттерн `selfcheck_installer_repoint.py`). Боевое дерево
не трогается. Реальный `launchctl` — только в T0 (под T0-меткой) и в ручных пунктах §7.4.

### 7.1 Селф-чеки (автоматика, `agent/selfcheck_*.py`)

**`selfcheck_installer_repoint.py` (расширение существующей сьюты):**

| Проверка | Что доказывает |
|---|---|
| `pa0` | после installer PA0 == `$BIN_DIR/mp3-to-m4b-agent`, `ProgramArguments` — ровно 1 элемент |
| `interval` | `StartInterval == 300` (integer), `RunAtLoad` true, `WatchPaths` ровно 2 |
| `preserve` | повторный installer НЕ трогает helper (inode+mtime неизменны) — грант не churn'ится |
| `golden_src` | битый **source** ⇒ отказ ДО любых записей (B5) |
| `golden_dst` | битый **destination** ⇒ отказ после установки, plist не опубликован |
| `golden_both` | src и dst одинаково битые ⇒ всё равно отказ (главная дыра src↔dst) |
| `nosymlink` | symlink на helper или на `BIN_DIR` ⇒ отказ (m2) |
| `heal` | засеять PA0 = `runner.sh` → повторный installer ⇒ PA0 вылечен, helper не тронут |
| `lock` | два одновременных installer'а: второй отказывается, дерево не полурушится (B4) |
| `rollback` | падение после publish plist ⇒ восстановлены prev plist и пакет, receipt отсутствует |
| `generation` | plist содержит UUID; receipt пишется **последним**; при падении receipt нет |
| `repair_only` | `--repair-launchd-only` не создаёт venv, не зовёт pip/ffmpeg-детект (проверка через PATH-заглушки, которые падают при вызове), перепекает plist и обновляет generation |
| `busy_refuse` | при манифесте `converting` с живым pid installer отказывается и не делает bootout |
| существующие `gen/commands/repoint/tilde` | регресс |

**`selfcheck_fda.py` (НОВЫЙ, агентная сторона, без launchctl):**
классификация probe (ok / `denied` через `chmod 000` / `missing` через `rm -rf`) ·
поля в state после `run_scan`, `folder_access_ts` меняется между сканами (суб-секунды) ·
**Р3-тест с зубами**: `done` → `denied` → `ok` ⇒ книга НЕ re-arm'ится, `presence.json` и
`notified.json` не изменились, `skip`-пометка цела · заморозка витрины при `denied` (`books`
не пустеет) · transient-`missing` (≥2 скана И ≥10 мин до разрушительного reconcile) ·
carry-forward в `refresh_showcase` · edge-pop: рекордер `MP3TOM4B_NUDGE_CMD` — ровно один
вызов на фронте `≠denied→denied`, ноль на `denied→denied` · команда `recheck-access`
обновляет `folder_access_ts` безусловно · экономия записей: два подряд идентичных скана ⇒
`state.json` mtime не изменился · ротация `events.jsonl` + `read_events()` видит `.1`+текущий
как одну последовательность (gate-инвариант `build_started` после `confirm_accepted`
переживает ротацию между ними).

**`selfcheck_agent_helper.py` (НОВЫЙ, артефакт):**
`packaging/mp3-to-m4b-agent` существует · `lipo -archs` = arm64+x86_64 ·
`codesign --verify --strict` проходит · sha256 == значению из `PROVENANCE.md` (парсить
таблицу) — ловит случайную пересборку/мутацию в git раньше, чем сборочный страж ·
**name-parity**: литерал `mp3-to-m4b-agent` согласован в `installer.sh` / `build-app.sh` /
`FolderAccessCard` (шаг 3) / фолбэке `EngineClient` / `PROVENANCE.md`.

`selfcheck_all.py`: обе сьюты добавляются **плоско**.

### 7.2 Изолированный прогон (руками разработчика, но автоматизируемо)

- **Сигнальный тест (M4f):** запустить агент под T0-меткой на фейковой длинной сборке
  (ffmpeg на большом входе или заглушка-`sleep`), сделать `launchctl bootout` ⇒ через 5 с
  ни одного потомка (`pgrep -P`), temp подметён, манифест в `error: interrupted`.
- **Swift-инвариант поверхности:** чистые функции роутера/`FolderRecheck` вынесены отдельно
  и проверяются маленьким компилируемым раннером (как существующий
  `app/selfcheck_routing.swift` + `agent/selfcheck_app_routing.py`) — таблица
  `(folder_access, PA0, generation, updatePhase) → surface`, включая fail-closed-случаи.
- **Четыре границы golden SHA (M3f):** прогон `build-app.sh` + `make-dmg.sh` со сверкой
  helper'а в repo / staging / `build/dist` / смонтированном DMG.
- **Замер цены тика (M6f):** `powermetrics` + wall/CPU/I-O на watch с 0 / 100 / 1000 файлов,
  до финального подтверждения `StartInterval = 300`.

### 7.3 Что НЕ проверяется автоматически (осознанно)

Реальная TCC-атрибуция (только T0 + человек) · панель System Settings (врёт, не является
доказательством) · Intel-слайс поведенчески (машина arm64; `lipo` гарантирует наличие,
смок — через Rosetta, если доступен) · поведение WatchPaths на iCloud-Desktop после логина.

### 7.4 Руками человека (обязательно)

| # | Что | Сколько |
|---|---|---|
| 1 | **T0.3 + T0.4**: включить FDA для `mp3-to-m4b-agent-t0`, затем негативный контроль байтов (подмена/возврат) | ~5 мин, один заход |
| 2 | **R1b**: включить FDA для боевого `mp3-to-m4b-agent` до релиза — тогда сам релиз проходит для него без похода в настройки | ~2 мин |
| 3 | **Ребут-тест**: перезагрузка + логин, приложение НЕ открывать, бросить папку в watch → окно-предложение всплыло ≤5–6 мин (StartInterval 300) | ~10 мин |
| 4 | **Sanity v0.9 ДО апгрейда**: изолированный probe — работает ли у него сегодня подбор из Desktop вообще (проверка допущения о статус-кво, §6.4) | ~2 мин |
| 5 | **Боевой e2e апгрейда**: обновить `.app` → карточка → грант → книга реально собралась в `.m4b` без терминала (урок `real-e2e-before-shipping`) | ~15 мин |
| 6 | **Уборка панели**: снять строку `mp3-to-m4b-agent-t0` из FDA после T0 | ~1 мин |

---

## 8. Риски и открытые вопросы к человеку

### 8.1 Остаточные риски (после всех правок)

| Риск | Заслон | Что остаётся |
|---|---|---|
| Байты helper'а изменятся в конвейере (пересборка, `codesign --deep`, iCloud-эвикция, git-фильтры) | freeze-guard, golden SHA в installer, страж на 4 границах, `.gitattributes`, selfcheck против PROVENANCE | правка `.c` «по мелочи» без понимания — гасится шапкой и env-стражем, но не технически |
| `codesign --force --deep` мутирует Mach-O в Resources | эмпирика донора + release-blocking страж | это **эмпирика, не контракт Apple**: страж и существует ровно затем, чтобы поймать смену поведения |
| Установщик убит посреди работы | lock, stage→validate→replace, backup plist, откат, receipt последним | окно «bootout сделан, bootstrap не дошёл» ловится generation-проверкой при следующем запуске приложения |
| Тик `StartInterval` пропускается во время сборки и во сне (контракт launchd) | UI честно говорит «после сборки»; probe обновится сам постсборочным сканом | «Проверить снова» не может быть мгновенной во время многоминутного encode — это принято, не баг |
| Заморозка витрины при `denied` скрывает реальное исчезновение книг | при deny агент физически не может знать правду; после гранта первый скан приводит витрину к реальности | принято осознанно |
| Ротация журналов теряет события старше двух файлов | 5 МБ на файл, gate-инварианты живут внутри сессии | gate-тест, охватывающий > 5 МБ журнала, непроверяем — принято |
| tccd поменяет модель атрибуции (macOS 27+) | T0-харнес остаётся в repo как перегоняемый смок | не устраняется в принципе |
| Intel-слайс не проверен поведенчески | `lipo` + Rosetta-смок если доступен | риск принимаем (как донор) |

### 8.2 Что сознательно НЕ делаем в 1.0

- **Долгоживущий FSEvents-агент** вместо run-once + StartInterval (M9f): цена — event loop,
  initial scan, coalescing, очередь команд, sleep/wake, shutdown/update, периодическая полная
  сверка. Это отдельный milestone 1.x, а не «замена одной строки в plist».
- **Авто-апдейтер приложения** (Sparkle) — отдельный трек; в 1.0 закрываем release notes'ами.
- **Обслуживание recheck внутри идущей сборки** (агент отвечает на probe-запрос, не выходя
  из encode-цикла): возможно, но добавляет команду-канал в самое горячее место. В 1.0 —
  честный текст «проверим после сборки» + автоматическое растворение карточки.
- **Помощь при downgrade уже выпущенной v0.9** — технически невозможно (её installer вернёт
  PA0), только release notes.

### 8.3 Открытые вопросы к человеку

1. **`StartInterval = 300 с`** — подтверждаешь как дефолт? Цена: дропнутая папка при мёртвых
   `WatchPaths` ждёт до 5 минут. Финальное число подтвердим замером `powermetrics` (M7), но
   решение о продуктовом обещании («положил папку — окно всплыло само за сколько?») — твоё.
2. **Имя в панели FDA:** боевое `mp3-to-m4b-agent` и тестовое `mp3-to-m4b-agent-t0` (плюс
   уже висящий соседский `fb2-to-epub-agent`). Ок, или тестовое назвать иначе?
3. **Два захода с тумблером** (T0.3+T0.4 ~5 мин на этапе M0 и R1b ~2 мин на этапе M7) —
   когда удобно? Без первого не открывается гейт M0, без второго не закрывается релиз.
4. **Swift-тест-раннер для инварианта поверхности** (§7.2): заводим в 1.0 (рекомендую — это
   единственный автоматический заслон для fail-closed-правила §6.3) или принимаем ослабление
   «коммент + ручной QA»?
5. **Release notes для внешних пользователей v0.9**: подтверди три обязательных абзаца —
   (а) система один раз попросит доступ, это смена механизма под macOS 26; (б) после
   обновления обязательно откройте приложение один раз; (в) downgrade до v0.9 на Tahoe
   не поддерживается.

---

## Приложение: где искать обоснование

| Тема | Источник |
|---|---|
| Полное обоснование варианта A, донорские инварианты helper'а, разбор альтернатив | `arch/plan-binrunner-mp3-claude.md` §Р1–Р4, Допущения A1–A11 |
| Формулировки атак, ссылки на строки кода и man-страницы, точный рецепт tccd-лога | `arch/plan-binrunner-mp3-codex.md` |
| Донорские решения судьи (имя, quarantine, байт-страж, negative control, R1b) | `../2026.06 fb2-to-epub/arch/plan-binrunner-synthesis.md` |
| Почему грант скрипту мёртв и почему панель нельзя считать доказательством | `../2026.06 fb2-to-epub/.patches/020-tahoe-fda-script-grant-dead-real-not-panel.md` |
| Боевой C-helper, который портируем | `../2026.06 fb2-to-epub/packaging/agent-src/fb2-to-epub-agent.c` |
