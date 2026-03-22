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
"""

import io
import json
import subprocess
import sys
import time as time_module
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

CTX_BAR_WIDTH     = 6
FIVE_HOUR_SECONDS = 5 * 3600
SEVEN_DAY_SECONDS = 7 * 24 * 3600
API_CACHE_TTL     = 300  # секунд

# Поля, получаемые из stdin (не из API)
STDIN_BAR_FIELDS = {'five_hour', 'seven_day'}

# ANSI-цвета
FG_RESET      = '\033[0m'
FG_GRAY_DARK  = '\033[90m'
FG_GRAY_LIGHT = '\033[37m'
FG_LABEL      = '\033[38;2;255;255;255m'   # белый текст поверх фона баров

# Фоновые цвета для четырёх состояний rate-limit баров
BG_BOTH       = '\033[48;2;0;140;45m'      # токены + время (яркий зелёный)
BG_TIME_ONLY  = '\033[48;2;0;80;22m'       # только время (тёмно-зелёный)
BG_TOKEN_ONLY = '\033[48;2;190;135;0m'     # только токены (янтарный)
BG_EMPTY      = '\033[48;2;55;55;55m'      # пусто (тёмно-серый)

# Фоновый цвет контекстного бара (заполненная часть)
FG_CTX_FILLED = '\033[37m'

# Подписи баров — рендерятся поверх фона, поэтому могут быть длиннее
LABEL_MAP = {
    'context_window':       '',           # Ctx без подписи
    'five_hour':            '5h',
    'seven_day':            '7d',
    'seven_day_sonnet':     'Sonnet 7d',
    'seven_day_opus':       'Opus 7d',
    'seven_day_oauth_apps': 'Apps 7d',
    'seven_day_cowork':     'CoWork 7d',
    'iguana_necktie':       'Iguana',
}

PERIOD_SECONDS = {
    'five_hour':            FIVE_HOUR_SECONDS,
    'seven_day':            SEVEN_DAY_SECONDS,
    'seven_day_sonnet':     SEVEN_DAY_SECONDS,
    'seven_day_opus':       SEVEN_DAY_SECONDS,
    'seven_day_oauth_apps': SEVEN_DAY_SECONDS,
    'seven_day_cowork':     SEVEN_DAY_SECONDS,
    'iguana_necktie':       SEVEN_DAY_SECONDS,
}


def get_label(field: str) -> str:
    return LABEL_MAP.get(field, field.replace('_', ' ').title())


def make_ctx_bar(percentage: float | None, width: int) -> str:
    """
    Бар контекстного окна с субсимвольным краем через восьмушки, без подписи.

    - percentage: процент заполненности (0–100 или None)
    - width:      ширина бара в символах
    """
    if percentage is None:
        return f'{BG_EMPTY}{" " * width}{FG_RESET}'

    fill_exact = percentage / 100.0 * width
    full_cells = int(fill_exact)
    remainder  = fill_exact - full_cells

    cells = []
    if full_cells > 0:
        cells.append(f'{FG_GRAY_LIGHT}{"█" * full_cells}{FG_RESET}')

    if full_cells < width and remainder > 0:
        eighth_index = max(0, round(remainder * 8) - 1)
        cells.append(f'{BG_EMPTY}{FG_GRAY_LIGHT}{"▏▎▍▌▋▊▉█"[eighth_index]}{FG_RESET}')
        empty_start = full_cells + 1
    else:
        empty_start = full_cells

    empty_count = width - empty_start
    if empty_count > 0:
        cells.append(f'{BG_EMPTY}{" " * empty_count}{FG_RESET}')

    return ''.join(cells)


def make_bar(token_pct: float | None, time_pct: float | None, width: int, label: str = '') -> str:
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


def time_pct_from_iso(resets_at_str: str | None, period_seconds: int) -> float | None:
    if not resets_at_str:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str).timestamp()
        period_start = resets_at - period_seconds
        elapsed = time_module.time() - period_start
        return max(0.0, min(elapsed / period_seconds * 100.0, 100.0))
    except (ValueError, OSError):
        return None


def read_cache(cache_path: Path) -> dict:
    """Читает кеш состояния баров с диска."""
    try:
        return json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def write_cache(cache_path: Path, cache: dict) -> None:
    """Записывает кеш состояния баров на диск, игнорируя ошибки."""
    try:
        cache_path.write_text(json.dumps(cache), encoding='utf-8')
    except OSError:
        pass


def maybe_refresh_api(credentials_path: Path, cache: dict, cached_bars: dict) -> bool:
    """
    Обновляет cached_bars из OAuth usage API если истёк TTL (API_CACHE_TTL секунд).

    Перед попыткой запроса очищает все API-бары из cached_bars. На успехе заполняет
    актуальными данными. На любой ошибке (недоступен API, ошибка авторизации, таймаут)
    бары остаются пустыми — устаревшие данные не отображаются. TTL обновляется в обоих
    случаях, чтобы не повторять запрос при каждом ходу.

    Возвращает True если кеш был изменён, False если TTL ещё не истёк.

    Использует curl вместо urllib — Cloudflare блокирует Python по TLS-отпечатку.

    - credentials_path: путь к файлу с OAuth-токеном
    - cache:            верхний уровень кеша (для хранения _api_cached_at)
    - cached_bars:      словарь баров для обновления
    """
    if time_module.time() - cache.get('_api_cached_at', 0) < API_CACHE_TTL:
        return False

    # Очищаем API-бары до запроса: на ошибке останутся пустыми, на успехе — перезаполнятся
    for field in [f for f in cached_bars if f not in STDIN_BAR_FIELDS]:
        del cached_bars[field]

    try:
        creds = json.loads(credentials_path.read_text(encoding='utf-8'))
        token = creds['claudeAiOauth']['accessToken']
        result = subprocess.run(
            [
                'curl', '-s', '--max-time', '5',
                '-H', f'x-api-key: {token}',
                '-H', 'Accept: application/json',
                '-H', 'User-Agent: Claude-Code/2.1.81',
                'https://claude.ai/api/oauth/usage',
            ],
            capture_output=True, text=True, timeout=6,
        )
        data = json.loads(result.stdout)
        for field, value in data.items():
            if not isinstance(value, dict):
                continue
            utilization = value.get('utilization')
            if utilization is None:
                continue
            resets_at_iso = value.get('resets_at')
            try:
                resets_at_unix = int(datetime.fromisoformat(resets_at_iso).timestamp()) if resets_at_iso else None
            except (ValueError, OSError):
                resets_at_unix = None
            cached_bars[field] = {'token_pct': utilization, 'resets_at': resets_at_unix}
    except Exception:
        pass
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
    try:
        stdin_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        stdin_data = {}

    home = Path.home()
    cache_path = home / '.claude/status_limits_cache.json'
    cache = read_cache(cache_path)
    cached_bars = cache.setdefault('bars', {})
    cache_updated = False

    # Обновляем API-бары если истёк TTL; API выполняется первым,
    # чтобы данные из stdin могли перекрыть пересекающиеся поля (five_hour, seven_day)
    if maybe_refresh_api(home / '.claude/.credentials.json', cache, cached_bars):
        cache_updated = True

    # Контекстное окно: stdin → кеш
    ctx_pct: float | None = None
    ctx_node = stdin_data.get('context_window')
    if isinstance(ctx_node, dict):
        ctx_pct = ctx_node.get('used_percentage')

    if ctx_pct is not None:
        cache['ctx_pct'] = ctx_pct
        cache_updated = True
    else:
        ctx_pct = cache.get('ctx_pct')

    # Rate-limit бары: (field, token_pct, time_pct)
    bars: list[tuple[str, float | None, float | None]] = []

    # five_hour и seven_day: stdin обновляет кеш и перекрывает данные из API
    rate_limits = stdin_data.get('rate_limits') or {}
    for field, period in [('five_hour', FIVE_HOUR_SECONDS), ('seven_day', SEVEN_DAY_SECONDS)]:
        window = rate_limits.get(field) or {}
        token_pct = window.get('used_percentage')
        resets_at = window.get('resets_at')

        if token_pct is not None or resets_at is not None:
            cached_bars[field] = {'token_pct': token_pct, 'resets_at': resets_at}
            cache_updated = True
        else:
            cached = cached_bars.get(field) or {}
            token_pct = cached.get('token_pct')
            resets_at = cached.get('resets_at')

        bars.append((field, token_pct, time_pct_from_unix(resets_at, period)))

    # API-бары: все поля кеша кроме stdin-полей
    for field, bar_data in cached_bars.items():
        if field in STDIN_BAR_FIELDS:
            continue
        period = PERIOD_SECONDS.get(field, SEVEN_DAY_SECONDS)
        bars.append((field, bar_data.get('token_pct'), time_pct_from_unix(bar_data.get('resets_at'), period)))

    if cache_updated:
        write_cache(cache_path, cache)

    # Адаптивная ширина.
    # Итоговая строка: ctx_bar + N_bars×bar + N_bars×separator(1)
    # → bar_width = (terminal_width - CTX_BAR_WIDTH - N) / N
    terminal_width = _get_terminal_width()
    n              = max(1, len(bars))
    remaining      = terminal_width - CTX_BAR_WIDTH - n * 1
    bar_width      = max(8, remaining // n)
    last_bar_extra = remaining - bar_width * n

    # Вывод
    parts = [make_ctx_bar(ctx_pct, CTX_BAR_WIDTH)]
    for idx, (field, token_pct, time_pct) in enumerate(bars):
        width = bar_width + (last_bar_extra if idx == len(bars) - 1 else 0)
        parts.append(make_bar(token_pct, time_pct, width, get_label(field)))

    print(' '.join(parts))


if __name__ == '__main__':
    main()
