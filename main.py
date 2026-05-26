import configparser
import json
import os
import platform
import signal
import sys
import threading
import time
from pathlib import Path

APP_CONFIG_DIR_NAME = 'vpinleaders-client'
CONFIG_FILE_NAME = 'config.ini'
CONFIG_OVERRIDE_PATH = ''
NOTIFICATION_SINK = None


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


def _platform_config_dir() -> str:
    system = platform.system()
    home = Path.home()
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


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

from app_logging import configure_logging, default_log_file, get_logger, log_message
from nvram_monitor import NVRAMMonitor
from screenshot import capture_screen

_score_ocr = None
_SCORE_OCR_AVAILABLE = None


# =========================
# LOGGING
# =========================
LOGGER = get_logger('vpinscoretracker')


def _log(level, msg):
    log_message(LOGGER, level, msg)


# =========================
# GLOBAL STATE
# =========================
config = configparser.ConfigParser()

# Per-integration enable flags. Multiple integrations can be on at the same
# time; a single manual send fans out to every enabled one.
WOVP_ENABLED = False
ISCORED_ENABLED = False
VPINPLAY_ENABLED = False
VPINPLAY_AUTO_SEND = False

# NVRAM source settings
NVRAM_DIR = ''
NVRAM_SCAN_PATTERN = '**/pinmame/nvram/*.nv;*.nv'
NVRAM_POLL_INTERVAL_SEC = 1.0
NVRAM_GAME_END_STABLE_SEC = 8.0
NVRAM_IDLE_END_SEC = 45.0
NVRAM_LIVE_PINMAME = True

# Common game filtering
MIN_GAME_DURATION_SEC = 60

# Screenshot settings. Capture is unconditional whenever a manual send fires
# and at least one integration is enabled; the only knob is which display
# gets captured.
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
# Reference to the running NVRAMMonitor (set in run_nvram_monitor)
_nvram_monitor_ref = None

# Reference to the system tray (set in run_tray_ui, used for cross-thread signals)
_tray_ref = None
_preloaded_wovp_challenges = []
_preloaded_iscored_games = []
_nvram_monitor_thread = None
_nvram_monitor_lock = threading.Lock()

# Last known score for manual mode triggers.
# _last_score_vpx_file is snapshotted at game-end time so manual sends always
# carry the VPX filename of the game that was actually played, not whatever
# table happens to be running when the hotkey is pressed later.
_last_score_rom = None
_last_score_value = None
_last_score_vpx_file = ''
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


def _detect_score_from_screenshot(pil_image) -> int:
    """
    Convert a PIL screenshot to OpenCV format and run score_ocr on it.
    Returns the detected score as int, or 0 if nothing was found.
    """
    score_ocr = _get_score_ocr()
    if score_ocr is None or pil_image is None:
        return 0
    try:
        import numpy as np
        import cv2
        img_rgb = np.array(pil_image.convert('RGB'))
        img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        result = score_ocr.detect_score(img_bgr)
        if result.best and result.best.score:
            return int(result.best.score)
    except Exception as e:
        _log('WARN', f'Score OCR error: {e}')
    return 0


def _get_score_ocr():
    global _score_ocr, _SCORE_OCR_AVAILABLE
    if _SCORE_OCR_AVAILABLE is False:
        return None
    if _score_ocr is not None:
        return _score_ocr
    try:
        import score_ocr as module
    except ImportError:
        _SCORE_OCR_AVAILABLE = False
        return None
    _score_ocr = module
    _SCORE_OCR_AVAILABLE = True
    return _score_ocr


def _get_best_available_rom() -> str:
    """Return the best ROM name we can determine right now (may be empty)."""
    monitor = _nvram_monitor_ref
    if monitor is not None:
        try:
            if monitor.active_rom:
                return monitor.active_rom
        except AttributeError:
            pass
    with _last_score_lock:
        return _last_score_rom or ''


def _get_best_available_vpx() -> str:
    """Return the best VPX filename we can determine right now (may be empty)."""
    monitor = _nvram_monitor_ref
    if monitor is not None:
        try:
            if monitor.last_detected_table_path:
                return os.path.basename(monitor.last_detected_table_path)
        except AttributeError:
            pass
    with _last_score_lock:
        return _last_score_vpx_file or ''


def _resize_pil_for_send(pil_image):
    """Resize a PIL Image to SCREENSHOT_MAX_WIDTH, preserving aspect ratio."""
    if not pil_image or not SCREENSHOT_MAX_WIDTH or SCREENSHOT_MAX_WIDTH <= 0:
        return pil_image
    w, h = pil_image.size
    if w <= SCREENSHOT_MAX_WIDTH:
        return pil_image
    from PIL import Image as _PilImage
    ratio = SCREENSHOT_MAX_WIDTH / w
    return pil_image.resize((SCREENSHOT_MAX_WIDTH, int(h * ratio)), _PilImage.LANCZOS)


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


def _set_last_score(rom_name, score, vpx_file: str = ''):
    global _last_score_rom, _last_score_value, _last_score_vpx_file
    with _last_score_lock:
        _last_score_rom = rom_name
        _last_score_value = score
        _last_score_vpx_file = vpx_file


def _get_last_score():
    with _last_score_lock:
        return _last_score_rom, _last_score_value, _last_score_vpx_file


def _get_manual_score_snapshot():
    monitor = _nvram_monitor_ref
    if monitor is not None:
        try:
            snap = monitor.current_score_snapshot()
        except Exception as e:
            _log('WARN', f'Could not read current score snapshot: {e}')
            snap = None
        if snap:
            rom = snap.get('rom')
            score = _normalize_score(snap.get('score'))
            vpx_file = str(snap.get('vpx_file') or '')
            if rom and score > 0:
                return rom, score, vpx_file
    return _get_last_score()


def _set_notification_sink(sink):
    global NOTIFICATION_SINK
    NOTIFICATION_SINK = sink


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
def _truthy(value: str) -> bool:
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def load_config():
    global SCREENSHOT_SCREEN_ID, SCREENSHOT_MAX_WIDTH, SCREENSHOT_JPEG_QUALITY
    global MANUAL_SEND_KEYBOARD_BINDING, MANUAL_SEND_JOYSTICK_BUTTONS
    global WOVP_ENABLED, ISCORED_ENABLED, VPINPLAY_ENABLED, VPINPLAY_AUTO_SEND
    global NVRAM_DIR, LOG_FILE_PATH, CONFIG_PATH

    CONFIG_PATH = _config_path()
    config.clear()
    config.read(CONFIG_PATH)
    config.remove_section('vpinleaders')
    config.remove_section('credentials')

    # ── Logging ───────────────────────────────────────────────────────────
    if 'logging' in config:
        log_file = config['logging'].get('file', '').strip()
        if log_file:
            LOG_FILE_PATH = os.path.expanduser(log_file)
    actual_log_file = configure_logging(log_file=LOG_FILE_PATH, console=True)
    if actual_log_file:
        LOG_FILE_PATH = actual_log_file

    # ── WoVP ──────────────────────────────────────────────────────────────
    if 'wovp' in config:
        WOVP_ENABLED = _truthy(config['wovp'].get('enable', 'false'))
    else:
        WOVP_ENABLED = False

    # ── iScored ───────────────────────────────────────────────────────────
    if 'iscored' in config:
        ISCORED_ENABLED = _truthy(config['iscored'].get('enable', 'false'))
    else:
        ISCORED_ENABLED = False

    # ── VPinPlay ─────────────────────────────────────────────────────────
    if 'vpinplay' in config:
        VPINPLAY_ENABLED = _truthy(config['vpinplay'].get('enable', 'false'))
        VPINPLAY_AUTO_SEND = _truthy(config['vpinplay'].get('auto_send', 'false'))
    else:
        VPINPLAY_ENABLED = False
        VPINPLAY_AUTO_SEND = False

    # ── Screenshot ────────────────────────────────────────────────────────
    if 'screenshot' in config:
        # New key: screen_to_capture  |  Old key: capture_screen
        sid = (
            config['screenshot'].get('screen_to_capture')
            or config['screenshot'].get('capture_screen')
            or ''
        ).strip()
        SCREENSHOT_SCREEN_ID = int(sid) if sid else None

    # ── Hotkeys ───────────────────────────────────────────────────────────
    if 'hotkeys' in config:
        if config.has_option('hotkeys', 'keyboard'):
            MANUAL_SEND_KEYBOARD_BINDING = _parse_keyboard_binding(
                config['hotkeys'].get('keyboard', '')
            )
        if config.has_option('hotkeys', 'joystick_buttons'):
            MANUAL_SEND_JOYSTICK_BUTTONS = _parse_button_combo(
                config['hotkeys'].get('joystick_buttons', '')
            )

    # ── NVRAM ─────────────────────────────────────────────────────────────
    if 'nvram' in config:
        base_dir = config['nvram'].get('base_dir', '').strip()
        if base_dir:
            NVRAM_DIR = os.path.expanduser(base_dir)

    # Normalize the in-memory config to the new format so every subsequent
    # save_config() call writes canonical keys, even after loading an old file.

    try:
        from screeninfo import get_monitors
        monitors = get_monitors()
        _log('INFO', f'Detected {len(monitors)} monitor(s):')
        for i, m in enumerate(monitors):
            _log('INFO', f'  Monitor {i}: {m.width}x{m.height} at ({m.x}, {m.y})')
    except Exception as e:
        _log('WARN', f'Could not enumerate monitors: {e}')

    enabled_labels = ','.join(
        name for name, on in (
            ('vpinplay', VPINPLAY_ENABLED),
            ('wovp', WOVP_ENABLED),
            ('iscored', ISCORED_ENABLED),
        ) if on
    ) or 'none'
    _log(
        'INFO',
        (
            f'Config loaded. enabled={enabled_labels} | '
            f'vpinplay_auto_send={"on" if VPINPLAY_AUTO_SEND else "off"} | '
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
    if isinstance(message_or_score, (int, float)):
        title = 'VPinScore Tracker'
        score_str = f"{int(message_or_score):,}"
        message = f'Integration: {title_or_table}\nScore: {score_str}'
    elif isinstance(message_or_score, str) and message_or_score.replace(',', '').isdigit():
        title = 'VPinScore Tracker'
        score_str = f"{int(message_or_score.replace(',', '')):,}"
        message = f'Integration: {title_or_table}\nScore: {score_str}'
    else:
        title = title_or_table
        message = message_or_score

    _log('INFO', f"Emitting notification: Title='{title}', Kind='{kind}', Msg='{message}'")
    if callable(NOTIFICATION_SINK):
        try:
            NOTIFICATION_SINK(title, message, kind)
        except Exception as e:
            _log('WARN', f'Notification sink failed: {e}')


# =========================
# WOVP SUBMISSION
# =========================
def send_wovp_score(table_name, score, screenshot_image, vpx_file: str = ''):
    """
    Submit a score to WOVP (World of Virtual Pinball).
    Typically called from manual send triggers.

    Args:
        table_name: ROM name (e.g. "sman_261")
        score: The numeric score
        screenshot_image: PIL Image object (captured screenshot)
        vpx_file: VPX filename (e.g. "Spider-Man.vpx")
    """
    from wovp_client import WovpClient

    clean_score = _normalize_score(score)
    if clean_score <= 0:
        return

    try:
        wovp = WovpClient(CONFIG_PATH)

        if not wovp.is_ready():
            _log('ERROR', 'WoVP: not configured (missing api_key or challenge selection)')
            show_notification('WoVP Send Failed', 'WoVP not configured. Check settings.', kind='error')
            return

        # Determine which VPX file was actually played
        effective_vpx = vpx_file or (
            os.path.basename(_nvram_monitor_ref.last_detected_table_path)
            if _nvram_monitor_ref and _nvram_monitor_ref.last_detected_table_path
            else ''
        )

        if not effective_vpx:
            _log('ERROR', 'WoVP: could not determine VPX filename. Aborting WOVP submission.')
            show_notification(
                'WoVP Send Failed',
                'Could not detect the VPX file. Make sure the table is launched through VPX.',
                kind='error',
            )
            return

        # Verify the player is on the correct table for the challenge
        _, challenge_name = wovp.get_selected_challenge()
        if not WovpClient.table_matches_challenge(effective_vpx, challenge_name):
            msg = (
                f'Table mismatch: playing "{effective_vpx}" '
                f'but challenge is "{challenge_name}". Submission aborted.'
            )
            _log('ERROR', f'WoVP: {msg}')
            show_notification('WoVP Send Failed', msg, kind='error')
            return

        if not screenshot_image:
            _log('ERROR', 'WoVP: screenshot could not be captured. Skipping WOVP submission.')
            show_notification('WoVP Send Failed', 'Could not capture screenshot.', kind='error')
            return

        # Submit using the convenience method (handles temp file cleanup)
        wovp.submit_score_with_screenshot(
            screenshot_image=screenshot_image,
            score=clean_score,
            rom=table_name,
            vpx_file=effective_vpx,
            jpeg_quality=SCREENSHOT_JPEG_QUALITY,
        )

        _log('INFO', f'WoVP: score submitted — {table_name} → "{challenge_name}"')
        show_notification('WoVP', clean_score)

    except Exception as e:
        _log('ERROR', f'WoVP submission failed: {e}')
        show_notification('WoVP Send Failed', str(e), kind='error')


# =========================
# ISCORED SUBMISSION
# =========================
def send_iscored_score(table_name, score, vpx_file: str = '', screenshot_image=None):
    # screenshot_image is forwarded for parity with the other integrations; the
    # current iScored API surface does not yet accept image uploads, so the
    # value is unused downstream until the client gains support.
    from iscored_client import IScoredClient

    clean_score = _normalize_score(score)
    if clean_score <= 0:
        return

    try:
        iscored = IScoredClient(CONFIG_PATH)

        if not iscored.player_name:
            _log('ERROR', 'iScored: player_name is not configured')
            show_notification('iScored Send Failed', 'iScored player_name is not configured.', kind='error')
            return

        effective_vpx = vpx_file or (
            os.path.basename(_nvram_monitor_ref.last_detected_table_path)
            if _nvram_monitor_ref and _nvram_monitor_ref.last_detected_table_path
            else ''
        )

        result = iscored.submit_score(
            score=clean_score,
            rom=table_name,
            vpx_file=effective_vpx,
        )

        msg = result.get('message', '')
        if result.get('success'):
            _log('INFO', f'iScored: score submitted — {table_name} ({msg})')
            show_notification('iScored', clean_score)
        else:
            _log('ERROR', f'iScored submission failed: {msg}')
            show_notification('iScored Send Failed', msg or 'Unknown error', kind='error')

    except Exception as e:
        _log('ERROR', f'iScored submission failed: {e}')
        show_notification('iScored Send Failed', str(e), kind='error')


# =========================
# VPINPLAY SUBMISSION
# =========================
def send_vpinplay_score(table_name, score, vpx_file: str = ''):
    from vpinplay_client import VPinPlayClient, VPinPlayResolveError

    clean_score = _normalize_score(score)
    if clean_score <= 0:
        return

    effective_path = ''
    try:
        client = VPinPlayClient(CONFIG_PATH)
        if not client.is_ready():
            _log('ERROR', 'VPinPlay: not configured (api_url, user_id, or initials missing)')
            show_notification('VPinPlay Send Failed', 'Check VPinPlay settings.', kind='error')
            return

        if _nvram_monitor_ref is not None and _nvram_monitor_ref.last_detected_table_path:
            detected_path = _nvram_monitor_ref.last_detected_table_path
            if not vpx_file or os.path.basename(detected_path) == vpx_file:
                effective_path = detected_path

        _log(
            'INFO',
            (
                f"VPinPlay: preparing score submission; rom={table_name}; "
                f"score={clean_score}; vpx={vpx_file or 'unknown'}; path={effective_path or 'auto-resolve'}"
            ),
        )
        result = client.submit_score_snapshot(
            rom=table_name,
            score=clean_score,
            vpx_file=vpx_file,
            vpx_path=effective_path,
        )
        if result.get('success'):
            _log(
                'INFO',
                (
                    f"VPinPlay: score synced for {table_name}; score={clean_score}; "
                    f"vpx={vpx_file or 'unknown'}; vpsId={result.get('vpsId') or 'unknown'}; "
                    f"message={result.get('message')}"
                ),
            )
            show_notification('VPinPlay', clean_score)
        else:
            _log(
                'ERROR',
                (
                    f"VPinPlay sync failed for {table_name}; score={clean_score}; "
                    f"vpx={vpx_file or 'unknown'}; path={effective_path or 'unknown'}; result={result}"
                ),
            )
            show_notification('VPinPlay Send Failed', result.get('message') or 'VPinPlay sync failed.', kind='error')
    except VPinPlayResolveError as e:
        diagnostics = getattr(e, 'diagnostics', {}) or {}
        try:
            detail = json.dumps(diagnostics, ensure_ascii=True, sort_keys=True)
        except Exception:
            detail = repr(diagnostics)
        _log(
            'ERROR',
            (
                f"VPinPlay submission failed while resolving VPS ID for {table_name}; "
                f"score={clean_score}; vpx={vpx_file or 'unknown'}; "
                f"path={effective_path or 'unknown'}; error={e}; diagnostics={detail}"
            ),
        )
        show_notification('VPinPlay Send Failed', str(e), kind='error')
    except Exception as e:
        _log(
            'ERROR',
            (
                f"VPinPlay submission failed for {table_name}; score={clean_score}; "
                f"vpx={vpx_file or 'unknown'}; path={effective_path or 'unknown'}; error={e}"
            ),
        )
        show_notification('VPinPlay Send Failed', str(e), kind='error')


def _trigger_vpinplay_auto_send(rom, score, vpx_file: str = ''):
    if not VPINPLAY_ENABLED or not VPINPLAY_AUTO_SEND:
        return

    def _runner():
        _log('INFO', f'VPinPlay auto-send triggered for {rom} (vpx={vpx_file or "unknown"})')
        try:
            send_vpinplay_score(rom, score, vpx_file=vpx_file)
        except Exception as e:
            _log('ERROR', f'VPinPlay auto-send raised: {e}')

    threading.Thread(target=_runner, daemon=True).start()


# =========================
# SCORE EVENT PROCESSING
# =========================
def handle_game_start_event(rom_name):
    _set_last_score(rom_name, 0, '')
    _log('INFO', f'Game started: {rom_name} - score reset for manual send')


def handle_current_scores_event(rom_name, scores, current_ball=None):
    # The monitor keeps live score state internally for game detection and
    # manual-send snapshots. Avoid logging or publishing every polled score.
    return


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
    _log('INFO', f'Final score selected for {rom_name}: {best_score}')

    # Snapshot the VPX filename NOW, while the right table is still the active process.
    # Manual sends may arrive seconds or minutes later when a different table is loaded.
    vpx_file = ''
    if _nvram_monitor_ref is not None and _nvram_monitor_ref.last_detected_table_path:
        vpx_file = os.path.basename(_nvram_monitor_ref.last_detected_table_path)
    _set_last_score(rom_name, best_score, vpx_file)

    _log('INFO', f'Score stored for manual send: {rom_name}')
    _trigger_vpinplay_auto_send(rom_name, best_score, vpx_file=vpx_file)


def handle_status_message_event(title, message):
    show_notification(title, message)


# =========================
# NVRAM SOURCE
# =========================
def run_nvram_monitor():
    global _nvram_monitor_ref

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
        on_game_start=lambda rom: handle_game_start_event(rom),
        on_status_message=lambda title, message: handle_status_message_event(title, message),
        use_live_pinmame=NVRAM_LIVE_PINMAME,
        poll_interval_sec=NVRAM_POLL_INTERVAL_SEC,
        game_end_stable_sec=NVRAM_GAME_END_STABLE_SEC,
        idle_end_sec=NVRAM_IDLE_END_SEC,
        min_game_duration_sec=MIN_GAME_DURATION_SEC,
    )
    _nvram_monitor_ref = monitor
    monitor.run_forever()


def _nvram_monitor_config_signature():
    return (
        os.path.abspath(os.path.expanduser(NVRAM_DIR or '')),
        NVRAM_SCAN_PATTERN,
        bool(NVRAM_LIVE_PINMAME),
        float(NVRAM_POLL_INTERVAL_SEC),
        float(NVRAM_GAME_END_STABLE_SEC),
        float(NVRAM_IDLE_END_SEC),
        float(MIN_GAME_DURATION_SEC),
    )


def _start_nvram_monitor():
    global _nvram_monitor_thread
    with _nvram_monitor_lock:
        if _nvram_monitor_thread is not None and _nvram_monitor_thread.is_alive():
            return
        _log('INFO', 'Starting source thread: nvram')
        _nvram_monitor_thread = threading.Thread(target=run_nvram_monitor, daemon=True)
        _nvram_monitor_thread.start()


def _stop_nvram_monitor(timeout=3.0):
    global _nvram_monitor_ref, _nvram_monitor_thread
    with _nvram_monitor_lock:
        monitor = _nvram_monitor_ref
        thread = _nvram_monitor_thread
        if monitor is not None:
            try:
                monitor.stop()
            except Exception as e:
                _log('WARN', f'Could not stop NVRAM monitor cleanly: {e}')

    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            _log('WARN', 'NVRAM monitor did not stop before timeout; keeping existing monitor')
            return False

    with _nvram_monitor_lock:
        if _nvram_monitor_thread is thread:
            _nvram_monitor_thread = None
        if _nvram_monitor_ref is monitor:
            _nvram_monitor_ref = None
    return True


def _restart_nvram_monitor():
    _log('INFO', 'Restarting NVRAM monitor after settings change')
    if _stop_nvram_monitor():
        _start_nvram_monitor()


# =========================
# MANUAL SEND INPUTS
# =========================
_hotkey_listener = None
_hotkey_listener_combo = ''
_joybutton_listener = None
_joybutton_listener_combo = ()


def _trigger_manual_send(source):
    global _manual_send_inflight, _manual_send_last_signature, _manual_send_last_ts

    rom, score, vpx_file = _get_manual_score_snapshot()
    has_nvram_score = (rom is not None and score is not None and score > 0)
    ocr_needed = not has_nvram_score

    now = time.time()
    with _manual_send_lock:
        if _manual_send_inflight:
            _log('WARN', f'{source} ignored: a manual send is already in progress')
            return
        if has_nvram_score:
            # Full dedup: same rom+score within the window
            signature = (rom, int(score))
            if _manual_send_last_signature == signature and (now - _manual_send_last_ts) < MANUAL_SEND_DEDUPE_SEC:
                _log('WARN', f'{source} ignored: duplicate manual send for {rom} within {MANUAL_SEND_DEDUPE_SEC:.0f}s')
                return
            _manual_send_last_signature = signature
        else:
            # No NVRAM score: time-based dedup only (score will come from OCR)
            if (now - _manual_send_last_ts) < MANUAL_SEND_DEDUPE_SEC:
                _log('WARN', f'{source} ignored: OCR send attempted too quickly after last send')
                return
        _manual_send_inflight = True
        _manual_send_last_ts = now

    if ocr_needed:
        _log('INFO', f'{source} triggered: no NVRAM score — will attempt screenshot OCR')
    else:
        _log('INFO', f'{source} triggered: sending {rom} score={score} (vpx={vpx_file or "unknown"})')

    def _runner():
        global _manual_send_inflight, _manual_send_last_signature
        nonlocal rom, score, vpx_file
        try:
            targets = []
            if VPINPLAY_ENABLED:
                targets.append('vpinplay')
            if WOVP_ENABLED:
                targets.append('wovp')
            if ISCORED_ENABLED:
                targets.append('iscored')

            if not targets:
                _log('WARN', f'{source} pressed but no integration is enabled')
                show_notification('No Integration', 'Enable at least one integration in Settings.', kind='error')
                return

            screenshot = None

            # ------------------------------------------------------------------
            # OCR fallback: no NVRAM score → take a screenshot and detect score
            # ------------------------------------------------------------------
            if ocr_needed:
                if _get_score_ocr() is None:
                    _log('WARN', 'score_ocr module not available. Install pytesseract and/or easyocr.')
                    show_notification('No Score', 'No NVRAM score and OCR is unavailable.', kind='error')
                    return

                _log('INFO', 'Capturing screenshot for OCR score detection...')
                show_notification('Detecting Score...', 'Scanning screenshot for pinball score.')

                ocr_image = capture_screen(screen_id=SCREENSHOT_SCREEN_ID, max_width=None)
                if ocr_image is None:
                    _log('ERROR', 'Screenshot capture failed — cannot detect score via OCR')
                    show_notification('No Score', 'Screenshot capture failed.', kind='error')
                    return

                ocr_score = _detect_score_from_screenshot(ocr_image)
                if not ocr_score:
                    _log('WARN', 'OCR found no valid score in the screenshot')
                    show_notification('No Score', 'No score detected in screenshot.', kind='error')
                    return

                score = ocr_score
                if not vpx_file:
                    vpx_file = _get_best_available_vpx()
                if not rom:
                    # Prefer VPX filename (e.g. "Theatre of Magic.vpx" → "Theatre of Magic")
                    if vpx_file:
                        rom = os.path.splitext(vpx_file)[0]
                    else:
                        rom = _get_best_available_rom() or 'unknown'

                _log('INFO', f'OCR score detected: {score} (rom={rom}, vpx={vpx_file or "unknown"})')

                # Update dedup signature now that we have the real score
                with _manual_send_lock:
                    _manual_send_last_signature = (rom, int(score))

                # Reuse the OCR screenshot for integrations (resized as needed)
                screenshot = _resize_pil_for_send(ocr_image)

            # ------------------------------------------------------------------
            # Normal path: capture screenshot for integrations that need it
            # ------------------------------------------------------------------
            if screenshot is None and ('wovp' in targets or 'iscored' in targets):
                _log('INFO', 'Capturing screenshot for manual send')
                screenshot = capture_screen(
                    screen_id=SCREENSHOT_SCREEN_ID,
                    max_width=SCREENSHOT_MAX_WIDTH,
                )

            _log('INFO', f'Manual send fan-out: {",".join(targets)}')

            if 'vpinplay' in targets:
                try:
                    send_vpinplay_score(rom, score, vpx_file=vpx_file)
                except Exception as e:
                    _log('ERROR', f'VPinPlay submission raised: {e}')

            if 'wovp' in targets:
                try:
                    send_wovp_score(rom, score, screenshot, vpx_file=vpx_file)
                except Exception as e:
                    _log('ERROR', f'WoVP submission raised: {e}')

            if 'iscored' in targets:
                try:
                    send_iscored_score(rom, score, vpx_file=vpx_file, screenshot_image=screenshot)
                except Exception as e:
                    _log('ERROR', f'iScored submission raised: {e}')
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
        self._pygame = None
        self._joysticks = []
        self._last_count = 0
        self._timer = None

    def _use_qt_timer(self):
        return platform.system() == 'Darwin'

    def start(self):
        if self._thread is not None or self._timer is not None:
            return
        if self._use_qt_timer():
            if self._init_pygame():
                try:
                    from PyQt6.QtCore import QTimer
                    self._timer = QTimer()
                    self._timer.setInterval(30)
                    self._timer.timeout.connect(self._poll_once)
                    self._timer.start()
                except Exception as exc:
                    _log('WARN', f'Joystick listener unavailable: Qt timer failed ({exc})')
                    self._shutdown_pygame()
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
            self._shutdown_pygame()
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

    def _init_pygame(self):
        os.environ.setdefault('SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS', '1')
        os.environ.setdefault('PYGAME_HIDE_SUPPORT_PROMPT', '1')
        if platform.system() == 'Darwin':
            # The app already owns the Cocoa/Qt event loop. For joystick polling,
            # keep pygame's SDL video backend away from Cocoa to avoid AppKit
            # event pumping crashes and SDL class collisions with OpenCV.
            os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', message='pkg_resources is deprecated as an API.*')
                import pygame
        except Exception as exc:
            _log('WARN', f'Joystick listener unavailable: pygame import failed ({exc})')
            return False

        try:
            pygame.init()
            pygame.joystick.init()
        except Exception as exc:
            _log('WARN', f'Joystick listener unavailable: pygame init failed ({exc})')
            return False

        self._pygame = pygame

        _log(
            'INFO',
            (
                'Starting joystick listener: '
                f'buttons={",".join(str(b) for b in self.buttons)}'
            ),
        )
        self._joysticks = self._refresh_joysticks(pygame)
        self._last_count = len(self._joysticks)
        if self._last_count == 0:
            _log('WARN', 'Joystick listener started but no joystick/gamepad is available')
        return True

    def _shutdown_pygame(self):
        pygame = self._pygame
        self._pygame = None
        self._joysticks = []
        self._last_count = 0
        if pygame is None:
            return
        try:
            pygame.joystick.quit()
        except Exception:
            pass
        try:
            pygame.quit()
        except Exception:
            pass

    def _poll_once(self):
        pygame = self._pygame
        if pygame is None:
            return
        try:
            pygame.event.pump()
        except Exception:
            pass

        current_count = 0
        try:
            current_count = pygame.joystick.get_count()
        except Exception:
            current_count = self._last_count
        if current_count != self._last_count:
            self._joysticks = self._refresh_joysticks(pygame)
            self._last_count = len(self._joysticks)

        combo_pressed = False
        for joy in self._joysticks:
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

    def _run(self):
        if not self._init_pygame():
            return
        try:
            while not self._stop_event.is_set():
                self._poll_once()
                time.sleep(0.03)
        finally:
            self._shutdown_pygame()


def _four_char_code(value: str) -> int:
    raw = str(value or '')[:4].ljust(4, '\0').encode('macroman', errors='replace')
    return int.from_bytes(raw, byteorder='big')


def _macos_keycode_for_token(token: str):
    token = str(token or '').strip().lower()
    keycodes = {
        'a': 0, 's': 1, 'd': 2, 'f': 3, 'h': 4, 'g': 5, 'z': 6, 'x': 7,
        'c': 8, 'v': 9, 'b': 11, 'q': 12, 'w': 13, 'e': 14, 'r': 15,
        'y': 16, 't': 17, '1': 18, '2': 19, '3': 20, '4': 21, '6': 22,
        '5': 23, '=': 24, '9': 25, '7': 26, '-': 27, '8': 28, '0': 29,
        ']': 30, 'o': 31, 'u': 32, '[': 33, 'i': 34, 'p': 35, 'l': 37,
        'j': 38, "'": 39, 'k': 40, ';': 41, '\\': 42, ',': 43, '/': 44,
        'n': 45, 'm': 46, '.': 47, 'tab': 48, 'space': 49, '`': 50,
        'backspace': 51, 'delete': 51, 'escape': 53, 'esc': 53,
        'return': 36, 'enter': 36,
    }
    return keycodes.get(token)


def _parse_macos_carbon_hotkey(binding: str):
    tokens = [part.strip().lower() for part in str(binding or '').split('+') if part.strip()]
    modifiers = 0
    key_code = None

    # Carbon Event Manager modifier masks.
    cmd_key = 1 << 8
    shift_key = 1 << 9
    option_key = 1 << 11
    control_key = 1 << 12

    for token in tokens:
        if token in ('cmd', 'command', 'meta', 'super', 'win', 'windows'):
            modifiers |= cmd_key
        elif token in ('shift',):
            modifiers |= shift_key
        elif token in ('alt', 'option'):
            modifiers |= option_key
        elif token in ('ctrl', 'control'):
            modifiers |= control_key
        else:
            key_code = _macos_keycode_for_token(token)

    if key_code is None:
        raise ValueError(f'unsupported macOS hotkey binding: {binding}')
    return key_code, modifiers


class _MacCarbonHotkeyListener:
    def __init__(self, binding: str, callback):
        self.binding = binding
        self._callback = callback
        self._carbon = None
        self._handler_proc = None
        self._handler_ref = None
        self._hotkey_ref = None

    def start(self):
        import ctypes

        key_code, modifiers = _parse_macos_carbon_hotkey(self.binding)
        carbon = ctypes.cdll.LoadLibrary('/System/Library/Frameworks/Carbon.framework/Carbon')

        class EventTypeSpec(ctypes.Structure):
            _fields_ = [
                ('eventClass', ctypes.c_uint32),
                ('eventKind', ctypes.c_uint32),
            ]

        class EventHotKeyID(ctypes.Structure):
            _fields_ = [
                ('signature', ctypes.c_uint32),
                ('id', ctypes.c_uint32),
            ]

        handler_type = ctypes.CFUNCTYPE(
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )

        def _handler(_next_handler, _event, _user_data):
            try:
                self._callback()
            except Exception:
                pass
            return 0

        self._handler_proc = handler_type(_handler)

        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        target = carbon.GetApplicationEventTarget()
        if not target:
            raise RuntimeError('GetApplicationEventTarget returned null')

        carbon.InstallApplicationEventHandler.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(EventTypeSpec),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.InstallApplicationEventHandler.restype = ctypes.c_int32

        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            EventHotKeyID,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32

        event_types = (EventTypeSpec * 1)(EventTypeSpec(_four_char_code('keyb'), 5))
        handler_ref = ctypes.c_void_p()
        status = carbon.InstallApplicationEventHandler(
            ctypes.cast(self._handler_proc, ctypes.c_void_p),
            1,
            event_types,
            None,
            ctypes.byref(handler_ref),
        )
        if status != 0:
            raise RuntimeError(f'InstallApplicationEventHandler failed with status {status}')

        hotkey_ref = ctypes.c_void_p()
        hotkey_id = EventHotKeyID(_four_char_code('vplc'), 1)
        status = carbon.RegisterEventHotKey(
            int(key_code),
            int(modifiers),
            hotkey_id,
            target,
            0,
            ctypes.byref(hotkey_ref),
        )
        if status != 0:
            try:
                carbon.RemoveEventHandler(handler_ref)
            except Exception:
                pass
            raise RuntimeError(f'RegisterEventHotKey failed with status {status}')

        self._carbon = carbon
        self._handler_ref = handler_ref
        self._hotkey_ref = hotkey_ref

    def stop(self):
        if self._carbon is not None:
            if self._hotkey_ref is not None:
                try:
                    self._carbon.UnregisterEventHotKey(self._hotkey_ref)
                except Exception:
                    pass
            if self._handler_ref is not None:
                try:
                    self._carbon.RemoveEventHandler(self._handler_ref)
                except Exception:
                    pass
        self._handler_ref = None
        self._hotkey_ref = None
        self._handler_proc = None
        self._carbon = None


def _parse_macos_appkit_hotkey(binding: str):
    tokens = [part.strip().lower() for part in str(binding or '').split('+') if part.strip()]
    key = ''
    required = 0

    # NSEventModifierFlag* values.
    shift = 1 << 17
    control = 1 << 18
    option = 1 << 19
    command = 1 << 20

    for token in tokens:
        if token in ('cmd', 'command', 'meta', 'super', 'win', 'windows'):
            required |= command
        elif token in ('shift',):
            required |= shift
        elif token in ('alt', 'option'):
            required |= option
        elif token in ('ctrl', 'control'):
            required |= control
        else:
            key = token.lower()

    if not key:
        raise ValueError(f'unsupported macOS hotkey binding: {binding}')
    return key, required


class _MacAppKitHotkeyListener:
    def __init__(self, binding: str, callback):
        self.binding = binding
        self._callback = callback
        self._global_monitor = None
        self._local_monitor = None
        self._key = ''
        self._required_modifiers = 0

    def start(self):
        import AppKit

        self._key, self._required_modifiers = _parse_macos_appkit_hotkey(self.binding)
        mask = AppKit.NSEventMaskKeyDown
        self._global_monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            mask,
            self._handle_event,
        )
        self._local_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            mask,
            self._handle_local_event,
        )
        if self._global_monitor is None and self._local_monitor is None:
            raise RuntimeError('AppKit returned no keyboard monitor')

    def _event_matches(self, event) -> bool:
        try:
            if hasattr(event, 'isARepeat') and event.isARepeat():
                return False
            chars = str(event.charactersIgnoringModifiers() or '').lower()
            flags = int(event.modifierFlags()) & 0xFFFF0000
            return chars == self._key and (flags & self._required_modifiers) == self._required_modifiers
        except Exception:
            return False

    def _handle_event(self, event):
        if self._event_matches(event):
            self._callback()

    def _handle_local_event(self, event):
        self._handle_event(event)
        return event

    def stop(self):
        try:
            import AppKit
            if self._global_monitor is not None:
                AppKit.NSEvent.removeMonitor_(self._global_monitor)
            if self._local_monitor is not None:
                AppKit.NSEvent.removeMonitor_(self._local_monitor)
        except Exception:
            pass
        self._global_monitor = None
        self._local_monitor = None


def _start_hotkey_listener():
    global _hotkey_listener, _hotkey_listener_combo

    hotkey_combo, display_combo = _build_pynput_hotkey(MANUAL_SEND_KEYBOARD_BINDING)
    if not hotkey_combo:
        _stop_hotkey_listener()
        return
    if _hotkey_listener is not None and _hotkey_listener_combo == hotkey_combo:
        return

    _stop_hotkey_listener()

    _log('INFO', f'Starting hotkey listener: {display_combo}')
    if platform.system() == 'Darwin':
        listener = None
        last_exc = None
        for label, candidate in (
            ('Carbon', _MacCarbonHotkeyListener(MANUAL_SEND_KEYBOARD_BINDING, _on_hotkey_pressed)),
            ('AppKit', _MacAppKitHotkeyListener(MANUAL_SEND_KEYBOARD_BINDING, _on_hotkey_pressed)),
        ):
            try:
                candidate.start()
                listener = candidate
                _log('INFO', f'Hotkey listener backend: {label}')
                break
            except Exception as exc:
                last_exc = exc
                _log('WARN', f'{label} hotkey backend failed: {exc}')
        if listener is None:
            _log('ERROR', f'Hotkey listener failed to start: {last_exc}')
            return
    else:
        try:
            from pynput import keyboard as _pynput_kb
        except Exception as exc:
            _log('WARN', f'Hotkey listener unavailable: pynput import failed ({exc})')
            return
        listener = _pynput_kb.GlobalHotKeys({hotkey_combo: _on_hotkey_pressed})
        listener.daemon = True
        try:
            listener.start()
        except Exception as exc:
            _log('ERROR', f'Hotkey listener failed to start: {exc}')
            return
    _hotkey_listener = listener
    _hotkey_listener_combo = hotkey_combo
    _log('INFO', f'Hotkey listener started: {display_combo}')


def _stop_hotkey_listener():
    global _hotkey_listener, _hotkey_listener_combo
    if _hotkey_listener is not None:
        _log('INFO', 'Stopping hotkey listener')
        _hotkey_listener.stop()
        _hotkey_listener = None
    _hotkey_listener_combo = ''


def _start_joybutton_listener():
    global _joybutton_listener, _joybutton_listener_combo

    if not _joystick_binding_enabled():
        _stop_joybutton_listener()
        return
    combo = tuple(MANUAL_SEND_JOYSTICK_BUTTONS)
    if _joybutton_listener is not None and _joybutton_listener_combo == combo:
        return

    _stop_joybutton_listener()

    _joybutton_listener = _JoyButtonListener(MANUAL_SEND_JOYSTICK_BUTTONS)
    _joybutton_listener.start()
    _joybutton_listener_combo = combo


def _stop_joybutton_listener():
    global _joybutton_listener, _joybutton_listener_combo
    if _joybutton_listener is not None:
        _log('INFO', 'Stopping joystick listener')
        _joybutton_listener.stop()
        _joybutton_listener = None
    _joybutton_listener_combo = ()


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


def _any_integration_enabled() -> bool:
    return bool(VPINPLAY_ENABLED or WOVP_ENABLED or ISCORED_ENABLED)


def _integration_configured(name: str) -> bool:
    if name == 'wovp':
        return bool(config.get('wovp', 'api_key', fallback='').strip())
    if name == 'iscored':
        return bool(config.get('iscored', 'player_name', fallback='').strip())
    if name == 'vpinplay':
        return bool(
            config.get('vpinplay', 'api_url', fallback='').strip()
            and config.get('vpinplay', 'user_id', fallback='').strip()
            and config.get('vpinplay', 'initials', fallback='').strip()
        )
    return False


def _refresh_manual_send_listeners():
    """Start/stop the hotkey + joystick listeners based on whether any
    integration is enabled. Safe to call repeatedly."""
    if _any_integration_enabled():
        _start_manual_send_listeners()
    else:
        _stop_manual_send_listeners()


def _load_wovp_challenges_sync():
    if not WOVP_ENABLED:
        _log('INFO', 'WOVP: skipping challenge fetch - integration disabled')
        return []
    try:
        from wovp_client import WovpClient
        wovp = WovpClient(CONFIG_PATH)
        if not wovp.api_key:
            _log('INFO', 'WOVP: skipping challenge fetch - no api_key configured in [wovp]')
            return []
        _log('INFO', 'WOVP: preloading active challenges…')
        challenges = wovp.search_challenges()
        _log('INFO', f'WOVP: {len(challenges)} challenge(s) preloaded')
        return challenges
    except Exception as e:
        _log('ERROR', f'WOVP challenges preload failed: {e}')
        return []


def _load_iscored_games_sync():
    if not ISCORED_ENABLED:
        _log('INFO', 'iScored: skipping games fetch - integration disabled')
        return []
    try:
        from iscored_client import IScoredClient
        iscored = IScoredClient(CONFIG_PATH)
        if not iscored.player_name:
            _log('INFO', 'iScored: skipping games fetch - no player_name configured in [iscored]')
            return []
        _log('INFO', f'iScored: preloading games from {len(iscored.room_urls)} room(s)…')
        games = iscored.list_all_games(request_timeout=10)
        _log('INFO', f'iScored: {len(games)} game(s) preloaded; saving cache')
        iscored.save_games_cache(games)
        return games
    except Exception as e:
        _log('ERROR', f'iScored games preload failed: {e}')
        try:
            from iscored_client import IScoredClient
            return IScoredClient(CONFIG_PATH).load_games_cache()
        except Exception:
            return []


def preload_tray_data():
    global _preloaded_wovp_challenges, _preloaded_iscored_games
    _preloaded_wovp_challenges = _load_wovp_challenges_sync()
    _preloaded_iscored_games = _load_iscored_games_sync()


def _refresh_wovp_challenges_bg():
    """Refresh WoVP challenges in the background and hand UI updates to Qt."""
    def _run():
        challenges = _load_wovp_challenges_sync()
        global _preloaded_wovp_challenges
        _preloaded_wovp_challenges = challenges
        tray = _tray_ref
        if tray is not None:
            try:
                tray.wovp_challenges_loaded.emit(challenges)
            except Exception as e:
                _log('WARN', f'WoVP menu refresh signal failed: {e}')
        if challenges:
            show_notification('WoVP', f'Refreshed {len(challenges)} challenge(s).')

    threading.Thread(target=_run, daemon=True).start()


def _refresh_iscored_games_bg():
    """Refresh iScored games in the background and hand UI updates to Qt."""
    def _run():
        games = _load_iscored_games_sync()
        global _preloaded_iscored_games
        _preloaded_iscored_games = games
        tray = _tray_ref
        if tray is not None:
            try:
                tray.iscored_games_loaded.emit(games)
            except Exception as e:
                _log('WARN', f'iScored menu refresh signal failed: {e}')
        if games:
            show_notification('iScored', f'Refreshed {len(games)} game(s).')

    threading.Thread(target=_run, daemon=True).start()


def _run_desktop_app():
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import QAction, QActionGroup, QIcon
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    from notifier import NotificationOverlay

    def _set_macos_accessory_policy():
        if platform.system() != 'Darwin':
            return True
        try:
            from AppKit import NSApplication
            ns_app = NSApplication.sharedApplication()
            # Accessory keeps the tray-app feel while allowing normal windows.
            ns_app.setActivationPolicy_(1)
            return True
        except Exception as exc:
            _log('WARN', f'macOS accessory activation policy failed: {exc}')
            return False

    def _activate_app_for_dialog():
        if platform.system() != 'Darwin':
            return
        try:
            from AppKit import NSApplication
            _set_macos_accessory_policy()
            ns_app = NSApplication.sharedApplication()
            ns_app.activateIgnoringOtherApps_(True)
        except Exception as exc:
            _log('WARN', f'macOS app activation for Settings failed: {exc}')

    class VPinScoreTray(QSystemTrayIcon):
        notify_requested = pyqtSignal(str, str, str)
        menu_update_requested = pyqtSignal()
        wovp_challenges_loaded = pyqtSignal(object)
        iscored_games_loaded = pyqtSignal(object)

        def __init__(self, icon, parent=None):
            super().__init__(icon, parent)
            self.notify_requested.connect(self.display_overlay)
            self.menu_update_requested.connect(self._schedule_menu_update)
            self.wovp_challenges_loaded.connect(self._apply_wovp_challenges)
            self.iscored_games_loaded.connect(self._apply_iscored_games)

            self.menu = QMenu(parent)
            self._settings_dialog = None

            # ── Integration toggles ────────────────────────────────────
            self.act_vpinplay_enable = QAction('Enable VPinPlay', self.menu)
            self.act_vpinplay_enable.setCheckable(True)
            self.act_vpinplay_enable.triggered.connect(lambda: self._toggle_integration('vpinplay'))
            self.menu.addAction(self.act_vpinplay_enable)

            self.act_wovp_enable = QAction('Enable WoVP', self.menu)
            self.act_wovp_enable.setCheckable(True)
            self.act_wovp_enable.triggered.connect(lambda: self._toggle_integration('wovp'))
            self.menu.addAction(self.act_wovp_enable)

            self.act_iscored_enable = QAction('Enable iScored', self.menu)
            self.act_iscored_enable.setCheckable(True)
            self.act_iscored_enable.triggered.connect(lambda: self._toggle_integration('iscored'))
            self.menu.addAction(self.act_iscored_enable)

            self.menu.addSeparator()

            # ── Selection actions ──────────────────────────────────────
            self.wovp_challenge_actions = []
            self.iscored_game_actions = []
            self.screen_actions = []
            self.wovp_challenge_group = QActionGroup(self.menu)
            self.wovp_challenge_group.setExclusive(True)
            self.iscored_game_group = QActionGroup(self.menu)
            self.iscored_game_group.setExclusive(True)
            self.screen_action_group = QActionGroup(self.menu)
            self.screen_action_group.setExclusive(True)

            self.iscored_section_marker = self.menu.addSeparator()
            self.capture_section_marker = self.menu.addSeparator()
            self.selection_end_marker = self.menu.addSeparator()

            self._rebuild_challenges_menu(_preloaded_wovp_challenges)
            self._rebuild_games_menu(_preloaded_iscored_games)
            self._populate_screen_actions()

            self.act_settings = QAction('Settings…', self.menu)
            self.act_settings.triggered.connect(self._open_settings_dialog)
            self.menu.addAction(self.act_settings)

            self.act_exit = QAction('Exit', self.menu)
            self.act_exit.triggered.connect(QApplication.instance().quit)
            self.menu.addAction(self.act_exit)

            self.setContextMenu(self.menu)
            self.active_notification = None
            self.update_menu_state()

        def update_menu_state(self):
            from wovp_client import WovpClient

            self.act_vpinplay_enable.setChecked(VPINPLAY_ENABLED)
            self.act_vpinplay_enable.setText(
                'VPinPlay Enabled' if VPINPLAY_ENABLED else 'Enable VPinPlay'
            )
            self.act_wovp_enable.setChecked(WOVP_ENABLED)
            self.act_wovp_enable.setText(
                'WoVP Enabled' if WOVP_ENABLED else 'Enable WoVP'
            )
            self.act_iscored_enable.setChecked(ISCORED_ENABLED)
            self.act_iscored_enable.setText(
                'iScored Enabled' if ISCORED_ENABLED else 'Enable iScored'
            )

            # WoVP challenges: accessible whenever api_key is present so the user
            # can pre-configure a challenge without having to enable WoVP first.
            wovp = WovpClient(CONFIG_PATH)
            for act in self.wovp_challenge_actions:
                act.setEnabled(bool(wovp.api_key) and act.data() not in ('empty', 'header'))
            try:
                from iscored_client import IScoredClient
                iscored_ready = IScoredClient(CONFIG_PATH).is_ready()
            except Exception:
                iscored_ready = False
            for act in self.iscored_game_actions:
                act.setEnabled(iscored_ready and act.data() not in ('empty', 'header'))

            # Tooltip summarises which integrations are live.
            enabled_labels = [
                name for name, on in (
                    ('VPinPlay', VPINPLAY_ENABLED),
                    ('WoVP', WOVP_ENABLED),
                    ('iScored', ISCORED_ENABLED),
                ) if on
            ]
            if enabled_labels:
                self.setToolTip('VPinLeaders Client | ' + ', '.join(enabled_labels))
            else:
                self.setToolTip('VPinLeaders Client | no integration enabled')

        def _defer_menu_update(self):
            self.menu_update_requested.emit()

        def _schedule_menu_update(self):
            QTimer.singleShot(150, self.update_menu_state)

        def _toggle_integration(self, name):
            global WOVP_ENABLED, ISCORED_ENABLED, VPINPLAY_ENABLED

            currently_enabled = {
                'vpinplay': VPINPLAY_ENABLED,
                'wovp': WOVP_ENABLED,
                'iscored': ISCORED_ENABLED,
            }.get(name)
            if currently_enabled is None:
                return

            if not currently_enabled and not _integration_configured(name):
                _log('INFO', f'Integration {name} needs setup before enabling')
                self._defer_menu_update()
                QTimer.singleShot(150, lambda n=name: self._open_integration_setup(n))
                return

            if name == 'vpinplay':
                VPINPLAY_ENABLED = not VPINPLAY_ENABLED
                new_value = VPINPLAY_ENABLED
            elif name == 'wovp':
                WOVP_ENABLED = not WOVP_ENABLED
                new_value = WOVP_ENABLED
            elif name == 'iscored':
                ISCORED_ENABLED = not ISCORED_ENABLED
                new_value = ISCORED_ENABLED
            else:
                return

            if name not in config:
                config[name] = {}
            config[name]['enable'] = 'true' if new_value else 'false'
            save_config()

            _refresh_manual_send_listeners()
            _log('INFO', f'Integration {name} {"enabled" if new_value else "disabled"}')
            if new_value and name == 'wovp':
                _refresh_wovp_challenges_bg()
            elif new_value and name == 'iscored':
                _refresh_iscored_games_bg()
            self._defer_menu_update()

        def _open_settings_dialog(self):
            QTimer.singleShot(150, self._show_settings_dialog)

        def _show_settings_dialog(self):
            try:
                from settings_ui import SettingsDialog
            except Exception as exc:
                _log('ERROR', f'Settings dialog unavailable: {exc}')
                return

            if self._settings_dialog is not None and self._settings_dialog.isVisible():
                _log('INFO', 'Settings dialog already open; raising existing window')
                self._settings_dialog.raise_()
                self._settings_dialog.activateWindow()
                return

            _log('INFO', 'Opening settings dialog')
            try:
                dlg = SettingsDialog(CONFIG_PATH)
                dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
                self._settings_dialog = dlg
                _log('INFO', 'Settings dialog constructed')
            except Exception as exc:
                _log('ERROR', f'Settings dialog construction failed: {exc}')
                self._settings_dialog = None
                return

            def _on_saved():
                old_monitor_config = _nvram_monitor_config_signature()
                load_config()
                new_monitor_config = _nvram_monitor_config_signature()
                self._populate_screen_actions()
                _refresh_manual_send_listeners()
                if old_monitor_config != new_monitor_config:
                    _restart_nvram_monitor()
                if WOVP_ENABLED:
                    _refresh_wovp_challenges_bg()
                else:
                    self._apply_wovp_challenges([])
                if ISCORED_ENABLED:
                    _refresh_iscored_games_bg()
                else:
                    self._apply_iscored_games([])
                _log('INFO', 'Settings updated; config reloaded')
                self._defer_menu_update()

            def _on_finished(_result):
                _log('INFO', 'Settings dialog closed')
                self._settings_dialog = None
                self._defer_menu_update()

            dlg.accepted.connect(_on_saved)
            dlg.finished.connect(_on_finished)
            _activate_app_for_dialog()
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            QApplication.processEvents()
            _log('INFO', f'Settings dialog show requested; visible={dlg.isVisible()}')

        def _open_integration_setup(self, name):
            try:
                from settings_ui import open_integration_setup
            except Exception as exc:
                _log('ERROR', f'Integration setup unavailable: {exc}')
                return

            saved = open_integration_setup(CONFIG_PATH, name)
            if saved:
                load_config()
                self._populate_screen_actions()
                _refresh_manual_send_listeners()
                if name == 'wovp':
                    _refresh_wovp_challenges_bg()
                elif name == 'iscored':
                    _refresh_iscored_games_bg()
                _log('INFO', f'Integration {name} configured and enabled')
            else:
                _log('INFO', f'Integration {name} setup cancelled')
            self._defer_menu_update()

        def _populate_screen_actions(self):
            """Rebuilds the screen command list from the currently connected displays."""
            # Remove any existing screen actions
            for act in self.screen_actions:
                self.screen_action_group.removeAction(act)
                self.menu.removeAction(act)
            self.screen_actions = []

            screens = QApplication.instance().screens()
            selected_idx = SCREENSHOT_SCREEN_ID if SCREENSHOT_SCREEN_ID is not None else 0
            if screens:
                header = QAction('Capture Display', self.menu)
                header.setData('header')
                header.setEnabled(False)
                self.screen_actions.append(header)
                self.menu.insertAction(self.selection_end_marker, header)

            for idx, screen in enumerate(screens):
                geom = screen.geometry()
                name = screen.name() or f'Screen {idx}'
                label = f'Screen {idx}  -  {name}  ({geom.width()}x{geom.height()})'
                act = QAction(label, self.menu)
                act.setCheckable(True)
                act.setChecked(idx == selected_idx)
                self.screen_action_group.addAction(act)
                act.triggered.connect(lambda checked, i=idx: self._select_screen(i))
                self.screen_actions.append(act)
                self.menu.insertAction(self.selection_end_marker, act)

        def _select_screen(self, screen_idx):
            global SCREENSHOT_SCREEN_ID
            SCREENSHOT_SCREEN_ID = screen_idx
            if 'screenshot' not in config:
                config['screenshot'] = {}
            config['screenshot']['screen_to_capture'] = str(screen_idx)
            save_config()
            QTimer.singleShot(150, self._populate_screen_actions)
            self._defer_menu_update()

        def _apply_wovp_challenges(self, challenges):
            self._rebuild_challenges_menu(challenges)
            self._defer_menu_update()

        def _apply_iscored_games(self, games):
            self._rebuild_games_menu(games)
            self._defer_menu_update()

        def _rebuild_games_menu(self, games):
            from iscored_client import IScoredClient

            for act in self.iscored_game_actions:
                self.iscored_game_group.removeAction(act)
                self.menu.removeAction(act)
            self.iscored_game_actions = []

            selected_gameroom, selected_game_id, _selected_game_name = IScoredClient(CONFIG_PATH).get_selected_game()

            header = QAction('iScored Game', self.menu)
            header.setData('header')
            header.setEnabled(False)
            self.iscored_game_actions.append(header)
            self.menu.insertAction(self.capture_section_marker, header)

            if not games:
                empty = QAction('No iScored games found', self.menu)
                empty.setData('empty')
                empty.setEnabled(False)
                self.iscored_game_actions.append(empty)
                self.menu.insertAction(self.capture_section_marker, empty)
            else:
                multi_room = len({g.get('room_url') for g in games}) > 1
                for g in games:
                    flags = []
                    if g.get('isGameLocked'):
                        flags.append('locked')
                    if g.get('hidden'):
                        flags.append('hidden')
                    flag_str = f"  [{', '.join(flags)}]" if flags else ''
                    room_suffix = f"  -  {g.get('room_name')}" if multi_room else ''
                    label = (
                        f"{g.get('name') or '(unnamed)'}  -  ID {g.get('id')}"
                        f"{flag_str}{room_suffix}"
                    )
                    gameroom = str(g.get('gameroom') or g.get('room_url') or '')
                    game_id = str(g.get('id') or '')
                    game_name = str(g.get('name') or '')
                    item = QAction(label, self.menu)
                    item.setCheckable(True)
                    item.setChecked(gameroom == selected_gameroom and game_id == selected_game_id)
                    self.iscored_game_group.addAction(item)
                    item.triggered.connect(
                        lambda checked, gr=gameroom, gid=game_id, gname=game_name: self._select_iscored_game(gr, gid, gname)
                    )
                    self.iscored_game_actions.append(item)
                    self.menu.insertAction(self.capture_section_marker, item)

            refresh_act = QAction('Refresh iScored Games', self.menu)
            refresh_act.triggered.connect(lambda: _refresh_iscored_games_bg())
            self.iscored_game_actions.append(refresh_act)
            self.menu.insertAction(self.capture_section_marker, refresh_act)

        def _select_iscored_game(self, gameroom, game_id, game_name):
            from iscored_client import IScoredClient
            iscored = IScoredClient(CONFIG_PATH)
            iscored.set_selected_game(gameroom, game_id, game_name)
            _log('INFO', f'iScored game selected: gameroom={gameroom} game_id={game_id} name={game_name!r}')
            QTimer.singleShot(150, lambda: self._rebuild_games_menu(_preloaded_iscored_games))
            self._defer_menu_update()

        def _rebuild_challenges_menu(self, challenges):
            from wovp_client import WovpClient

            for act in self.wovp_challenge_actions:
                self.wovp_challenge_group.removeAction(act)
                self.menu.removeAction(act)
            self.wovp_challenge_actions = []

            wovp = WovpClient(CONFIG_PATH)
            selected_id, _ = wovp.get_selected_challenge()

            header = QAction('WoVP Challenge', self.menu)
            header.setData('header')
            header.setEnabled(False)
            self.wovp_challenge_actions.append(header)
            self.menu.insertAction(self.iscored_section_marker, header)

            if not challenges:
                empty = QAction('No active challenges found', self.menu)
                empty.setData('empty')
                empty.setEnabled(False)
                self.wovp_challenge_actions.append(empty)
                self.menu.insertAction(self.iscored_section_marker, empty)
            else:
                for ch in challenges:
                    ch_id = ch['id']
                    ch_name = ch['name']
                    act = QAction(ch_name, self.menu)
                    act.setCheckable(True)
                    act.setChecked(ch_id == selected_id)
                    self.wovp_challenge_group.addAction(act)
                    act.triggered.connect(
                        lambda checked, cid=ch_id, cname=ch_name: self._select_challenge(cid, cname)
                    )
                    self.wovp_challenge_actions.append(act)
                    self.menu.insertAction(self.iscored_section_marker, act)

            refresh_act = QAction('Refresh WoVP Challenges', self.menu)
            refresh_act.triggered.connect(lambda: _refresh_wovp_challenges_bg())
            self.wovp_challenge_actions.append(refresh_act)
            self.menu.insertAction(self.iscored_section_marker, refresh_act)

        def _select_challenge(self, challenge_id, challenge_name):
            from wovp_client import WovpClient
            wovp = WovpClient(CONFIG_PATH)
            wovp.set_selected_challenge(challenge_id, challenge_name)
            self._refresh_tooltip_only()
            QTimer.singleShot(150, lambda: self._rebuild_challenges_menu(_preloaded_wovp_challenges))
            self._defer_menu_update()

        def _refresh_tooltip_only(self):
            """Lightweight tooltip refresh that avoids touching submenu structure."""
            try:
                from wovp_client import WovpClient
                parts = []
                if VPINPLAY_ENABLED:
                    parts.append('VPinPlay')
                if WOVP_ENABLED:
                    _, name = WovpClient(CONFIG_PATH).get_selected_challenge()
                    parts.append(f"WoVP: {name or 'no challenge'}")
                if ISCORED_ENABLED:
                    parts.append('iScored')
                self.setToolTip('VPinLeaders Client | ' + (', '.join(parts) or 'idle'))
            except Exception:
                pass

        def display_overlay(self, title, message, kind):
            if self.active_notification:
                try:
                    self.active_notification.close()
                except Exception:
                    pass

            self.active_notification = NotificationOverlay(title, message, kind=kind)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    _set_macos_accessory_policy()
    signal_timer = _install_desktop_signal_handlers(app, QTimer)

    path_to_icon = resource_path('assets/icon.png')
    if os.path.exists(path_to_icon):
        icon = QIcon(path_to_icon)
    else:
        from PyQt6.QtGui import QColor, QPixmap

        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor('blue'))
        icon = QIcon(pixmap)

    global _tray_ref
    tray = VPinScoreTray(icon)
    _tray_ref = tray
    tray.show()
    _set_notification_sink(lambda title, message, kind: tray.notify_requested.emit(title, message, kind))

    enabled_labels = ','.join(
        name for name, on in (
            ('vpinplay', VPINPLAY_ENABLED),
            ('wovp', WOVP_ENABLED),
            ('iscored', ISCORED_ENABLED),
        ) if on
    ) or 'none'
    _log(
        'INFO',
        f'Startup: enabled={enabled_labels} | '
        f'keyboard={"on" if _keyboard_binding_enabled() else "off"} '
        f'({MANUAL_SEND_KEYBOARD_BINDING or "none"}) | '
        f'joystick={"on" if _joystick_binding_enabled() else "off"}',
    )

    _start_nvram_monitor()

    _refresh_manual_send_listeners()
    if not _any_integration_enabled():
        _log('INFO', 'No integration enabled — manual send listeners are idle')

    app._vpin_signal_timer = signal_timer
    return app.exec()


if __name__ == '__main__':
    CONFIG_OVERRIDE_PATH = _extract_config_override(sys.argv[1:])

    _config_path_at_start = _config_path()
    _config_exists_at_start = os.path.exists(_config_path_at_start)

    if not _config_exists_at_start:
        try:
            from settings_ui import run_first_run_wizard
            if not run_first_run_wizard(_config_path_at_start):
                print('Setup cancelled. Exiting.', file=sys.stderr)
                sys.exit(0)
        except Exception as exc:
            print(f'ERROR: first-run wizard failed: {exc}', file=sys.stderr)
            sys.exit(1)

    load_config()

    required = [('nvram.base_dir', NVRAM_DIR)]
    missing = [k for k, v in required if not v or not v.strip()]
    if missing:
        _log('ERROR', f"Missing required config value(s): {', '.join(missing)}")
        sys.exit(1)

    preload_tray_data()
    sys.exit(_run_desktop_app())
