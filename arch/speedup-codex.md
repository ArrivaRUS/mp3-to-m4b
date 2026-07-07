# Архитектор #2 (GPT-5.5 / Codex) — ускорение движка mp3→.m4b

> Движок: gpt-5.5, reasoning effort xhigh, sandbox read-only. Дата: 2026-06-30.
> Сырой вызов: `codex exec -c model_reasoning_effort="xhigh" -s read-only` (бриф через stdin).
> Прочитанные файлы: agent/build_m4b.py, research/m4b-toolchain.md, agent/dispatcher.py, agent/scan.py, agent/split.py.
> Это ЧИСТОВОЙ ответ Codex (без reasoning-трассы). Полный сырой лог — рядом не хранится (376KB временного трейса).

---

codex
**Рекомендация (TL;DR)**
Брать не чистый per-chapter как дефолт, а **параллельный encode групп последовательных глав → финальный concat stream-copy**. Он даёт сильное ускорение и резко уменьшает риск AAC-швов: не 55 швов на 56 главах, а `workers-1`.

Самый быстрый безопасный первый шаг: добавить `aac_at` как кодер и прогресс. Самый быстрый максимум: группы/главы параллельно + `aac_at`, но после QA на швы и speed sweep `workers=4/8/12`.

Чистый per-chapter AAC stream-copy допустим как агрессивный режим, но не как безусловный дефолт: AAC priming/delay на каждом фрагменте может дать микропаузу/щелчок на границе.

**Ранжированные опции**
1. **Простой high-ROI: текущая архитектура + `aac_at` + progress**

Суть: оставить один ffmpeg, заменить `-c:a aac` на Apple AudioToolbox, добавить `-progress pipe:1`.

Filter path:
```bash
ffmpeg -hide_banner -nostdin -loglevel error -nostats -progress pipe:1 -y \
  -i "01.mp3" -i "02.mp3" ... -i chapters.ffmeta -i cover.jpg \
  -filter_complex_script filter.txt \
  -map "[aout]" -map 58:v:0 -map_metadata 57 -map_chapters 57 \
  -c:a aac_at -aac_at_mode cbr -b:a 192k -ar "$SR" -ac "$CH" \
  -c:v mjpeg -disposition:v attached_pic \
  -f ipod -movflags +faststart ".Book.m4b.x.tmp"
```

Demuxer path:
```bash
ffmpeg -hide_banner -nostdin -loglevel error -nostats -progress pipe:1 -y \
  -f concat -safe 0 -i list.txt -i chapters.ffmeta -i cover.jpg \
  -map 0:a -map 2:v:0 -map_metadata 1 -map_chapters 1 \
  -c:a aac_at -aac_at_mode cbr -b:a 192k -ar "$SR" -ac "$CH" \
  -c:v mjpeg -disposition:v attached_pic \
  -f ipod -movflags +faststart ".Book.m4b.x.tmp"
```

Ожидаемый выигрыш: грубо `1.3-3x`, зависит от железа и входов. Риски: качество/размер. Для дефолта я бы держал `cbr + -b:a 192k`; `cvbr/vbr` лучше звучат на низких битрейтах, но ломают точность оценки размера. Локально `aac_at` имеет `Threading capabilities: none`, так что `-threads` не спасает.

2. **Рекомендуемый сильный вариант: параллельные группы глав → concat stream-copy**

Суть: разбить книгу на `K ~= workers` последовательных групп с близкой суммарной длительностью. Каждую группу кодировать отдельным ffmpeg в `.m4a`, потом финальный `.m4b` собрать без аудио-реэнкода.

Group encode:
```bash
# group_003.filter:
[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a0];
[1:a]aformat=sample_rates=48000:channel_layouts=stereo[a1];
[a0][a1]concat=n=2:v=0:a=1[aout]

ffmpeg -hide_banner -nostdin -loglevel error -nostats -progress pipe:1 -y \
  -i "17.mp3" -i "18.mp3" \
  -filter_complex_script ".Book.m4b.x.group_003.filter" \
  -map "[aout]" -map_metadata -1 -vn -sn -dn \
  -c:a aac_at -aac_at_mode cbr -b:a 192k -ar 48000 -ac 2 \
  -f ipod ".Book.m4b.x.chunks/group_003.m4a.tmp"
```

Final mux:
```bash
ffmpeg -hide_banner -nostdin -loglevel error -nostats -progress pipe:1 -y \
  -f concat -safe 0 -i chunks.txt \
  -i chapters.ffmeta -i cover.jpg \
  -map 0:a -map 2:v:0 -map_metadata 1 -map_chapters 1 \
  -c:a copy -c:v mjpeg -disposition:v attached_pic \
  -f ipod -movflags +faststart ".Book.m4b.x.tmp"
```

`chunks.txt`:
```text
file '/abs/.Book.m4b.x.chunks/group_000.m4a'
file '/abs/.Book.m4b.x.chunks/group_001.m4a'
```

Ожидаемый выигрыш: на 56 главах / 26 ч, `8 workers` обычно `4-7x`, `12-16 workers` `6-10x`, пока не упрётесь в I/O и финальный `+faststart`. Главный риск: AAC priming только на границах групп. Закрытие: групп меньше, QA швов, fallback на single-encode при провале.

3. **Агрессивный максимум: per-chapter encode → concat stream-copy**

Per chapter:
```bash
ffmpeg -hide_banner -nostdin -loglevel error -nostats -progress pipe:1 -y \
  -i "01.mp3" -map 0:a:0 -map_metadata -1 -vn -sn -dn \
  -af "aformat=sample_rates=48000:channel_layouts=stereo" \
  -c:a aac_at -aac_at_mode cbr -b:a 192k -ar 48000 -ac 2 \
  -f ipod ".Book.m4b.x.chunks/ch_0001.m4a.tmp"
```

Final mux тот же, только список из `ch_0001.m4a...`.

Ожидаемый выигрыш: `8-15x`, иногда больше с `aac_at`, если диск не узкое место. Риск: **не гарантированная бесшовность**. AAC frame = 1024 samples, то есть ~23.2 ms при 44.1 kHz / ~21.3 ms при 48 kHz; каждый независимый encode несёт encoder delay/priming/padding. `.m4a/mp4` лучше ADTS, потому что хранит timing/edit-list; ADTS `.aac` для этого варианта не брать. Но даже `.m4a` не даёт такой же гарантии, как один непрерывный encode. Для речи часто приемлемо, если главы и так имеют тишину, но для бесшовных глав/музыки риск реальный.

**Тайминги глав**
Для single-encode оставлять текущие `duration_ms` исходников. Для parallel-fragment path главы лучше строить по **фактическим playback-duration перекодированных фрагментов**, иначе накопится дрейф от AAC frame rounding/padding. После каждого фрагмента:
```bash
ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 "ch_0001.m4a"
```
Сравнивать `encoded_ms` с исходным `duration_ms`; если дельта по главе/группе больше, например, `2 AAC frames + 20 ms`, падать или fallback на single-encode. В FFMETADATA писать `TIMEBASE=1/1000`, START/END из накопленной фактической timeline.

**Тюнинг и дешёвые победы**
`-threads` почти не поможет: локально и `aac`, и `aac_at` показывают `Threading capabilities: none`. В parallel mode лучше наоборот держать по процессу простую схему и ограничивать `workers`.

Можно ускорить текущий filter path: если scanner начнёт хранить per-chapter `sample_rate/channels`, то для полностью однородных входов строить:
```text
[0:a][1:a][2:a]concat=n=3:v=0:a=1[aout]
```
без `aformat`. Выигрыш небольшой, обычно `0-10%`, потому что главный расход — AAC encode.

Другие мелочи: в child encode всегда `-map 0:a:0 -vn -sn -dn -map_metadata -1`; intermediates не требуют `+faststart`; финальный `.m4b` требует. `-moov_size` как замена `+faststart` теоретически убирает второй проход, но риск позднего fail при недооценке moov для 26 ч AAC выше пользы.

**Конкретные изменения в `build_m4b.py`**
Добавить:
- `_audio_encoder_args(params, encoder)` → `aac` / `aac_at cbr`.
- `_run_ffmpeg_progress(argv, reason_on_fail, book_id, total_ms, progress_cb)`.
- `_terminate_ffmpeg_many(children)` → SIGTERM всем, общий grace 3 c, SIGKILL оставшимся, reap всех.
- `_ParallelFfmpegPool` или функции `_run_parallel_encode_jobs(...)`.
- `_plan_encode_groups(chapters, sources, workers)` с балансировкой по `duration_ms`.
- `_build_chunk_encode_argv(...)`, `_build_final_copy_mux_argv(...)`.
- `_build_with_parallel_chunks(...)`.
- `_ffmetadata_text(..., durations_ms_override=None)` для фактической AAC timeline.
- `_probe_fragment_duration_ms(path)` и `_validate_fragment_streams(...)`.
- `_ensure_free_space(..., strategy)` или multiplier: parallel требует примерно `2.1-2.4x estimate`, потому что одновременно лежат fragments + final tmp.
- `_cleanup_temp_tree(path)`; `dispatcher._cleanup_build_temps` должен удалять не только файлы, но и hidden temp-директории через `shutil.rmtree`.

Переписать/расширить:
- `_run_ffmpeg` оставить wrapper-совместимым, но внутри использовать progress runner.
- `_build_with_filter` / `_build_with_demuxer` принимать `encoder` и `progress_cb`.
- `build(manifest, out_path=None, progress_cb=None)` выбирать стратегию: `1 глава → single`; `parallel enabled && chapters>=2 → chunks`; иначе current.
- `dispatcher._real_build` передаёт callback, который throttled обновляет manifest/state.

**Гарантии корректности**
Валидный `.m4b`: финальный mux всегда `-f ipod -movflags +faststart`, audio `-c:a copy` только из унифицированных AAC-LC `.m4a`, главы из FFMETADATA через `-map_chapters 1`, обложка `-c:v mjpeg -disposition:v attached_pic`.

Атомарность: общий hidden temp root рядом с output, например `.<name>.<token>.chunks/`, финальный output `.<name>.<token>.tmp`, затем `os.replace`. Любой fail/cancel/timeout: kill всех детей, rmtree temp root, unlink final tmp/meta/list/filter. `recover_interrupted` должен подметать `.<name>.*` рекурсивно.

Gapless: single-encode гарантирован лучше всего. Group encode ограничивает риск `K-1` швами. Per-chapter создаёт шов на каждой главе; брать только после QA или как speed mode.

**Отмена, прогресс, SR**
Отмена: хранить список всех live `Popen`; poll каждые `0.3s`; при cancel для `book_id` гасить **только своих** детей из списка, без поиска чужих pid. При fail одного чанка: fail-fast, убить остальных, `BuildError("ffmpeg_chunk_failed", "chapter 12 file.mp3: ...")`.

Progress: запускать ffmpeg с `-progress pipe:1`, парсить `out_time_us`; `out_time_ms` у ffmpeg исторически тоже микросекунды, так что делить на 1000. Для parallel:
`encode_frac = sum(clamp(job_out_ms, 0, job_total_ms)) / total_ms`.
Пока encode: `percent = 90 * encode_frac`. На финальном mux: `percent = 90 + 10 * mux_out_ms / total_ms`. `current_chapter` считать по “эквивалентному” накопленному ms и cumulative chapter ends; `eta_s = elapsed_s * (100/percent - 1)` после малого порога, иначе `null`.

SR/качество: использовать текущую логику `_samplerate(params, manifest["source_samplerate"])`; дефолт остаётся source SR, явные 44100/48000 побеждают. Все chunks обязаны получать один `SR/channels/bitrate`, дефолт `192k`.
