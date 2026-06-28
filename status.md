# status.md — mp3-to-m4b (живой лог исполнения)

> Обновляется по ходу разработки. План — `plans.md`. Гейты — `test-plan.md`.
> **Обновлено:** 2026-06-28.

## Где мы
**ФАЗА 4 (разработка) — старт.** Пройдены гейты: G0 (clone-go) · G1 (PRD) · G2a (лого) · G2b (макеты) ·
G3 (дизайн-спец) · **G4 (архитектура)**. Дизайн и план полностью готовы и приняты человеком.

## Сделано
- Интейк, публичный репо, тех-разведка (ffmpeg-only).
- PRD/истории/беклог (MVP=26 Must). Лого + лого-пак. Макеты 7 экранов + флоу. Токены (~210) + дизайн-спец.
- Двойной архитектор + синтез (G4): протокол command-log/манифесты, Python-движок, обложки Pillow.
- execution-pack собран (`plans.md`/`status.md`/`test-plan.md`). Контрольный коммит запушен.
- **M0.1 ✅** каркас репо: agent-пакет (alive, 8 каталогов данных) + `bin/runner.sh` + пустое тёмное окно `app/` + `build/build-app.sh` (universal `.app`, codesign strict verify ok). Проверено Юркой.
- **M0.2 ✅** агент: скан → манифест `queue/books/<id>.json` (`pending-confirm`+`source_rev`+`confirm_token`, главы натур.сортировки, дефолты D2/D6) + витрина `state.json` (агент — единственный писатель). Проверено Юркой: идемпотентность + re-arm на новый файл + игнор мусора. `scan.py`/`metadata.py`(natural_sort+chapter_name)/`__main__.py --scan`.

- **M0.3 ✅** приложение читает `state`+манифесты, показывает книгу+главы, всплывает на rising-edge. `StateModel.swift` (Codable + partial-read защита), `main.swift` (ReaderModel/ConfirmView/file-watch DispatchSource). **Проверено Юркой ВИЗУАЛЬНО (computer-use):** окно «Подтверждение книги» → «Толстой - Война и мир» → 4 главы (имена очищены, натур.порядок) → «Собрать». Developer по пути починил macOS-12 API при таргете 11.0 + гонку codesign под iCloud (build в staging вне облака).
- **M0.4 ✅** кнопка «Собрать» пишет `queue/commands/<cmd_id>.json` (`confirm-build`+8 полей, атомарно) + UI-ack «Отправлено…». `EngineClient.swift`. **Проверено Юркой настоящим кликом (computer-use):** файл команды появился, `confirm_token`/`source_rev` совпали с манифестом, `idempotency_key` детерминированный, без `.tmp`, `bad/` пуст.
- **M0.5 ✅** агент дренирует `queue/commands/`, валидирует, **fake-engine → `done`** + `state.json`, удаляет команду, события. `dispatcher.py` (drain/validate/_fake_build/quarantine), `state.append_event`. **Проверено Юркой ВЖИВУЮ сквозь весь круг (computer-use):** клик «Собрать» → команда → агент-drain → книга `done` (`result.fake`) → команда удалена → **окно само ушло в «Очередь пуста»** (file-watch). Протокол подтверждения доказан end-to-end. (NB: `events.jsonl` в боевом прогоне вышел пустым — проверить в M0.6.)
- **M0.6 ✅** защиты протокола: stale→`confirm_rejected_stale`, дедуп `idempotency_key` (двойной клик=один build), рестарт в `converting`→`error: interrupted`+cleanup temp, malformed→`bad/`, `events.jsonl` durable (fsync). **Проверено Юркой: `python3 -m agent.selfcheck_m0` → 36/36 PASS** → гейт `test-plan §M0` ЗАКРЫТ.

## ✅ M0 ЗАВЕРШЁН (скелет протокола, fake-engine)
Протокол app↔agent доказан **end-to-end вживую** (клик→команда→исполнение→окно ушло) + **36/36** защит (I1/I2/G5, stale/дедуп/interrupted/malformed, журнал-гейт «нет build_started без confirm_accepted», единственный писатель). Код M0 закоммичен.
**Следующий: M0.5 — реальная разведка** (ffprobe: длительности глав, ID3 автор/название, детект встроенной обложки) → затем **M1** (реальный `.m4b`).

## Открытые хвосты / к ратификации позже (QA)
- `stik=Audiobook` для Apple Books — проверить на QA, иначе опц. AtomicParsley.
- Кодек `aac` vs Apple `aac_at` — опц. сравнение QA.
- Долг косметики: brand-basics 2-я стопа `#0E1A22`→`#0E1822` (spec §0.2).
- **Иконка к релизу:** сейчас `.icns` собирается через `sips` (cairosvg нет); перед релизом запинить `cairosvg` в `build/.venv` для качественного `.icns` (пометка в `build-app.sh`).
- **codesign detritus** (iCloud FinderInfo-гонка, урок соседа .patches/003): сборочный ретрай гасит; отдельный пост-`codesign --verify` может косметически ругаться `detritus not allowed` — на запуск не влияет.

## Текущий милстоун
M0 — скелет протокола (fake-engine). **✅ ЗАВЕРШЁН 6/6** (`selfcheck_m0` 36/36). Следующий милстоун: **M0.5 — реальная разведка ffprobe** → M1 (реальный `.m4b`).
