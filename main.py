import argparse
import json
import glob
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

APP_CONFIG_DIR_NAME = 'vpinleaders-client'
CONFIG_FILE_NAME = 'config.ini'
CONFIG_WEBSITE_URL = 'https://www.vpinleaders.com'
CONFIG_OVERRIDE_PATH = ''
HEADLESS_MODE = False
NOTIFICATION_SINK = None
_batocera_popup_lock = threading.Lock()


def _extract_config_override(argv) -> str:
    args = list(argv or [])
    for idx, arg in enumerate(args):
        if arg == '--config' and idx + 1 < len(args):
            value = str(args[idx + 1]).strip()
            if value:
                return os.path.abspath(os.path.expanduser(value))
        if isinstance(arg, str) and arg.startswith('--config='):
            value = arg.split('=', 1)[1].strip()
            if value:
                return os.path.abspath(os.path.expanduser(value))
    return ''


def _has_flag(argv, flag: str) -> bool:
    return any(str(arg).strip() == flag for arg in (argv or []))


def _platform_config_dir() -> str:
    system = platform.system()
    home = Path.home()
    if system == 'Linux' and platform.uname().node == "BATOCERA":
        return '/userdata/system/configs/vpinleaders-client'
    if system == 'Windows':
        base = os.environ.get('APPDATA')
        if not base:
            base = str(home / 'AppData' / 'Roaming')
        return os.path.join(base, APP_CONFIG_DIR_NAME)
    if system == 'Darwin':
        return str(home / 'Library' / 'Application Support' / APP_CONFIG_DIR_NAME)
    base = os.environ.get('XDG_CONFIG_HOME')
    if not base:
        base = str(home / '.config')
    return os.path.join(base, APP_CONFIG_DIR_NAME)


def _config_path() -> str:
    if CONFIG_OVERRIDE_PATH:
        return CONFIG_OVERRIDE_PATH
    return os.path.join(_platform_config_dir(), CONFIG_FILE_NAME)


def _ensure_config_dir() -> None:
    os.makedirs(os.path.dirname(_config_path()), exist_ok=True)


def _missing_config_message(config_path: str) -> str:
    return (
        'Configuration file not found. '
        f'Run the client with --register --machine-id YOUR_MACHINE_ID to complete setup. '
        f'Config path: {config_path}'
    )


def _default_nvram_base_dir() -> str:
    if platform.system() == 'Linux' and platform.uname().node == "BATOCERA":
        return '/userdata/roms/vpinball'
    return ''


def _ensure_config_seeded() -> bool:
    config_path = _config_path()
    if os.path.exists(config_path):
        return True
    example_path = resource_path('config.example.ini')
    if not os.path.exists(example_path):
        return False
    try:
        from registration import seed_config_from_example

        seed_config_from_example(
            config_path=config_path,
            example_path=example_path,
            api_url=CONFIG_WEBSITE_URL,
            nvram_base_dir=_default_nvram_base_dir(),
        )
        return True
    except Exception:
        return False


def _configured_nvram_base_dir() -> str:
    try:
        import configparser as _configparser
        cp = _configparser.ConfigParser()
        cp.read(_config_path())
        if 'nvram' in cp:
            configured = cp['nvram'].get('base_dir', '').strip()
            if configured:
                return os.path.expanduser(configured)
    except Exception:
        pass
    return ''


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def _fast_list_roms(index_path: str):
    with open(index_path, 'r', encoding='utf-8') as f:
        index = json.load(f)
    if not isinstance(index, dict):
        raise ValueError('index.json root is not an object')
    roms = sorted(
        str(k)
        for k, v in index.items()
        if isinstance(k, str)
        and not k.startswith('_')
        and isinstance(v, str)
        and v.endswith('.map.json')
    )
    print(f'Supported ROMs ({len(roms)}):')
    for rom in roms:
        print(rom)


def _format_table(rows):
    if not rows:
        return ''
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for idx, row in enumerate(rows):
        line = ' | '.join(str(row[i]).ljust(widths[i]) for i in range(len(row)))
        lines.append(line)
        if idx == 0:
            lines.append('-+-'.join('-' * w for w in widths))
    return '\n'.join(lines)


def _find_nv_file_for_rom(base_dir: str, rom: str):
    if not base_dir or not str(base_dir).strip():
        return None, 'Missing NVRAM base dir. Set [nvram].base_dir in config.ini or pass --base-dir.'
    root = os.path.expanduser(base_dir)
    patterns = [
        os.path.join(root, '**', 'pinmame', 'nvram', f'{rom}.nv'),
        os.path.join(root, f'{rom}.nv'),
    ]
    out = []
    seen = set()
    for pattern in patterns:
        for p in glob.glob(pattern, recursive=True):
            cp = os.path.normpath(os.path.expanduser(p))
            if cp in seen:
                continue
            seen.add(cp)
            out.append(p)
    matches = sorted(out)
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f'No .nv file found for ROM "{rom}" under {base_dir}'
    details = '\n'.join(f'  - {m}' for m in matches)
    return None, (
        f'Expected exactly one .nv file for ROM "{rom}", but found {len(matches)}:\n{details}\n'
        'Please keep only one matching table nvram for this ROM.'
    )


def _fast_list_highscores(maps_root: str, base_dir: str, rom: str):
    from nvram_monitor import MapRepository, DescriptorDecoder  # local import for CLI fast-path

    repo = MapRepository(maps_root)
    rel = repo._resolve_map_rel(rom)
    if not isinstance(rel, str) or not rel.endswith('.map.json'):
        raise ValueError(f'ROM "{rom}" is not in nvram-maps index')

    map_data = repo.map_for_rom(rom) or {}
    high_scores = map_data.get('high_scores')
    mode_champions = map_data.get('mode_champions')
    has_high_scores = isinstance(high_scores, list) and len(high_scores) > 0
    has_mode_champions = isinstance(mode_champions, list) and len(mode_champions) > 0
    if not has_high_scores and not has_mode_champions:
        raise ValueError(f'ROM "{rom}" has neither "high_scores" nor "mode_champions" mapping')

    nv_path, err = _find_nv_file_for_rom(base_dir, rom)
    if err:
        raise ValueError(err)
    assert nv_path is not None

    with open(nv_path, 'rb') as f:
        raw = f.read()

    decoder = DescriptorDecoder(map_data, repo.platform_for_map(map_data) or {})

    def _fmt_decoded(desc):
        if not isinstance(desc, dict):
            return ''
        val = decoder.decode(raw, desc)
        if val is None:
            return ''
        suffix = desc.get('suffix')
        if isinstance(val, dict):
            if {'year', 'month', 'day', 'hour', 'minute'}.issubset(val.keys()):
                try:
                    return f"{int(val['year']):04d}-{int(val['month']):02d}-{int(val['day']):02d} {int(val['hour']):02d}:{int(val['minute']):02d}"
                except Exception:
                    return str(val)
            return str(val)
        try:
            txt = f'{int(val):,}'
        except Exception:
            txt = str(val)
        if suffix:
            txt = f'{txt}{suffix}'
        return txt

    print(f'ROM: {rom}')
    print(f'NVRAM: {nv_path}')
    print(f'Map: {rel}')
    print()
    if has_high_scores:
        rows = [('Label', 'Initials', 'Score')]
        for i, entry in enumerate(high_scores):
            if not isinstance(entry, dict):
                continue
            label = str(entry.get('label') or entry.get('short_label') or f'Entry {i + 1}')
            initials = _fmt_decoded(entry.get('initials'))
            score_txt = _fmt_decoded(entry.get('score'))
            rows.append((label, initials, score_txt))
        print('High Scores')
        print(_format_table(rows))

    if has_mode_champions:
        if has_high_scores:
            print()
        rows = [('Label', 'Initials', 'Score', 'Timestamp')]
        for i, entry in enumerate(mode_champions):
            if not isinstance(entry, dict):
                continue
            label = str(entry.get('label') or entry.get('short_label') or f'Mode {i + 1}')
            initials = _fmt_decoded(entry.get('initials'))
            score_txt = _fmt_decoded(entry.get('score'))
            stamp_txt = _fmt_decoded(entry.get('timestamp'))
            rows.append((label, initials, score_txt, stamp_txt))
        print('Mode Champions')
        print(_format_table(rows))


# Fast-path for CLI commands: avoid importing optional runtime dependencies.
def _run_batocera_popup(title: str, message: str, kind: str = 'info') -> int:
    try:
        import pygame
    except Exception as exc:
        print(f'ERROR: pygame unavailable for Batocera popup ({exc})')
        return 1

    if 'DISPLAY' not in os.environ:
        os.environ['DISPLAY'] = ':0.0'

    try:
        pygame.init()
        pygame.font.init()
        display_sizes = pygame.display.get_desktop_sizes() if hasattr(pygame.display, 'get_desktop_sizes') else []
        if display_sizes:
            screen_w, screen_h = display_sizes[0]
        else:
            info = pygame.display.Info()
            screen_w, screen_h = info.current_w, info.current_h
        popup_w, popup_h = 520, 112
        margin = 24
        x = max(0, int(screen_w) - popup_w - margin)
        y = margin
        os.environ['SDL_VIDEO_WINDOW_POS'] = f'{x},{y}'
        screen = pygame.display.set_mode((popup_w, popup_h), pygame.NOFRAME)
    except Exception as exc:
        print(f'ERROR: failed to initialize Batocera popup ({exc})')
        return 1

    colors = {
        'info': {'bg': (37, 40, 46), 'accent': (56, 189, 248), 'title': (255, 255, 255), 'text': (216, 222, 233)},
        'error': {'bg': (54, 32, 32), 'accent': (248, 113, 113), 'title': (255, 245, 245), 'text': (254, 226, 226)},
    }
    palette = colors.get(kind, colors['info'])

    try:
        title_font = pygame.font.SysFont('Arial', 24, bold=True)
        body_font = pygame.font.SysFont('Arial', 20)
    except Exception:
        title_font = pygame.font.Font(None, 30)
        body_font = pygame.font.Font(None, 24)

    title_lines = [line.strip() for line in str(title or '').splitlines() if line.strip()] or ['VPinLeaders']
    body_lines = [line.strip() for line in str(message or '').splitlines() if line.strip()]
    body_lines = body_lines[:2]
    if not body_lines:
        body_lines = ['']

    start_time = time.time()
    duration = 4.0
    clock = pygame.time.Clock()

    try:
        while (time.time() - start_time) < duration:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    return 0
            screen.fill((0, 0, 0))
            panel = pygame.Rect(0, 0, popup_w, popup_h)
            pygame.draw.rect(screen, palette['bg'], panel, border_radius=18)
            pygame.draw.rect(screen, palette['accent'], pygame.Rect(0, 0, 10, popup_h), border_top_left_radius=18, border_bottom_left_radius=18)

            y_pos = 18
            for line in title_lines[:1]:
                surf = title_font.render(line, True, palette['title'])
                screen.blit(surf, (28, y_pos))
                y_pos += 34

            for line in body_lines:
                surf = body_font.render(line, True, palette['text'])
                screen.blit(surf, (28, y_pos))
                y_pos += 26

            pygame.display.flip()
            clock.tick(30)
    finally:
        pygame.quit()
    return 0


if '--list-roms' in sys.argv[1:] or '--list-highscores' in sys.argv[1:] or '--register' in sys.argv[1:]:
    CONFIG_OVERRIDE_PATH = _extract_config_override(sys.argv[1:])
    _maps_root = resource_path('nvram-maps')
    _index_path = os.path.join(_maps_root, 'index.json')
    _cli = argparse.ArgumentParser(description='VPinLeaders Score Sender CLI')
    _cli.add_argument('--list-roms', action='store_true', help='List supported ROM ids and exit')
    _cli.add_argument('--list-highscores', metavar='ROM', help='List mapped high scores for a ROM and exit')
    _cli.add_argument('--register', action='store_true', help='Start device registration and write config.ini')
    _cli.add_argument('--machine-id', default='', help='Unique machine id used for registration')
    _cli.add_argument('--nvrams-folder', default='', help='Base folder used to discover NVRAM files')
    _cli.add_argument('--base-dir', default='', help='Base folder used to discover NVRAM files')
    _cli.add_argument('--config', default='', help='Path to config.ini')
    _args, _ = _cli.parse_known_args(sys.argv[1:])
    try:
        if _args.config.strip():
            CONFIG_OVERRIDE_PATH = os.path.abspath(os.path.expanduser(_args.config.strip()))
        if _args.register:
            from registration import register as _register_device

            sys.exit(
                _register_device(
                    machine_id=_args.machine_id.strip(),
                    config_path=_config_path(),
                    example_path=resource_path('config.example.ini'),
                    nvram_base_dir=_args.nvrams_folder.strip(),
                    api_url=CONFIG_WEBSITE_URL,
                )
            )
        if _args.list_roms:
            _fast_list_roms(_index_path)
            sys.exit(0)
        if _args.list_highscores:
            base_dir = _args.base_dir.strip() or _configured_nvram_base_dir()
            if not base_dir:
                raise ValueError(
                    'Missing NVRAM base dir. Set [nvram].base_dir in config.ini or pass --base-dir.'
                )
            _fast_list_highscores(_maps_root, base_dir, _args.list_highscores.strip())
            sys.exit(0)
    except Exception as _e:
        print(f'ERROR: {_e}')
        sys.exit(1)

import configparser
import requests

from app_logging import configure_logging, default_log_file, get_logger, log_message
from nvram_monitor import NVRAMMonitor
from screenshot import capture_screen


# =========================
# LOGGING
# =========================
LOGGER = get_logger('VPinLeadersClient')


def _log(level, msg):
    log_message(LOGGER, level, msg)


# =========================
# GLOBAL STATE
# =========================
config = configparser.ConfigParser()

CURRENT_MODE = 'scores'      # scores | challenge
SEND_MODE = 'automatic'      # automatic | manual

# API/Credentials
API_URL = ''
API_KEY = ''
MACHINE_ID = ''

# NVRAM source settings
NVRAM_DIR = ''
NVRAM_SCAN_PATTERN = '**/pinmame/nvram/*.nv;*.nv'
NVRAM_POLL_INTERVAL_SEC = 1.0
NVRAM_GAME_END_STABLE_SEC = 8.0
NVRAM_IDLE_END_SEC = 45.0
NVRAM_LIVE_PINMAME = True

# Common game filtering
MIN_GAME_DURATION_SEC = 60

# Screenshot settings
SCREENSHOT_ENABLED = False
SCREENSHOT_SCREEN_ID = None
SCREENSHOT_MAX_WIDTH = 800
SCREENSHOT_JPEG_QUALITY = 75

# Manual-send input bindings
MANUAL_SEND_KEYBOARD_BINDING = 'cmd+shift+s'
MANUAL_SEND_JOYSTICK_BUTTONS = tuple()

# Logging settings
LOG_FILE_PATH = default_log_file()
CONFIG_PATH = _config_path()

# Deduplicate game-end events
last_game_end = {}
_last_logged_scores = {}

# Last known score for manual mode triggers
_last_score_rom = None
_last_score_value = None
_last_score_lock = threading.Lock()
_manual_send_lock = threading.Lock()
_manual_send_inflight = False
_manual_send_last_signature = None
_manual_send_last_ts = 0.0
MANUAL_SEND_DEDUPE_SEC = 5.0


def _normalize_score(raw):
    try:
        return int(str(raw).replace(',', '').replace('.', '').lstrip('0') or 0)
    except Exception:
        return 0


def _parse_button_combo(raw):
    if raw is None:
        return tuple()
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw).split(',')
    out = []
    for item in items:
        try:
            out.append(int(str(item).strip()))
        except Exception:
            continue
    return tuple(v for v in out if v >= 0)


def _parse_keyboard_binding(raw):
    if raw is None:
        return ''
    parts = [part.strip().lower() for part in str(raw).split('+')]
    parts = [part for part in parts if part]
    return '+'.join(parts)


def _keyboard_binding_enabled():
    return bool(MANUAL_SEND_KEYBOARD_BINDING.strip())


def _joystick_binding_enabled():
    return bool(MANUAL_SEND_JOYSTICK_BUTTONS)


def _build_pynput_hotkey(binding):
    normalized = _parse_keyboard_binding(binding)
    if not normalized:
        return '', ''

    tokens = normalized.split('+')
    hotkey_tokens = []
    display_tokens = []
    is_macos = platform.system() == 'Darwin'

    for token in tokens:
        if token in ('cmd', 'command', 'meta', 'super', 'win', 'windows'):
            hotkey_tokens.append('<cmd>' if is_macos else '<ctrl>')
            display_tokens.append('Cmd' if is_macos else 'Ctrl')
        elif token in ('ctrl', 'control'):
            hotkey_tokens.append('<ctrl>')
            display_tokens.append('Ctrl')
        elif token in ('shift',):
            hotkey_tokens.append('<shift>')
            display_tokens.append('Shift')
        elif token in ('alt', 'option'):
            hotkey_tokens.append('<alt>')
            display_tokens.append('Alt')
        else:
            hotkey_tokens.append(token)
            display_tokens.append(token.upper() if len(token) == 1 else token.title())

    return '+'.join(hotkey_tokens), '+'.join(display_tokens)


def _set_last_score(rom_name, score):
    global _last_score_rom, _last_score_value
    with _last_score_lock:
        _last_score_rom = rom_name
        _last_score_value = score


def _get_last_score():
    with _last_score_lock:
        return _last_score_rom, _last_score_value


def _set_notification_sink(sink):
    global NOTIFICATION_SINK
    NOTIFICATION_SINK = sink


def _is_batocera():
    if platform.system() != 'Linux':
        return False
    return platform.uname().node == "BATOCERA"


def show_notification_batocera(title_or_table, message_or_score, kind='info'):
    if isinstance(message_or_score, (int, float)):
        title = f'Score Sent ({CURRENT_MODE.title()})'
        score_str = f"{int(message_or_score):,}"
        message = f'Table: {title_or_table}\nScore: {score_str}'
    elif isinstance(message_or_score, str) and message_or_score.replace(',', '').isdigit():
        title = f'Score Sent ({CURRENT_MODE.title()})'
        score_str = f"{int(message_or_score.replace(',', '')):,}"
        message = f'Table: {title_or_table}\nScore: {score_str}'
    else:
        title = title_or_table
        message = message_or_score

    def _worker():
        if not _batocera_popup_lock.acquire(blocking=False):
            return
        try:
            _run_batocera_popup(title, message, kind)
        except Exception as e:
            _log('WARN', f'Batocera popup failed: {e}')
        finally:
            _batocera_popup_lock.release()

    threading.Thread(target=_worker, daemon=True).start()


def _install_headless_signal_handlers():
    def _handle_shutdown(sig, frame):
        _log('INFO', f'Signal received ({sig}), shutting down')
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)


def _install_desktop_signal_handlers(app, qtimer_cls):
    signal_timer = qtimer_cls()

    def _handle_sigint(sig, frame):
        _log('INFO', 'Ctrl+C received, shutting down')
        app.quit()

    signal.signal(signal.SIGINT, _handle_sigint)

    # Keep the Python interpreter responsive to POSIX signals while Qt runs.
    signal_timer.start(250)
    signal_timer.timeout.connect(lambda: None)
    return signal_timer


# =========================
# CONFIG
# =========================
def load_config():
    global API_URL, API_KEY, MACHINE_ID
    global SCREENSHOT_ENABLED, SCREENSHOT_SCREEN_ID, SCREENSHOT_MAX_WIDTH, SCREENSHOT_JPEG_QUALITY
    global MANUAL_SEND_KEYBOARD_BINDING, MANUAL_SEND_JOYSTICK_BUTTONS
    global CURRENT_MODE, SEND_MODE
    global NVRAM_DIR, LOG_FILE_PATH, CONFIG_PATH

    CONFIG_PATH = _config_path()
    config.read(CONFIG_PATH)

    if 'logging' in config:
        log_file = config['logging'].get('file', '').strip()
        if log_file:
            LOG_FILE_PATH = os.path.expanduser(log_file)

    actual_log_file = configure_logging(log_file=LOG_FILE_PATH, console=True)
    if actual_log_file:
        LOG_FILE_PATH = actual_log_file

    if 'credentials' in config:
        API_URL = config['credentials'].get('api_url', '').strip()
        API_KEY = config['credentials'].get('api_key', '').strip()
        MACHINE_ID = config['credentials'].get('machine_id', '').strip()

    if 'screenshot' in config:
        SCREENSHOT_ENABLED = config['screenshot'].get('enable', 'false').strip().lower() == 'true'
        sid = config['screenshot'].get('capture_screen', '').strip()
        SCREENSHOT_SCREEN_ID = int(sid) if sid else None

        for legacy_key in ('max_width', 'jpeg_quality'):
            if config.has_option('screenshot', legacy_key):
                config.remove_option('screenshot', legacy_key)

    if 'hotkeys' in config:
        if config.has_option('hotkeys', 'keyboard'):
            MANUAL_SEND_KEYBOARD_BINDING = _parse_keyboard_binding(config['hotkeys'].get('keyboard', ''))
        if config.has_option('hotkeys', 'joystick_buttons'):
            MANUAL_SEND_JOYSTICK_BUTTONS = _parse_button_combo(
                config['hotkeys'].get('joystick_buttons', '')
            )

    if 'score-mode' in config:
        CURRENT_MODE = 'challenge' if config['score-mode'].getboolean('challenge', False) else 'scores'

    if 'send-mode' in config:
        send_mode = config['send-mode'].get('send_mode', 'automatic').strip().lower()
        SEND_MODE = send_mode if send_mode in ('automatic', 'manual') else 'automatic'

    if 'nvram' in config:
        base_dir = config['nvram'].get('base_dir', '').strip()
        if base_dir:
            NVRAM_DIR = os.path.expanduser(base_dir)

    if not HEADLESS_MODE:
        try:
            from screeninfo import get_monitors
            monitors = get_monitors()
            _log('INFO', f'Detected {len(monitors)} monitor(s):')
            for i, m in enumerate(monitors):
                _log('INFO', f'  Monitor {i}: {m.width}x{m.height} at ({m.x}, {m.y})')
        except Exception as e:
            _log('WARN', f'Could not enumerate monitors: {e}')

    _log(
        'INFO',
        (
            f'Config loaded. API={API_URL} | mode={CURRENT_MODE}/{SEND_MODE} | '
            f'nvram_base_dir={NVRAM_DIR} | nvram_pattern={NVRAM_SCAN_PATTERN} | '
            f'manual_inputs=keyboard:{"on" if _keyboard_binding_enabled() else "off"},'
            f'joystick:{"on" if _joystick_binding_enabled() else "off"} | '
            f'config_file={CONFIG_PATH} | log_file={LOG_FILE_PATH}'
        )
    )


def save_config():
    try:
        _ensure_config_dir()
        with open(_config_path(), 'w', encoding='utf-8') as configfile:
            config.write(configfile)
        _log('INFO', f'Config saved: {_config_path()}')
    except Exception as e:
        _log('ERROR', f'Error saving config: {e}')


def get_input_string(title, prompt, default_val=''):
    try:
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(None, title, prompt, text=default_val)
        if ok:
            return text.strip()
    except Exception as e:
        _log('WARN', f'Input dialog unavailable: {e}')
    return None


# =========================
# NOTIFICATIONS + API SEND
# =========================
def show_notification(title_or_table, message_or_score, kind='info'):
    if _is_batocera():
        return show_notification_batocera(title_or_table, message_or_score, kind)

    if isinstance(message_or_score, (int, float)):
        title = f'Score Sent ({CURRENT_MODE.title()})'
        score_str = f"{int(message_or_score):,}"
        message = f'Table: {title_or_table}\nScore: {score_str}'
    elif isinstance(message_or_score, str) and message_or_score.replace(',', '').isdigit():
        title = f'Score Sent ({CURRENT_MODE.title()})'
        score_str = f"{int(message_or_score.replace(',', '')):,}"
        message = f'Table: {title_or_table}\nScore: {score_str}'
    else:
        title = title_or_table
        message = message_or_score

    _log('INFO', f"Emitting notification: Title='{title}', Kind='{kind}', Msg='{message}'")
    if callable(NOTIFICATION_SINK):
        try:
            NOTIFICATION_SINK(title, message, kind)
        except Exception as e:
            _log('WARN', f'Notification sink failed: {e}')


def _format_send_error(exc):
    msg = str(exc).strip() or exc.__class__.__name__
    lower = msg.lower()
    if 'connection refused' in lower:
        return 'Could not reach the score server. Connection refused.'
    if 'failed to establish a new connection' in lower:
        return 'Could not reach the score server.'
    if 'max retries exceeded' in lower:
        return 'Could not reach the score server after multiple attempts.'
    if 'read timed out' in lower or 'connect timeout' in lower or 'timed out' in lower:
        return 'The score server timed out.'
    return msg


def send_score(table_name, score, capture_screenshot=True):
    import io

    clean_score = _normalize_score(score)
    if clean_score <= 0:
        return

    if not API_URL or not API_KEY:
        _log('ERROR', 'API_URL or API_KEY not configured. Cannot send score.')
        show_notification('Score Send Failed', 'API URL or API key not configured.', kind='error')
        return

    _log('INFO', f'Sending score to API: {table_name} - {clean_score}')

    if SCREENSHOT_ENABLED and capture_screenshot:
        _log('INFO', 'Capturing screenshot for score submission')
        screenshot = capture_screen(
            screen_id=SCREENSHOT_SCREEN_ID,
            max_width=SCREENSHOT_MAX_WIDTH,
        )
    else:
        screenshot = None

    api_base = API_URL.rstrip('/')
    user_os = platform.system()
    if user_os == 'Darwin':
        user_os = 'macOS'
    if user_os == 'Linux' and platform.uname().node == "BATOCERA":
        user_os = 'Batocera'

    try:
        endpoint = f'{api_base}/api/submit-score'

        if screenshot:
            if screenshot.mode == 'RGBA':
                screenshot = screenshot.convert('RGB')

            buffer = io.BytesIO()
            screenshot.save(buffer, format='JPEG', quality=SCREENSHOT_JPEG_QUALITY, optimize=True)
            buffer.seek(0)

            files = {'screenshot': ('screenshot.jpg', buffer, 'image/jpeg')}
            data = {
                'apiKey': API_KEY,
                'machineID': MACHINE_ID,
                'romName': table_name,
                'score': str(clean_score),
                'user_os': user_os,
            }
            if CURRENT_MODE == 'challenge':
                c_id = config['challenge'].get('challenge_id', '') if 'challenge' in config else ''
                data['challenge_id'] = c_id

            r = requests.post(endpoint, files=files, data=data, timeout=30)
        else:
            payload = {
                'apiKey': API_KEY,
                'romName': table_name,
                'machineID': MACHINE_ID,
                'score': clean_score,
                'user_os': user_os,
            }
            if CURRENT_MODE == 'challenge':
                c_id = config['challenge'].get('challenge_id', '') if 'challenge' in config else ''
                payload['challenge_id'] = c_id

            r = requests.post(endpoint, json=payload, timeout=10)

        r.raise_for_status()
        result = r.json()
        _log('INFO', f'Response: status={r.status_code}, result={result}')

        if result.get('success'):
            table_display = result.get('tableName', table_name)
            _log('INFO', f'Score submitted successfully: {table_display} - {clean_score:,}')
            show_notification(table_display, clean_score)
        else:
            error_msg = str(result.get('error', 'Unknown'))
            _log('ERROR', f"API returned error: {error_msg}")
            show_notification('Score Send Failed', error_msg, kind='error')

    except Exception as e:
        _log('ERROR', f'Error sending score to API: {e}')
        show_notification('Score Send Failed', _format_send_error(e), kind='error')


# =========================
# SCORE EVENT PROCESSING
# =========================
def handle_current_scores_event(rom_name, scores, current_ball=None):
    if not scores:
        return
    normalized = [_normalize_score(s) for s in scores]
    best = 0
    for v in normalized:
        if v > best:
            best = v
    if best > 0:
        _set_last_score(rom_name, best)

    key = tuple(normalized)
    prev = _last_logged_scores.get(rom_name)
    if key != prev:
        _last_logged_scores[rom_name] = key
        parts = [f'P{i + 1}:{score:,}' for i, score in enumerate(normalized)]
        if current_ball is not None:
            _log('INFO', f'Live scores {rom_name} (ball={current_ball}): {" | ".join(parts)}')
        else:
            _log('INFO', f'Live scores {rom_name}: {" | ".join(parts)}')


def handle_game_end_event(rom_name, scores, reason='', game_duration=None):
    global last_game_end

    now = time.time()
    last = last_game_end.get(rom_name, 0)
    if now - last < 10:
        _log('WARN', f'Ignoring duplicate game_end for {rom_name} ({now - last:.1f}s since previous)')
        return
    last_game_end[rom_name] = now

    if game_duration is not None and int(game_duration) < MIN_GAME_DURATION_SEC:
        _log('WARN', f'Ignoring game_end for {rom_name}: duration too short ({game_duration}s < {MIN_GAME_DURATION_SEC}s)')
        return

    best_score = 0
    for s in scores or []:
        v = _normalize_score(s)
        if v > best_score:
            best_score = v

    if best_score <= 0:
        _log('WARN', f'Game ended for {rom_name} but no valid score found')
        return

    _log('INFO', f'Game over detected: {rom_name} (reason={reason}, duration={game_duration})')
    _log('INFO', f'Final score selected: {rom_name} - {best_score:,}')
    _set_last_score(rom_name, best_score)

    if SEND_MODE == 'automatic':
        send_score(rom_name, best_score, capture_screenshot=False)
    else:
        _log('INFO', 'Manual mode: score stored, waiting for manual send input')


def handle_status_message_event(title, message):
    show_notification(title, message)


# =========================
# NVRAM SOURCE
# =========================
def run_nvram_monitor():
    maps_root = resource_path('nvram-maps')
    _log(
        'INFO',
        (
            'Starting VPinLeaders Client monitoring nv files on '
            f'{NVRAM_DIR} (pattern={NVRAM_SCAN_PATTERN})'
        ),
    )

    monitor = NVRAMMonitor(
        maps_root=maps_root,
        nvram_dir=NVRAM_DIR,
        nvram_scan_pattern=NVRAM_SCAN_PATTERN,
        logger=_log,
        on_current_scores=lambda rom, scores, current_ball: handle_current_scores_event(rom, scores, current_ball),
        on_game_end=lambda rom, scores, reason, duration: handle_game_end_event(rom, scores, reason, duration),
        on_status_message=lambda title, message: handle_status_message_event(title, message),
        use_live_pinmame=NVRAM_LIVE_PINMAME,
        poll_interval_sec=NVRAM_POLL_INTERVAL_SEC,
        game_end_stable_sec=NVRAM_GAME_END_STABLE_SEC,
        idle_end_sec=NVRAM_IDLE_END_SEC,
        min_game_duration_sec=MIN_GAME_DURATION_SEC,
    )
    monitor.run_forever()


# =========================
# MANUAL SEND INPUTS
# =========================
_hotkey_listener = None
_joybutton_listener = None


def _trigger_manual_send(source):
    global _manual_send_inflight, _manual_send_last_signature, _manual_send_last_ts

    rom, score = _get_last_score()
    if rom is None or score is None or score <= 0:
        _log('WARN', f'{source} pressed but no score available to send')
        show_notification('No Score', 'No score available to send')
        return

    signature = (rom, int(score))
    now = time.time()
    with _manual_send_lock:
        if _manual_send_inflight:
            _log('WARN', f'{source} ignored: a manual send is already in progress')
            return
        if _manual_send_last_signature == signature and (now - _manual_send_last_ts) < MANUAL_SEND_DEDUPE_SEC:
            _log('WARN', f'{source} ignored: duplicate manual send for {rom} within {MANUAL_SEND_DEDUPE_SEC:.0f}s')
            return
        _manual_send_inflight = True
        _manual_send_last_signature = signature
        _manual_send_last_ts = now

    _log('INFO', f'{source} triggered: sending {rom} - {score:,}')

    def _runner():
        global _manual_send_inflight
        try:
            send_score(rom, score, capture_screenshot=SCREENSHOT_ENABLED)
        finally:
            with _manual_send_lock:
                _manual_send_inflight = False

    threading.Thread(target=_runner, daemon=True).start()


def _on_hotkey_pressed():
    _trigger_manual_send('Hotkey')


class _JoyButtonListener:
    def __init__(self, buttons):
        self.buttons = tuple(sorted(set(int(b) for b in buttons if int(b) >= 0)))
        self._stop_event = threading.Event()
        self._thread = None
        self._combo_active = False

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _refresh_joysticks(self, pygame):
        try:
            pygame.joystick.quit()
            pygame.joystick.init()
            return [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
        except Exception:
            return []

    def _run(self):
        os.environ.setdefault('SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', '1')
        try:
            import pygame
        except Exception as exc:
            _log('WARN', f'Joystick listener unavailable: pygame import failed ({exc})')
            return

        try:
            pygame.init()
            pygame.joystick.init()
        except Exception as exc:
            _log('WARN', f'Joystick listener unavailable: pygame init failed ({exc})')
            return

        _log(
            'INFO',
            (
                'Starting joystick listener: '
                f'buttons={",".join(str(b) for b in self.buttons)}'
            ),
        )
        joysticks = self._refresh_joysticks(pygame)
        last_count = len(joysticks)
        if last_count == 0:
            _log('WARN', 'Joystick listener started but no joystick/gamepad is available')

        try:
            while not self._stop_event.is_set():
                try:
                    pygame.event.pump()
                except Exception:
                    pass

                current_count = 0
                try:
                    current_count = pygame.joystick.get_count()
                except Exception:
                    current_count = last_count
                if current_count != last_count:
                    joysticks = self._refresh_joysticks(pygame)
                    last_count = len(joysticks)

                combo_pressed = False
                for joy in joysticks:
                    try:
                        if all(joy.get_button(btn) for btn in self.buttons):
                            combo_pressed = True
                            break
                    except Exception:
                        continue

                if combo_pressed and not self._combo_active:
                    self._combo_active = True
                    _trigger_manual_send('Joybutton')
                elif not combo_pressed:
                    self._combo_active = False

                time.sleep(0.03)
        finally:
            try:
                pygame.joystick.quit()
            except Exception:
                pass
            try:
                pygame.quit()
            except Exception:
                pass


def _start_hotkey_listener():
    global _hotkey_listener
    _stop_hotkey_listener()

    hotkey_combo, display_combo = _build_pynput_hotkey(MANUAL_SEND_KEYBOARD_BINDING)
    if not hotkey_combo:
        return

    try:
        from pynput import keyboard as pynput_keyboard
    except Exception as exc:
        _log('WARN', f'Hotkey listener unavailable: pynput import failed ({exc})')
        return

    _log('INFO', f'Starting hotkey listener: {display_combo}')
    _hotkey_listener = pynput_keyboard.GlobalHotKeys({hotkey_combo: _on_hotkey_pressed})
    _hotkey_listener.daemon = True
    _hotkey_listener.start()


def _stop_hotkey_listener():
    global _hotkey_listener
    if _hotkey_listener is not None:
        _log('INFO', 'Stopping hotkey listener')
        _hotkey_listener.stop()
        _hotkey_listener = None


def _start_joybutton_listener():
    global _joybutton_listener
    _stop_joybutton_listener()

    if not _joystick_binding_enabled():
        _log('WARN', 'Joystick listener disabled: no joystick button combo configured')
        return

    _joybutton_listener = _JoyButtonListener(MANUAL_SEND_JOYSTICK_BUTTONS)
    _joybutton_listener.start()


def _stop_joybutton_listener():
    global _joybutton_listener
    if _joybutton_listener is not None:
        _log('INFO', 'Stopping joystick listener')
        _joybutton_listener.stop()
        _joybutton_listener = None


def _start_manual_send_listeners():
    if _keyboard_binding_enabled():
        _start_hotkey_listener()
    else:
        _stop_hotkey_listener()

    _start_joybutton_listener()

    if not _keyboard_binding_enabled() and not _joystick_binding_enabled():
        _log('WARN', 'Manual mode is active but no hotkey or joystick bindings are enabled')


def _stop_manual_send_listeners():
    _stop_hotkey_listener()
    _stop_joybutton_listener()


def _show_missing_config_and_exit(config_path: str):
    message = _missing_config_message(config_path)
    print(f'ERROR: {message}', file=sys.stderr)
    if not HEADLESS_MODE:
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox
            app = QApplication(sys.argv)
            app.setQuitOnLastWindowClosed(False)
            QMessageBox.critical(None, 'VPinLeaders Configuration Missing', message)
        except Exception:
            pass
    sys.exit(1)


def _run_desktop_app():
    from PyQt6.QtCore import QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QActionGroup, QIcon
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    from notifier import NotificationOverlay

    class VPinScoreTray(QSystemTrayIcon):
        notify_requested = pyqtSignal(str, str, str)

        def __init__(self, icon, parent=None):
            super().__init__(icon, parent)
            self.setToolTip(f'VPin Score Tracker - {CURRENT_MODE.title()} ({SEND_MODE.title()})')
            self.notify_requested.connect(self.display_overlay)

            self.menu = QMenu(parent)

            self.mode_group = QActionGroup(self.menu)
            self.mode_group.setExclusive(True)

            self.act_score = QAction('Score Mode', self.menu, checkable=True)
            self.act_score.triggered.connect(lambda: self.set_mode('scores'))
            self.mode_group.addAction(self.act_score)
            self.menu.addAction(self.act_score)

            self.act_tourn = QAction('Tournament Mode', self.menu, checkable=True)
            self.act_tourn.triggered.connect(lambda: self.set_mode('challenge'))
            self.mode_group.addAction(self.act_tourn)
            self.menu.addAction(self.act_tourn)

            self.menu.addSeparator()

            self.send_mode_group = QActionGroup(self.menu)
            self.send_mode_group.setExclusive(True)

            self.act_auto = QAction('Automatic Send', self.menu, checkable=True)
            self.act_auto.triggered.connect(lambda: self.set_send_mode('automatic'))
            self.send_mode_group.addAction(self.act_auto)
            self.menu.addAction(self.act_auto)

            self.act_manual = QAction('Manual Send (Hotkey/Joy)', self.menu, checkable=True)
            self.act_manual.triggered.connect(lambda: self.set_send_mode('manual'))
            self.send_mode_group.addAction(self.act_manual)
            self.menu.addAction(self.act_manual)

            self.menu.addSeparator()

            self.act_exit = QAction('Exit', self.menu)
            self.act_exit.triggered.connect(QApplication.instance().quit)
            self.menu.addAction(self.act_exit)

            self.setContextMenu(self.menu)
            self.update_menu_state()
            self.active_notification = None

        def update_menu_state(self):
            self.act_score.setChecked(CURRENT_MODE == 'scores')
            self.act_tourn.setChecked(CURRENT_MODE == 'challenge')
            self.act_auto.setChecked(SEND_MODE == 'automatic')
            self.act_manual.setChecked(SEND_MODE == 'manual')
            self.setToolTip(f'VPin Score Tracker - {CURRENT_MODE.title()} ({SEND_MODE.title()})')

        def set_mode(self, selected_mode):
            global CURRENT_MODE

            if 'score-mode' not in config:
                config['score-mode'] = {}

            if selected_mode == 'scores':
                CURRENT_MODE = 'scores'
                config['score-mode']['scores'] = 'true'
                config['score-mode']['challenge'] = 'false'
                save_config()
                self.update_menu_state()
                return

            current_val = ''
            if selected_mode == 'challenge' and 'challenge' in config:
                current_val = config['challenge'].get('challenge_id', '')

            new_id = get_input_string('Challenge Mode', 'Enter Challenge ID:', default_val=current_val)
            if not new_id:
                self.update_menu_state()
                return

            CURRENT_MODE = selected_mode
            config['score-mode']['scores'] = 'false'
            config['score-mode']['challenge'] = 'false'
            config['score-mode'][selected_mode] = 'true'

            if selected_mode == 'challenge':
                if 'challenge' not in config:
                    config['challenge'] = {}
                config['challenge']['challenge_id'] = new_id

            save_config()
            self.update_menu_state()

        def set_send_mode(self, mode):
            global SEND_MODE
            SEND_MODE = mode

            if 'send-mode' not in config:
                config['send-mode'] = {}
            config['send-mode']['send_mode'] = mode
            save_config()

            if mode == 'manual':
                _start_manual_send_listeners()
            else:
                _stop_manual_send_listeners()

            self.update_menu_state()

        def display_overlay(self, title, message, kind):
            if self.active_notification:
                try:
                    self.active_notification.close()
                except Exception:
                    pass

            self.active_notification = NotificationOverlay(title, message, kind=kind)
            self.active_notification.show()
            self.active_notification.raise_()
            QApplication.processEvents()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    signal_timer = _install_desktop_signal_handlers(app, QTimer)

    path_to_icon = resource_path('assets/icon.png')
    if os.path.exists(path_to_icon):
        icon = QIcon(path_to_icon)
    else:
        from PyQt6.QtGui import QColor, QPixmap

        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor('blue'))
        icon = QIcon(pixmap)

    tray = VPinScoreTray(icon)
    tray.show()
    _set_notification_sink(lambda title, message, kind: tray.notify_requested.emit(title, message, kind))

    _log('INFO', 'Starting source thread: nvram')
    source_thread = threading.Thread(target=run_nvram_monitor, daemon=True)
    source_thread.start()

    if SEND_MODE == 'manual':
        _start_manual_send_listeners()

    app._vpin_signal_timer = signal_timer
    return app.exec()


def _run_headless():
    _set_notification_sink(None)
    _install_headless_signal_handlers()
    if SEND_MODE == 'manual':
        _log('WARN', 'Manual send mode is not supported in headless mode; scores will not be submitted automatically')
    _log('INFO', 'Running in headless mode')
    run_nvram_monitor()


if __name__ == '__main__':
    CONFIG_OVERRIDE_PATH = _extract_config_override(sys.argv[1:])
    HEADLESS_MODE = _has_flag(sys.argv[1:], '--headless')
    if not _ensure_config_seeded() and not os.path.exists(_config_path()):
        _show_missing_config_and_exit(_config_path())

    load_config()

    missing = [
        k for k, v in [
            ('api_key', API_KEY),
            ('machine_id', MACHINE_ID),
            ('nvram.base_dir', NVRAM_DIR),
        ]
        if not v or not v.strip()
    ]
    if missing:
        _log('ERROR', f"Missing required config value(s): {', '.join(missing)}")
        sys.exit(1)

    if HEADLESS_MODE:
        _run_headless()
    else:
        sys.exit(_run_desktop_app())
