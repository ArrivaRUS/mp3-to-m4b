# Архитектор #2 (Codex / gpt-5.5, effort=xhigh, sandbox=read-only) — авто-всплытие окна подтверждения

> Дата: 2026-07-07. Задача: при новой pending-confirm книге / grouping-промпте приложение само всплывает.
> Сырой финальный ответ Codex (сессия 019f3ae5). Полный лог reasoning не сохраняем — только вывод.

**(1) ВЫБОР ПУТИ + ПОЧЕМУ**
- Рекомендую **(А) агент будит приложение**.
- Вариант (Б) слабее: Cmd+Q полностью убивает app, значит watcher в GUI больше не существует и проблема возвращается.
- `open -b com.arrivarus.mp3tom4b` подходит: запускает закрытое приложение по bundle id и активирует уже запущенное. Bundle id задан в [agent/config.py](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/config.py:24) и [build/build-app.sh](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/build/build-app.sh:29).
- Использовать **без** `-g` и **без** `-j`: `-g` оставляет app в фоне, `-j` запускает hidden. `open -a` хуже как основной путь: зависит от имени/дубликатов app; годится только как ручной fallback.

**(2) МЕСТО В КОДЕ + RISING-EDGE + КОМАНДА**
- Не только `refresh_showcase()`: начальный баг создаётся через `run_scan()` в [agent/__main__.py](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/__main__.py:59), а `refresh_showcase()` на [agent/scan.py](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:1091) не вызывается на простое появление новой папки.
- Минимальное место: новый helper рядом с `run_scan()`/`refresh_showcase()` в [agent/scan.py](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:1062), условно `_publish_showcase_and_maybe_open(prev, showcase)`. Заменить только два финальных `state.write_state(showcase)` на [scan.py:1087](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:1087) и [scan.py:1129](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:1129). Детекция живёт в одном helper.
- Diff:
  - `prev_pending = {book_id из prev["books"] где status == "pending-confirm"}`
  - `next_pending = {book_id из showcase["books"] где status == "pending-confirm"}`
  - `new_pending = next_pending - prev_pending`
  - `prev_groups = {group_id из prev["pending_groups"]}`
  - `next_groups = {group_id из showcase["pending_groups"]}`
  - `new_groups = next_groups - prev_groups`
- Команда после успешной записи `state.json`: `subprocess.run(["/usr/bin/open", "-b", config.BUNDLE_ID], stdout=DEVNULL, stderr=DEVNULL, timeout=2)`.

**(3) СНЯТИЕ КАЖДОГО EDGE / ЗАЩИТА ОТ ПЕТЛИ**
- Хранить уже поднятые edge в отдельном agent-owned файле, например `config.state_dir() / "notified.json"`, через `state.read_json()`/`state.write_json_atomic()` из [agent/state.py](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/state.py:26). Не в `state.json`.
- Для книг лучше ledger key не голый `book_id`, а `book:<book_id>:<source_rev[:16]>:<confirm_token[:16]>`, прочитанный из `queue/books/<book_id>.json`; `state.json` сам не содержит rev/token, см. `build_state()` rows на [scan.py:899](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:899).
- Для grouping: `group:<group_id>:<rev[:16]>:<token[:16]>`, поля уже есть в `_build_pending_group()` на [scan.py:485](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:485).
- Открывать app только если `(new_edge_keys - notified_keys)` не пуст. После успешного `open` добавить эти keys в `notified.json`. Тогда команда из GUI в `queue/commands/` снова разбудит agent через WatchPaths, но старый pending/grouping уже не даст повторного foreground.

**(4) РИСКИ + МИТИГАЦИЯ**
- Auto-update экран: app может стартовать в `.updating` на [app/main.swift:3755](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/app/main.swift:3755). Это приемлемо: после успеха `handleInstalled()` перечитывает state и уходит в `.confirm`, если есть manifest, на [app/main.swift:3956](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/app/main.swift:3956). При failure пользователь увидит update error вместо confirm.
- Чтение во время записи: достаточно безопасно. Agent пишет tmp→fsync→`os.replace` на [state.py:36](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/state.py:36), app читает defensively на [StateModel.swift:813](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/app/StateModel.swift:813).
- iCloud partial copy: для subfolder books уже есть debounce `_files_are_stable()` на [scan.py:355](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:355) и skip в `_write_manifest()` на [scan.py:596](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:596). Для loose grouping такого debounce сейчас нет перед [scan.py:851](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:851); если будут ложные grouping edges, добавить тот же stability check перед `_build_pending_group()`.
- Rapid-fire: один `open` на один showcase publish, не по одному на книгу. Несколько новых книг в одном scan дают один foreground.

**(5) ЧЕГО НЕ ДЕЛАТЬ**
- Не переводить agent в `KeepAlive`/polling/таймер: текущая run-once модель описана в [agent/__main__.py](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/__main__.py:15) и plist уже `WatchPaths` + `ThrottleInterval` на [packaging/installer.sh:274](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/packaging/installer.sh:274).
- Не давать app писать `state.json`; app пишет только commands, см. [EngineClient.swift:7](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/app/EngineClient.swift:7).
- Не ломать `refresh_showcase()` preservation: `pending_groups`/`grouping_processed` сохраняются на [scan.py:1117](/Users/arrivarus/Documents/VibeCoding2/2026.06%20mp3-to-m4b/agent/scan.py:1117).
- Не всплывать на каждом scan/progress/status tick.
- Не трогать ffmpeg/build path, atomics, command validation, `confirm_token`/`source_rev` протокол.
