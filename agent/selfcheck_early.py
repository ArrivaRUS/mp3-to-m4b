"""§early self-check — «ранний нудж» (D17): инварианты I1–I9 синтез-плана.

Запуск отдельно::

    python3 -m agent.selfcheck_early

Что защищает эта сьюта. D17 разрезал публикацию книги на фазы
``skeleton → chapters → ready → done`` и увёл веб-поиск обложек с критического
пути, чтобы окно поднималось за ~0.8 с вместо ~12 с. Обмен опасный: окно теперь
открывается на манифесте, который ещё дозаполняется, а сборка по такому манифесту
дала бы человеку обрезанную аудиокнигу. Сьюта гоняет ПРОДАКШН-путь
(``scan.run_scan`` / ``dispatcher.drain_commands`` / ``agent.__main__.main``) на
настоящем временном дереве с настоящими (крошечными, сгенерированными ffmpeg)
mp3 и проверяет девять инвариантов из ``arch/early-nudge-synthesis.md`` §3:

  I1  сборка по НЕПОЛНОМУ манифесту невозможна. Главный кейс — TOCTOU: команда,
      рождённая по скелету, отвергается И ПОСЛЕ того, как тот же ``source_rev``
      дозаполнился до ``ready`` с тем же ``confirm_token``. Плюс fail-closed:
      токена нет / не совпал / главы структурно пусты.
  I2  один нудж на публикацию: ключ леджера у скелета и у ready СОВПАДАЕТ, две
      публикации одной книги дают ровно один подъём окна.
  I3  окно не открывается на копирующейся книге: дебаунс стоит ПЕРЕД скелетом, и
      файл, доехавший ПОСЛЕ составления списка, ловится (M-E).
  I4  скелет не замерзает: halt после любой незавершённой фазы → следующий тик
      достраивает, сохранив ``confirm_token``.
  I5  пропущенная книга не воскресает: ни готовая, ни скелет.
  I6  сетевой сбой ≠ проблема доступа: мёртвая сеть не рисует карточку «нет
      доступа к папке». С НЕГАТИВНЫМ КОНТРОЛЕМ — тот же тик с веб-ногой,
      возвращённой внутрь ``_finish_manifest``, обязан упасть в ``exit 75`` +
      ``folder_access='blocked'``.
  I7  порядок публикации: манифест → ``state.json`` → леджер → нудж.
  I8  старые манифесты читаются: отсутствие ``phase`` = ``done``, до-D17 манифест
      поднимается на месте, без нуджа и без ре-арма.
  I9  лента обложек append-only: список до прихода веба — ПРЕФИКС списка после;
      поздний воркер прошлого поколения отбрасывается.

Плюс §0 — сам критический путь, СТРУКТУРНО, а не секундомером: скелет пишется и
нудж уходит ДО первого ffprobe, а веб-нога не трогается ни разу до ``ready``.
Замеры M-A/M-B (0.52 с до нуджа; 1.735 с против 1.757 с на живой и мёртвой сети —
разница 0.022 с) держатся именно этими двумя свойствами; секундомер на загруженной
машине флакует, а порядок вызовов — нет.

Сеть НЕ трогается: ``cover.search_web`` подменён стендом с самого старта, поэтому
ни один кейс не может уйти в интернет. Реальное приложение НЕ поднимается: нудж
идёт через ``MP3TOM4B_NUDGE_CMD`` в скрипт-регистратор.

Сьюта прогоняет ТОЛЬКО свои проверки (кросс-сьютовая регрессия оркеструется один
раз в ``agent.selfcheck_all`` — вложенных перезапусков нет). Нужны ffmpeg/ffprobe
(настоящие mp3) и Pillow (генерация обложек); без них — SKIP с ненулевым кодом.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# --- крошечный harness проверок ---------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


# --- инструменты -------------------------------------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_tools() -> bool:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return False
    try:
        import PIL  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _make_mp3(path: Path, *, seconds: float = 0.4, tags: dict | None = None) -> None:
    """Настоящий (без обложки) mp3 из синуса, с опциональными ID3-тегами."""
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "libmp3lame", "-id3v2_version", "3",
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


def _jpeg_bytes() -> bytes:
    """Байты настоящего маленького JPEG — «скачанная» стендом обложка."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 64), (40, 44, 52)).save(buf, "JPEG")
    return buf.getvalue()


# --- команды (ровно те формы, что пишет приложение) --------------------------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


_MISSING = object()


def _confirm_cmd(manifest: dict, *, build_token: object = _MISSING) -> dict:
    """Команда «Собрать», которую приложение чеканит ПО ЭТОМУ манифесту.

    ``build_token`` по умолчанию берётся из манифеста — то есть у скелета его нет
    вовсе, ровно как у настоящего приложения, которое эхом возвращает поле,
    которого оно не видело. Явное значение подменяет его (подделка / null).
    """
    bid = manifest["book_id"]
    rev = manifest["source_rev"]
    cmd = {
        "cmd_id": str(uuid.uuid4()),
        "action": "confirm-build",
        "book_id": bid,
        "source_rev": rev,
        "confirm_token": manifest.get("confirm_token"),
        "idempotency_key": f"{bid}:{rev[:16]}",
        "params": dict(manifest.get("params", {})),
        "ts": time.time(),
    }
    token = manifest.get("build_token") if build_token is _MISSING else build_token
    if token is not None:
        cmd["build_token"] = token
    return cmd


def _skip_cmd(book_id: str) -> dict:
    return {
        "cmd_id": str(uuid.uuid4()), "action": "skip", "book_id": book_id,
        "idempotency_key": f"skip:{book_id}", "ts": time.time(),
    }


# --- наблюдение ---------------------------------------------------------------


def _nudge_count(log: Path) -> int:
    try:
        return len(log.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return 0


def _events_of(state, kind: str) -> list[dict]:
    return [e for e in state.read_events() if e.get("event") == kind]


def _manifest_for(config, state, suffix: str) -> dict | None:
    for p in sorted(config.books_dir().glob("*.json")):
        m = state.read_json(p, default=None)
        if isinstance(m, dict) and str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


def _ledger_keys(config, state) -> set:
    data = state.read_json(config.notified_file(), default=None)
    keys = data.get("keys") if isinstance(data, dict) else None
    return {k for k in keys if isinstance(k, str)} if isinstance(keys, list) else set()


def _is_hex32(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 32
            and all(c in "0123456789abcdef" for c in value))


# --- стенд веб-поиска обложек (сеть НИКОГДА не трогается) --------------------


class _WebStub:
    """Подмена :func:`agent.cover.search_web`: файлы на диск, ноль сети.

    Живёт весь прогон, поэтому ни одна проверка не может случайно уйти в
    интернет. Три ручки: ``calls`` (счётчик — структурная замена секундомеру),
    ``stall_s`` (сколько «висеть» — чтобы регрессия, вернувшая веб на критический
    путь, краснела И по времени) и ``on_call`` (хук, которым I9 подменяет книгу
    прямо «во время поиска»).
    """

    def __init__(self, jpeg: bytes) -> None:
        self.jpeg = jpeg
        self.calls = 0
        self.stall_s = 0.0
        self.results = 2
        self.on_call = None

    def __call__(self, author, title, out_dir, book_id, *, exclude=None,
                 generation=None, start_index=0):
        self.calls += 1
        if self.stall_s:
            time.sleep(self.stall_s)
        if self.on_call is not None:
            self.on_call()
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"{book_id}-{generation}" if generation else str(book_id)
        paths = []
        for i in range(self.results):
            p = out / f"{stem}-web-{int(start_index) + i}.jpg"
            p.write_bytes(self.jpeg)
            paths.append(p)
        return paths


# --- I6: дочерний процесс (сторож уходит через os._exit, нужен свой процесс) --

#: Сколько «висит» каждый сетевой запрос в дочернем процессе I6 (мёртвая сеть).
_I6_STALL_S = 3.0
#: Дедлайн фазы для обоих детей I6. Локальный скан 2-файловой книги — доли
#: секунды, так что охраняемый прогон имеет ~10× запаса и не флакует; неохраняемый
#: платит 2 × _I6_STALL_S = 6 с веба ВНУТРИ фазы и не может случайно уложиться.
_I6_DEADLINE_S = 5.0


def _i6_child(mode: str) -> int:
    """Один тик агента при МЁРТВОЙ сети. ``mode``: ``guarded`` | ``unguarded``.

    ``guarded`` — сегодняшний порядок (``scan → drain → enrich``): веб живёт после
    дренажа и вне фазового сторожа. ``unguarded`` — НЕГАТИВНЫЙ КОНТРОЛЬ: веб-нога
    возвращена внутрь ``_finish_manifest``, то есть ровно туда, где она была до
    M-B. Второй обязан упасть в ``exit 75`` и опубликовать ``folder_access =
    blocked`` — ложную карточку доступа к папке при исправной папке.
    """
    import socket
    import urllib.request

    def _hang(*_a, **_k):
        time.sleep(_I6_STALL_S)          # таймаут сокета, целиком
        raise socket.timeout("timed out")

    urllib.request.urlopen = _hang       # type: ignore[assignment]

    from agent import __main__ as agent_main
    from agent import cover, scan

    if mode == "unguarded":
        real_finish = scan._finish_manifest

        def _finish_with_web_inline(plan):
            """До-M-B форма: веб внутри дозаполнения, под фазовым сторожем."""
            manifest = real_finish(plan)
            cover.web_options_for(manifest)
            return manifest

        scan._finish_manifest = _finish_with_web_inline

    t0 = time.monotonic()
    rc = agent_main.main([])
    print(f"i6-child[{mode}] rc={rc} in {time.monotonic() - t0:.2f}s", flush=True)
    return rc


def _i6_run(root: Path, repo_root: Path, mode: str) -> dict:
    """Прогнать дочерний тик I6 и вернуть, что он оставил на диске."""
    home = root / f"i6-{mode}"
    support, watch = home / "support", home / "watch"
    for sub in ("support", "watch", "LaunchAgents", "tmp"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    book = watch / "Сеть - Книга"
    for i in (1, 2):
        _make_mp3(book / f"{i:02d}.mp3", tags={"album": "Книга", "title": f"Глава {i}"})

    env = dict(os.environ)
    env.update({
        "MP3TOM4B_SUPPORT_DIR": str(support),
        "MP3TOM4B_WATCH_DIR": str(watch),
        "MP3TOM4B_LAUNCHAGENTS_DIR": str(home / "LaunchAgents"),
        "MP3TOM4B_LABEL": f"com.arrivarus.mp3tom4b.selfcheck-early-{os.getpid()}",
        "TMPDIR": str(home / "tmp"),
        "MP3TOM4B_COVER_WEB": "1",                     # веб-нога включена
        "MP3TOM4B_STABILITY_DEBOUNCE_S": "0",
        "MP3TOM4B_PHASE_DEADLINE_S": str(_I6_DEADLINE_S),
    })
    # Без MP3TOM4B_NUDGE_CMD и с MP3TOM4B_SUPPORT_DIR нудж молчит сам (scan._nudge_command).
    env.pop("MP3TOM4B_NUDGE_CMD", None)

    proc = subprocess.run(
        [sys.executable, "-m", "agent.selfcheck_early", "--i6-child", mode],
        cwd=str(repo_root), capture_output=True, text=True, env=env,
    )
    try:
        st = json.loads((support / "state" / "state.json").read_text("utf-8"))
        access = (st.get("agent") or {}).get("folder_access")
    except Exception as exc:  # noqa: BLE001
        access = f"<unreadable: {exc!r}>"
    try:
        kinds = [json.loads(ln).get("event") for ln in
                 (support / "state" / "events.jsonl").read_text("utf-8").splitlines()]
    except Exception:  # noqa: BLE001
        kinds = []
    manifest: dict = {}
    try:
        for p in (support / "queue" / "books").glob("*.json"):
            manifest = json.loads(p.read_text("utf-8"))
            break
    except Exception:  # noqa: BLE001
        pass
    return {"rc": proc.returncode, "access": access, "events": kinds,
            "manifest": manifest, "stdout": proc.stdout, "stderr": proc.stderr}


# ═════════════════════════════════════════════════════════════════════════════


def run() -> int:  # noqa: C901 - один линейный сценарий, намеренно плоский
    if not _has_tools():
        print("§early self-check: SKIPPED — нет ffmpeg/ffprobe или Pillow")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-early-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)

    # Регистратор: каждый «подъём приложения» — строка в файле; настоящее
    # приложение НЕ открывается ни разу.
    nudge_log = root / "nudges.log"
    recorder = root / "recorder.sh"
    recorder.write_text(
        f"#!/bin/sh\nprintf 'nudge\\n' >> {shlex.quote(str(nudge_log))}\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)

    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    os.environ["MP3TOM4B_LAUNCHAGENTS_DIR"] = str(root / "LaunchAgents")
    os.environ.setdefault(
        "MP3TOM4B_LABEL", f"com.arrivarus.mp3tom4b.selfcheck-early-{os.getpid()}")
    # Веб-нога ВКЛЮЧЕНА: иначе «веб не трогается до ready» доказывал бы всего лишь
    # выключенный флаг. Сеть при этом недостижима — search_web подменён стендом.
    os.environ["MP3TOM4B_COVER_WEB"] = "1"
    os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"
    os.environ["MP3TOM4B_NUDGE_CMD"] = shlex.quote(str(recorder))
    os.environ.pop("MP3TOM4B_HALT_AFTER_PHASE", None)

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import config, cover, dispatcher, scan, state  # noqa: E402

    web = _WebStub(_jpeg_bytes())
    cover.search_web = web              # ни одна проверка не уйдёт в сеть

    print(f"self-check tree: {root}\n  support: {support}\n  watch:   {watch}\n")

    # ═════ §0. КРИТИЧЕСКИЙ ПУТЬ — структурно, а не секундомером ═══════════════
    print("§0 · критический путь: скелет и нудж ДО ffprobe, веб — вообще не тут")
    big = watch / "Толстой - Война и мир"
    for i in range(1, 13):
        _make_mp3(big / f"{i:02d} - Глава {i}.mp3",
                  tags={"album": "Война и мир", "album_artist": "Лев Толстой",
                        "title": f"Глава {i}", "track": str(i)})

    trace: list[str] = []
    seen_skeleton: dict = {}
    real_probe = scan._probe_book
    real_nudge = scan._nudge_app
    real_atomic = state.write_json_atomic

    def _traced_probe(mp3s):
        trace.append("probe")
        return real_probe(mp3s)

    def _traced_nudge(keys):
        trace.append("nudge")
        return real_nudge(keys)

    def _traced_atomic(path, data):
        if isinstance(data, dict) and data.get("book_id") and data.get("chapters") is not None:
            phase = data.get("phase") or "legacy"
            trace.append(f"manifest:{phase}")
            if phase == "skeleton" and not seen_skeleton:
                seen_skeleton.update(json.loads(json.dumps(data)))
        return real_atomic(path, data)

    # Если регрессия вернёт веб на критический путь — стенд ЗАВИСНЕТ на 5 с, и
    # проверка времени покраснеет отдельно от проверки счётчика вызовов.
    web.stall_s = 5.0
    scan._probe_book = _traced_probe
    scan._nudge_app = _traced_nudge
    state.write_json_atomic = _traced_atomic
    try:
        t0 = time.monotonic()
        scan.run_scan()
        t_ready = time.monotonic() - t0
    finally:
        scan._probe_book = real_probe
        scan._nudge_app = real_nudge
        state.write_json_atomic = real_atomic
        web.stall_s = 0.0

    def _idx(token: str) -> int:
        return trace.index(token) if token in trace else 10**6

    man_big = _manifest_for(config, state, "Война и мир")
    check("§0 скелет попадает на диск ПЕРВЫМ манифестным письмом",
          trace and trace[0] == "manifest:skeleton", " → ".join(trace))
    check("§0 нудж уходит ДО первого ffprobe (окно раньше разведки)",
          _idx("nudge") < _idx("probe"), " → ".join(trace))
    check("§0 скелет записан ДО нуджа (окно не откроется в пустоту)",
          _idx("manifest:skeleton") < _idx("nudge"), " → ".join(trace))
    check("§0 после нуджа идут chapters и только потом ready",
          _idx("nudge") < _idx("manifest:chapters") < _idx("manifest:ready"),
          " → ".join(trace))
    check("§0 веб-нога НЕ вызывается на пути к ready ни разу",
          web.calls == 0, f"calls={web.calls}")
    check("§0 время до ready не содержит сетевого ожидания",
          t_ready < web.stall_s + 5.0 and t_ready < 5.0, f"{t_ready:.2f}s < 5.0s")
    check("§0 скелет НЕ пустой: 12 глав по именам файлов",
          len(seen_skeleton.get("chapters") or []) == 12
          and all(c.get("duration_ms") is None
                  for c in seen_skeleton.get("chapters") or []),
          f"chapters={len(seen_skeleton.get('chapters') or [])}")
    check("§0 скелет уже знает автора/название (из имени папки) и объём",
          bool(seen_skeleton.get("title")) and bool(seen_skeleton.get("author"))
          and seen_skeleton.get("file_count") == 12
          and int(seen_skeleton.get("total_bytes") or 0) > 0,
          f"{seen_skeleton.get('author')!r} / {seen_skeleton.get('title')!r}")
    check("§0 у скелета НЕТ build_token физически",
          "build_token" not in seen_skeleton)
    check("§0 итог скана — ready с настоящими длительностями и build_token",
          man_big is not None
          and scan.manifest_phase(man_big) == "ready"
          and _is_hex32(man_big.get("build_token"))
          and len(man_big["chapters"]) == 12
          and all(c["duration_ms"] for c in man_big["chapters"]),
          f"phase={scan.manifest_phase(man_big or {})}")
    check("§0 нудж за скан ровно один", _nudge_count(nudge_log) == 1,
          f"count={_nudge_count(nudge_log)}")

    # ═════ I1. СБОРКА ПО НЕПОЛНОМУ МАНИФЕСТУ НЕВОЗМОЖНА ══════════════════════
    print("\nI1 · сборка по неполному манифесту невозможна (TOCTOU + fail-closed)")
    os.environ["MP3TOM4B_HALT_AFTER_PHASE"] = "skeleton"
    toc = watch / "Гоголь - Мёртвые души"
    for i in (1, 2, 3):
        _make_mp3(toc / f"{i:02d}.mp3", tags={"title": f"Глава {i}"})
    scan.run_scan()
    os.environ.pop("MP3TOM4B_HALT_AFTER_PHASE")

    skel = _manifest_for(config, state, "Мёртвые души")
    check("I1 книга лежит на диске СКЕЛЕТОМ",
          skel is not None and scan.manifest_phase(skel) == "skeleton",
          f"phase={scan.manifest_phase(skel or {})}")
    check("I1 у скелета нет build_token, но уже есть confirm_token",
          not scan.manifest_build_token(skel or {})
          and _is_hex32((skel or {}).get("confirm_token")))
    early_cmd = _confirm_cmd(skel)      # приложение чеканит команду ПРЯМО СЕЙЧАС
    check("I1 команда, которую приложение может отчеканить здесь, БЕЗ build_token",
          "build_token" not in early_cmd)
    cmd_path = _drop_command(config.commands_dir(), early_cmd)

    # ...и ТОЛЬКО ПОТОМ агент дозаполняет манифест: тот же rev, тот же токен.
    scan.run_scan()
    final = _manifest_for(config, state, "Мёртвые души")
    check("I1 resume довёл ту же книгу до ready БЕЗ смены rev и confirm_token",
          final is not None and scan.manifest_phase(final) == "ready"
          and final["source_rev"] == skel["source_rev"]
          and final["confirm_token"] == skel["confirm_token"],
          f"phase={scan.manifest_phase(final or {})}")
    verdict, reason = dispatcher.validate_command(early_cmd, final)
    check("I1 TOCTOU: ранняя команда отвергнута ПОСЛЕ финализации того же rev",
          verdict == dispatcher.VERDICT_REJECT_NOT_READY
          and reason == "build_token_mismatch", f"{verdict}/{reason}")

    before_ev = len(state.read_events())
    dispatcher.drain_commands()
    new_ev = state.read_events()[before_ev:]
    after = _manifest_for(config, state, "Мёртвые души")
    check("I1 после дренажа книга ОСТАЛАСЬ pending-confirm (команда умерла, книга — нет)",
          after.get("status") == "pending-confirm", f"status={after.get('status')!r}")
    check("I1 отказ журналирован своим событием confirm_rejected_not_ready",
          sum(1 for e in new_ev if e.get("event") == "confirm_rejected_not_ready") == 1,
          str([e.get("event") for e in new_ev]))
    check("I1 сборка НЕ стартовала",
          not any(e.get("event") == "build_started" for e in new_ev))
    check("I1 файл команды съеден дренажом", not cmd_path.exists())
    fresh_ok, fresh_reason = dispatcher.validate_command(_confirm_cmd(after), after)
    check("I1 свежая команда (эхом build_token) валидна",
          fresh_ok == dispatcher.VERDICT_ACCEPT, f"{fresh_ok}/{fresh_reason}")

    # fail-closed: три формы «не докажу, что видел полную книгу»
    forged = _confirm_cmd(after, build_token="f" * 32)
    check("I1 fail-closed: ПОДДЕЛАННЫЙ build_token — отказ",
          dispatcher.validate_command(forged, after)
          == (dispatcher.VERDICT_REJECT_NOT_READY, "build_token_mismatch"))
    nulled = _confirm_cmd(after, build_token=None)
    check("I1 fail-closed: build_token отсутствует в команде — отказ",
          dispatcher.validate_command(nulled, after)
          == (dispatcher.VERDICT_REJECT_NOT_READY, "build_token_mismatch"))
    # ``pop`` с дефолтом намеренно: под сломанным гвардом сюда приезжает скелет, и
    # сьюта обязана в этом месте ПОКРАСНЕТЬ ниже по своим проверкам, а не упасть,
    # унеся с собой все проверки следующих инвариантов.
    skel_like = dict(after)
    skel_like.pop("build_token", None)
    skel_like["phase"] = "chapters"
    check("I1 fail-closed: у манифеста нет токена (фаза chapters) — отказ",
          dispatcher.validate_command(_confirm_cmd(after), skel_like)
          == (dispatcher.VERDICT_REJECT_NOT_READY, "manifest_not_ready:'chapters'"))
    empty_ch = dict(after)
    empty_ch["chapters"] = []
    check("I1 fail-closed: главы структурно пусты — отказ даже с верным токеном",
          dispatcher.validate_command(_confirm_cmd(after), empty_ch)
          == (dispatcher.VERDICT_REJECT_NOT_READY, "chapters_empty"))
    check("I1 build_token — 32 hex и у РАЗНЫХ книг разный (чеканится, не выводится)",
          _is_hex32(after.get("build_token"))
          and after.get("build_token") != man_big.get("build_token"))

    # ═════ I2. ОДИН НУДЖ НА ПУБЛИКАЦИЮ ═══════════════════════════════════════
    print("\nI2 · один нудж на публикацию (ключ скелета == ключ ready)")
    nudges_before = _nudge_count(nudge_log)
    os.environ["MP3TOM4B_HALT_AFTER_PHASE"] = "skeleton"
    two = watch / "Чехов - Каштанка"
    for i in (1, 2):
        _make_mp3(two / f"{i:02d}.mp3", tags={"title": f"Глава {i}"})
    scan.run_scan()
    os.environ.pop("MP3TOM4B_HALT_AFTER_PHASE")
    skel_two = _manifest_for(config, state, "Каштанка")
    key_skeleton = scan._book_edge_key(skel_two)
    check("I2 публикация скелета подняла окно ровно один раз",
          _nudge_count(nudge_log) == nudges_before + 1,
          f"{nudges_before} → {_nudge_count(nudge_log)}")
    check("I2 ключ леджера скелета записан",
          key_skeleton in _ledger_keys(config, state), key_skeleton)

    scan.run_scan()                     # вторая публикация: та же книга, ready
    ready_two = _manifest_for(config, state, "Каштанка")
    key_ready = scan._book_edge_key(ready_two)
    check("I2 ключ леджера у ready СОВПАДАЕТ с ключом скелета",
          key_ready == key_skeleton, f"{key_skeleton} vs {key_ready}")
    check("I2 две публикации одной книги = ОДИН нудж",
          _nudge_count(nudge_log) == nudges_before + 1,
          f"count={_nudge_count(nudge_log)}")
    scan.run_scan()
    scan.run_scan()
    check("I2 повторные сканы устоявшейся книги добавляют НОЛЬ нуджей",
          _nudge_count(nudge_log) == nudges_before + 1,
          f"count={_nudge_count(nudge_log)}")
    check("I2 confirm_token один и тот же во всех фазах (леджерный ключ не двигался)",
          ready_two["confirm_token"] == skel_two["confirm_token"])

    # ═════ I3. ОКНО НЕ ОТКРЫВАЕТСЯ НА КОПИРУЮЩЕЙСЯ КНИГЕ ═════════════════════
    print("\nI3 · окно не открывается на копирующейся книге (дебаунс + M-E)")
    late = watch / "Достоевский - Идиот"
    for i in (1, 2):
        _make_mp3(late / f"{i:02d}.mp3", tags={"title": f"Глава {i}"})
    donor = late / "02.mp3"
    stale_list = scan._list_mp3s(late)

    # Файл доезжает СТРОГО между двумя наблюдениями дебаунса — детерминированно,
    # без гонки: подменяем сон, который дебаунс и есть.
    real_sleep = time.sleep
    dropped: list[str] = []

    def _sleep_and_deliver(seconds):
        if not dropped:
            dropped.append("03")
            shutil.copy2(donor, late / "03.mp3")
        real_sleep(seconds)

    os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0.05"
    scan.time.sleep = _sleep_and_deliver
    try:
        old_way = (scan._size_mtime_snapshot(stale_list)
                   == scan._size_mtime_snapshot(stale_list))
        check("I3 негативный контроль: СТАРОЕ сравнение (список сам с собой) "
              "зовёт растущую книгу стабильной", old_way,
              "это и есть баг, который чинил M-E")
        dropped.clear()
        (late / "03.mp3").unlink(missing_ok=True)
        new_way = scan._files_are_stable(stale_list, late)
        check("I3 НОВАЯ проверка перечитывает каталог и зовёт её НЕстабильной",
              new_way is False, f"stable={new_way}")
    finally:
        scan.time.sleep = real_sleep

    (late / "03.mp3").unlink(missing_ok=True)
    dropped.clear()
    nudges_before = _nudge_count(nudge_log)
    skeletons_before = len(_events_of(state, "manifest_skeleton"))
    scan.time.sleep = _sleep_and_deliver
    try:
        scan.run_scan()                 # файл доезжает во время дебаунса
    finally:
        scan.time.sleep = real_sleep
    copying = _manifest_for(config, state, "Идиот")
    check("I3 копирующаяся книга НЕ вооружена: манифеста нет",
          copying is None, f"manifest={bool(copying)}")
    check("I3 и скелет для неё не писался (дебаунс стоит ПЕРЕД скелетом)",
          len(_events_of(state, "manifest_skeleton")) == skeletons_before)
    check("I3 окно не поднималось", _nudge_count(nudge_log) == nudges_before,
          f"{nudges_before} → {_nudge_count(nudge_log)}")
    check("I3 отказ вооружать журналирован (book_still_copying)",
          len(_events_of(state, "book_still_copying")) >= 1)

    os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"
    scan.run_scan()
    settled = _manifest_for(config, state, "Идиот")
    check("I3 как только копирование кончилось — книга вооружается ЦЕЛИКОМ (3 главы)",
          settled is not None and len(settled["chapters"]) == 3
          and scan.manifest_phase(settled) == "ready",
          f"chapters={len((settled or {}).get('chapters') or [])}")
    check("I3 и ровно один нудж — на настоящую, доехавшую книгу",
          _nudge_count(nudge_log) == nudges_before + 1,
          f"count={_nudge_count(nudge_log)}")

    # ═════ I4. СКЕЛЕТ НЕ ЗАМЕРЗАЕТ ═══════════════════════════════════════════
    print("\nI4 · скелет не замерзает (halt после фазы → следующий тик достраивает)")
    for phase in ("skeleton", "chapters"):
        os.environ["MP3TOM4B_HALT_AFTER_PHASE"] = phase
        folder = watch / f"Куприн - Книга {phase}"
        for i in (1, 2):
            _make_mp3(folder / f"{i:02d}.mp3", tags={"title": f"Глава {i}"})
        scan.run_scan()
        os.environ.pop("MP3TOM4B_HALT_AFTER_PHASE")
        halted = _manifest_for(config, state, f"Книга {phase}")
        halts = [e for e in _events_of(state, "manifest_halted")
                 if e.get("after_phase") == phase]
        check(f"I4 halt после «{phase}»: манифест замер именно на этой фазе",
              halted is not None and scan.manifest_phase(halted) == phase
              and bool(halts), f"phase={scan.manifest_phase(halted or {})}")
        check(f"I4 halt после «{phase}»: build_token не выдан",
              not scan.manifest_build_token(halted or {}))
        if phase == "chapters":
            check("I4 на фазе chapters длительности уже настоящие (окно наполнилось)",
                  all(c["duration_ms"] for c in halted["chapters"]))
        # ключевая проверка: неизменившийся rev НЕ замыкает накоротко
        resumes_before = len(_events_of(state, "manifest_resumed"))
        nudges_before = _nudge_count(nudge_log)
        scan.run_scan()
        done_now = _manifest_for(config, state, f"Книга {phase}")
        check(f"I4 следующий тик ДОСТРОИЛ книгу («{phase}» → ready)",
              scan.manifest_phase(done_now) == "ready"
              and _is_hex32(done_now.get("build_token")),
              f"phase={scan.manifest_phase(done_now)}")
        check(f"I4 resume сохранил confirm_token и rev («{phase}»)",
              done_now["confirm_token"] == halted["confirm_token"]
              and done_now["source_rev"] == halted["source_rev"])
        check(f"I4 resume журналирован и НЕ поднял окно второй раз («{phase}»)",
              len(_events_of(state, "manifest_resumed")) > resumes_before
              and _nudge_count(nudge_log) == nudges_before,
              f"nudges={_nudge_count(nudge_log)}")

    # структурная причина, по которой скелет не замерзает: замыкание фазозависимо
    args = scan._book_manifest_args(watch / "Куприн - Книга chapters")
    cur, plan = scan._begin_manifest(**args, staged=False)
    check("I4 у ПОЛНОГО манифеста тот же rev замыкается накоротко (plan=None)",
          plan is None and cur is not None)
    frozen_path = config.books_dir() / f"{done_now['book_id']}.json"
    frozen = dict(done_now)
    frozen["phase"] = "chapters"
    frozen.pop("build_token", None)
    state.write_json_atomic(frozen_path, frozen)
    cur2, plan2 = scan._begin_manifest(**args, staged=False)
    check("I4 у НЕПОЛНОГО манифеста тот же rev даёт RESUME (plan есть)",
          plan2 is not None)
    scan.run_scan()                     # вернуть книгу в ready

    # ═════ I5. ПРОПУЩЕННАЯ КНИГА НЕ ВОСКРЕСАЕТ ═══════════════════════════════
    print("\nI5 · пропущенная книга не воскресает (ни готовая, ни скелет)")
    ready_skip = _manifest_for(config, state, "Каштанка")
    _drop_command(config.commands_dir(), _skip_cmd(ready_skip["book_id"]))
    dispatcher.drain_commands()
    nudges_before = _nudge_count(nudge_log)
    scan.run_scan()
    after_skip = _manifest_for(config, state, "Каштанка")
    check("I5 готовая пропущенная книга остаётся skipped после скана",
          after_skip.get("status") == "skipped", f"status={after_skip.get('status')!r}")
    check("I5 и не поднимает окно", _nudge_count(nudge_log) == nudges_before,
          f"count={_nudge_count(nudge_log)}")

    os.environ["MP3TOM4B_HALT_AFTER_PHASE"] = "skeleton"
    sk_book = watch / "Куприн - Гранатовый браслет"
    _make_mp3(sk_book / "01.mp3", tags={"title": "Раз"})
    scan.run_scan()
    os.environ.pop("MP3TOM4B_HALT_AFTER_PHASE")
    sk_man = _manifest_for(config, state, "Гранатовый браслет")
    check("I5 подготовка: книга лежит скелетом",
          scan.manifest_phase(sk_man) == "skeleton")
    sk_path = config.books_dir() / f"{sk_man['book_id']}.json"
    _drop_command(config.commands_dir(), _skip_cmd(sk_man["book_id"]))
    dispatcher.drain_commands()

    # Дренаж закрывается собственным run_scan, поэтому к моменту проверки книга уже
    # прошла через замыкание один раз. Возвращаем на диск ровно ту форму, про
    # которую инвариант и написан — НЕПОЛНЫЙ манифест со статусом skipped, — и
    # спрашиваем гвард по ней. Форма не выдуманная: человек жмёт «Пропустить» в
    # окне, открытом на скелете, а процесс агента может умереть до закрывающего
    # скана; следующий тик launchd видит на диске именно это.
    sk_probe = dict(state.read_json(sk_path))
    sk_probe["phase"] = "skeleton"
    sk_probe.pop("build_token", None)
    sk_probe["status"] = "skipped"
    state.write_json_atomic(sk_path, sk_probe)
    sk_args = scan._book_manifest_args(sk_book)
    sk_cur, sk_plan = scan._begin_manifest(**sk_args, staged=False)
    check("I5 пропущенный СКЕЛЕТ не берётся в дозаполнение (plan=None) — "
          "статус несущий в условии замыкания",
          sk_plan is None and (sk_cur or {}).get("status") == "skipped",
          f"plan={sk_plan is not None} status={(sk_cur or {}).get('status')!r}")
    nudges_before = _nudge_count(nudge_log)
    scan.run_scan()
    sk_after = _manifest_for(config, state, "Гранатовый браслет")
    check("I5 СКЕЛЕТ, который человек пропустил, не воскресает finalize'ом",
          sk_after.get("status") == "skipped",
          f"status={sk_after.get('status')!r} phase={scan.manifest_phase(sk_after)}")
    check("I5 агент вообще не работает над пропущенной книгой: она так и лежит "
          "скелетом, БЕЗ build_token",
          scan.manifest_phase(sk_after) == "skeleton"
          and not scan.manifest_build_token(sk_after),
          f"phase={scan.manifest_phase(sk_after)} "
          f"token={bool(scan.manifest_build_token(sk_after))}")
    check("I5 и его дозаполнение не подняло окно",
          _nudge_count(nudge_log) == nudges_before,
          f"count={_nudge_count(nudge_log)}")

    # ═════ I7. ПОРЯДОК ПУБЛИКАЦИИ ════════════════════════════════════════════
    print("\nI7 · порядок публикации: манифест → state.json → леджер → нудж")
    order: list[str] = []
    real_write_state = state.write_state
    real_ledger = scan._notified_write
    real_nudge = scan._nudge_app
    real_atomic = state.write_json_atomic

    def _ord_atomic(path, data):
        if isinstance(data, dict) and data.get("phase") == "skeleton":
            order.append("manifest")
        return real_atomic(path, data)

    state.write_state = lambda s: (order.append("state"), real_write_state(s))[1]
    scan._notified_write = lambda k: (order.append("ledger"), real_ledger(k))[1]
    scan._nudge_app = lambda k: (order.append("nudge"), real_nudge(k))[1]
    state.write_json_atomic = _ord_atomic
    try:
        _make_mp3(watch / "Лермонтов - Герой" / "01.mp3", tags={"title": "Раз"})
        scan.run_scan()
    finally:
        state.write_state = real_write_state
        scan._notified_write = real_ledger
        scan._nudge_app = real_nudge
        state.write_json_atomic = real_atomic
    check("I7 первые четыре шага публикации: manifest → state → ledger → nudge",
          order[:4] == ["manifest", "state", "ledger", "nudge"], str(order))
    check("I7 нудж — ПОСЛЕДНИЙ (ничего не в полёте, когда приложение смотрит)",
          "nudge" in order and order.index("nudge") > order.index("ledger")
          > order.index("state") > order.index("manifest"), str(order))

    # ═════ I8. СТАРЫЕ МАНИФЕСТЫ ЧИТАЮТСЯ ═════════════════════════════════════
    print("\nI8 · старые манифесты читаются (нет поля фазы = done)")
    check("I8 отсутствие поля phase читается как done",
          scan.manifest_phase({}) == "done"
          and scan.manifest_phase({"phase": None}) == "done"
          and scan.manifest_phase({"phase": "skeleton"}) == "skeleton")
    check("I8 отсутствие build_token читается как пустая строка, не как None",
          scan.manifest_build_token({}) == ""
          and scan.manifest_build_token({"build_token": 17}) == "")

    leg_dir = watch / "Бунин - Тёмные аллеи"
    _make_mp3(leg_dir / "01.mp3", tags={"title": "Раз"})
    scan.run_scan()
    leg = _manifest_for(config, state, "Тёмные аллеи")
    leg_path = config.books_dir() / f"{leg['book_id']}.json"
    stripped = {k: v for k, v in leg.items() if k not in ("phase", "build_token")}
    state.write_json_atomic(leg_path, stripped)      # как будто писал до-D17 агент
    nudges_before = _nudge_count(nudge_log)
    upgrades_before = len(_events_of(state, "manifest_phase_upgraded"))
    scan.run_scan()
    upgraded = state.read_json(leg_path)
    check("I8 до-D17 манифест поднят НА МЕСТЕ: phase=ready + build_token",
          scan.manifest_phase(upgraded) == "ready"
          and _is_hex32(upgraded.get("build_token")),
          f"phase={scan.manifest_phase(upgraded)}")
    check("I8 подъём сохранил confirm_token / source_rev / processed_keys",
          upgraded["confirm_token"] == leg["confirm_token"]
          and upgraded["source_rev"] == leg["source_rev"]
          and upgraded["processed_keys"] == leg["processed_keys"])
    check("I8 подъём журналирован и НЕ поднял окно (нет ре-арма)",
          len(_events_of(state, "manifest_phase_upgraded")) > upgrades_before
          and _nudge_count(nudge_log) == nudges_before,
          f"nudges={_nudge_count(nudge_log)}")
    check("I8 и книга снова собираема",
          dispatcher.validate_command(_confirm_cmd(upgraded), upgraded)[0]
          == dispatcher.VERDICT_ACCEPT)

    orphan = {
        "book_id": "orphan0000000001", "src_dir": str(watch), "status": "pending-confirm",
        "source_rev": "deadbeef" * 8, "confirm_token": "a" * 32,
        "chapters": [{"index": 1, "file": "01.mp3", "name": "Раз", "duration_ms": 1000}],
        "processed_keys": [], "params": {},
    }
    orphan_path = config.books_dir() / "orphan0000000001.json"
    state.write_json_atomic(orphan_path, orphan)
    scan._upgrade_legacy_manifests([dict(orphan)])
    lifted = state.read_json(orphan_path)
    check("I8 книга БЕЗ подпапки (материализованная группировкой) тоже поднимается",
          scan.manifest_phase(lifted) == "ready"
          and _is_hex32(lifted.get("build_token")))
    orphan_path.unlink(missing_ok=True)

    forged_skeleton = {"book_id": "x", "status": "pending-confirm", "phase": "skeleton",
                       "chapters": []}
    guarded = scan._ensure_build_token(dict(forged_skeleton),
                                       config.books_dir() / "x.json")
    check("I8 гвард: СКЕЛЕТУ токен НЕ выдаётся (иначе подделали бы само основание)",
          not scan.manifest_build_token(guarded)
          and not (config.books_dir() / "x.json").exists())
    done_book = {"book_id": "y", "status": "done", "chapters": []}
    check("I8 гвард: готовой/собранной книге токен не выдаётся",
          not scan.manifest_build_token(
              scan._ensure_build_token(dict(done_book), config.books_dir() / "y.json")))

    # ═════ I9. ЛЕНТА ОБЛОЖЕК APPEND-ONLY ═════════════════════════════════════
    print("\nI9 · лента обложек append-only (веб только дополняет хвост)")
    art = _manifest_for(config, state, "Тёмные аллеи")
    art_path = config.books_dir() / f"{art['book_id']}.json"
    # ``.get`` вместо подписки: под сломанным гвардом сюда доезжает книга без
    # ленты, и сьюта обязана покраснеть проверкой, а не упасть на KeyError.
    before_opts = [dict(o) for o in (art.get("cover_options") or [])]
    before_sel = art.get("cover_selected")
    check("I9 до веба лента уже не пуста (локальная гарантия PRD G4)",
          len(before_opts) >= 1 and art.get("cover_web") == "pending",
          f"options={len(before_opts)} web={art.get('cover_web')}")

    web.calls = 0
    enriched = scan.enrich_covers_web()
    art2 = state.read_json(art_path)
    after_opts = art2.get("cover_options") or []
    check("I9 веб-нога живёт в enrich-проходе (счётчик вызовов вырос ИМЕННО тут)",
          web.calls >= 1 and enriched >= 1, f"calls={web.calls} books={enriched}")
    check("I9 старый список — ПРЕФИКС нового",
          after_opts[:len(before_opts)] == before_opts,
          f"{len(before_opts)} → {len(after_opts)}")
    check("I9 уже показанные плитки не сменили id/имён файлов",
          [(o["id"], Path(o["path"]).name) for o in after_opts[:len(before_opts)]]
          == [(o["id"], Path(o["path"]).name) for o in before_opts])
    check("I9 выбор человека не тронут", art2.get("cover_selected") == before_sel,
          f"{before_sel} → {art2.get('cover_selected')}")
    check("I9 ревизия книги не тронута (rev/токены/фаза/статус)",
          all(art2[k] == art[k] for k in
              ("source_rev", "confirm_token", "build_token", "phase", "status")))
    check("I9 веб-нога закрыта: cover_web=done, tries=1, событие записано",
          art2.get("cover_web") == "done" and art2.get("cover_web_tries") == 1
          and len(_events_of(state, "cover_web_enriched")) >= 1,
          f"web={art2.get('cover_web')} tries={art2.get('cover_web_tries')}")
    web.calls = 0
    scan.enrich_covers_web()
    art3 = state.read_json(art_path)
    check("I9 повторный проход по закрытой книге ничего не делает",
          web.calls == 0 and (art3.get("cover_options") or []) == after_opts,
          f"calls={web.calls}")

    # поздний воркер ПРОШЛОГО поколения
    gen_dir = watch / "Гончаров - Обломов"
    _make_mp3(gen_dir / "01.mp3", tags={"title": "Раз"})
    scan.run_scan()
    gen_man = _manifest_for(config, state, "Обломов")
    gen_path = config.books_dir() / f"{gen_man['book_id']}.json"
    gen_before = [dict(o) for o in (gen_man.get("cover_options") or [])]
    discards_before = len(_events_of(state, "cover_web_discarded"))

    def _rearm_midsearch() -> None:
        """Книга перевооружается, пока «поиск» ещё идёт — новое поколение."""
        live = state.read_json(gen_path)
        live["confirm_token"] = "b" * 32          # ре-арм → другое поколение
        state.write_json_atomic(gen_path, live)

    web.on_call = _rearm_midsearch
    try:
        scan.enrich_covers_web()
    finally:
        web.on_call = None
    gen_after = state.read_json(gen_path)
    check("I9 поздний воркер ПРОШЛОГО поколения отброшен, лента не тронута",
          (gen_after.get("cover_options") or []) == gen_before
          and len(_events_of(state, "cover_web_discarded")) > discards_before,
          f"options={len(gen_after.get('cover_options') or [])}")

    # бюджет и уступка команде — обе причины «оставить pending и уйти»
    fresh_dir = watch / "Тургенев - Отцы и дети"
    _make_mp3(fresh_dir / "01.mp3", tags={"title": "Раз"})
    scan.run_scan()
    fresh_man = _manifest_for(config, state, "Отцы и дети")
    fresh_path = config.books_dir() / f"{fresh_man['book_id']}.json"
    stub_cmd = _drop_command(config.commands_dir(),
                             {"action": "noop-selfcheck", "book_id": "нет"})
    web.calls = 0
    scan.enrich_covers_web()
    check("I9 очередь команд перебивает поиск обложек (cover_web_yielded)",
          web.calls == 0 and len(_events_of(state, "cover_web_yielded")) >= 1,
          f"calls={web.calls}")
    stub_cmd.unlink(missing_ok=True)
    dispatcher.drain_commands()          # убрать мусорную команду из очереди

    os.environ["MP3TOM4B_COVER_WEB_BUDGET_S"] = "0"
    web.calls = 0
    scan.enrich_covers_web()
    os.environ.pop("MP3TOM4B_COVER_WEB_BUDGET_S")
    check("I9 исчерпанный бюджет прохода останавливает ноги без сети "
          "(cover_web_budget_exhausted)",
          web.calls == 0 and len(_events_of(state, "cover_web_budget_exhausted")) >= 1,
          f"calls={web.calls}")
    check("I9 недообслуженная книга осталась pending — её возьмёт следующий тик",
          state.read_json(fresh_path).get("cover_web") == "pending")

    web.results = 0                      # «ничего не нашлось» — три попытки и хватит
    for _ in range(scan.COVER_WEB_MAX_TRIES):
        scan.enrich_covers_web()
    web.results = 2
    exhausted = state.read_json(fresh_path)
    check(f"I9 после {scan.COVER_WEB_MAX_TRIES} пустых попыток книга больше "
          "ничего не должна",
          exhausted.get("cover_web") == "done"
          and exhausted.get("cover_web_tries") == scan.COVER_WEB_MAX_TRIES,
          f"web={exhausted.get('cover_web')} tries={exhausted.get('cover_web_tries')}")

    # ═════ I6. СЕТЕВОЙ СБОЙ ≠ ПРОБЛЕМА ДОСТУПА (два дочерних тика) ═══════════
    print("\nI6 · сетевой сбой ≠ проблема доступа (+ негативный контроль)")
    guarded_run = _i6_run(root, repo_root, "guarded")
    check("I6 мёртвая сеть: тик уходит с exit 0",
          guarded_run["rc"] == 0, f"rc={guarded_run['rc']}")
    check("I6 мёртвая сеть: folder_access остаётся ok",
          guarded_run["access"] == "ok", f"access={guarded_run['access']!r}")
    check("I6 мёртвая сеть: фазовый сторож НЕ срабатывал",
          "phase_deadline_exceeded" not in guarded_run["events"]
          and "folder_access_lost" not in guarded_run["events"],
          str([e for e in guarded_run["events"] if "access" in str(e)
               or "deadline" in str(e)]))
    check("I6 мёртвая сеть: книга всё равно доехала до ready с build_token",
          guarded_run["manifest"].get("phase") == "ready"
          and _is_hex32(guarded_run["manifest"].get("build_token")),
          f"phase={guarded_run['manifest'].get('phase')}")

    neg = _i6_run(root, repo_root, "unguarded")
    check("I6 НЕГАТИВНЫЙ КОНТРОЛЬ: веб обратно внутрь _finish_manifest → exit 75",
          neg["rc"] == 75, f"rc={neg['rc']}")
    check("I6 НЕГАТИВНЫЙ КОНТРОЛЬ: и ложная карточка доступа "
          "(folder_access='blocked')",
          neg["access"] == "blocked", f"access={neg['access']!r}")
    check("I6 НЕГАТИВНЫЙ КОНТРОЛЬ: фазовый дедлайн действительно взорвался",
          "phase_deadline_exceeded" in neg["events"],
          str(neg["events"][-4:]))
    check("I6 два прогона отличаются ТОЛЬКО местом веб-ноги — значит зелёный "
          "держит именно гвард",
          guarded_run["rc"] == 0 and neg["rc"] == 75
          and guarded_run["access"] == "ok" and neg["access"] == "blocked")

    # мёртвая сеть в самом проходе обогащения не трогает вердикт доступа
    def _boom(*_a, **_k):
        raise TimeoutError("network is down")

    web_real = cover.search_web
    cover.search_web = _boom
    try:
        scan.enrich_covers_web()
    finally:
        cover.search_web = web_real
    st_after = state.read_state()
    check("I6 падение веб-поиска внутри процесса не трогает folder_access",
          (st_after.get("agent") or {}).get("folder_access") in (None, "ok"),
          str((st_after.get("agent") or {}).get("folder_access")))

    return _finish(root)


def _finish(root: Path) -> int:
    # Плоская верификация: сьюта прогоняет ТОЛЬКО свои проверки; кросс-сьютовая
    # регрессия оркеструется один раз в ``agent.selfcheck_all``.
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§early self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--i6-child":
        sys.exit(_i6_child(sys.argv[2]))
    sys.exit(run())
