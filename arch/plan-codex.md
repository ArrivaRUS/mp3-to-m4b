# mp3-to-m4b — архитектурный план (Архитектор #2 · GPT-5.5 через Codex)

> Сгенерирован независимо от Архитектора #1 (Claude) — второе мнение на другой LLM.
> **Движок:** Codex CLI v0.142.0 · модель `gpt-5.5` · reasoning effort `xhigh` · sandbox `read-only`.
> **Дата:** 2026-06-28. **Вход:** prd/PRD.md, design/spec.md, design/flows.md, research/m4b-toolchain.md, decisions/log.md + каркас-сосед fb2-to-epub (Codex прочитал их сам в read-only).
> **Главный тезис Codex:** state.json — только односторонняя витрина; подтверждение — НЕ двунаправленный RPC и НЕ общий мутируемый файл, а **app-owned append-only командный лог + agent-owned манифесты**, где агент — единственный владелец переходов состояния.

---

**1. Процессная модель app/agent**

- `SwiftUI app` · UI, подтверждение, очередь, Status; читает `state/` и `queue/books/`, пишет только команды в `queue/commands/` · зависит от `StateModel`, `EngineClient`, LaunchAgent label · тест: закрыть app, кинуть книгу, агент открывает app; app без прав на папку не конвертирует сама.
- `LaunchAgent/runner` · единственный владелец сканирования, job-файлов, `state.json`, ffmpeg, выхода `.m4b` · зависит от FDA, `ffmpeg`, `ffprobe`, Python venv · тест: `launchctl print` может быть `not running`, но active=true если bootstrapped+plist+not disabled.
- `Engine Python + thin bash watcher` · я бы не писал m4b-движок в bash: bash оставить для lock/launchd, Python - для JSON, ffprobe, команд ffmpeg argv-массивами · зависит от agent env · тест: пути с пробелами/кириллицей.
- `Trust boundary` · app не трогает исходники и финальные m4b; agent под FDA читает source и пишет output · зависит от стабильного runner path · тест: app пишет confirm-job, agent делает всю сборку.

**2. ★★ Протокол app↔agent ДЛЯ ПОДТВЕРЖДЕНИЯ**

Мой вариант: `state.json` - только витрина. Не RPC. Не двунаправленный файл. Рабочая модель - agent-owned job manifest + app-owned immutable command files.

- `state/state.json` · краткая витрина: `pending_confirm_count`, `batch`, `recent`, `current`, totals · пишет agent атомарно tmp→fsync→rename, app только читает · тест: битый/частичный state декодится в empty UI.
- `queue/books/<book_id>.json` · agent-owned durable manifest книги: `status`, `source_rev`, `source_fingerprint`, главы, длительности, cover candidates, `confirm_token`, progress, error · app читает, не пишет · тест: ручное редактирование app отсутствует по коду.
- `queue/commands/<cmd_id>.json` · app-owned команда: `action=confirm|skip|cancel|retry|grouping|cover_research`, `book_id`, `source_rev`, `confirm_token`, `idempotency_key`, payload правок · app пишет `.tmp` потом rename; agent игнорирует `.tmp` · тест: оборвать запись tmp - сборка не стартует.
- `covers/{previews,generated,manual}` · agent пишет previews/generated; app может положить выбранный вручную файл в `covers/manual/<uuid>` атомарно, agent валидирует и нормализует в jpg · тест: битая картинка даёт error, не build.
- `events.jsonl` · append-only аудит: `confirm_required`, `confirm_accepted`, `build_started`, `build_done`, `confirm_rejected_stale` · зависит от agent · тест G2: нет `build_started` без предшествующего `confirm_accepted`.

Ключи:
- `book_id = sha256(canonical watch-root relative group id)`, стабильный для той же книги.
- `source_fingerprint = hash(relpath,size,mtime_ns,duration)`; при изменении mp3 меняется `source_rev`.
- `confirm_token` генерит agent при `pending-confirm`; app обязана вернуть его.
- `idempotency_key = book_id/source_rev/action/build`, agent хранит обработанные ключи.

Инвариант “без подтверждения не собирать”:
- scanner никогда не вызывает build;
- build запускается только из `accept_confirm(command)` после проверки `status == pending-confirm`, `source_rev` совпал, `confirm_token` совпал, title валиден, cover asset существует;
- `converting` нельзя выставить из app;
- на рестарте `converting` без живого pid -> `error: interrupted`, temp удалён, пользователь жмёт retry.

Гонки:
- app пишет правки пока agent сканирует · правки живут локально в app до “Собрать”; если `source_rev` сменился, confirm rejected stale, UI перечитывает manifest.
- двойной клик · app disabled после первого клика; agent дополнительно дедупит `idempotency_key`; второй command архивируется как duplicate.
- та же книга появилась снова · если output новее всех mp3 - `skipped_idempotent`; если source changed - новый `source_rev`, старые команды невалидны.
- app закрыли · команды нет, job бесконечно `pending-confirm`.
- частичный jobs-файл · `.tmp` игнор; malformed `.json` -> `commands/bad/`, no build.
- устаревшая правка к done/error · agent проверяет status/source_rev и пишет `confirm_rejected_stale`.

Где я расхожусь с наивным Claude-подходом: не надо давать app писать `state.json`, менять `queue/books/*.json` или ставить `confirmed=true` в общем файле. Это создаёт гонку читатель/писатель и дыру “случайно подтвердили”. Надёжнее command-log: app просит, agent валидирует и единолично меняет состояние. Цена - больше файлов и нужен janitor для архивов.

**3. Движок ffmpeg**

- `probe.py` · ffprobe JSON: duration, tags, track, APIC/video streams; natural sort; corrupt detection · зависит от ffprobe · тест: mixed tags, кириллица, битый mp3.
- `metadata.py` · FFMETADATA `TIMEBASE=1/1000`, START/END из накопленных длительностей, global tags · зависит от probe · тест: `ffprobe -show_chapters`.
- `build_m4b.py` · concat filter + per-input `aformat`, AAC encode, cover map, `-f ipod -movflags +faststart` · зависит от accepted confirm · тест: разнородные SR/channels дают сумму без дрейфа.
- `cover pipeline` · APIC extract -> web candidates -> generated square SVG→PNG -> normalize selected cover to jpg · зависит от Python venv `cairosvg/pillow` · тест: no network still yields cover.
- `split.py` · готовый m4b режет stream-copy `-ss/-to`, per-part metadata, обязательно `-map_chapters 1` · зависит от full m4b · тест: нет дублей/нулевых фантомных глав.
- `progress` · ffmpeg `-progress pipe:1`, map `out_time_ms` to chapter boundary · зависит от cumulative durations · тест: Status показывает главу N/M.
- `cancel` · app пишет cancel command, agent TERM/KILL pid, удаляет temp, status `cancelled` · зависит от pid file · тест: финального `.m4b` нет, исходники целы.

**4. launchd / installer**

- `installer.sh` · детект `ffmpeg`/`ffprobe`, Python, venv deps; копирует runner/watcher/engine; plist через `plutil` · зависит от Homebrew/system paths · тест: путь с пробелами в WATCH_DIR.
- `LaunchAgent plist` · `WatchPaths = [WATCH_DIR, queue/commands]`, env: absolute `FFMPEG`, `FFPROBE`, `PYTHON`, `PATH`, dirs · зависит от installer · тест: команда confirm kickstart-ит agent даже без нового mp3.
- `runner.sh` · стабильная FDA-цель в App Support, bytes не churn без причины · зависит от packaging · тест: переустановка не меняет runner при совпадении.
- `EngineClient` · active = bootstrapped + plist exists + not disabled; running pid не нужен · зависит от `launchctl` · тест: idle `not running` отображается как “Активен”.

**5. Сборка / DMG**

- `.app` · клонировать native SwiftUI universal build, bundle id `com.arrivarus.mp3tom4b` · зависит от `swiftc`, `lipo`, `codesign` · тест: launch, icon, dark window.
- `Resources` · installer, runner, watcher, Python engine, cover templates/assets · зависит от build script · тест: installed App Support scripts byte-match bundled.
- `codesign` · ad-hoc `codesign -s -`, strip xattrs, retry FinderInfo race · зависит от соседского build pattern · тест: strict verify.
- `DMG` · `dmgbuild`, @2x фон, real Finder screenshot verification · зависит от dmg settings · тест: mount/open/screenshot, не только `hdiutil verify`.
- `venv` · runtime venv в App Support для `cairosvg/pillow`; build venv отдельно для icon/dmg tooling · тест: no internet install degrades only generated covers if deps absent.

**6. Карта файлов**

| файл | клон/новое | ответственность |
|---|---|---|
| `app/main.swift` | клон+правка | окна, rising-edge pending-confirm, height cap |
| `app/Tokens.swift` | клон+переложить токены | дизайн-токены mp3 |
| `app/StateModel.swift` | новое по схеме | state + book manifests decode |
| `app/EngineClient.swift` | клон+правка | launchd/install/status bridge |
| `app/EngineClient+Commands.swift` | новое | atomic confirm/cancel/grouping commands |
| `app/StatusView.swift` | клон+правка | Status D8 |
| `app/ConfirmView.swift` | новое | главное окно подтверждения |
| `app/QueueView.swift` | новое | pending/converting/error queue |
| `app/GroupingView.swift` | новое | одиночные mp3 в корне |
| `app/CoverPickerView.swift` | новое/из CoverSelect идеи | cover states до сборки |
| `bin/mp3-to-m4b-watcher.sh` | клон+тоньше | lock, drain commands, call engine |
| `bin/mp3-to-m4b-engine.py` | новое | scan/probe/manifest/build orchestration |
| `bin/m4b_build.py` | новое | ffmpeg command assembly |
| `bin/cover_search.py` | клон идеи, новое аудио | DDG/Yandex/litres/OpenLibrary 1:1 |
| `bin/cover_gen.py` | клон движка, новые шаблоны | square fallback covers |
| `packaging/installer.sh` | клон+правка | ffmpeg/venv/plist/FDA |
| `packaging/mp3-to-m4b-runner.sh` | клон+правка | stable FDA runner |
| `build/build-app.sh`, `make-dmg.sh` | клон+правка | app/DMG release |

**7. Майлстоны по зависимостям (validation-first)**

- `M0 protocol skeleton` · agent видит mp3, создаёт `pending-confirm`, app читает очередь, “Собрать” пишет command, fake engine ставит done · зависит от cloned app/installer/state · тест: без command нет converting; stale rev rejected; double click duplicate.
- `M0.5 real probe + metadata` · ffprobe durations/tags/APIC, главы, cover preview · зависит от protocol · тест: 3 книги: теги, без тегов, битый mp3.
- `M1 MVP vertical` · всплытие -> правка -> confirm -> real `.m4b` с главами+обложкой -> Status · зависит от ffmpeg build · тест: `ffprobe format=major_brand`, chapters count, attached_pic=1, output newer idempotent skip.
- `M1 queue/edge` · группировка root mp3, multiple pending, no network generated cover, no space/corrupt errors · зависит от M1 vertical · тест: 5 папок, app closed, user absent overnight, retry/skip.
- `M1 package QA` · LaunchAgent install, FDA hint, DMG, real render/click tests · зависит от full app · тест: fresh HOME, protected folder, Finder DMG screenshot, click every visible control.

**8. Риски**

- `confirmation protocol` · главный риск - случайный auto-build или stale confirm · митигация: command-only, source_rev, confirm_token, idempotency_key, build gate tests.
- `ffmpeg concat` · сотни файлов/open-file limit/filter string length · митигация: `filter_complex_script`, safety threshold, fallback normalized concat demuxer.
- `split` · дубли глав/фантомная глава · митигация: обязательный `-map_chapters 1`, ffprobe regression.
- `covers` · сеть пуста, tofu кириллицы, битый ручной файл · митигация: generated fallback до “Собрать”, зелёный список шрифтов, render validation, jpg normalization.
- `UI` · окно подтверждения выше экрана, мёртвые plain buttons · митигация: cap by `visibleFrame`, scroll chapters/queue, `.contentShape`, click tests.
- `launchd/FDA` · путать idle `not running` с поломкой, потерять TCC grant · митигация: status semantics соседа, stable runner, no byte churn.
- `disk/temp` · ENOSPC и полуфайлы · митигация: temp рядом с output, estimate preflight, atomic rename, cleanup on startup.
