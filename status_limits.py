#!/usr/bin/env python3
"""
Статус-бар для Claude Code: Ctx + динамические rate-limit бары.

Источники данных:
  stdin JSON  — context_window, rate_limits.five_hour, rate_limits.seven_day
  OAuth API   — дополнительные лимиты (sonnet, opus и др.), кеш 5 минут

Состояние баров кешируется в ~/.claude/status_limits_cache.json. При старте
нового чата, когда stdin ещё не содержит данных, отображаются последние
известные значения из кеша.

Ctx — бар без подписи (восьмушки для плавного края).
Rate-limit бары — название рендерится текстом поверх цветного фона бара.
Ширина баров адаптируется под ширину терминала.
Имя папки проекта — белый хвост строки за пределами этой ширины.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import time as time_module
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Контекстный бар адаптируется под размер окна модели: один символ покрывает
# CTX_TOKENS_PER_CHAR токенов, что даёт стабильную «цену деления» независимо
# от окна. При 50_000 токенов/символ типичный шаг 150k всегда занимает ≈3
# символа: 200k → 4, 500k → 10, 1M → 20. CTX_BAR_MIN_WIDTH страхует на случай
# очень маленьких окон, чтобы бар оставался читаемым.
CTX_TOKENS_PER_CHAR = 50_000
CTX_BAR_MIN_WIDTH   = 6
FIVE_HOUR_SECONDS = 5 * 3600
SEVEN_DAY_SECONDS = 7 * 24 * 3600
API_CACHE_TTL     = 300  # секунд

# TTL после неудачной попытки — короче обычного. Сбои usage API обычно
# транзиентные (типичный случай — Windows не смог проверить отзыв сертификата
# и оборвал TLS), и держать алерт '!API' с пропавшими API-барами все
# API_CACHE_TTL секунд не за что. Сильно уменьшать тоже не стоит: неудачная
# попытка тратит до --max-time секунд прямо в рендере статус-бара.
API_ERROR_CACHE_TTL = 60  # секунд

# Параметры межпроцессного лока на кеш-файл. Лок берётся через O_CREAT|O_EXCL
# — единственный кроссплатформенный без зависимостей способ. LOCK_TIMEOUT
# страхует от мёртвых локов (процесс упал, не освободив): если файл-лок
# старше этого значения, считаем его просроченным и перезахватываем.
LOCK_TIMEOUT          = 5.0    # секунд — после этого лок считается мёртвым
LOCK_ACQUIRE_TIMEOUT  = 2.0    # секунд — сколько ждать на захват
LOCK_RETRY_INTERVAL   = 0.05   # секунд между попытками

# Запасная версия Claude Code для User-Agent, если stdin её не содержит и в кеше
# тоже нет (например, первый запуск). Реальная версия приходит в stdin-JSON
# в поле "version" и кешируется в status_limits_cache.json.
FALLBACK_CLAUDE_VERSION = '2.1.81'

# Размер буфера autocompact в токенах — фиксированная величина, не зависящая
# от размера контекстного окна. Claude Code резервирует этот буфер и запускает
# компрессию при достижении (context_window_size - AUTOCOMPACT_BUFFER_TOKENS).
# Для 200K окна буфер ≈ 16.5%, для 1M — ≈ 3.3%.
AUTOCOMPACT_BUFFER_TOKENS: int = 33_000

# Порог «дорогого» контекста в токенах. Сам Claude Code в своих сообщениях
# выделяет границу >150K (например, "71% of your usage was at >150k context"),
# и это намёк, что выше этого значения применяются повышенные тарифы / нагрузка.
# Переходя порог, контекстный бар окрашивается в янтарный — тот же сигнал,
# что и при входе в autocompact-буфер.
CTX_AMBER_THRESHOLD_TOKENS: int = 150_000

# Порог «опасного» контекста — двойное превышение янтарного. На больших окнах
# (400K+ и тем более 1M) autocompact срабатывает очень поздно, поэтому без
# отдельного сигнала легко не заметить, что контекст давно вышел за разумный
# по стоимости/задержке размер. Превышение даёт красный, который имеет приоритет
# над янтарным.
CTX_RED_THRESHOLD_TOKENS: int = CTX_AMBER_THRESHOLD_TOKENS * 2

# Если установлена CLAUDE_AUTOCOMPACT_PCT_OVERRIDE, Claude Code использует её
# как порог запуска компрессии (процент от окна), что неявно меняет размер буфера.
_autocompact_pct_override: str | None = os.environ.get('CLAUDE_AUTOCOMPACT_PCT_OVERRIDE')

# Поля, получаемые из stdin (не из API). Кортеж, чтобы фиксировать порядок
# отображения базовых баров (5h перед 7d).
STDIN_BAR_FIELDS: tuple[str, ...] = ('five_hour', 'seven_day')

# Короткие ярлыки для уровня thinking-effort из stdin (поле effort.level).
# Отсутствующий в словаре уровень не показывается — лучше не врать, чем показывать
# угадку, если Anthropic добавит новый уровень. Ярлыки обязаны отличаться друг
# от друга: 'max' взят двухсимвольным, иначе он совпал бы с 'medium'.
EFFORT_LABELS: dict[str, str] = {
    'low':    'L',
    'medium': 'M',
    'high':   'H',
    'xhigh':  'xH',
    'max':    'Mx',
}

# ANSI-цвета
FG_RESET = '\033[0m'
FG_LABEL = '\033[38;2;255;255;255m'   # белый: подписи поверх фона баров и имя папки

# Фоновые цвета для четырёх состояний rate-limit баров
BG_BOTH       = '\033[48;2;0;140;45m'      # токены + время (яркий зелёный)
BG_TIME_ONLY  = '\033[48;2;0;80;22m'       # только время (тёмно-зелёный)
BG_TOKEN_ONLY = '\033[48;2;190;135;0m'     # только токены (янтарный)
BG_EMPTY      = '\033[48;2;55;55;55m'      # пусто (тёмно-серый)

# Палитра контекстного бара.
# Заливка использует один цвет на весь бар (выбирается по уровню тревоги:
# нейтральный/янтарный/красный). Пустая часть подсвечена «зональным» фоном,
# отражающим близость к порогам срабатывания: до CTX_AMBER_THRESHOLD_TOKENS
# фон нейтрально-серый, между янтарным и красным порогом — с лёгким тёплым
# оттенком, после красного — с лёгким красноватым. Тинты подобраны очень
# слабыми (~ту же светлоту, что у нейтрального BG_EMPTY=(55,55,55)), чтобы
# не «зашумлять» бар, но позволять оценить удалённость до следующей границы.
FG_CTX_FILLED   = '\033[38;2;192;192;192m'  # норма, заливка серая
FG_CTX_WARN     = '\033[38;2;190;135;0m'    # warn, заливка янтарная
FG_CTX_DANGER   = '\033[38;2;150;35;35m'    # danger, заливка красная (тот же оттенок, что у TIER_RGB[3])
BG_EMPTY_AMBER  = '\033[48;2;65;47;16m'     # пусто, зона 150–300k токенов (приглушённый янтарный)
BG_EMPTY_DANGER = '\033[48;2;64;28;28m'     # пусто, зона >300k токенов (приглушённый красный)

# Единая 4-уровневая шкала фоновых цветов «slate → green → amber → red».
# Применяется и к категориям моделей, и к уровням effort: Unknown ≡ L (slate),
# Haiku ≡ M (green), Sonnet ≡ H (amber), Opus/Fable ≡ xH (red). Один и тот же
# уровень интенсивности на обеих осях окрашивается одинаково — взгляд сразу
# видит «насколько мощно/энергично». Палитра отличается от rate-limit баров
# (тёмно-серый пустого участка), чтобы блок «модель/контекст/effort»
# визуально отделялся от баров.
# Хранится RGB-кортежами, а не готовыми ANSI-кодами: из цвета фона дополнительно
# вычисляется цвет маркера отличия от дефолта (смесь белого с фоном,
# доля белого — MARKER_WHITE_ALPHA).
TIER_RGB: tuple[tuple[int, int, int], ...] = (
    (80, 90, 100),   # 0: slate
    (40, 110, 70),   # 1: green
    (190, 130, 0),   # 2: amber
    (150, 35, 35),   # 3: red (тот же оттенок, что у FG_CTX_DANGER)
)

# Фон для неизвестной модели и нейтральных бейджей (например, размера контекста).
# Совпадает с уровнем 0 шкалы, чтобы Unknown визуально стыковался с L.
RGB_UNKNOWN = TIER_RGB[0]

# Сопоставление подстроки в имени модели с цветом фона бейджа.
# Подстроки берутся в нижнем регистре; имя модели матчится через `.lower()`.
MODEL_TIER_RGB: dict[str, tuple[int, int, int]] = {
    'haiku':  TIER_RGB[1],
    'sonnet': TIER_RGB[2],
    'opus':   TIER_RGB[3],
    'fable':  TIER_RGB[3],
}

# Сопоставление сырого уровня effort из stdin с цветом фона бейджа.
EFFORT_RGB: dict[str, tuple[int, int, int]] = {
    'low':    TIER_RGB[0],
    'medium': TIER_RGB[1],
    'high':   TIER_RGB[2],
    'xhigh':  TIER_RGB[3],
    'max':    TIER_RGB[3],
}

# Символ маркера «значение отличается от глобального дефолта». Рисуется в обеих
# паддинг-ячейках бейджа (слева и справа от текста) — ширина бейджа не меняется.
MARKER_CHAR = '!'

# Доля белого в цвете маркера. Маркер рисуется «полупрозрачным белым» — смесью
# белого с фоном бейджа: 1.0 — чисто белый, 0.0 — сливается с фоном.
MARKER_WHITE_ALPHA = 0.65

# Цвет текстового алерта '!DEF' после бейджей — тот же красный, что у фона
# бейджей верхнего tier (Opus/Fable, xH).
RGB_ALERT = TIER_RGB[3]

# Сопоставление префикса имени API-поля с (краткое обозначение периода, длительность в секундах).
# Имя поля разбирается как <prefix>_<suffix>: префикс задаёт период, суффикс — название категории.
# Поле без известного префикса игнорируется — мы не знаем, какой у него период,
# и не можем корректно посчитать процент прошедшего времени.
PERIOD_PREFIXES: dict[str, tuple[str, int]] = {
    'five_hour': ('5h', FIVE_HOUR_SECONDS),
    'seven_day': ('7d', SEVEN_DAY_SECONDS),
}

# Сопоставление поля group записи массива limits (новый формат usage API)
# с префиксом имени поля бара. Через префикс scoped-запись превращается в
# синтетическое имя вида 'seven_day_fable', которое дальше обрабатывается
# существующим parse_field. Запись с неизвестной группой пропускается —
# неизвестен период окна.
LIMIT_GROUP_PREFIXES: dict[str, str] = {
    'session': 'five_hour',
    'weekly':  'seven_day',
}


def bg_escape(rgb: tuple[int, int, int]) -> str:
    """ANSI-код true-color фона."""
    return f'\033[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m'


def fg_escape(rgb: tuple[int, int, int]) -> str:
    """ANSI-код true-color цвета текста."""
    return f'\033[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m'


def render_badge(text: str, bg_rgb: tuple[int, int, int], marked: bool) -> tuple[str, str]:
    """
    Собирает цветной бейдж с паддингами: ' text ', а при marked — '!text!':
    маркеры занимают существующие паддинг-ячейки, ширина бейджа не меняется.

    Терминал не поддерживает альфа-канал, поэтому «полупрозрачный белый» для
    маркера предвычисляется как смесь белого (доля MARKER_WHITE_ALPHA) с цветом
    фона бейджа — цвет маркера автоматически согласуется с любым фоном.

    Возвращает (rendered, plain): вариант с ANSI-кодами и plain-вариант той же
    видимой ширины для расчёта раскладки.

    - text:   текст бейджа (без ANSI-кодов)
    - bg_rgb: цвет фона бейджа
    - marked: True — значение отличается от глобального дефолта
    """
    bg = bg_escape(bg_rgb)
    if marked:
        marker_fg = fg_escape(tuple(round(c + (255 - c) * MARKER_WHITE_ALPHA) for c in bg_rgb))
        rendered = f'{bg}{marker_fg}{MARKER_CHAR}{FG_LABEL}{text}{marker_fg}{MARKER_CHAR}{FG_RESET}'
    else:
        rendered = f'{bg}{FG_LABEL} {text} {FG_RESET}'
    return rendered, f' {text} '


def parse_field(field: str) -> tuple[str, int] | None:
    """
    Разбирает имя API-поля в (label, period_seconds).

    - <prefix> без суффикса → подпись = только обозначение периода ('5h', '7d').
    - <prefix>_<suffix>     → подпись = '<Suffix Title> <период>' (например, 'Sonnet 7d').
    - неизвестный префикс  → None (поле пропускается рендером).

    - field: имя поля API (например, 'seven_day_sonnet').
    """
    for prefix, (period_label, period_seconds) in PERIOD_PREFIXES.items():
        if field == prefix:
            return period_label, period_seconds
        if field.startswith(prefix + '_'):
            suffix = field[len(prefix) + 1:].replace('_', ' ').title()
            return f'{suffix} {period_label}', period_seconds
    return None


def make_ctx_bar(
    percentage: float | None,
    width: int,
    level: str = 'normal',
    amber_at: float | None = None,
    danger_at: float | None = None,
) -> str:
    """
    Бар контекстного окна с субсимвольным краем через восьмушки, без подписи.
    Заливка одноцветная (по уровню тревоги). Фон пустых ячеек тонирован зонами,
    показывая, насколько далеко до следующей границы срабатывания цвета.

    - percentage: процент заполненности (0–100 или None); значения >100 обрезаются до 100
    - width:      ширина бара в символах
    - level:      уровень тревоги для заполненной части:
                    'normal' — серая,
                    'warn'   — янтарная (вход в autocompact или > CTX_AMBER_THRESHOLD_TOKENS),
                    'danger' — красная (> CTX_RED_THRESHOLD_TOKENS).
                  Неизвестное значение трактуется как 'normal'.
    - amber_at:   позиция (в долях символа) перехода фона в янтарный тинт; None — без зоны
    - danger_at:  позиция (в долях символа) перехода фона в красноватый тинт; None — без зоны
    """
    if level == 'danger':
        fg = FG_CTX_DANGER
    elif level == 'warn':
        fg = FG_CTX_WARN
    else:
        fg = FG_CTX_FILLED

    # None трактуем как «процент неизвестен» — рисуем только пустой фон с зонами.
    if percentage is None:
        full_cells   = 0
        eighth_char: str | None = None
    else:
        percentage = min(100.0, percentage)
        fill_exact = percentage / 100.0 * width
        full_cells = int(fill_exact)
        remainder  = fill_exact - full_cells
        if full_cells < width and remainder > 0:
            eighth_char = '▏▎▍▌▋▊▉█'[max(0, round(remainder * 8) - 1)]
        else:
            eighth_char = None

    cells: list[str] = []
    for i in range(width):
        # Зональный фон: в какой диапазон попадает середина ячейки i.
        # Используем (i + 0.5), чтобы граница ложилась между символами,
        # а не отрезала первую ячейку зоны раньше времени.
        cell_center = i + 0.5
        if danger_at is not None and cell_center >= danger_at:
            bg = BG_EMPTY_DANGER
        elif amber_at is not None and cell_center >= amber_at:
            bg = BG_EMPTY_AMBER
        else:
            bg = BG_EMPTY

        if i < full_cells:
            # Полная ячейка: BG не виден за █, но указываем для случая, если
            # терминал склеит соседние пустые/полные сегменты.
            cells.append(f'{bg}{fg}█{FG_RESET}')
        elif eighth_char is not None and i == full_cells:
            cells.append(f'{bg}{fg}{eighth_char}{FG_RESET}')
        else:
            cells.append(f'{bg} {FG_RESET}')

    return ''.join(cells)


def make_bar(
    token_pct: float | None,
    time_pct: float | None,
    width: int,
    label: str = '',
) -> str:
    """
    Двойной бар с подписью поверх фона.

    Фон кодирует время (тёмно-зелёный = время прошло, тёмный = нет).
    Символ █ кодирует токены (зелёный = в норме, жёлтый = перерасход).
    Подпись рисуется белым текстом поверх первых символов бара.

    - token_pct: процент токенов (0–100 или None)
    - time_pct:  процент прошедшего времени (0–100 или None)
    - width:     ширина бара в символах
    - label:     подпись, рендерится поверх левого края бара
    """
    token_filled = round((token_pct or 0) / 100.0 * width)
    time_filled  = round((time_pct  or 0) / 100.0 * width)

    # Подпись — слева, с отступом по одному пробелу с каждой стороны
    label_str = (' ' + label + ' ') if label else ''
    label_len = min(len(label_str), width)

    # Процент — справа, с отступом в один пробел от края
    pct_str   = f'{round(token_pct or 0)}% '
    pct_len   = min(len(pct_str), width)
    pct_start = width - pct_len

    cells = []

    for i in range(width):
        t = i < token_filled
        m = i < time_filled

        if t and m:
            bg = BG_BOTH
        elif m:
            bg = BG_TIME_ONLY
        elif t:
            bg = BG_TOKEN_ONLY
        else:
            bg = BG_EMPTY

        if i < label_len:
            cells.append(f'{bg}{FG_LABEL}{label_str[i]}{FG_RESET}')
        elif i >= pct_start:
            cells.append(f'{bg}{FG_LABEL}{pct_str[i - pct_start]}{FG_RESET}')
        else:
            cells.append(f'{bg} {FG_RESET}')

    return ''.join(cells)


def time_pct_from_unix(resets_at: int | None, period_seconds: int) -> float | None:
    if resets_at is None:
        return None
    period_start = resets_at - period_seconds
    elapsed = time_module.time() - period_start
    return max(0.0, min(elapsed / period_seconds * 100.0, 100.0))


def iso_to_unix(iso: str | None) -> int | None:
    """
    Переводит ISO-время в unix-секунды.

    Возвращает None для пустого значения или непарсибельной строки.

    - iso: время в формате ISO 8601 (или None)
    """
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except (ValueError, OSError):
        return None


def read_cache(cache_path: Path) -> dict:
    """Читает кеш состояния баров с диска."""
    try:
        return json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def write_cache(cache_path: Path, cache: dict) -> None:
    """
    Атомарно записывает кеш на диск через write-to-tmp + os.replace.

    Атомарность важна, так как кеш разделяется между параллельными
    процессами Claude Code — без неё читатель может увидеть полузаписанный JSON.
    Любые ошибки ввода-вывода игнорируются: кеш не критичен, восстановится
    на следующем запуске.
    """
    try:
        tmp_path = cache_path.with_suffix(cache_path.suffix + '.tmp')
        tmp_path.write_text(json.dumps(cache), encoding='utf-8')
        os.replace(str(tmp_path), str(cache_path))
    except OSError:
        pass


def acquire_lock(lock_path: Path) -> int | None:
    """
    Захватывает межпроцессный лок созданием эксклюзивного файла O_CREAT|O_EXCL.

    Если файл-лок уже существует, ждёт его освобождения до LOCK_ACQUIRE_TIMEOUT.
    Если существующий лок старше LOCK_TIMEOUT — считает его мёртвым (процесс
    упал, не освободив) и перезахватывает.

    Возвращает file descriptor лока (для последующего release_lock) или None
    при таймауте — в этом случае работаем без лока, рискуя потерять одну дельту.

    - lock_path: путь к файлу-локу
    """
    deadline = time_module.monotonic() + LOCK_ACQUIRE_TIMEOUT
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            try:
                age = time_module.time() - lock_path.stat().st_mtime
                if age > LOCK_TIMEOUT:
                    try:
                        lock_path.unlink()
                        continue
                    except OSError:
                        pass
            except OSError:
                pass
            if time_module.monotonic() >= deadline:
                return None
            time_module.sleep(LOCK_RETRY_INTERVAL)
        except OSError:
            return None


def release_lock(fd: int | None, lock_path: Path) -> None:
    """Освобождает лок, закрывая дескриптор и удаляя файл."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        lock_path.unlink()
    except OSError:
        pass


# Алиасы поля model из settings.json, которые невозможно надёжно сопоставить
# с конкретным model.id: их разрешение зависит от типа аккаунта и версии
# Claude Code. Для них маркер отличия от дефолта не показываем.
UNRESOLVABLE_MODEL_ALIASES: frozenset[str] = frozenset({'default', 'best', 'opusplan'})


def load_global_defaults(settings_path: Path) -> tuple[str | None, str | None]:
    """
    Читает глобальные дефолты из ~/.claude/settings.json: поля model и
    effortLevel — именно туда /model и /effort сохраняют «дефолт для новых
    сессий».

    Возвращает (model, effort_level); каждый элемент None, если поле
    отсутствует, имеет не-строковый тип или файл нечитаем.

    - settings_path: путь к settings.json
    """
    try:
        settings = json.loads(settings_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None, None
    model = settings.get('model')
    effort = settings.get('effortLevel')
    return (model if isinstance(model, str) else None,
            effort if isinstance(effort, str) else None)


def split_model_window(saved_model: str) -> tuple[str, int | None]:
    """
    Разбирает значение поля model из settings.json на базовое имя (в нижнем
    регистре) и размер контекстного окна из суффикса '[<N>m]' / '[<N>k]':
    'claude-fable-5[1m]' → ('claude-fable-5', 1_000_000).

    Размер не хардкодится под конкретные окна: '[2m]' даст 2_000_000, и
    проверки продолжат работать при росте окон. Возвращает размер None, если
    суффикса нет или он нераспознан (неизвестная единица, не-число).

    - saved_model: значение поля model (ID или алиас, возможно с суффиксом окна)
    """
    value = saved_model.lower()
    if not value.endswith(']') or '[' not in value:
        return value, None
    base, _, suffix = value[:-1].rpartition('[')
    multipliers = {'m': 1_000_000, 'k': 1_000}
    unit = suffix[-1:]
    if unit in multipliers:
        try:
            return base, round(float(suffix[:-1]) * multipliers[unit])
        except ValueError:
            pass
    return base, None


def model_matches_default(model_id: str, saved_model: str) -> bool | None:
    """
    Проверяет, соответствует ли текущая модель сохранённому глобальному дефолту.

    Сохранённое значение нормализуется через split_model_window (суффикс окна
    отрезается, регистр нижний) и ищется как подстрока в model.id: это покрывает
    и полные ID ('claude-fable-5[1m]' → 'claude-fable-5'), и алиасы ('fable'
    содержится в 'claude-fable-5', 'opus' — в 'claude-opus-4-8').

    Возвращает None, если сопоставление невозможно (алиас из
    UNRESOLVABLE_MODEL_ALIASES) — в этом случае вывод о различии не делается.

    - model_id:    идентификатор текущей модели из stdin (например, 'claude-fable-5')
    - saved_model: значение поля model из settings.json (ID, ID с '[1m]' или алиас)
    """
    base, _ = split_model_window(saved_model)
    if base in UNRESOLVABLE_MODEL_ALIASES:
        return None
    return base in model_id.lower()


def detect_model_tier(model_name: str) -> str | None:
    """
    Возвращает ключ категории модели ('haiku' / 'sonnet' / 'opus' / 'fable')
    по подстроке в имени модели, либо None для неизвестных. Сопоставление
    регистронезависимое.

    - model_name: имя модели для разбора (например, 'Opus 4.7')
    """
    name_lower = model_name.lower()
    for tier in MODEL_TIER_RGB:
        if tier in name_lower:
            return tier
    return None


def folder_label(stdin_data: dict) -> str | None:
    """
    Возвращает имя папки проекта — хвостовой сегмент статуса.

    Длина сознательно не ограничивается: сегмент рендерится за пределами
    бюджета ширины, отданного барам, поэтому лишнее обрезает сам Claude Code
    по фактической ширине терминала. Так бары не платят за длинное имя,
    а короткое видно целиком.

    Источник — `workspace.project_dir`, директория запуска Claude Code: имя
    остаётся стабильным на всю жизнь окна, даже если рабочая директория сессии
    ушла в подпапку. Фоллбэки по убыванию надёжности: `workspace.current_dir`,
    затем cwd процесса — statusLine запускается из директории проекта.

    Значение сознательно не кешируется, в отличие от модели и контекста: кеш
    общий для всех окон Claude Code, и имя чужого проекта в свежем чате
    дезинформировало бы ровно тогда, когда сегмент нужен больше всего —
    при поиске глазами нужного окна.

    Возвращает None, если имя определить не удалось.

    - stdin_data: разобранный JSON из stdin
    """
    workspace = stdin_data.get('workspace')
    sources = (
        [workspace.get('project_dir'), workspace.get('current_dir')]
        if isinstance(workspace, dict) else []
    )

    name = ''
    for source in sources:
        if isinstance(source, str) and source:
            name = Path(source).name
            if name:
                break

    if not name:
        try:
            name = Path.cwd().name
        except OSError:
            return None
    return name or None


def format_context_size(size: int) -> str:
    """
    Форматирует размер контекстного окна компактно: 1_000_000 → '1m',
    200_000 → '200k', 500_000 → '500k'. Используется в префиксе статуса
    рядом с именем модели, чтобы показать реальный размер окна числом,
    а не парсить его из текстового `display_name`.

    - size: размер окна в токенах
    """
    if size >= 1_000_000:
        return f'{size / 1_000_000:g}m'
    if size >= 1_000:
        return f'{size / 1_000:g}k'
    return str(size)


def format_remaining(seconds: float) -> str:
    """
    Компактное представление оставшегося времени единым старшим разрядом:
    Ns / Nm / Nh / Nd. Цель — однозначно передать порядок («осталось минуты,
    а не часы») при минимальной ширине, чтобы приписка влезала даже в узкий
    жёлтый сегмент бара.

    Округление к ближайшему (round-to-nearest) с порогами «.5 единицы до
    следующего разряда»: 89 sec → '1m', 90 sec → '2m', 23 h 40 min → '1d'.
    Так избегаем артефактов вроде «задал ровно 3 дня, увидел 2d» из-за
    микросекундной задержки между вычислением resets_at и time.time().

    - seconds: оставшееся время в секундах (ожидается > 0)
    """
    seconds = max(0.0, seconds)
    if seconds < 59.5:
        return f'{round(seconds)}s'
    minutes = seconds / 60
    if minutes < 59.5:
        return f'{round(minutes)}m'
    hours = minutes / 60
    if hours < 23.5:
        return f'{round(hours)}h'
    days = hours / 24
    return f'{round(days)}d'


def eta_label(label: str, token_pct: float | None, time_pct: float | None, resets_at: int | None) -> str:
    """
    Дописывает к подписи бара '(Nx left)' если расход токенов опережает время —
    т.е. бар окрашен жёлтым. Это сигнал «жжёшь токены быстрее, чем течёт окно»;
    оставшееся время до сброса даёт пользователю чёткий ориентир «сколько ещё терпеть».

    Возвращает исходный label без изменений если:
      - неизвестен token_pct, time_pct или resets_at,
      - token_pct <= time_pct (бар не жёлтый — приписка не нужна),
      - время сброса уже в прошлом (на границе периода до обновления данных).

    - label:     базовая подпись бара (например, '7d')
    - token_pct: процент использованных токенов (0–100 или None)
    - time_pct:  процент прошедшего времени окна (0–100 или None)
    - resets_at: unix-время сброса окна (или None)
    """
    if token_pct is None or time_pct is None or resets_at is None:
        return label
    if token_pct <= time_pct:
        return label
    remaining = resets_at - time_module.time()
    if remaining <= 0:
        return label
    return f'{label} ({format_remaining(remaining)} left)'


def _token_fingerprint(credentials_path: Path) -> str | None:
    """
    Возвращает короткий хэш OAuth-токена для отслеживания его смены.

    Хранится в кеше как _token_fingerprint. Если токен изменился (перезапуск
    Claude Code обновил авторизацию), TTL сбрасывается для немедленного обновления.
    """
    try:
        creds = json.loads(credentials_path.read_text(encoding='utf-8'))
        token = creds['claudeAiOauth']['accessToken']
        return hashlib.sha256(token.encode()).hexdigest()[:16]
    except Exception:
        return None


def maybe_refresh_api(credentials_path: Path, cache: dict, cached_bars: dict, claude_version: str) -> bool:
    """
    Обновляет cached_bars из OAuth usage API если истёк TTL или если OAuth-токен
    изменился с момента последнего запроса. TTL зависит от исхода прошлой попытки:
    API_CACHE_TTL после успеха, укороченный API_ERROR_CACHE_TTL после ошибки.

    Разбирает два формата ответа: плоские поля верхнего уровня со словарём
    {utilization, resets_at} (старый формат) и массив limits со scoped-записями
    по конкретным моделям (новый формат; например, недельный лимит Fable).

    Перед попыткой запроса очищает все API-бары из cached_bars. На успехе заполняет
    актуальными данными. На любой ошибке (недоступен API, ошибка авторизации, таймаут)
    бары остаются пустыми — устаревшие данные не отображаются. TTL обновляется в обоих
    случаях, чтобы не повторять запрос при каждом ходу.

    Побочный эффект: пишет в cache флаг _api_error (True — последняя попытка
    обновления провалилась), по которому рендер показывает алерт '!API'.

    Возвращает True если кеш был изменён, False если TTL ещё не истёк.

    Использует curl вместо urllib — Cloudflare блокирует Python по TLS-отпечатку.

    - credentials_path: путь к файлу с OAuth-токеном
    - cache:            верхний уровень кеша (для хранения _api_cached_at)
    - cached_bars:      словарь баров для обновления
    - claude_version:   версия Claude Code для подстановки в User-Agent
    """
    fingerprint = _token_fingerprint(credentials_path)
    if fingerprint and fingerprint != cache.get('_token_fingerprint'):
        # Токен изменился — сбрасываем TTL для немедленного обновления
        cache['_api_cached_at'] = 0

    # После ошибки повторяем раньше, чем после успеха.
    ttl = API_ERROR_CACHE_TTL if cache.get('_api_error') else API_CACHE_TTL
    if time_module.time() - cache.get('_api_cached_at', 0) < ttl:
        return False

    # Очищаем API-бары до запроса: на ошибке останутся пустыми, на успехе — перезаполнятся
    for field in [f for f in cached_bars if f not in STDIN_BAR_FIELDS]:
        del cached_bars[field]

    try:
        creds = json.loads(credentials_path.read_text(encoding='utf-8'))
        token = creds['claudeAiOauth']['accessToken']
        if fingerprint:
            cache['_token_fingerprint'] = fingerprint
        result = subprocess.run(
            [
                'curl', '-s', '--max-time', '5',
                '-H', f'Authorization: Bearer {token}',
                '-H', 'Accept: application/json',
                '-H', f'User-Agent: Claude-Code/{claude_version}',
                'https://claude.ai/api/oauth/usage',
            ],
            capture_output=True, text=True, timeout=6,
        )
        data = json.loads(result.stdout)
        # API-ошибки (протухший токен, смена схемы авторизации) приходят валидным
        # JSON вида {"type": "error", ...} — без явной проверки они выглядели бы
        # как «успех без баров», и поломка синхронизации оставалась бы незамеченной.
        if not isinstance(data, dict) or data.get('type') == 'error':
            raise ValueError('usage API вернул ошибку')
        for field, value in data.items():
            if not isinstance(value, dict):
                continue
            utilization = value.get('utilization')
            if utilization is None:
                continue
            resets_at_iso = value.get('resets_at')
            # Категория объявлена в API, но окно для пользователя не открыто
            # и использования нет: бар бесполезен, скрываем, чтобы не занимать
            # ширину у значимых баров. Появится автоматически, как только
            # будет фактическое потребление или сервер откроет окно.
            if utilization == 0 and resets_at_iso is None:
                continue
            cached_bars[field] = {'token_pct': utilization, 'resets_at': iso_to_unix(resets_at_iso)}
        # Scoped-лимиты из массива limits (новый формат API): например, недельный
        # лимит конкретной модели (Fable). Записи без scope пропускаются — это
        # агрегаты session / weekly_all, дублирующие stdin-бары five_hour/seven_day.
        for entry in data.get('limits') or []:
            if not isinstance(entry, dict):
                continue
            prefix = LIMIT_GROUP_PREFIXES.get(entry.get('group'))
            scope = entry.get('scope')
            percent = entry.get('percent')
            if prefix is None or percent is None or not isinstance(scope, dict):
                continue
            model_scope = scope.get('model')
            display_name = model_scope.get('display_name') if isinstance(model_scope, dict) else None
            if not display_name:
                continue
            resets_at_iso = entry.get('resets_at')
            # То же правило скрытия, что и у плоских полей: лимит объявлен,
            # но окно не открыто и расхода нет.
            if percent == 0 and resets_at_iso is None:
                continue
            field = f'{prefix}_{display_name.lower().replace(" ", "_")}'
            cached_bars[field] = {'token_pct': percent, 'resets_at': iso_to_unix(resets_at_iso)}
        cache['_api_error'] = False
    except Exception:
        cache['_api_error'] = True
    finally:
        cache['_api_cached_at'] = time_module.time()

    return True


def _get_terminal_width() -> int:
    """
    Возвращает ширину терминала из первого аргумента командной строки.

    Claude Code запускает скрипт без консоли — стандартные методы определения
    ширины недоступны. Ширина задаётся явно в statusLine.command:
        python status_limits.py 144
    """
    if len(sys.argv) > 1:
        try:
            width = int(sys.argv[1])
            if width > 0:
                return width
        except ValueError:
            pass
    return 120


def main() -> None:
    # stdin читается байтами с явным UTF-8: Claude Code сериализует JSON через
    # JSON.stringify и отдаёт сырой UTF-8, а текстовый sys.stdin на Windows
    # берёт системную кодировку (например, cp1251) — кириллица в путях
    # превращалась в мохибейк вида 'РђСЂС….РђР»СЊР±РѕРј'. errors='replace',
    # чтобы одиночный битый байт не обнулял весь stdin вместе с барами.
    try:
        stdin_data = json.loads(sys.stdin.buffer.read().decode('utf-8', errors='replace'))
    except (json.JSONDecodeError, ValueError):
        stdin_data = {}

    home = Path.home()
    cache_path = home / '.claude/status_limits_cache.json'
    lock_path  = home / '.claude/status_limits_cache.json.lock'

    # Все операции с кешем — под общим локом. Несколько параллельных процессов
    # Claude Code делят один кеш; лок страхует от race condition при записи.
    lock_fd = acquire_lock(lock_path)
    try:
        cache = read_cache(cache_path)
        cached_bars     = cache.setdefault('bars', {})
        cache_updated = False

        # Версия Claude Code для User-Agent: stdin → кеш → fallback. Claude Code
        # передаёт актуальную версию в stdin-JSON каждым запуском statusLine,
        # поэтому в нормальном режиме UA всегда соответствует текущему клиенту.
        claude_version = stdin_data.get('version')
        if claude_version:
            if cache.get('claude_version') != claude_version:
                cache['claude_version'] = claude_version
                cache_updated = True
        else:
            claude_version = cache.get('claude_version') or FALLBACK_CLAUDE_VERSION

        # Обновляем API-бары если истёк TTL; API выполняется до обработки stdin
        # rate_limits, чтобы данные из stdin могли перекрыть пересекающиеся поля.
        if maybe_refresh_api(home / '.claude/.credentials.json', cache, cached_bars, claude_version):
            cache_updated = True

        # Контекстное окно: stdin → кеш
        ctx_pct: float | None = None
        ctx_window_size: int | None = None
        ctx_node = stdin_data.get('context_window')
        if isinstance(ctx_node, dict):
            ctx_pct = ctx_node.get('used_percentage')
            ctx_window_size = ctx_node.get('context_window_size')

        if ctx_pct is not None:
            cache['ctx_pct'] = ctx_pct
            cache_updated = True
        else:
            ctx_pct = cache.get('ctx_pct')

        if ctx_window_size is not None:
            cache['ctx_window_size'] = ctx_window_size
            cache_updated = True
        else:
            ctx_window_size = cache.get('ctx_window_size')

        # Имя модели для префикса. Из `model.display_name` отрезаем хвост в скобках
        # (например, "Opus 4.7 (1M context)" → "Opus 4.7"): размер окна мы знаем
        # отдельно как число и не зависим от текстового представления.
        model_node = stdin_data.get('model')
        if isinstance(model_node, dict):
            raw_name = model_node.get('display_name')
            if isinstance(raw_name, str) and raw_name:
                parenthesis_idx = raw_name.find(' (')
                model_name: str | None = raw_name[:parenthesis_idx] if parenthesis_idx > 0 else raw_name
                if cache.get('claude_model_name') != model_name:
                    cache['claude_model_name'] = model_name
                    cache_updated = True
            else:
                model_name = cache.get('claude_model_name')
        else:
            model_name = cache.get('claude_model_name')

        # Идентификатор модели (model.id) — для сравнения с глобальным дефолтом
        # из settings.json. Кешируется по той же схеме, что display_name.
        model_id: str | None = None
        if isinstance(model_node, dict):
            raw_id = model_node.get('id')
            if isinstance(raw_id, str) and raw_id:
                model_id = raw_id
                if cache.get('claude_model_id') != model_id:
                    cache['claude_model_id'] = model_id
                    cache_updated = True
        if model_id is None:
            model_id = cache.get('claude_model_id')

        # Флаг fast mode (`/fast`, ускоренный вывод Opus): верхнеуровневый буль
        # `fast_mode` в stdin. Кешируем, чтобы при пустом stdin показать
        # последнее известное состояние.
        fast_mode = stdin_data.get('fast_mode')
        if isinstance(fast_mode, bool):
            if cache.get('claude_fast_mode') != fast_mode:
                cache['claude_fast_mode'] = fast_mode
                cache_updated = True
        else:
            fast_mode = bool(cache.get('claude_fast_mode'))

        # Уровень thinking-effort из stdin: `effort.level` = 'low'|'medium'|'high'|'xhigh'|'max'.
        # Кешируем сырое значение, чтобы при пустом stdin показать последнее известное.
        effort_node = stdin_data.get('effort')
        if isinstance(effort_node, dict):
            level_raw = (effort_node.get('level') or '').lower()
            if level_raw:
                if cache.get('claude_effort') != level_raw:
                    cache['claude_effort'] = level_raw
                    cache_updated = True
            else:
                level_raw = cache.get('claude_effort') or ''
        else:
            level_raw = cache.get('claude_effort') or ''
        effort_label = EFFORT_LABELS.get(level_raw)

        # five_hour и seven_day: stdin обновляет кеш и перекрывает данные из API
        rate_limits = stdin_data.get('rate_limits') or {}
        for field in STDIN_BAR_FIELDS:
            window = rate_limits.get(field) or {}
            token_pct = window.get('used_percentage')
            resets_at = window.get('resets_at')

            if token_pct is not None or resets_at is not None:
                cached_bars[field] = {'token_pct': token_pct, 'resets_at': resets_at}
                cache_updated = True

        # Rate-limit бары: (label, token_pct, time_pct)
        bars: list[tuple[str, float | None, float | None]] = []

        for field in STDIN_BAR_FIELDS:
            cached = cached_bars.get(field) or {}
            token_pct = cached.get('token_pct')
            resets_at = cached.get('resets_at')

            parsed = parse_field(field)
            if parsed is None:
                continue
            label, period = parsed
            time_pct = time_pct_from_unix(resets_at, period)
            bars.append((eta_label(label, token_pct, time_pct, resets_at), token_pct, time_pct))

        # API-бары: все поля кеша кроме stdin-полей. Поля с неизвестным префиксом
        # пропускаются: без известного периода нельзя посчитать time_pct.
        for field, bar_data in cached_bars.items():
            if field in STDIN_BAR_FIELDS:
                continue
            parsed = parse_field(field)
            if parsed is None:
                continue
            label, period = parsed
            token_pct = bar_data.get('token_pct')
            resets_at = bar_data.get('resets_at')
            time_pct = time_pct_from_unix(resets_at, period)
            bars.append((eta_label(label, token_pct, time_pct, resets_at), token_pct, time_pct))

        if cache_updated:
            write_cache(cache_path, cache)
    finally:
        release_lock(lock_fd, lock_path)

    # Маркер отличия от глобального дефолта: текущие модель и effort сравниваются
    # с полями model / effortLevel из ~/.claude/settings.json. Типичный случай —
    # возобновлённая сессия, оставшаяся на старой модели после смены дефолта
    # (resume сохраняет модель транскрипта и игнорирует настройки). При отличии
    # к тексту бейджа дописывается '*'. Если дефолт не задан или алиас
    # неразрешим — маркер не показывается.
    default_model, default_effort = load_global_defaults(home / '.claude/settings.json')
    model_differs = bool(
        model_id and default_model
        and model_matches_default(model_id, default_model) is False
    )
    effort_differs = bool(level_raw and default_effort) and level_raw != default_effort

    # Контекстное окно меньше дефолтного: сохранённый дефолт явно задаёт размер
    # суффиксом ('[1m]' и т.п.), а сессия работает с меньшим окном — типично для
    # возобновлённой сессии, сохранившей вариант модели без расширенного окна.
    # Обратное направление не проверяем: на Max-планах Opus апгрейдится до
    # расширенного окна автоматически без суффикса в настройках, это штатно.
    default_window = split_model_window(default_model)[1] if default_model else None
    ctx_differs = bool(default_window and ctx_window_size and ctx_window_size < default_window)

    # Суффикс справа от баров: "!Opus 4.7 ⚡! [1m] !xH! !DEF !API" — имя модели
    # (с молнией, если включён fast mode), размер контекстного окна числом и
    # короткий ярлык effort; маркеры '!' в паддинг-ячейках бейджа — значение
    # отличается от глобального дефолта, при хотя бы одном отличии в конце
    # дописывается красный '!DEF'; красный '!API' — последняя попытка обновить
    # usage API провалилась. Любой компонент может отсутствовать. Имя модели и
    # effort окрашиваются фоном по категории (Haiku/Sonnet/Opus/Fable, L/M/H/xH);
    # бейдж размера окна — нейтральный серый.
    # Параллельно копим plain-варианты сегментов: они нужны для расчёта
    # видимой ширины (ANSI-коды в len() не годятся).
    model_info_rendered_parts: list[str] = []
    model_info_plain_parts:    list[str] = []

    # Бейджи модели и effort рендерятся с внутренними пробелами-паддингами
    # (' text '), чтобы цветной фон не прилипал к краю текста. Неизвестная
    # модель/уровень тоже получает бейдж — нейтральный серый.
    if model_name:
        tier = detect_model_tier(model_name)
        rgb = MODEL_TIER_RGB[tier] if tier else RGB_UNKNOWN
        rendered, plain = render_badge(f'{model_name} ⚡' if fast_mode else model_name,
                                       rgb, model_differs)
        model_info_rendered_parts.append(rendered)
        # ⚡ (U+26A1) — широкий символ: терминал рисует его в 2 ячейки, а len()
        # считает за 1. Добавляем пробел в plain-вариант, чтобы расчёт видимой
        # ширины совпадал с фактической.
        model_info_plain_parts.append((plain + ' ') if fast_mode else plain)

    if ctx_window_size:
        # Размер контекстного окна — такой же бейдж, что у модели/effort,
        # но нейтральный (серый фон, белый текст): это не категория, просто число.
        rendered, plain = render_badge(format_context_size(ctx_window_size),
                                       RGB_UNKNOWN, ctx_differs)
        model_info_rendered_parts.append(rendered)
        model_info_plain_parts.append(plain)

    if effort_label:
        rendered, plain = render_badge(effort_label,
                                       EFFORT_RGB.get(level_raw, RGB_UNKNOWN),
                                       effort_differs)
        model_info_rendered_parts.append(rendered)
        model_info_plain_parts.append(plain)

    # Текстовый алерт «сессия отличается от глобального дефолта»: красный '!DEF'
    # через пробел после бейджей, если отличается хотя бы один из трёх
    # параметров. Дублирует маркеры в паддингах бейджей укрупнённым сигналом.
    if model_differs or ctx_differs or effort_differs:
        model_info_rendered_parts.append(f' {fg_escape(RGB_ALERT)}!DEF{FG_RESET}')
        model_info_plain_parts.append(' !DEF')

    # Текстовый алерт «не удалось обновить данные usage API»: красный '!API'.
    # Без него ошибка синхронизации неотличима от «просто нет дополнительных
    # баров», и пропажу API-лимитов легко долго не замечать.
    if cache.get('_api_error'):
        model_info_rendered_parts.append(f' {fg_escape(RGB_ALERT)}!API{FG_RESET}')
        model_info_plain_parts.append(' !API')

    # Сегменты склеиваются без пробелов-разделителей: у каждого бейджа уже
    # есть свои внутренние паддинги, сплошной ряд смотрится как единый блок.
    model_info_text        = ''.join(model_info_rendered_parts)
    model_info_visible_len = sum(len(p) for p in model_info_plain_parts)

    # Адаптивная ширина.
    # Итоговая строка: ctx_bar + N_bars×bar + suffix + (separators по одному)
    # → bar_width = (terminal_width - ctx_bar_width - suffix - N) / N
    # Имя папки в расчёте не участвует: оно дописывается хвостом за пределами
    # terminal_width (см. ниже), поэтому не отбирает ширину у баров.
    # ctx_bar_width зависит от размера контекстного окна модели: один символ
    # покрывает CTX_TOKENS_PER_CHAR токенов, поэтому 150k токенов всегда
    # выглядят одной и той же шириной независимо от окна. ceil-деление через
    # (-(-a // b)) — без импорта math.
    terminal_width    = _get_terminal_width()
    n                 = max(1, len(bars))
    model_info_width  = model_info_visible_len + (1 if model_info_visible_len > 0 else 0)
    if ctx_window_size:
        ctx_bar_width = max(CTX_BAR_MIN_WIDTH, -(-ctx_window_size // CTX_TOKENS_PER_CHAR))
    else:
        ctx_bar_width = CTX_BAR_MIN_WIDTH
    remaining         = terminal_width - ctx_bar_width - model_info_width - n * 1
    bar_width         = max(8, remaining // n)
    last_bar_extra    = remaining - bar_width * n

    # Масштабируем ctx_pct относительно реально доступного окна (за вычетом буфера).
    # В кеше остаётся сырое значение; корректировка только для рендера.
    # Буфер autocompact — фиксированные 33k токенов. Процент зависит от размера окна:
    # 200K → 16.5%, 1M → 3.3%. Если CLAUDE_AUTOCOMPACT_PCT_OVERRIDE задан,
    # он определяет порог запуска (процент от окна), буфер = 100 - порог.
    if _autocompact_pct_override is not None:
        try:
            buffer_pct = 100.0 - float(_autocompact_pct_override)
        except ValueError:
            buffer_pct = AUTOCOMPACT_BUFFER_TOKENS / (ctx_window_size or 200_000) * 100.0
    else:
        buffer_pct = AUTOCOMPACT_BUFFER_TOKENS / (ctx_window_size or 200_000) * 100.0

    usable_pct = 100.0 - buffer_pct
    ctx_display_pct = min(100.0, ctx_pct / usable_pct * 100.0) if ctx_pct is not None else None

    # Вывод. Цвет заполненной части бара выбирается по трёхуровневой эскалации:
    #   danger (красный) — контекст превысил CTX_RED_THRESHOLD_TOKENS (300k);
    #   warn   (янтарный) — контекст вошёл в autocompact-буфер ИЛИ превысил
    #                       CTX_AMBER_THRESHOLD_TOKENS (150k);
    #   normal (серый)    — всё остальное.
    # Приоритет: danger > warn > normal.
    ctx_tokens = (ctx_pct / 100.0 * ctx_window_size) if (ctx_pct is not None and ctx_window_size) else None
    if ctx_tokens is not None and ctx_tokens >= CTX_RED_THRESHOLD_TOKENS:
        ctx_level = 'danger'
    elif (
        (ctx_display_pct is not None and ctx_display_pct >= 100.0)
        or (ctx_tokens is not None and ctx_tokens >= CTX_AMBER_THRESHOLD_TOKENS)
    ):
        ctx_level = 'warn'
    else:
        ctx_level = 'normal'

    # Позиции зональных границ фона. Вычисляем из реальной «цены деления»
    # (usable_tokens / ctx_bar_width), а не из CTX_TOKENS_PER_CHAR — так фон
    # переключается ровно там, где сработает соответствующий уровень цвета.
    # Если окно неизвестно или порог за пределами бара — зону не рисуем.
    if ctx_window_size:
        usable_tokens   = ctx_window_size * usable_pct / 100.0
        tokens_per_char = usable_tokens / ctx_bar_width
        amber_at  = CTX_AMBER_THRESHOLD_TOKENS / tokens_per_char if tokens_per_char > 0 else None
        danger_at = CTX_RED_THRESHOLD_TOKENS   / tokens_per_char if tokens_per_char > 0 else None
    else:
        amber_at = danger_at = None

    parts: list[str] = [
        make_ctx_bar(ctx_display_pct, ctx_bar_width, level=ctx_level,
                     amber_at=amber_at, danger_at=danger_at)
    ]
    for idx, (label, token_pct, time_pct) in enumerate(bars):
        width = bar_width + (last_bar_extra if idx == len(bars) - 1 else 0)
        parts.append(make_bar(token_pct, time_pct, width, label))

    if model_info_text:
        parts.append(model_info_text)

    # Имя папки проекта — последним, уже за границей terminal_width. Ширину
    # не резервируем и длину не ограничиваем: хвост обрежет сам Claude Code
    # по фактической ширине терминала, зато бары всегда получают полный бюджет.
    # Ведущий пробел даёт двойной отступ от бейджей (второй добавит join):
    # имя без фона иначе читается как продолжение последнего бейджа.
    folder_name = folder_label(stdin_data)
    if folder_name:
        parts.append(f' {FG_LABEL}{folder_name}{FG_RESET}')

    print(' '.join(parts))


if __name__ == '__main__':
    main()
