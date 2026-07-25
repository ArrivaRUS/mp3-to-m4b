# Адверсариальный разбор плана порта бинарного раннера — Архитектор #2 (Codex / GPT-5.6 Sol)

> Дата: 2026-07-25 · Движок: `gpt-5.6-sol`, `model_reasoning_effort=xhigh`, `-s read-only`, ~14 мин, 261 633 токенов.
> Разбираемый документ: `arch/plan-binrunner-mp3-claude.md`. Codex читал наш код и код донора,
> man-страницы `launchd.plist(5)` из MacOSX26.5.sdk и заголовки EndpointSecurity. Файлы не менялись.
> Сырой вывод, переложен без правок содержания.

[BLOCKER] A1 пока неизвестно, а не «принято»

Что сломается: FDA helper’а не покроет Python после `bash exec→python`; весь порт сохранит молчаливый deny.

При каком сценарии: `launchd → helper → /bin/bash runner.sh`, затем PID ребёнка исполняет `execve()` venv-Python и уже Python вызывает `os.listdir()`.

Доказательство/рассуждение: план утверждает законность `exec` до её проверки ([plan:52](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:52>), [plan:422](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:422>)); фактический `exec` стоит в [runner.sh:55](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/bin/runner.sh:55>). Донор оставляет helper и bash живыми ([donor C:126](</Users/arrivarus/Documents/VibeCoding2/2026.06 fb2-to-epub/packaging/agent-src/fb2-to-epub-agent.c:126>)). Модель ОС говорит в пользу A1: `responsible_audit_token` отделён от parent, а exec меняет `pidversion` ([ESMessageCore.h:60](/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/usr/include/EndpointSecurity/ESMessageCore.h:60), [ESMessageCore.h:69](/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/usr/include/EndpointSecurity/ESMessageCore.h:69)); Apple различает parent и responsible process. Но публичного контракта «TCC/FDA на macOS 26 обязательно сохраняет subject через exec» нет. Это сильная гипотеза, не доказательство.

Конкретная правка плана: пометить вариант A как provisional до exact-exec T0. Принимать только лог, где `accessing` — PID Python после exec, а `responsible_path` и `AUTHREQ_SUBJECT` — helper. Общий успешный probe без корреляции PID/msgID не считается.

[MAJOR] T0 смешивает scratch-path и боевой FDA-path

Что сломается: тест выдаст грант одному пути, а запустит другой; либо зелёный результат будет относиться не к тому helper’у.

При каком сценарии: `MP3TOM4B_SUPPORT_DIR=/tmp/<scratch>` переносит `$BIN_DIR` в `/tmp`, но T0.2 одновременно требует добавить «боевой `$BIN_DIR/mp3-to-m4b-agent`».

Доказательство/рассуждение: scratch root задан в [plan:304](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:304>), боевой путь — в [plan:314](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:314>). В текущем installer `$BIN_DIR` всегда производен от `MP3TOM4B_SUPPORT_DIR` ([installer.sh:44](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:44>), [installer.sh:48](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:48>)).

Конкретная правка плана: развести `HELPER_PATH` и `STATE_ROOT`: PA0 — один явно указанный стабильный helper, а `MP3TOM4B_SUPPORT_DIR` — только scratch-state. В T0 печатать и проверять `realpath(PA0)`, SHA и loaded PA0 перед каждым kickstart.

[BLOCKER] «PA0-only ремонт за 1–2 секунды» может уйти в сеть до показа UI

Что сломается: приложение зависнет на старте на десятки секунд или минуты, ровно чего DEVIATION-2 пытался избежать.

При каком сценарии: PA0 старый, файлы совпадают, но сеть медленная; либо venv сломан/удалён.

Доказательство/рассуждение: план объявляет синхронный путь дешёвым ([plan:100](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:100>)). Реальный installer перед проверкой `import PIL` всегда делает best-effort `pip install --upgrade pip` без timeout ([installer.sh:224](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:224>)); при отсутствующем venv создаёт его и может устанавливать Pillow ([installer.sh:215](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:215>), [installer.sh:231](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:231>)). Совпадение helper/runner/agent не доказывает здоровье venv.

Конкретная правка плана: отдельный `installer.sh --repair-launchd-only`, строго offline: проверить уже уложенные файлы и golden SHA helper’а, перепечь plist, reload/verify job. Никаких ffmpeg-поисков, venv или pip. Полный installer оставить асинхронным.

[BLOCKER] Проверка plist на диске не доказывает, что launchd запустил новый PA0

Что сломается: UI скопирует правильный helper-путь, пользователь выдаст ему FDA, но реально продолжит работать загруженный старый job с `/bin/bash`. Следующий запуск приложения также может посчитать всё исправленным.

При каком сценарии: installer сделал атомарный `mv` нового plist и умер до `bootout/bootstrap`; либо bootstrap завершился ошибкой.

Доказательство/рассуждение: plist публикуется в [installer.sh:309](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:309>), а launchd перегружается только позже ([installer.sh:320](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:320>)). Инвариант плана смотрит лишь фактический PA0 из файла ([plan:109](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:109>)). Риск объявлен закрытым «следующим запуском», но disk PA0 уже правильный, поэтому PA0-self-heal не сработает ([plan:390](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:390>)).

Конкретная правка плана: добавить install generation. Installer пишет UUID в plist environment, bootstrap’ит job, проверяет `launchctl print`, а новый агент переносит UUID в `state.json`. FolderAccess разрешён только при `disk PA0 == helper && state.install_generation == expected`. Receipt успешной установки писать последним. Live-кнопка должна fail-closed отказывать, если поколение или PA0 не совпадают.

[BLOCKER] Installer не транзакционен и не защищён от двух одновременных запусков

Что сломается: частичный Python-package, потерянный job, неверная watch-папка или два конкурирующих `bootout/bootstrap`.

При каком сценарии: пользователь одновременно нажал «Сменить папку» и «Обновить агент»; открыты две копии приложения; installer запущен из Terminal во время автообновления; приложение/система убиты посреди pip.

Доказательство/рассуждение: текущий installer сначала удаляет весь `bin/agent`, затем копирует файлы по одному ([installer.sh:197](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:197>)), потом занимается venv ([installer.sh:208](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:208>)), и лишь затем plist/launchd. В Settings две независимые фазы намеренно допускают два installer-процесса ([main.swift:401](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:401>), [main.swift:416](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:416>)). План называет параллельные installer’ы «идемпотентными» ([plan:395](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:395>)); `rm -rf` плюс два bootstrap’а идемпотентными не являются.

Конкретная правка плана: обязательный межпроцессный lock в installer, плюс общий single-flight в Swift. Все долгие preflight/pip выполнить до боевых файлов. Под lock: defer при активной сборке → bootout → stage/validate → заменить package целиком → atomically publish plist → bootstrap/verify → receipt. На ошибке восстановить предыдущий plist/job.

[MAJOR] `currentWatchDir()` может откатить новую настройку к старой

Что сломается: после сбоя ремонта приложение перепечёт plist обратно на старую watch-папку.

При каком сценарии: plist уже содержит новую папку, но новый агент ещё не успел записать state; старый `state.json` содержит прежний путь.

Доказательство/рассуждение: resolver предпочитает state plist’у ([main.swift:4036](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:4036>), [main.swift:4040](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:4040>)). План требует сохранить эту последовательность ([plan:115](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:115>)). State — наблюдение последнего запуска, plist — текущая конфигурация; в окне обновления они расходятся.

Конкретная правка плана: источником истины сделать receipt последней успешно загруженной generation. При его отсутствии — валидный plist раньше state. Никогда не ремонтировать конфигурацию из state другого поколения.

[BLOCKER] src↔dst SHA-проверка не ловит испорченный source

Что сломается: испорченный или переподписанный helper будет успешно установлен и уничтожит грант.

При каком сценарии: helper изменился внутри `.app` после build guard; installer сравнил его с установленным файлом, скопировал и затем увидел равные SHA.

Доказательство/рассуждение: план обещает fail-closed `sha src↔dst` ([plan:219](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:219>)), а тест требует, чтобы порча source привела к отказу ([plan:339](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:339>)). Но после копирования corrupted source и corrupted destination равны. Проверка доказывает качество копирования, не identity.

Конкретная правка плана: embedded immutable `EXPECTED_HELPER_SHA256`, взятый из frozen provenance. Проверять `src == expected` до любых записей и `dst == expected` после установки. Тестировать три случая независимо: corrupted source, corrupted destination, оба одинаково corrupted.

[MAJOR] Байт-страж стоит слишком рано для нашего build pipeline

Что сломается: helper пройдёт guard в staging, а затем изменится при `ditto`, DMG-сборке или будущем sign/notarize/staple-шаге.

При каком сценарии: guard вставлен «после codesign» по донору, но наш build после этого копирует весь bundle в `build/dist`.

Доказательство/рассуждение: план требует guard после codesign ([plan:254](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:254>)); наш pipeline затем делает `ditto` ([build-app.sh:266](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/build/build-app.sh:266>), [build-app.sh:277](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/build/build-app.sh:277>)). DMG scripts проверяют образ/подпись, но не helper внутри финального образа ([make-dmg.sh:103](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/build/make-dmg.sh:103>), [build-dmg.sh:74](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/build/build-dmg.sh:74>)).

Конкретная правка плана: golden SHA проверять на четырёх границах: repo → signed staging `.app` → `build/dist/*.app` после ditto → `.app`, извлечённый из смонтированного финального DMG. Повторять после любых будущих notarize/staple/update-процессов. Добавить `.gitattributes` с `binary -filter` для helper’а и CI-гейт «SHA изменился относительно прошлого релиза ⇒ обязательный `requires_fda_regrant=true`».

[MAJOR] Сигнал helper’а не доходит гарантированно до ffmpeg

Что сломается: после bootout/logout/update останется orphan ffmpeg, пишущий удалённый temp-файл; новый агент одновременно начнёт recovery.

При каком сценарии: helper получает SIGTERM во время многоминутной сборки. Он сигналит только PID Python; Python умирает стандартным обработчиком, а ffmpeg — его отдельный ребёнок.

Доказательство/рассуждение: донорский helper делает `kill(child, sig)` только непосредственному ребёнку ([donor C:63](</Users/arrivarus/Documents/VibeCoding2/2026.06 fb2-to-epub/packaging/agent-src/fb2-to-epub-agent.c:63>)). У нас bash заменён Python ([runner.sh:55](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/bin/runner.sh:55>)); `agent/__main__.py` не ставит signal handlers ([__main__.py:34](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/__main__.py:34>)), а ffmpeg запускается через `Popen`. Installer делает bootout при каждом обновлении ([installer.sh:328](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:328>)).

Конкретная правка плана: до первой заморозки либо создать отдельную process group в helper и форвардить сигнал всей группе, либо добавить Python signal-handler, который выставляет cancel, завершает и reap’ит все live ffmpeg, затем чистит temp. Обязательный тест: bootout посреди encode → нет потомков/ffmpeg через 5 секунд.

[MAJOR] Recheck и StartInterval не работают в обещанный срок во время сборки

Что сломается: пользователь выдаст FDA, нажмёт «Проверить снова» и получит timeout, хотя всё настроено правильно.

При каком сценарии: label уже занят длинной ffmpeg-сборкой; `kickstart` без `-k` не запускает второй процесс, а interval fire пропускается.

Доказательство/рассуждение: план обещает recheck за ~10 секунд и «добивочный probe ≤60 с» ([plan:171](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:171>)), но сам же признаёт, что тики пропускаются, пока job жив ([plan:186](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:186>)). Это официальный контракт: firing во время работающего job пропускается ([launchd.plist.5:441](/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk/usr/share/man/man5/launchd.plist.5:441)). Во сне StartInterval также пропускается без накопления ([launchd.plist.5:443](/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk/usr/share/man/man5/launchd.plist.5:443)).

Конкретная правка плана: recheck должен различать `job busy` и `probe failed`: показать «проверим после текущей сборки», не timeout. Либо текущий долгоживущий Python должен обслуживать recheck-команду сам. Не использовать `-k`: это правильно, сборку убивать нельзя.

[MAJOR] Цена минутного polling сильно занижена

Что сломается: постоянные disk wakeups, UI-refresh раз в минуту, лишние процессы и FileProvider-работа на больших библиотеках.

При каком сценарии: ноутбук работает на батарее, watch содержит сотни папок/треков либо iCloud dataless-файлы.

Доказательство/рассуждение: это не только `os.listdir`. Каждый новый Python-процесс запускает `ffmpeg -version`, потому что cache process-local ([scan.py:97](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:97>), [scan.py:118](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:118>)); полный scan перебирает дерево ([scan.py:947](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:947>)); state всегда меняется из-за `ts` ([scan.py:1050](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:1050>)); presence/notified также переписываются ([scan.py:1258](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:1258>), [scan.py:1463](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:1463>)); `agent_started` делает fsync ([state.py:79](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/state.py:79>)). Все эти файлы лежат в отслеживаемом приложением `state/`, поэтому поднимают Swift refresh ([config.py:42](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/config.py:42>), [main.swift:4151](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:4151>)).

Конкретная правка плана: до принятия 60 секунд — измерения на 0/100/1000 файлов через `powermetrics`, wall/CPU/I/O и wakeups. Не писать state/presence/notified при семантически неизменном результате; heartbeat-журналировать редко. Cache версии ffmpeg вынести в installed metadata. Рассмотреть 300 секунд как safety reconciliation.

[MAJOR] Ротация events.jsonl ломает собственных читателей и gate-инварианты

Что сломается: `read_events()` увидит `build_started`, но потеряет предшествующий `confirm_accepted`, оказавшийся в `.1`, и выдаст ложное нарушение.

При каком сценарии: ротация происходит между двумя логически связанными событиями; читатель открывает журнал одновременно с rename.

Доказательство/рассуждение: план сохраняет только один backup ([plan:191](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:191>)), но текущий reader читает исключительно `events.jsonl` ([state.py:110](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/state.py:110>)). Журнал прямо назван gate-test source ([state.py:82](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/state.py:82>)). Для `StandardOutPath` внутренняя ротация дополнительно сложна: launchd открывает stdout до старта Python, поэтому rename не перенаправит текущий fd.

Конкретная правка плана: `read_events()` читает `.1`, затем current как одну последовательность и терпит смену inode. Ротацию сериализовать. Для stdout либо полностью убрать idle-вывод и принять редкий error-log, либо отказаться от launchd StandardOutPath в пользу собственного rotating logger; не обещать «тот же size-cap» без fd-дизайна.

[MAJOR] Denied/missing может превратить всю библиотеку в «повторно добавленные книги»

Что сломается: после возврата доступа все ранее готовые книги могут снова перейти в pending-confirm и поднять приложение.

При каком сценарии: TCC deny или временный iCloud/FileProvider error даёт пустой scan; `_reconcile_presence` помечает все книги absent; следующий успешный scan видит absent→present и re-arm’ит done-книги.

Доказательство/рассуждение: план говорит лишь «заморозить books/pending_groups» ([plan:141](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:141>)), но текущий `run_scan` вызывает presence reconcile до построения state ([scan.py:1497](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:1497>)). Reconcile помечает отсутствующими все unseen IDs ([scan.py:1247](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:1247>)) и re-arm’ит вернувшийся `done` ([scan.py:1231](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/agent/scan.py:1231>)).

Конкретная правка плана: `denied` — ранний выход до scan и до `_reconcile_presence`; не трогать presence/notified ledgers. `missing`/прочие OSError считать transient несколько последовательных тиков до разрушительных reconciliation-изменений. Добавить тест «done → denied/missing → ok не re-arm’ит книгу».

[MAJOR] Причина мёртвого WatchPaths не установлена

Что сломается: release навсегда получит polling-нагрузку, хотя реальный дефект мог быть в пути, FileProvider-представлении или моменте bootstrap.

При каком сценарии: Desktop перенесён iCloud, watched path — symlink/redirect, каталог материализуется после загрузки job либо vnode заменяется FileProvider’ом.

Доказательство/рассуждение: диагноз в плане ограничен «WatchPaths инертен» ([plan:176](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:176>)). Apple прямо предупреждает, что WatchPaths race-prone и может пропускать изменения ([launchd.plist.5:426](/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk/usr/share/man/man5/launchd.plist.5:426)). `DispatchSource` поверх того же vnode/kqueue-класса не является автоматически лучшим исправлением.

Конкретная правка плана: сначала зафиксировать canonical/real path, inode/dev до и после login/iCloud hydration и сравнить WatchPaths на локальной и iCloud-папке. StartInterval допустим как safety reconciliation. Долгоживущий FSEvents-agent — средняя/высокая цена: event loop, initial scan, coalescing, command queue, sleep/wake, shutdown/update и периодическая полная сверка. Это отдельный milestone, не дешёвая замена одной plist-строки.

[MAJOR] Предложенный rollback возвращает именно исходный Tahoe-баг

Что сломается: rollback «успешно» укажет PA0 на `runner.sh`, но защищённая Desktop-папка снова станет недоступной.

При каком сценарии: разработчик выполняет предложенный plutil-repoint после проблем релиза 1.0.

Доказательство/рассуждение: корень плана — shebang PA0 превращается в `/bin/bash` и грант скрипту мёртв ([plan:12](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:12>)). Тем не менее rollback предлагается ровно на runner.sh ([plan:285](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:285>)).

Конкретная правка плана: rollback никогда не меняет PA0. Остаётся frozen helper, а откатываются только mutable `runner.sh` и agent package. Для v0.9 downgrade объявить unsupported на Tahoe и дать отдельный rollback-пакет, сохраняющий helper.

[MAJOR] Downgrade ошибочно считается «апдейтом» и снова поставит старый PA0

Что сломается: установка v0.9 поверх v1.0 перетрёт agent старой версией и перепечёт plist на `runner.sh`, после чего в v0.9 нет новой карточки и StartInterval.

При каком сценарии: пользователь скачал старый DMG для отката.

Доказательство/рассуждение: `AgentUpdate.freshness` не сравнивает версии; любое неравенство fingerprints означает `.outdated` ([SetupView.swift:263](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/SetupView.swift:263>)). Старый installer всегда пишет PA0=`runner.sh` ([installer.sh:270](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/packaging/installer.sh:270>)). Расширение fingerprint на helper/runner не решает направление версии.

Конкретная правка плана: installed receipt с schema/engine version; автоустановка разрешена только `bundled >= installed`, а неизвестный downgrade требует явного подтверждения. После installer проверять convergence всех частей и runtime generation; mismatch после success — terminal failure, а не новый бесконечный auto-run. Для уже выпущенной v0.9 защита невозможна — release notes обязаны прямо запретить downgrade.

[MAJOR] Миграция без запуска приложения не существует, а failed-screen ведёт в тупик

Что сломается: пользователь обновит/скачает приложение, но не откроет его — старый агент останется мёртвым неделями. Если watch-dir неизвестен, приложение скажет «через Настройки», но попасть туда нельзя.

При каком сценарии: старая v0.9 + инертный WatchPaths; либо plist/state повреждены и `currentWatchDir()==nil`.

Доказательство/рассуждение: план сам описывает недели старого PA0 и объявляет это «поведенчески закрытым» ([plan:390](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:390>)); никакого updater’а приложения в проекте нет ([main.swift:1119](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:1119>)). При неизвестной папке план отправляет в Settings ([plan:118](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:118>)), но `AgentUpdatingView.failed` показывает только «Повторить» ([main.swift:1364](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:1364>), [main.swift:1373](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/app/main.swift:1373>)).

Конкретная правка плана: не считать миграцию до первого запуска закрытой; release notes: «после обновления обязательно один раз открыть приложение». Failed-screen должен иметь выбор папки/переход в Setup/Settings и диагностический вывод фактических plist/receipt путей.

[MINOR] `PermissionError` не означает «исправь FDA»

Что сломается: при chmod/ACL пользователь будет бесконечно переключать FDA, хотя это не меняет Unix-права.

При каком сценарии: папка или один из родителей имеет restrictive ACL/mode; FileProvider возвращает иной access error.

Доказательство/рассуждение: план осознанно сливает EPERM/EACCES ([plan:128](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:128>)), но A7 утверждает, что remedy тот же ([plan:441](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:441>)). Это неверно для chmod/ACL.

Конкретная правка плана: хранить status=`denied`, но текст — «нет доступа», не «FDA запрещён». FDA предлагать как основной шаг только для защищённой зоны; рядом дать проверку Finder permissions/ACL. Chmod-тест доказывает ветку UI, но не TCC-механику.

[MINOR] SHA-256 достаточен только как сравнение с независимым golden

Что сломается: команда будет добавлять cdhash-проверки, но оставит главную дыру — сравнение двух одинаково неправильных файлов.

При каком сценарии: source и destination уже содержат один изменённый helper.

Доказательство/рассуждение: точное совпадение полного raw SHA с frozen artifact сильнее проверки одного cdhash: тот же файл означает ту же подпись, DR, Team ID и оба slice. Quarantine/resource-fork xattrs не меняют raw SHA/cdhash, хотя могут ломать Gatekeeper/strict codesign. App Translocation не опасна, потому что FDA-цель устанавливается в App Support, а не исполняется из translocated `.app`. Опасны смена абсолютного App Support path, symlink/realpath и переезд home; A6 считает путь безусловно стабильным ([plan:439](</Users/arrivarus/Documents/VibeCoding2/2026.06 mp3-to-m4b/arch/plan-binrunner-mp3-claude.md:439>)).

Конкретная правка плана: SHA — основной identity gate против golden; дополнительно release-проверка `codesign --verify --strict`, DR и cdhash обоих `--arch`. Installer запрещает symlink helper/BIN_DIR и сверяет ожидаемый path с `realpath`. Смена home/path — документированный re-grant. Тот же SHA при иной DR/team практически невозможен без SHA-256 collision.

## (а) ВЕРДИКТ ПО A1

**НЕИЗВЕСТНО; механистически вероятно верно.** `exec` сохраняет PID/process responsibility, helper остаётся живым, поэтому ожидаемый результат — helper остаётся responsible. Но именно TCC/FDA policy macOS 26 для цепочки `helper → bash exec→venv-python` донором не проверена.

Точный T0:

1. Создать отдельный стабильный каталог, не `/tmp`, например:

```bash
T0="$HOME/Library/Application Support/mp3-to-m4b-t0"
BIN="$T0/bin"
LABEL="com.arrivarus.mp3tom4b.t0exec"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WATCH="$HOME/Desktop/mp3tom4b-t0-probe"
mkdir -p "$BIN" "$WATCH"
printf 'marker\n' > "$WATCH/marker.txt"
install -m 0755 packaging/mp3-to-m4b-agent "$BIN/mp3-to-m4b-agent"
python3 -m venv "$T0/venv"
```

2. T0-runner должен записать PID bash, а затем сделать именно exec:

```bash
echo "$$" > "$MP3TOM4B_T0_DIR/bash.pid"
exec "$PYTHON3" "$HERE/t0_probe.py"
```

`probe.py` до `os.listdir()` пишет `os.getpid()`, `os.getppid()`, `sys.executable`; затем реально проверяет наличие `marker.txt`. Bash PID и Python PID обязаны совпасть — это доказывает, что проверен именно exec.

3. Plist: `ProgramArguments[0]="$BIN/mp3-to-m4b-agent"`, один элемент; env содержит `PYTHON3="$T0/venv/bin/python3"`, `WATCH`, `MP3TOM4B_T0_DIR`.

4. Перед запуском:

```bash
DOMAIN="gui/$(id -u)"
START="$(date '+%Y-%m-%d %H:%M:%S')"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl print "$DOMAIN/$LABEL" |
  sed -n '/program =/p;/arguments = {/,/}/p'

launchctl kickstart "$DOMAIN/$LABEL"
```

5. После появления `python.pid`:

```bash
PY_PID="$(cat "$T0/python.pid")"
BASH_PID="$(cat "$T0/bash.pid")"
test "$PY_PID" = "$BASH_PID" || echo "NOT EXEC: pid changed"

log show --start "$START" --info --debug --style compact \
  --predicate 'process == "tccd" AND subsystem == "com.apple.TCC"' |
  grep -E "AUTHREQ_(ATTRIBUTION|SUBJECT|RESULT)|Handling access request|Sub:|Resp:|responsible_path|binary_path|pid=${PY_PID}|mp3-to-m4b-agent|/bin/bash|python"
```

Принимать GREEN только если для одного msgID:

- `accessing.pid == PY_PID`;
- `accessing.binary_path` — venv/base Python;
- `responsible_path` и `AUTHREQ_SUBJECT`/`Sub` — `$BIN/mp3-to-m4b-agent`;
- `/bin/bash` и Python не являются subject/responsible;
- без гранта функциональный probe denied;
- после выдачи FDA именно `$BIN/mp3-to-m4b-agent` probe реально видит `marker.txt`;
- negative PA0-control с `ProgramArguments[0]=runner.sh` показывает subject `/bin/bash` и deny.

`csops/codesign` доказывает cdhash/image, но не responsible-chain. `launchctl print` доказывает loaded PA0, но не TCC subject. Решающий источник — коррелированные `AUTHREQ_ATTRIBUTION` (`accessing`, `responsible`), `AUTHREQ_SUBJECT`/`Sub`/`Resp` и `AUTHREQ_RESULT`.

Если A1 ложно:

- Предпочтительный дешёвый fallback: runner запускает Python фоном и `wait`’ит его, с traps, форвардящими TERM/INT/HUP. Bash остаётся жив — форма донора. Цена: около 20 строк shell + signal/e2e тест; helper не меняется.
- Более чистый fallback до заморозки: helper читает фиксированный `PYTHON3` из env и `posix_spawn`’ит `python -m agent` напрямую, затем wait. Цена: небольшой C-код и 1–2 дня тестов; замораживается контракт env/ошибок, но не venv resolver.

## (б) ТРИ ГЛАВНЫЕ ПРАВКИ, без которых не начинать

1. Exact-exec T0 с PID/msgID-корреляцией; A1 до него имеет статус «неизвестно».

2. Транзакционный installer: offline `repair-launchd-only`, межпроцессный lock, generation/receipt, проверка реально загруженного job и общий Swift single-flight.

3. Независимый golden SHA и end-to-end gate до содержимого финального DMG; src↔dst SHA оставить только как проверку копирования.

## (в) ЧТО В ПЛАНЕ ХОРОШО и трогать не надо

- Донорский helper `spawn+wait`, `_NSGetExecutablePath+realpath`, immutable artifact и отказ от exec в самом PA0.
- Фактическое действие и tccd-log важнее System Settings; negative control обязателен.
- Агентный, а не app-side access probe; атомарная запись state; заморозка витрины при deny.
- Атомарный temp→mv для plist: `plutil` увидит старый или новый файл, не половину.
- Отказ от default watch-dir при неизвестной конфигурации — правильный; исправить надо источник/поколение, не сам fail-closed принцип.
- `kickstart` recheck без `-k` — правильно: пользовательскую сборку нельзя убивать.
- Разные имена `mp3-to-m4b-agent` и `fb2-to-epub-agent`, точный путь в буфере и функциональная повторная проверка.
- Утверждение «launchd не запустит второй экземпляр того же job, а interval во время работы пропустит» верно; проблема только в обещанном recheck-timeout и стоимости polling.

