# mp3-to-m4b — дизайн-токены (человекочитаемо)

> Единый словарь стилей по принятым макетам и бренд-базису. **Все значения сняты
> вербатим из CSS** `design/mockups/*.html` (канон по стилям — `03-confirm-core.html`)
> и таблиц `branding/brand-basics.md`. Машиночитаемая копия — `design/tokens.json`.
> Приложение **безусловно тёмное** (utility, не следует системной теме).
>
> Соглашение об именах повторяет соседа `fb2-to-epub/app/Tokens.swift`
> (namespace `C` цвета · `G` градиенты · `F` шрифты · `Track` трекинг · `M` метрики),
> чтобы разработчик переложил 1-в-1. rgba даны и как `rgba()`, и как `#RRGGBBAA`
> (8-значный hex для `Color(hex:)`).

Источники: `01-setup` · `02-status` · `03-confirm-core` (★ канон) · `04-confirm-cover-states` ·
`05-confirm-states` · `06-grouping-and-split` · `07-queue`.

---

## 1. Цвета (`color.*`)

### 1.1 Фоны / поверхности (`color.bg.*`)
| Токен | Значение | Где |
|---|---|---|
| `color.bg.void` (=`.page`) | `#050709` | рабочий стол за окном (`body`) — только в макетах |
| `color.bg.app` | `#0E1A22` | brand-basics: UI-поверхности / тайл лого *(в макетах канва окна стартует с `#0E1822` — см. §7)* |
| `color.bg.tile` | `#0B1118` | brand-basics: подложка-диск под play |
| `color.bg.input` | `#0a1018` | поля ввода, install-box, field-input |
| `color.bg.card` | `#11161d` | карточки status/queue (`.card`, `.qrow`) |
| `color.bg.cardDeep` | `#0c121a` | вложенные блоки на канве: список глав, бокс качества, cover-skeleton/empty |

### 1.2 Канвы-градиенты (`color.canvas.*`)
| Токен | Тип | Стопы | Форма |
|---|---|---|---|
| `color.canvas.window` | radial | `#14202A 0%` → `#0E1822 38%` → `#0a1018 64%` → `#070B10 100%` | `120% 70% at 50% -6%` (status `90% … -10%`) |
| `color.canvas.appIcon` | radial | `#15212B 0%` → `#0C141C 60%` → `#070B10 100%` | `120% 120% at 50% 28%` (= `gradient.panel` из brand-basics) |

### 1.3 Бренд / акцент (`color.brand.*`, `color.accent.*`)
| Токен | HEX | Роль |
|---|---|---|
| `color.brand.cyan` | `#34E0D2` | старт градиента; hover/active; success/teal-точки |
| `color.brand.teal` | `#22B5E0` | середина; основной audio-тон; **primary action**; accent-подсветка |
| `color.brand.indigo` | `#4A6BFF` | конец; **ссылки**; прогресс/заполнение; count-badge |
| `color.accent.tealText` | `#34E0D2` | цвет ссылок-действий в UI (`.apply-all`, `.link`, `.qbtn`, `.recheck`, `.confirm-all`) |
| `color.accent.linkBlue` | `#5B9DF9` | ссылка GitHub в кредит-подвале (`.credit a`) |

### 1.4 Текст — лестница серых (`color.text.*`)
В макетах **8 оттенков** холодно-серого. Документированы все с точкой применения.
| Токен | HEX | Роль |
|---|---|---|
| `color.text.high` | `#EAF6FA` | основной текст, заголовки, значения |
| `color.text.soft` | `#cfe0e7` | текст на ghost/вторичных кнопках (`.btn-ghost`, `.cv-btn`, `.empty h3`) |
| `color.text.muted` | `#9fb2bd` | приглушённый средний (`.q-counter`, badge sub, conv-sub, pager) |
| `color.text.mutedAlt` | `#8a99a3` | текст skip-кнопки (`.btn-skip`) |
| `color.text.secondary` | `#7e93a0` | **вторичный по умолчанию**: подзаголовки, `.lbl`, `.caption`, `.hero-sub`, `.q-name` |
| `color.text.tertiary` | `#6E8390` | caps-микроподписи (`.sec-cap/.cap/.stage-label`), chevron *(= brand-basics `text.muted`)* |
| `color.text.quaternary` | `#5a6b76` | очень тихий: `.ch-n`, `.q-suffix`, credit-text, disabled-иконки |
| `color.text.placeholder` | `#4a5862` | плейсхолдер input |

Текст на акценте/обложке: `color.text.onAccent` `#06121a` (на ярком градиенте — кнопки) ·
`color.text.onAccentHigh` `#EAFBFF` (блик/play-верх/cover-badge) · `color.text.onAccentLow`
`#DDFBFF` (play-низ) · `color.text.onCover` `#ffffff` · `color.text.onCoverSub` `rgba(255,255,255,.85)`.

### 1.5 Границы (`color.border.*`) — hairline-семейство на белом с альфой
| Токен | rgba | hex8 | Где |
|---|---|---|---|
| `color.border.window` | `rgba(255,255,255,.07)` | `#FFFFFF12` | контур окна (`.win`) |
| `color.border.card` | `rgba(255,255,255,.06)` | `#FFFFFF0F` | карточки (`.card`, `.qrow`, `.ch-list`) |
| `color.border.hairline` | `rgba(255,255,255,.05)` | `#FFFFFF0D` | разделители (header/footer/`.hairline`, q-line top) |
| `color.border.hairlineSoft` | `rgba(255,255,255,.045)` | `#FFFFFF0B` | разделитель строк-книг (`.book`) |
| `color.border.hairlineFaint` | `rgba(255,255,255,.04)` | `#FFFFFF0A` | разделитель строк глав (`.ch`) |
| `color.border.control` | `rgba(255,255,255,.1)` | `#FFFFFF1A` | input, seg, preset, toggle/slider/mini-track, btn |
| `color.border.controlStrong` | `rgba(255,255,255,.12)` | `#FFFFFF1F` | ghost/cv/field/copy/pager-кнопки |
| `color.border.fieldInput` | `rgba(255,255,255,.08)` | `#FFFFFF14` | field-input, quality-box, q-counter, choice, file-strip |
| `color.border.iconBtn` | `rgba(255,255,255,.06)` | `#FFFFFF0F` | icon-btn в шапке |
| `color.border.hairlineSpec` | `rgba(255,255,255,.04)` | `#FFFFFF0A` | внешний spec-ring окна `0 0 0 .5px` |

### 1.6 Заливки контролов (`color.surfaceFill.*`)
| Токен | rgba | Где |
|---|---|---|
| `color.surfaceFill.control` | `rgba(255,255,255,.05)` | кнопки/контролы (`.btn`, `.btn-ghost`, `.cv-btn`, `.preset` off, `.qbtn.ghost`) |
| `color.surfaceFill.controlSoft` | `rgba(255,255,255,.04)` | тише (`.icon-btn`, `.q-counter`, `.empty-ic`) |
| `color.surfaceFill.controlFaint` | `rgba(255,255,255,.03)` | самая тихая (`.choice` rest, `.clear-btn`) |
| `color.surfaceFill.footer` | `rgba(7,11,16,.5)` | фон футера/actions (`.footer`, `.actions`) |
| `color.surfaceFill.barTrack` | `rgba(255,255,255,.07)` | трек stat-бара / ring-track |
| `color.surfaceFill.progressTrack` | `rgba(255,255,255,.08)` | прогресс/slider/mini/toggle-off трек |

### 1.7 Состояния — акцент-tint (`color.state.*`)
Выбор/подсветка строятся на **teal `#22B5E0`** (контролы) и **cyan `#34E0D2`** (бейджи).
| Токен | rgba | Где |
|---|---|---|
| `color.state.accentTintBg` | `rgba(34,181,224,.16)` | **выбранный** пресет/сегмент/cv-btn primary |
| `color.state.accentTint14` | `rgba(34,181,224,.14)` | `.qbtn` fill, step-cur, stat-tint |
| `color.state.accentTint12` | `rgba(34,181,224,.12)` | row-ic teal, recheck, batch-chip, sheet-icon, choice-ic |
| `color.state.accentTint10` | `rgba(34,181,224,.1)` | choice.sel, batch-chip bg |
| `color.state.accentTint07` | `rgba(34,181,224,.07)` | estimate-блок |
| `color.state.accentTint06` | `rgba(34,181,224,.06)` | confirm-all dashed |
| `color.state.accentBorder*` | `.6 / .55 / .5 / .45 / .4 / .3 / .28 / .25` | контуры: выбор `.6`, focus `.55`, badge `.5`, step-cur `.45`, qbtn `.4`, sheet-icon `.3`, pill `.28`, batch `.25` |
| `color.state.focusRing` | `rgba(34,181,224,.14)` | focus-кольцо input `0 0 0 3px` |

Success/teal-бейджи (`#34E0D2` основа): `successTint12` `rgba(52,224,210,.12)` (pill, badge-ok,
row-ic) · `successTint14` `.14` (step-ok, qstatus-ic ok) · `successTint18` `.18` (cover-badge) ·
бордеры `successBorder50/.40/.28`.

Indigo-акценты (`#4A6BFF`): `indigoTint20` (cover-badge web) · `indigoTint12` (row-ic) ·
`indigoTint08` (split-preview) · бордеры `indigoBorder50/.20`.

### 1.8 Danger / ошибки (`color.danger.*`) — из `05-confirm-states` + `01-setup`
| Токен | Значение | Роль |
|---|---|---|
| `color.danger.base` | `#FF6B6B` | иконка/штрих ошибки (step-bad ✕, no-disk), цифра step-bad |
| `color.danger.text` | `#FF8B8B` | текст ошибки (inline-err, step-bad-sub) |
| `color.danger.textSoft` | `#FFB0B0` | cancel-кнопка (`.btn-cancel`) |
| `color.danger.textVerySoft` | `#FFD0D0` | текст на danger primary bbtn |
| `color.danger.tint16/.13/.12/.10` | `rgba(255,99,99,…)` | primary-bbtn `.16`, step-bad-num `.13`, invalid-focus `.12`, banner/cancel `.10` |
| `color.danger.border55/.50/.40/.30` | `rgba(255,99,99,…)` | invalid-input `.55`, primary-bbtn `.50`, step-bad-num `.40`, banner/cancel `.30` |

### 1.9 Warn / предупреждения (`color.warn.*`) — битый файл и т.п.
| Токен | Значение | Роль |
|---|---|---|
| `color.warn.base` | `#FFB454` | warn иконка/текст (битый файл, `qsub.err`, `book.warn`, warn-banner) |
| `color.warn.textSoft` | `#FFE3B8` | текст на warn primary bbtn |
| `color.warn.tint16/.14/.12/.10` | `rgba(255,180,84,…)` | primary-bbtn `.16`, book-cov/qstatus `.14`, qbtn.warn `.12`, banner `.10` |
| `color.warn.border50/.40/.30` | `rgba(255,180,84,…)` | primary-bbtn `.50`, qbtn.warn `.40`, banner `.30` |

> Примечание о семантике ошибок: **danger** (красный `#FF6B6B`) — жёсткие ошибки/валидация
> (нет ffmpeg, нет места, пустое название, отмена). **warn** (янтарный `#FFB454`) —
> восстановимые/частичные (битый mp3 «собрать без неё»). Это два разных семантических цвета,
> не оттенки одного.

---

## 2. Градиенты (`gradient.*`)
| Токен | Стопы | Угол | Где |
|---|---|---|---|
| `gradient.brand` | `#34E0D2 0%` → `#22B5E0 48%` → `#4A6BFF 100%` | 135° | обложка (`.cover-grad`), mini-cover *(середина 48%, brand-basics 46% — §7)* |
| `gradient.brandButton` | `#34E0D2 0%` → `#22B5E0 45%` → `#4A6BFF 100%` | 135° | **primary-кнопка «Собрать»/«Продолжить»** |
| `gradient.brandTealIndigo` | `#22B5E0` → `#4A6BFF` | 135° | toggle.on, count-badge, sp-part, slider-fill |
| `gradient.ring` | `#34E0D2 0%` → `#22B5E0 50%` → `#4A6BFF 100%` | linear→stroke | кольцо-прогресс hero + batch-chip ring |
| `gradient.progressFill` | `#34E0D2 0%` → `#22B5E0 60%` → `#4A6BFF 100%` | 90° | линейный прогресс сборки (`.progress-fill`) |
| `gradient.miniProgressFill` | `#34E0D2` → `#4A6BFF` | 90° | мини-прогресс в строке очереди |
| `gradient.barSolid` | `#34E0D2` → `#22B5E0` | 90° | stat-бар «собрано» |
| `gradient.barTealIndigo` | `#22B5E0` → `#4A6BFF` | 90° | stat-бар «за сегодня» |
| `gradient.barDeep` | `#1d9e96` → `#34E0D2` | 90° | stat-бар ffmpeg |
| `gradient.coverSpine` | `rgba(0,0,0,.32)` → `rgba(255,255,255,.1)` → `rgba(0,0,0,0)` | 90° | блик-корешок обложки |
| `gradient.coverFallback` | per-книга, напр. `#5b2a6e → #15102a` | 135/160/200° | **декоративные плейсхолдер-обложки** (не бренд) |

---

## 3. Типографика (`font.*`)

**Семейство:** макеты используют один display-стек на всё —
`-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif`.
Моно — `ui-monospace, "SF Mono", Menlo, monospace` (пути, имена файлов, команды).
Для нативного SwiftUI оба = `.system`.

**Размеры (px), фактический список:** `9 · 9.5 · 10 · 10.5 · 11 · 11.5 · 12 · 12.5 · 13 · 14 ·
15 · 16 · 17 · 18 · 19 · 20 · 21 · 24`.
Ключевые: 9 caps-микроподпись · 11 вторичный/lbl/sub · 11.5 кнопки контролов/длительности ·
12 caption/link/badge · 13 ch-name/row-label/qtitle · 14 input/primary-btn/step-title ·
16–17 h1 шапки · 18 заголовок обложки/sheet h2 · 19 welcome h2 · 20 ring-число · 21 stat-val ·
24 hero-метрика.

**Веса:** `400` regular · `500` medium (автор-инпут, hero-unit) · `600` semibold (кнопки,
caps, поля ввода) · `700` bold (заголовки/значения/caps) · **`800` heavy** (заголовок на обложке).

**Трекинг (letter-spacing, px):** `-0.4` hero-метрика · `-0.3` обложка-заголовок/welcome h2/sheet h2 ·
`-0.2` h1 шапки · `+0.1` credit · `+0.2` pill · `+0.3` cover-badge · `+1.2` caps-микроподпись ·
`+2` stage-label *(служебная)*.
brand-basics задаёт `-2` для вордмарка и `+6` для caps-логотипа — это **только логотип**, в
UI-макетах не применяется (см. §7).

**Межстрочный:** `1.1` h1/обложка (hero-метрика `1`) · `1.15` gen-cell · `1.4` panel-note ·
`1.45` welcome/footnote/empty/sheet-p.

**Tabular-nums:** включён на **всех числах** — счётчики «N из M», длительности `1:12:40`,
проценты, размеры ГБ/МБ, статистика, прогресс.

---

## 4. Отступы и размеры

### 4.1 Spacing-шкала (`space.*`, px, из CSS)
`0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 16 18 20 22 24 26 28 30 40 48`.
Рабочая гамма: **8 / 10 / 11 / 12 / 14 / 16 / 18 / 20 / 24**. Внутренние паддинги окон
H = 18–20, V = 14–18. Вертикальный ритм между карточками = **12**, между смысловыми блоками
в колонке = **18**.

### 4.2 Ширины окон/колонок (`size.window.*`)
| Токен | px | Окно |
|---|---|---|
| `size.window.confirmCore` | **640** | окно подтверждения (ЯДРО) |
| `size.window.states` | 560 | окно состояний (05) |
| `size.window.sheet` | 440 | диалог группировки (06) |
| `size.window.standard` | 400 | status / setup / queue |
| `size.window.panel` | 300 | панели обложек/нарезки |
| `size.window.rightColumn` | **280** | правая колонка окна подтверждения (grid `1fr 280px`) |
| `size.window.rightColumnStates` | 200 | правая колонка окна состояний |

### 4.3 Высоты/размеры контролов (`size.control.*`, `size.ring/progress/thumb/dot.*`)
- input: padding `10×12`, radius 10 · field-input ≈ выс. 38 (padding `9×11`).
- **toggle 40×23**, ползунок 19 (ход left 2→19) · slider track выс. 6, knob 16.
- preset/seg: padding V = 6.
- app-icon: **40** (status/setup) · **34** (confirm-core) · **30** (states); icon-btn 28; back-btn 30; btn-icon 32.
- кольцо hero **104** (r 44, stroke 8, длина 276.5) · batch-ring 16 (r 6, stroke 2.4).
- прогресс: track 8 · mini-track 70×5 · stat-bar выс. 3.
- миниатюры: qcov 38 · book-cov 22 · row-ic 28 · qstatus-ic 26 · step-num 26 · sheet-icon 48 ·
  choice-ic 42 · empty-ic 60 · spinner 30.
- точки: pill/badge 5–6 · foot-dot 7 · pulse 8.
- **обложка везде квадрат 1:1** (`aspect-ratio 1/1`).

### 4.4 Раскладки (`layout.grid.*`)
- body окна подтверждения: `1fr 280px`, gap колонок = **0** (разделены `border-right`).
- окно состояний: `1fr 200px`, gap 18.
- stat-карты: `1fr 1fr 1fr`, gap 8.
- строка главы: `26px minmax(0,1fr) auto`, gap 10 · строка книги: `22px minmax(0,1fr) auto`.
- gen-grid обложек: `1fr 1fr`, gap 8.

---

## 5. Радиусы (`radius.*`)
| Токен | px | Где |
|---|---|---|
| `radius.window` | **16** | окно (`.win`, `.sheet`), hero-card, wizard |
| `radius.sheetIcon` | 13 | sheet-icon, stat-card |
| `radius.card` | **12** | ch-list, quality, cover-box, qrow, banner, choice, toggle-track-pill контекст |
| `radius.estimate` | 11 | estimate-блок, split-preview |
| `radius.control` | **10** | input, btn-ghost, btn-primary, field-input/btn, install-box, recheck, batch-chip, mini-cover |
| `radius.controlSmall` | 9 | app-icon(34), cv-btn, copy-btn, footer-btn, qbtn, back-btn, pager-btn, gen-cell |
| `radius.chip` | 8 | q-counter, icon-btn(28), row-ic, qstatus-ic, app-icon(states 30→8) |
| `radius.small` | 7 | preset, seg, cover-badge, pill, badge-ok |
| `radius.tiny` | 6 | file-chip, clear-btn, ph, autoBadge |
| `radius.barTrack` | 5 | progress/mini-track (slider 4, bar 2) |
| `radius.pill` | 50% / 12px | точки и кружки = 50%; toggle-track = pill 12px |

---

## 6. Границы и тени (`shadow.*`)
**Hairline-границы** — см. §1.5 (всё семейство `rgba(255,255,255,α)`).

| Токен | Значение | Где |
|---|---|---|
| `shadow.window` | `0 30px 70px -22px rgba(0,0,0,.85), 0 0 0 .5px rgba(255,255,255,.04)` | окно 640 |
| `shadow.windowStatus` | `0 24px 60px -20px rgba(0,0,0,.8), 0 0 0 .5px rgba(255,255,255,.04)` | окна 400 |
| `shadow.windowStates` | `0 24px 60px -22px rgba(0,0,0,.85)` | окна 560 |
| `shadow.sheet` | `0 28px 70px -22px rgba(0,0,0,.85)` | диалог 440 |
| `shadow.panel` | `0 20px 50px -20px rgba(0,0,0,.8)` | панели 300 |
| `shadow.appIcon` | `0 5px 14px -4px rgba(34,181,224,.45), inset 0 1px 0 rgba(255,255,255,.18)` | app-icon (34); 40px → `0 6px 16px -4px …` |
| `shadow.cover` | `0 8px 24px -8px rgba(0,0,0,.7)` | обложка (mini → `0 6px 18px -6px …`) |
| `shadow.coverThumb` | `0 2px 6px rgba(0,0,0,.5)` | qcov (38); book-cov(22) → `0 1px 3px …` |
| `shadow.buttonPrimary` | `0 10px 24px -8px rgba(34,181,224,.6), inset 0 1px 0 rgba(255,255,255,.4)` | primary-кнопка (glow + inner) |
| `shadow.toggleKnob` | `0 1px 3px rgba(0,0,0,.4)` | ползунок toggle (slider knob → `0 1px 4px rgba(0,0,0,.5)`) |
| `shadow.genCellSel` | `0 0 0 2px #22B5E0, 0 0 14px rgba(34,181,224,.4)` | выбранная обложка-вариант |
| `shadow.coverTextShadow` | `0 2px 8px rgba(0,0,0,.35)` | text-shadow заголовка обложки |
| `shadow.glowAccent` | `drop-shadow(0 0 6px rgba(34,181,224,.5))` | кольцо-прогресс (fill → `0 0 12px …`) |
| `shadow.glowDot` | `0 0 6/7/8px #34E0D2` | живые точки (pill 6, foot 7, pulse 8) |
| `shadow.focusRingInput` | `0 0 0 3px rgba(34,181,224,.14)` | focus-кольцо input |
| `shadow.focusRingInvalid` | `0 0 0 3px rgba(255,99,99,.12)` | focus-кольцо invalid |
| `shadow.presetInset` / `.choiceInset` | `inset 0 0 0 1px rgba(34,181,224,.3)` | внутренний контур выбранных preset/choice |
| `shadow.heroInset` | `inset 0 1px 0 rgba(255,255,255,.03)` | верхний блик hero-card |

---

## 7. Брейкпоинты, z-уровни, анимация
- **Брейкпоинты:** нативное macOS-приложение, окна фиксированной ширины — **медиа-брейкпоинтов
  в макетах нет**. «Адаптив» = список глав внутренне скроллит (`max-height 470`), переменные
  секции cap'ятся по высоте экрана. Mobile-брейкпоинтов нет.
- **z-уровни (`zIndex.*`):** явных `z-index` в CSS нет. Слоение — порядком потока + `position:absolute`
  внутри относительных контейнеров (cover-badge/spine/check/ring-center/pulse). Sheet S4 (модальный
  диалог группировки) логически перекрывает Status.
- **Анимация (`motion.*`):** toggle `transition .15s` · spinner `1s linear infinite` ·
  pulse `1.2s ease-in-out infinite` (opacity 1↔.35).

---

## 8. Компонентные токены (ключевые элементы)

### Окно подтверждения (★ `03-confirm-core`)
- **Поле ввода `.inp`:** bg `color.bg.input` · border `color.border.control` · radius `radius.control`(10) ·
  padding `10×12` · текст `color.text.high` 14px **600** (автор `.inp.author` = **500**) ·
  focus: border `color.state.accentBorder55` + `shadow.focusRingInput`. Invalid: border
  `color.danger.border55` + `shadow.focusRingInvalid`, плейсхолдер `color.text.placeholder`.
- **Список глав `.ch-list` / `.ch`:** контейнер bg `color.bg.cardDeep` · border `color.border.card` ·
  radius `radius.card`(12) · `min-height 180 / max-height 470` (внутр. скролл). Строка: grid
  `26px 1fr auto` gap 10, padding `9×12`, border-bottom `color.border.hairlineFaint`. Номер `.ch-n`
  11px `color.text.quaternary` tnum · имя `.ch-name` 13px `color.text.high` (hover → `color.brand.cyan`) ·
  длительность `.ch-dur` 11.5px `color.text.secondary` tnum.
- **Превью обложки `.cover-box`:** квадрат 1:1 · radius `radius.card`(12) · border `color.border.fieldInput` ·
  `shadow.cover` · фон `gradient.brand` · заголовок `.ct` 18px **800** `#fff` `shadow.coverTextShadow` ·
  автор `.ca` 12px **600** `rgba(255,255,255,.85)` · корешок `gradient.coverSpine` (ширина 6).
- **Бейдж обложки `.cover-badge`:** padding `4×9` · radius `radius.small`(7) · `backdrop-filter blur(8)`.
  ИЗ ФАЙЛА/СВОЯ: bg `color.state.successTint18` + border `successBorder50`, точка `#34E0D2`.
  ИЗ СЕТИ (`.web`): `color.state.indigoTint20` + `indigoBorder50`, точка `#4A6BFF`. GEN (`.gen`):
  `rgba(255,255,255,.12)` + `.25`. Текст `b` 10px **700** `color.text.onAccentHigh` tracking `+0.3`.
- **Бокс качества `.quality` / `.q-line`:** контейнер border `color.border.fieldInput` · radius
  `radius.card`(12) · bg `color.bg.cardDeep` · padding `11×12`. Строки разделены `q-line + q-line`
  top-border `color.border.hairline`. Имя `.q-name` 11px `color.text.secondary`, ширина 48.
- **Пресет `.preset`:** flex `1`, padding `6×0`, radius `radius.small`(7), border `color.border.control`,
  bg `color.surfaceFill.controlSoft`, текст 11.5px **600** `color.text.muted` tnum. **on:** border
  `color.state.accentBorder60` + bg `color.state.accentTintBg` + текст `color.text.high` + `shadow.presetInset`.
- **Сегмент `.seg button`:** flex `1`, padding `6×2`, текст 11.5px **600** `color.text.muted` tnum,
  разделитель `border-left color.border.control`. **on:** bg `color.state.accentTintBg` + `color.text.high`.
- **Toggle нарезки `.toggle`:** 40×23, radius pill 12, bg off `color.surfaceFill.progressTrack`.
  **on:** `gradient.brandTealIndigo`. Ползунок `i` 19×19 круг `#fff` `shadow.toggleKnob`, ход left 2→19,
  `transition .15s`.
- **Блок оценки `.estimate`:** padding `12×14` · radius `radius.estimate`(11) · bg `color.state.accentTint07` ·
  border `color.state.accentBorder` (`rgba(34,181,224,.18)`). Большое число `.est-big` 17px **700**
  `color.brand.cyan` tnum · подпись `.est-sub` 11px `color.text.secondary`.
- **Кнопки футера `.actions`:** бар padding `14×20`, top-border `color.border.card`, bg
  `color.surfaceFill.footer`.
  - **primary «Собрать»:** padding `10×22` · radius `radius.control`(10) · `gradient.brandButton` ·
    текст `color.text.onAccent`(#06121a) 14px **700** · `shadow.buttonPrimary`. **disabled:** bg
    `rgba(255,255,255,.08)`, текст `color.text.quaternary`, без тени.
  - **ghost «Позже в очередь»:** padding `10×16` · border `color.border.controlStrong` · bg
    `color.surfaceFill.control` · текст `color.text.soft` 13px **600**.
  - **skip «Пропустить»:** ghost + текст `color.text.mutedAlt` + border `color.border.hairlineSpec`(.08).
  - **apply-all:** ссылка `color.accent.tealText` 12px **600**, иконка-чек cyan.
- **Счётчик «1 из 3» `.q-counter`:** padding `5×10` · radius `radius.chip`(8) · bg
  `color.surfaceFill.controlSoft` · border `color.border.fieldInput` · 12px `color.text.muted`,
  число `b` — `color.text.high` **700**.

### Окно состояний (`05`)
- **Баннер `.banner`:** padding `13×16` · radius `radius.card`(12). danger: bg `color.danger.tint10` +
  border `.30`. warn (`.warn`): bg `color.warn.tint10` + border `.30`. Заголовок `.bt-title` 13px **700**
  `color.text.high`, под `.bt-sub` 11.5px `color.text.muted`.
- **`.bbtn`:** padding `7×12` · radius `radius.chip`(8) · 12px **600**. primary danger: `color.danger.tint16`
  + border `.50` + текст `color.danger.textVerySoft`. primary warn: `color.warn.tint16` + `.50` + `color.warn.textSoft`.
- **Прогресс сборки `.progress-track`/`.progress-fill`:** трек выс. 8, radius `radius.barTrack`(5),
  bg `color.surfaceFill.progressTrack`. Заливка `gradient.progressFill` + `shadow.glowAccent`(12px).
  Пульс `.pulse` 8px `#34E0D2` + glow, анимация `motion.pulse`.

### Status (`02`)
- **Кольцо-прогресс hero:** 104×104, r 44, stroke 8, track `color.surfaceFill.barTrack`, дуга
  `gradient.ring` + `shadow.glowAccent`(6px), `stroke-linecap round`, `rotate(-90)`. В центре число
  20px **700** `color.brand.cyan` tnum.
- **Stat-карта `.stat`:** bg `color.bg.card` · border `color.border.card` · radius `radius.sheetIcon`(13) ·
  padding `11×11/12`. Cap 9px **700** `color.text.tertiary` `+1.2`. Значение `.stat-val` 21px **700** tnum
  (цвет per-карта: cyan/indigo). Бар `.bar` выс. 3 radius 2, трек `color.surfaceFill.barTrack`,
  заливка — `gradient.barSolid` / `barTealIndigo` / `barDeep`.
- **Строки группы `.row`:** padding `12×14`, gap 11, иконка `.row-ic` 28 radius 8 (tint per-роль:
  `successTint12` / `indigoTint12` / ...), label 13px `color.text.high`, sub 11px `color.text.secondary` mono.
  Бейдж-ОК: `color.state.successTint12` + `successBorder28`. count-badge: `gradient.brandTealIndigo`,
  min-w 20, h 20, radius 10, 11px **700** `#fff`.
- **Строка книги `.book`:** grid `22px 1fr auto`, border-bottom `color.border.hairlineSoft`. err-вариант:
  имя `color.text.mutedAlt`, `small.warn` = `color.warn.base`, обложка-плашка `color.warn.tint14`.

### Очередь (`07`)
- **`.qrow`:** padding `11×12` · radius `radius.card`(12) · bg `color.bg.card` · border `color.border.card`.
  Обложка `.qcov` 38 radius `radius.chip`(8) `shadow.coverThumb`. Заголовок 13px **600**, sub 11px
  `color.text.secondary` (ok → `color.brand.cyan`, err → `color.warn.base`).
- **`.qbtn`:** padding `7×13` radius `radius.controlSmall`(9) 12px **600** — base `color.state.accentTint14`
  + border `.40` + `color.accent.tealText`; ghost → `color.surfaceFill.control` + `color.text.soft`;
  warn → `color.warn.tint12` + `.40` + `color.warn.base`.
- **batch-chip:** padding `6×11` radius `radius.control`(10) bg `color.state.accentTint10` + border `.25`,
  мини-кольцо 16 (`gradient.ring`), число 12px `color.text.high` tnum.
- **confirm-all:** dashed border `rgba(34,181,224,.35)`, bg `color.state.accentTint06`, `color.accent.tealText`.

### Setup (`01`)
- **step-num:** 26 круг. ok: `color.state.successTint14` + `successBorder40`. bad:
  `color.danger.tint13` + `border40` + текст `color.danger.base`. cur: `color.state.accentTint14` +
  `accentBorder45` + `color.brand.teal`. disabled: `rgba(255,255,255,.05)` + `border control`.
- **install-box:** bg `color.bg.input` + border `color.border.iconBtn`, mono 12px `color.text.high`.
- **recheck:** border `color.state.accentBorder40` + bg `color.state.accentTint12` + `color.accent.tealText` 12.5px **600**.

### Группировка / нарезка (`06`)
- **sheet-icon:** 48 radius `radius.sheetIcon`(13) bg `color.state.accentTint12` + border `.30`.
- **`.choice`:** padding `14×16` radius `radius.card`(12) bg `color.surfaceFill.controlFaint` + border
  `color.border.fieldInput`. **sel:** border `color.state.accentBorder60` + bg `color.state.accentTint10` +
  `shadow.choiceInset`. Радио-кнопка 20 круг, активное — `color.brand.teal` заливка.
- **split-preview:** padding `12×14` radius `radius.estimate`(11) bg `color.state.indigoTint08` + border
  `indigoBorder20`. Большое число `.sp-big` 16px **700** `color.brand.indigo`. Части `.sp-part`
  `gradient.brandTealIndigo`, текст `color.text.onAccent`.
- **slider:** трек выс. 6 radius 4 `color.surfaceFill.progressTrack`, fill `gradient.brandTealIndigo`,
  knob 16 `#fff` `shadow.sliderKnob`.

### Кредит-подвал (везде)
`.credit` padding `9 16 13`, 11px `color.text.quaternary` tracking `+0.1`; ссылка `.credit a`
`color.accent.linkBlue`(#5B9DF9) **600**.

---

## 9. Находки / риски (расхождения brand-basics ↔ макеты)

> Зафиксированы, **не «угадывались»**. Решение — за Юркой/человеком.

1. **Два «muted»-серого.** brand-basics: `color.text.muted = #6E8390`. В макетах `#6E8390`
   используется только для **caps-микроподписей** (`.sec-cap/.cap`), а самый частый вторичный
   текст — **`#7e93a0`** (`.lbl`, `.caption`, подзаголовки). В токенах разведены: `text.tertiary`
   = `#6E8390` (caps), `text.secondary` = `#7e93a0` (body-secondary). Это согласованная иерархия,
   но имя «muted» из brand-basics указывает на `#6E8390` — стоит закрепить именно такое значение
   за caps и не путать с `#7e93a0`.
2. **`color.bg.app`: `#0E1A22` (brand-basics) vs `#0E1822` (2-я стопа канвы окна в макетах).**
   Разница в одном байте зелёного (1A vs 18). Канва окна — 4-стоповый радиал, отличный от
   `gradient.panel`. Я взял **фактические значения макетов** для `canvas.window`, а `#0E1A22`
   оставил как `color.bg.app` (по brand-basics). Если это должен быть один токен — нужно решение.
3. **Панельный радиал: brand-basics `gradient.panel` 3 стопа `#15212B→#0C141C→#070B10`** (cx50 cy38)
   — в макетах **этим радиалом залита app-иконка в шапке** (`color.canvas.appIcon`, форма
   `at 50% 28%`, не 38%). А **канва окна** — другой, 4-стоповый радиал. То есть «фон панели» из
   brand-basics в UI стал фоном иконки, не окна. Контекст разный; помечаю, не свожу силой.
4. **Середина бренд-градиента: 48% в макете обложки vs 46% в brand-basics.** Кнопка использует 45%.
   Мелочь (1–3%), но три разных значения для «одного» градиента. Для кода предлагаю
   `gradient.brand`=48% (обложка) и `gradient.brandButton`=45% (кнопка) как отдельные токены, как
   и сделано — но если хотим единый стоп, нужно выбрать одно.
5. **Трекинги логотипа ≠ трекинги UI.** brand-basics `font.tracking.caps = +6` и `wordmark = -2`
   относятся **к логотипу**. В UI caps-микроподпись = **+1.2**, заголовки = -0.2…-0.4. Не
   противоречие, но `+6/-2` нельзя тащить в UI-токены — оставил их помеченными как «logo-only».
6. **Page-фон `#050709` есть только в макетах** (рабочий стол за окном), в brand-basics его нет.
   В нативном приложении окна канвенные — `#050709` может вообще не понадобиться (нет «страницы»).
   Помечаю как `bg.void` с пометкой происхождения.
7. **«Серые» обложки-плейсхолдеры** (`#5b2a6e`, `#3a2e6e`, `#1d6e5a`, `#6e2a3a`, `#2a3a6e`…) —
   это **намеренно НЕ бренд**, декоративная per-книга палитра (демо-данные). Не сводил их в
   систему: это контент, а не токены. Вынес в `gradient.coverFallback` с пометкой.
8. **Радиусы app-иконки плавают по экранам:** 11 (status/setup, 40px) · 9 (confirm-core, 34px) ·
   8 (states, 30px). Похоже на «радиус ∝ размер», а не единый токен. Документировал все три;
   если нужен инвариант (напр. `radius ≈ 0.27×сторона`), это решение Юрки.

---

## 10. Сводка покрытия
**~210 токенов** в `tokens.json`, группы:
- `color` — bg(6) · canvas(2) · brand/accent(7) · text(13) · border(10) · surfaceFill(6) ·
  state(28) · danger(13) · warn(11) · onGradient(3).
- `gradient` — 11 + fallback-набор.
- `font` — family · size(18) · weight(5) · tracking(11) · lineHeight(4) · tabularNums.
- `space` — шкала 0…48.
- `radius` — 13.
- `shadow` — 24.
- `size` — window(7) · control(13) · ring(6) · progress(4) · thumb(11) · dot(4) · coverAspect.
- `layout` · `breakpoint` (n/a, объяснено) · `zIndex` (n/a, объяснено) · `motion`(3).
- **Компонентные токены** — для всех ключевых элементов 9 экранов (§8).

Покрыты: ✅ цвета · ✅ типографика · ✅ spacing · ✅ радиусы · ✅ тени · ✅ границы ·
✅ брейкпоинты (обоснованно n/a) · ✅ z-уровни (обоснованно n/a) · ✅ компонентные токены.
