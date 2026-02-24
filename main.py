import websocket
import json
import requests
import threading
import os
import time
import sys
import configparser
import platform

# PyQt6 Imports
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QInputDialog
from PyQt6.QtGui import QIcon, QAction, QActionGroup
from PyQt6.QtCore import QObject, pyqtSignal

# Custom Notifier
from notifier import NotificationOverlay

# Screenshot capture
from screenshot import capture_screen
from PIL import Image

# Global hotkey listener
from pynput import keyboard as pynput_keyboard

# =========================
# LOGGING CONFIG
# =========================
from datetime import datetime

def _ts():
    """Return a timestamp string matching VPX log format: 2026-02-10 18:58:43.893"""
    now = datetime.now()
    return now.strftime('%Y-%m-%d %H:%M:%S.') + f'{now.microsecond // 1000:03d}'

def _log(level, msg):
    """Print a log line with VPX-style timestamp."""
    print(f"{_ts()} {level}  [ScoreSender] {msg}")

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# =========================
# CONFIG & STATE
# =========================
config = configparser.ConfigParser()

CURRENT_MODE = "scores" # scores, challenge
SEND_MODE = "automatic" # automatic, manual
sent_sessions = set()
game_session_data = {}

# Connection timestamp: ignore messages older than when we connected
ws_connected_at = None  # datetime (UTC)

# Debounce: track last processed game_end per ROM to prevent duplicates
last_game_end = {}  # rom_name -> time.time()

SCREENSHOT_ENABLED = False         # Whether to capture and send screenshots
SCREENSHOT_MAX_WIDTH = 800         # Max width in pixels for screenshot resize (0 = no resize)
SCREENSHOT_JPEG_QUALITY = 75       # JPEG quality (1-100) for compressed screenshots
MIN_GAME_DURATION_SEC = 60         # Minimum game duration in seconds to accept a game_end

# Last known score for manual mode hotkey trigger
_last_score_rom = None      # str: ROM name of last known score
_last_score_value = None    # int: score value of last known score
_last_score_lock = threading.Lock()

# Signal Manager for Thread Safety
class SignalManager(QObject):
    show_notification_signal = pyqtSignal(str, str)
    def __init__(self):
        super().__init__()

# Instantiate AFTER app creation in main if possible, or global is fine if app created later.
# For debugging, let's keep it global but ensure QObject init happens.
signals = SignalManager()

def load_config():
    global API_URL, API_KEY, MACHINE_ID, CURRENT_MODE, SEND_MODE, SCORE_HOST, SCORE_PORT, SCREENSHOT_ENABLED, SCREENSHOT_SCREEN_ID, SCREENSHOT_MAX_WIDTH, SCREENSHOT_JPEG_QUALITY
    try:
        config.read('config.ini')

        # Credentials
        if 'credentials' in config:
            API_URL = config['credentials'].get('api_url', '')
            API_KEY = config['credentials'].get('api_key', '')
            MACHINE_ID = config['credentials'].get('machine_id', '')

        # Server
        if 'score-server' in config:
            SCORE_HOST = config['score-server'].get('host', 'localhost')
            SCORE_PORT = config['score-server'].get('port', '3131')

        # Screenshot
        if 'screenshot' in config:
            SCREENSHOT_ENABLED = config['screenshot'].get('enable', 'false').strip().lower() == 'true'
            sid = config['screenshot'].get('capture_screen', '').strip()
            SCREENSHOT_SCREEN_ID = int(sid) if sid else None
            mw = config['screenshot'].get('max_width', '').strip()
            SCREENSHOT_MAX_WIDTH = int(mw) if mw else SCREENSHOT_MAX_WIDTH
            jq = config['screenshot'].get('jpeg_quality', '').strip()
            SCREENSHOT_JPEG_QUALITY = int(jq) if jq else SCREENSHOT_JPEG_QUALITY
        else:
            SCREENSHOT_SCREEN_ID = None

        # Mode
        if 'score-mode' in config:
            if config['score-mode'].getboolean('challenge', False):
                CURRENT_MODE = 'challenge'
            else:
                CURRENT_MODE = 'scores'

        # Send mode
        if 'send-mode' in config:
            sm = config['send-mode'].get('send_mode', 'automatic').strip().lower()
            if sm in ('automatic', 'manual'):
                SEND_MODE = sm
            else:
                _log("WARN", f"Unknown send_mode '{sm}', defaulting to automatic")
                SEND_MODE = "automatic"

        # Log available monitors and selected capture screen
        try:
            from screeninfo import get_monitors
            monitors = get_monitors()
            _log("INFO", f"Detected {len(monitors)} monitor(s):")
            for i, m in enumerate(monitors):
                _log("INFO", f"  Monitor {i}: {m.width}x{m.height} at ({m.x}, {m.y})")
            if SCREENSHOT_SCREEN_ID is not None:
                if SCREENSHOT_SCREEN_ID >= len(monitors):
                    _log("WARN", f"capture_screen={SCREENSHOT_SCREEN_ID} but only {len(monitors)} monitor(s) detected. Will fall back to primary screen.")
                else:
                    _log("INFO", f"capture_screen={SCREENSHOT_SCREEN_ID} (Monitor {SCREENSHOT_SCREEN_ID})")
            else:
                _log("INFO", "capture_screen not set, using primary screen")
        except Exception as e:
            _log("WARN", f"Could not enumerate monitors: {e}")

        _log("INFO", f"Config loaded. API: {API_URL}, WS: {SCORE_HOST}:{SCORE_PORT}, Mode: {CURRENT_MODE}, SendMode: {SEND_MODE}")
    except Exception as e:
        _log("ERROR", f"Error loading config: {e}")

def save_config():
    try:
        with open('config.ini', 'w') as configfile:
            config.write(configfile)
        _log("INFO", "Config saved.")
    except Exception as e:
        _log("ERROR", f"Error saving config: {e}")

def get_input_string(title, prompt, default_val=""):
    """
    Get input from user using PyQt6 QInputDialog.
    This must be called from the main thread or via signal/slot if needed.
    However, since mode switching happens from Tray Menu (Main Thread), direct call is fine.
    """
    text, ok = QInputDialog.getText(None, title, prompt, text=default_val)
    if ok:
        return text.strip()
    return None

def show_notification(title_or_table, message_or_score):
    """
    Emit signal to show notification on Main Thread.
    """
    # Determine if this is a Score object/number or a plain message
    if isinstance(message_or_score, (int, float)) or (isinstance(message_or_score, str) and message_or_score.replace(',','').replace('.','').isdigit()):
        # It's a score
        title = f"Score Sent ({CURRENT_MODE.title()})"
        try:
             # Try to format as number if possible
             score_val = int(str(message_or_score).replace(',','').replace('.',''))
             score_str = f"{score_val:,}"
        except:
             score_str = str(message_or_score)

        message = f"Table: {title_or_table}\nScore: {score_str}"
    else:
        # It's a generic message
        title = title_or_table
        message = message_or_score

    # Emit Signal
    _log("INFO", f"Emitting notification: Title='{title}', Msg='{message}'")
    signals.show_notification_signal.emit(title, message)


def _resize_screenshot(img):
    """
    Resize a screenshot to SCREENSHOT_MAX_WIDTH (preserving aspect ratio).
    Returns the resized PIL.Image, or the original if no resize is needed.
    """
    if not img or SCREENSHOT_MAX_WIDTH <= 0:
        return img

    w, h = img.size
    if w <= SCREENSHOT_MAX_WIDTH:
        return img

    ratio = SCREENSHOT_MAX_WIDTH / w
    new_h = int(h * ratio)
    resized = img.resize((SCREENSHOT_MAX_WIDTH, new_h), Image.LANCZOS)
    _log("INFO", f"Screenshot resized: {w}x{h} -> {SCREENSHOT_MAX_WIDTH}x{new_h}")
    return resized


def _set_last_score(rom_name, score):
    """Store the last known score for manual mode sending."""
    global _last_score_rom, _last_score_value
    with _last_score_lock:
        _last_score_rom = rom_name
        _last_score_value = score


def _get_last_score():
    """Retrieve the last known score (rom_name, score_value)."""
    with _last_score_lock:
        return _last_score_rom, _last_score_value


def send_score(table_name, score):
    import io

    try:
        clean_score = int(str(score).lstrip('0') or 0)
    except:
        clean_score = 0

    if clean_score <= 0:
        return

    if not API_URL or not API_KEY:
        _log("ERROR", "API_URL or API_KEY not configured. Cannot send score.")
        return

    _log("INFO", f"Sending score to API: {table_name} - {clean_score}")

    if SCREENSHOT_ENABLED:
        _log("INFO", "Capturing screenshot for score submission")
        screenshot = capture_screen(screen_id=SCREENSHOT_SCREEN_ID)
    else:
        _log("INFO", "Screenshot capture disabled, skipping")
        screenshot = None

    api_base = API_URL.rstrip('/')
    os_info = platform.system()

    if os_info == "Darwin":
        os_info = "macOS"

    try:
        if screenshot:
            # Resize screenshot to reduce file size
            screenshot = _resize_screenshot(screenshot)

            # Convert to RGB if necessary (JPEG doesn't support RGBA)
            if screenshot.mode == 'RGBA':
                screenshot = screenshot.convert('RGB')

            # Submit with screenshot (multipart form) - use JPEG for smaller size
            buffer = io.BytesIO()
            screenshot.save(buffer, format='JPEG', quality=SCREENSHOT_JPEG_QUALITY, optimize=True)
            buffer.seek(0)
            _log("INFO", f"Screenshot captured: {screenshot.size}, {len(buffer.getvalue())} bytes (JPEG q={SCREENSHOT_JPEG_QUALITY})")

            files = {
                'screenshot': ('screenshot.jpg', buffer, 'image/jpeg')
            }
            data = {
                'apiKey': API_KEY,
                'machineID': MACHINE_ID,
                'romName': table_name,
                'score': str(clean_score),
                'user_os': os_info
            }

            # Add challenge metadata
            if CURRENT_MODE == 'challenge':
                c_id = config['challenge'].get('challenge_id', '') if 'challenge' in config else ''
                data['challenge_id'] = c_id
                _log("INFO", f"Sending as CHALLENGE score: {c_id}")

            endpoint = f"{api_base}/api/submit-score"
            _log("INFO", f"Submitting to {endpoint}")
            r = requests.post(endpoint, files=files, data=data, timeout=30)
        else:
            # Fallback: submit without screenshot (JSON)
            _log("WARN", "Screenshot capture failed, submitting score without screenshot")
            payload = {
                "apiKey": API_KEY,
                "romName": table_name,
                "machineID": MACHINE_ID,
                "score": clean_score,
                "user_os": os_info,
            }
            if CURRENT_MODE == 'challenge':
                c_id = config['challenge'].get('challenge_id', '') if 'challenge' in config else ''
                payload['challenge_id'] = c_id

            endpoint = f"{api_base}/api/submit-score"
            r = requests.post(endpoint, json=payload, timeout=10)

        r.raise_for_status()
        result = r.json()
        _log("INFO", f"Response: status={r.status_code}, result={result}")

        if result.get('success'):
            table_display = result.get('tableName', table_name)
            _log("INFO", f"Score submitted successfully: {table_display} - {clean_score:,}")
            show_notification(table_display, clean_score)
        else:
            _log("ERROR", f"API returned error: {result.get('error', 'Unknown')}")
    except Exception as e:
        _log("ERROR", f"Error sending score to API: {e}")

def on_ws_open(ws):
    global ws_connected_at
    ws_connected_at = datetime.utcnow()
    _log("INFO", f"WebSocket connected (will ignore messages timestamped before {ws_connected_at.strftime('%Y-%m-%dT%H:%M:%S')}Z)")

def on_message(ws, message):
    global game_session_data, last_game_end
    try:
        data = json.loads(message)
    except:
        return

    # Ignore stale messages that were queued before we connected
    msg_timestamp = data.get('timestamp', '')
    if msg_timestamp and ws_connected_at:
        try:
            msg_time = datetime.strptime(msg_timestamp.replace('Z', ''), '%Y-%m-%dT%H:%M:%S.%f')
            if msg_time < ws_connected_at:
                msg_type = data.get('type', '')
                _log("INFO", f"Ignoring stale {msg_type} message (timestamp={msg_timestamp}, connected at {ws_connected_at.strftime('%Y-%m-%dT%H:%M:%S')}Z)")
                return
        except ValueError:
            pass

    rom_name = data.get('rom', 'unknown_rom')
    msg_type = data.get('type', '')

    if msg_type in ['table_loaded', 'game_start']:
        game_session_data[rom_name] = {}
        last_game_end.pop(rom_name, None)
        _log("INFO", f"Game started: {rom_name}")
        return

    if msg_type == 'game_end':
        reason = data.get('reason', '')

        # Ignore plugin_unload events
        if reason == 'plugin_unload':
            _log("INFO", f"Ignoring game_end (plugin_unload) for: {rom_name}")
            game_session_data.pop(rom_name, None)
            return

        # Check minimum game duration (if provided by score-server plugin)
        game_duration = data.get('game_duration', None)
        if game_duration is not None and int(game_duration) < MIN_GAME_DURATION_SEC:
            _log("WARN", f"Ignoring game_end for {rom_name}: game duration too short ({game_duration}s < {MIN_GAME_DURATION_SEC}s)")
            return

        # Debounce: ignore duplicate game_end for the same ROM within 10 seconds
        now = time.time()
        last = last_game_end.get(rom_name, 0)
        if now - last < 10:
            _log("WARN", f"Ignoring duplicate game_end for {rom_name} (received {now - last:.1f}s after previous)")
            return
        last_game_end[rom_name] = now

        _log("INFO", f"Game ended: {rom_name} (reason={reason}, duration={game_duration}s)")

        # Find the highest score from all players
        best_score = 0

        # Prefer scores from game_end payload (sent by score-server)
        end_scores = data.get('scores', [])
        if end_scores:
            _log("INFO", f"Using scores from game_end payload ({len(end_scores)} players)")
            for p_data in end_scores:
                try:
                    raw_score = p_data.get('score', 0)
                    score = int(str(raw_score).replace(',', '').replace('.', '').lstrip('0') or 0)
                    if score > best_score:
                        best_score = score
                except:
                    pass
        # Fallback: use accumulated session data
        elif rom_name in game_session_data and game_session_data[rom_name]:
            _log("INFO", f"No scores in game_end payload, using accumulated session data")
            for player_id, p_data in game_session_data[rom_name].items():
                try:
                    raw_score = p_data.get('score', 0)
                    score = int(str(raw_score).replace(',', '').replace('.', '').lstrip('0') or 0)
                    if score > best_score:
                        best_score = score
                except:
                    pass
        else:
            _log("WARN", f"game_end received for {rom_name} but no scores available")

        if best_score > 0:
            _log("INFO", f"Best score: {rom_name} - {best_score:,} (send_mode={SEND_MODE})")
            _set_last_score(rom_name, best_score)

            if SEND_MODE == "automatic":
                send_score(rom_name, best_score)
            else:
                _log("INFO", "Manual mode: score stored, waiting for hotkey")

        game_session_data.pop(rom_name, None)
        return

    if msg_type == 'current_scores':
        if rom_name not in game_session_data:
            game_session_data[rom_name] = {}

        for p_data in data.get('scores', []):
            try:
                p_label = str(p_data.get('player', ''))
                p_score = p_data.get('score', 0)
                p_id = p_label.replace("Player", "").strip() if "Player" in p_label else p_label

                game_session_data[rom_name][p_id] = {
                    'score': p_score,
                    'ball': data.get('current_ball')
                }
            except Exception as e:
                _log("ERROR", f"Error parsing player data: {e}")

        # Update last known score from current gameplay data (for manual mode)
        if SEND_MODE == "manual":
            best_current = 0
            for p_id, p_info in game_session_data.get(rom_name, {}).items():
                try:
                    s = int(str(p_info.get('score', 0)).replace(',', '').replace('.', '').lstrip('0') or 0)
                    if s > best_current:
                        best_current = s
                except:
                    pass
            if best_current > 0:
                _set_last_score(rom_name, best_current)

# =========================
# GLOBAL HOTKEY (Manual Mode)
# =========================
_hotkey_listener = None


def _on_hotkey_pressed():
    """Called when the manual send hotkey is pressed."""
    rom, score = _get_last_score()
    if rom is None or score is None or score <= 0:
        _log("WARN", "Hotkey pressed but no score available to send")
        show_notification("No Score", "No score available to send")
        return

    _log("INFO", f"Hotkey triggered: sending {rom} - {score:,}")
    threading.Thread(target=send_score, args=(rom, score), daemon=True).start()


def _start_hotkey_listener():
    """Start the global hotkey listener for manual send mode."""
    global _hotkey_listener

    _stop_hotkey_listener()

    os_name = platform.system()
    if os_name == "Darwin":
        hotkey_combo = '<cmd>+<shift>+s'
        display_combo = 'Cmd+Shift+S'
    else:
        hotkey_combo = '<ctrl>+<shift>+s'
        display_combo = 'Ctrl+Shift+S'

    _log("INFO", f"Starting hotkey listener: {display_combo}")

    _hotkey_listener = pynput_keyboard.GlobalHotKeys({
        hotkey_combo: _on_hotkey_pressed
    })
    _hotkey_listener.daemon = True
    _hotkey_listener.start()


def _stop_hotkey_listener():
    """Stop the global hotkey listener if running."""
    global _hotkey_listener
    if _hotkey_listener is not None:
        _log("INFO", "Stopping hotkey listener")
        _hotkey_listener.stop()
        _hotkey_listener = None


def run_websocket():
    url = f'ws://{SCORE_HOST}:{SCORE_PORT}'

    while True:
        try:
            _log("INFO", f"Connecting to WebSocket at {url}...")
            ws = websocket.WebSocketApp(
                url,
                on_open=on_ws_open,
                on_message=on_message
            )
            ws.run_forever()

            _log("WARN", "WebSocket connection closed. Reconnecting in 10 seconds...")
        except Exception as e:
            _log("ERROR", f"WebSocket error: {e}")

        time.sleep(10)

# =========================
# SYSTEM TRAY CLASS
# =========================
class VPinScoreTray(QSystemTrayIcon):
    def __init__(self, icon, parent=None):
        super().__init__(icon, parent)
        self.setToolTip(f"VPin Score Tracker - {CURRENT_MODE.title()}")

        # Menu
        self.menu = QMenu(parent)

        # Actions Group for Mutually Exclusive Mode Selection
        self.mode_group = QActionGroup(self.menu)
        self.mode_group.setExclusive(True)

        self.act_score = QAction("Score Mode", self.menu, checkable=True)
        self.act_score.triggered.connect(lambda: self.set_mode("scores"))
        self.mode_group.addAction(self.act_score)
        self.menu.addAction(self.act_score)

        self.act_tourn = QAction("Tournament Mode", self.menu, checkable=True)
        self.act_tourn.triggered.connect(lambda: self.set_mode("challenge"))
        self.mode_group.addAction(self.act_tourn)
        self.menu.addAction(self.act_tourn)

        self.menu.addSeparator()

        # Send Mode Group
        self.send_mode_group = QActionGroup(self.menu)
        self.send_mode_group.setExclusive(True)

        self.act_auto = QAction("Automatic Send", self.menu, checkable=True)
        self.act_auto.triggered.connect(lambda: self.set_send_mode("automatic"))
        self.send_mode_group.addAction(self.act_auto)
        self.menu.addAction(self.act_auto)

        self.act_manual = QAction("Manual Send (Hotkey)", self.menu, checkable=True)
        self.act_manual.triggered.connect(lambda: self.set_send_mode("manual"))
        self.send_mode_group.addAction(self.act_manual)
        self.menu.addAction(self.act_manual)

        self.menu.addSeparator()

        self.act_exit = QAction("Exit", self.menu)
        self.act_exit.triggered.connect(QApplication.instance().quit)
        self.menu.addAction(self.act_exit)

        self.setContextMenu(self.menu)

        # Update Check State
        self.update_menu_state()

        # Connect Notification Signal
        signals.show_notification_signal.connect(self.display_overlay)

        # Active Notifications
        self.active_notification = None

    def update_menu_state(self):
        self.act_score.setChecked(CURRENT_MODE == "scores")
        self.act_tourn.setChecked(CURRENT_MODE == "challenge")
        self.act_auto.setChecked(SEND_MODE == "automatic")
        self.act_manual.setChecked(SEND_MODE == "manual")
        self.setToolTip(f"VPin Score Tracker - {CURRENT_MODE.title()} ({SEND_MODE.title()})")

    def set_mode(self, selected_mode):
        global CURRENT_MODE

        _log("INFO", f"Switching to mode: {selected_mode}")

        # Ensure sections exist
        if 'score-mode' not in config: config['score-mode'] = {}

        if selected_mode == "scores":
            CURRENT_MODE = "scores"
            config['score-mode']['scores'] = 'true'
            config['score-mode']['challenge'] = 'false'
            save_config()
            self.update_menu_state()
            return

        # For Tournament
        prompt_pfx = selected_mode.capitalize()

        # Check existing ID
        current_val = ""
        if selected_mode == "challenge":
            if 'challenge' in config: current_val = config['challenge'].get('challenge_id', '')

        # Get Input (QInputDialog)
        new_id = get_input_string(f"{prompt_pfx} Mode", f"Enter {prompt_pfx} ID:", default_val=current_val)

        if new_id:
            CURRENT_MODE = selected_mode

            # Update Config Logic
            config['score-mode']['scores'] = 'false'
            config['score-mode']['challenge'] = 'false'
            config['score-mode'][selected_mode] = 'true'

            # Save ID
            if selected_mode == "challenge":
                if 'challenge' not in config: config['challenge'] = {}
                config['challenge']['challenge_id'] = new_id

            save_config()
            _log("INFO", f"Mode set to {selected_mode} with ID: {new_id}")
        else:
            _log("INFO", "Mode switch cancelled by user.")
            # Revert UI state if cancelled
            self.update_menu_state()

        self.update_menu_state()

    def set_send_mode(self, mode):
        global SEND_MODE
        _log("INFO", f"Switching send mode to: {mode}")
        SEND_MODE = mode

        if 'send-mode' not in config:
            config['send-mode'] = {}
        config['send-mode']['send_mode'] = mode
        save_config()

        if mode == "manual":
            _start_hotkey_listener()
        else:
            _stop_hotkey_listener()

        self.update_menu_state()

    def display_overlay(self, title, message):
        _log("INFO", f"Displaying notification overlay: '{title}'")
        # Close existing if active
        if self.active_notification:
            try:
                self.active_notification.close()
            except:
                pass

        self.active_notification = NotificationOverlay(title, message)
        self.active_notification.show()
        self.active_notification.raise_()
        QApplication.processEvents()


# =========================
# MAIN EXECUTION
# =========================
if __name__ == "__main__":
    load_config()

    missing = [k for k, v in [("api_key", API_KEY), ("machine_id", MACHINE_ID)] if not v or not v.strip()]
    if missing:
        _log("ERROR", f"Missing required config value(s) in config.ini: {', '.join(missing)}. Cannot start.")
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False) # Keep app running when notification closes

    # Load Icon
    path_to_icon = resource_path('assets/icon.png')
    if os.path.exists(path_to_icon):
        icon = QIcon(path_to_icon)
    else:
        _log("ERROR", f"Icon not found at {path_to_icon}")
        # Create a fallback pixmap
        from PyQt6.QtGui import QPixmap, QColor
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor("blue"))
        icon = QIcon(pixmap)

    # Init Tray
    tray = VPinScoreTray(icon)
    tray.show()


    # Start WebSocket in Background Thread
    ws_thread = threading.Thread(target=run_websocket, daemon=True)
    ws_thread.start()

    # Start hotkey listener if in manual mode
    if SEND_MODE == "manual":
        _start_hotkey_listener()

    sys.exit(app.exec())
