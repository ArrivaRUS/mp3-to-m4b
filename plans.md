# plans.md — mp3-to-m4b (источник правды исполнения)

> Durable, резюмируемый план. Основан на `arch/synthesis.md` (G4). **Дата:** 2026-06-28.
> Живой статус — `status.md`. Гейты валидации — `test-plan.md`. Решения — `decisions/log.md` (D1–D12).
> Принцип: **validation-first** — самый рискованный кусок (протокол подтверждения) валидируем первым,
> микрошагами (<2 мин), скриншот/прогон после каждого.

## Архитектура (кратко, детали в `arch/synthesis.md`)
- **Приложение (SwiftUI, читатель):** Setup · Status · Окно подтверждения · Очередь · Настройки.
  Читает `state/` + `queue/books/`; пишет ТОЛЬКО `queue/commands/`. Без FDA.
- **Агент (launchd, единственный писатель):** тонкий `bin/runner.sh` (FDA-цель) → `exec python3 -m agent`.
  Скан папки, манифесты, `state.json`, ffmpeg, выход `.m4b`.
- **Протокол:** агент пишет `state/state.json` (витрина) + `queue/books/<book_id>.json` (манифест);
  app пишет `queue/commands/<cmd_id>.json` (atomic, с `source_rev`+`confirm_token`+`idempotency_key`).
  `WatchPaths` = папка + `queue/commands/`. **Сборка — только в обработчике `confirm-build` (I2).**
- **Движок (Python):** `probe.py · metadata.py · cover.py · build_m4b.py · split.py` (рецепты `research/`).
- **Данные:** `~/Library/Application Support/mp3-to-m4b/{state,queue/{books,commands,commands/bad},covers,bin,venv}`.

## Карта файлов (клон соседа vs новое)
| Путь | Источник |
|---|---|
| `app/*.swift` (main, StateModel, EngineClient, Tokens, *View) | клон fb2 + адаптация под манифесты/команды |
| `agent/*.py` (dispatcher, scan, probe, metadata, cover, build_m4b, split, state) | **новое** (Python-движок) |
| `bin/runner.sh` | клон fb2 (тонкая FDA-цель) |
| `packaging/installer.sh` · `launchd/*.plist.template` | клон fb2 + новые пути/label/детект ffmpeg |
| `build/build-app.sh · make-dmg.sh` | клон fb2 + bundle id `com.arrivarus.mp3tom4b` |
| `branding/icon-app.svg` → AppIcon.icns | готово (G2a) |

---

## M0 — Скелет протокола (fake-engine) · риск №1 первым
Цель: доказать протокол app↔agent БЕЗ реального ffmpeg.
- [ ] M0.1 Каркас репо: `app/` (пустое окно), `agent/` (python-пакет), `bin/runner.sh`, каталоги данных. *(developer)*
- [ ] M0.2 Агент: скан папки → для книги пишет `queue/books/<id>.json` (`status=pending-confirm`, `source_rev`, `confirm_token`) + строку в `state.json`. *(developer)*
- [ ] M0.3 Приложение: читает state+манифесты, показывает книгу, всплывает на rising-edge `pending-confirm`. *(developer + Юрка-скриншот)*
- [ ] M0.4 «Собрать» → app пишет `queue/commands/<cmd_id>.json` (`confirm-build`+token+rev, atomic). *(developer)*
- [ ] M0.5 Агент: `WatchPaths` ловит команду → валидирует (status/rev/token) → **fake-engine** ставит `done`. *(developer)*
- [ ] M0.6 Защиты: нет команды → нет `converting`; stale `source_rev` → `rejected`; дубль `idempotency_key` → один build; malformed → `commands/bad/`; рестарт в `converting` без pid → `error: interrupted`+cleanup. *(developer)*
- **Гейт M0:** `test-plan.md §M0` зелёный (протокол/гонки). I1/I2/G5 доказаны на fake-engine.

## M0.5 — Реальная разведка
- [ ] ffprobe `probe.py`: длительности/теги/детект встроенной обложки. *(developer)*
- [ ] `metadata.py`: автор/название (album/artist → имя папки «Автор - Название»), порядок (track → натуральная сортировка), имена глав (title → имя файла без префикса). *(developer)*
- [ ] Манифест наполняется реальными главами + превью обложки из mp3 (если есть). *(developer)*

## M1 — Вертикаль MVP (реальный .m4b), затем слои
- [ ] **Вертикаль:** `build_m4b.py` — concat filter+aformat→AAC, FFMETADATA-главы 1/1000, attached_pic, `-f ipod +faststart`, atomic temp→rename. Подтверждение→реальная книга→Status. *(developer)* ⟵ сердце
- [ ] **Окно подтверждения целиком** по `design/spec.md §3`: поля, главы (cap высоты+скролл!), бокс качества, обложка, оценка, кнопки, все состояния (disabled/converting/error danger+warn). *(developer + Юрка pixel-verify)*
- [ ] **Цепочка обложки** (`cover.py`): из mp3 → веб (urllib DuckDuckGo/Yandex, квадрат-фильтр) → генерация Pillow (градиент+текст, зелёный шрифт) → `cover-choice`-команда. *(developer)*
- [ ] **Группировка** одиночных mp3: sheet D1 → `grouping-choice`. *(developer)*
- [ ] **Очередь** нескольких книг + прогресс пачки (кольцо `batch{}`). *(developer)*
- [ ] **Status** целиком (`spec §5`): кольцо, стат-карты, строки, последние книги, авто-рефреш событийно. *(developer)*
- [ ] **Надёжность** (edge E1–E18): нет сети→генерация, нет места, битый mp3 (warn «без него»), идемпотентность, недокопированный файл, мусор. *(developer)*
- [ ] **Нарезка** (P1, последней): `split.py` по границам глав + предпросмотр частей. *(developer)*

## Упаковка / релиз
- [ ] `installer.sh` (детект ffmpeg, plist plutil, bundle id, миграция) + venv (pillow). *(developer)*
- [ ] `build-app.sh` (swiftc universal + codesign-ретрай) + AppIcon из `branding/icon-app.svg`. *(developer)*
- [ ] `make-dmg.sh` + **живой рендер/клик-тест DMG** (урок соседа). *(developer + Юрка)*
- [ ] README RU/EN, changelog. *(tech-writer / changelog-writer)*
- **Гейт релиза ⛔ G5:** PR + DMG только после «да» человека.

## Сквозные правила (из spec/уроков)
- Окно по контенту, **cap высоты по `screen.visibleFrame`** + скролл переменной секции; **тест на максимуме** глав/книг.
- Каждый видимый контрол рабочий + `.contentShape`. Числа — tabular-nums. Тема тёмная всегда.
- После каждой микрозадачи — скриншот/прогон Юркой. Лимиты: 3 цикла ревью, 2 переделки; баг >2 раз = долг процесса.
