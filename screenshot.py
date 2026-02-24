"""
Cross-platform screenshot capture module.

Handles platform-specific quirks:
- Windows: requires all_screens=True for multi-monitor bbox capture
- macOS: uses logical (non-Retina) coordinates for bbox; RGBA output
- Linux: uses gnome-screenshot(sorry), with some fallbacks

Usage:
    from screenshot import capture_screen
    img = capture_screen(screen_id=1)  # capture monitor 1
    img = capture_screen()             # capture primary screen
"""

import os
import platform
import subprocess
import tempfile
from PIL import Image, ImageGrab
from screeninfo import get_monitors


def _ts():
    from datetime import datetime
    now = datetime.now()
    return now.strftime('%Y-%m-%d %H:%M:%S.') + f'{now.microsecond // 1000:03d}'


def _log(level, msg):
    print(f"{_ts()} {level}  [Screenshot] {msg}")


def _is_wayland():
    """Detect if we are running under a Wayland session."""
    return os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland' or \
           os.environ.get('WAYLAND_DISPLAY', '') != ''


def _find_tool(names):
    """Find the first available CLI tool from a list of candidates."""
    for name in names:
        try:
            result = subprocess.run(
                ['which', name],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return name
        except Exception:
            pass
    return None


def capture_screen(screen_id=None):
    """
    Capture a screenshot from a specific monitor or the primary screen.

    Args:
        screen_id: Integer monitor index (0-based, matching screeninfo order).
                   If None, captures the primary screen.

    Returns:
        PIL.Image or None if capture failed.
    """
    try:
        if screen_id is not None:
            return _capture_monitor(screen_id)
        else:
            _log("INFO", "Capturing primary screen")
            os_name = platform.system()
            if os_name == "Linux" and _is_wayland():
                return _capture_wayland_full()
            return ImageGrab.grab()
    except Exception as e:
        _log("ERROR", f"Screenshot capture failed: {e}")
        return None


def _capture_monitor(screen_id):
    """Capture a specific monitor by index."""
    monitors = get_monitors()

    if screen_id >= len(monitors):
        _log("ERROR", f"Monitor {screen_id} not found (only {len(monitors)} available)")
        return None

    mon = monitors[screen_id]
    _log("INFO", f"Capturing monitor {screen_id}: {mon.width}x{mon.height} at ({mon.x}, {mon.y})")

    os_name = platform.system()

    if os_name == "Windows":
        return _capture_windows(mon)
    elif os_name == "Darwin":
        return _capture_macos(mon)
    else:
        return _capture_linux(mon)


def _capture_windows(mon):
    """
    Windows: ImageGrab.grab needs all_screens=True when capturing
    monitors that aren't the primary (coordinates can be negative).
    """
    bbox = (mon.x, mon.y, mon.x + mon.width, mon.y + mon.height)
    _log("INFO", f"Windows bbox: {bbox} (all_screens=True)")
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def _capture_macos(mon):
    """
    macOS: bbox uses logical (non-Retina) coordinates.
    screeninfo already reports logical coordinates, so we use them directly.
    The returned image may be 2x resolution on Retina displays (that's fine).
    """
    bbox = (mon.x, mon.y, mon.x + mon.width, mon.y + mon.height)
    _log("INFO", f"macOS bbox: {bbox}")
    try:
        img = ImageGrab.grab(bbox=bbox)
        # Convert RGBA to RGB for consistency
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        return img
    except Exception as e:
        _log("ERROR", f"macOS ImageGrab.grab failed: {e}")
        # Fallback: try without bbox (captures primary only)
        _log("INFO", "Falling back to primary screen capture")
        img = ImageGrab.grab()
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        return img


def _capture_wayland_full():
    """Capture the full screen on Wayland using gnome-screenshot."""
    tool = _find_tool(['gnome-screenshot'])
    if not tool:
        _log("WARN", "No Wayland screenshot tool found (tried gnome-screenshot)")
        _log("INFO", "Falling back to ImageGrab (may fail on Wayland)")
        return ImageGrab.grab()

    return _capture_wayland_tool(tool)


def _capture_wayland_tool(tool, output_name=None):
    """
    Capture using a Wayland-compatible CLI tool.

    Args:
        tool: 'gnome-screenshot'
        output_name: For grim, the Wayland output name (e.g., 'DP-1') to capture
                     a specific monitor. None captures the full desktop.
    """
    tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        cmd = ['gnome-screenshot', '-f', tmp_path]
        _log("INFO", f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            _log("ERROR", f"{tool} failed (rc={result.returncode}): {result.stderr.strip()}")
            return None

        img = Image.open(tmp_path)
        img.load()  # Force load before we delete the temp file
        return img
    except subprocess.TimeoutExpired:
        _log("ERROR", f"{tool} timed out")
        return None
    except Exception as e:
        _log("ERROR", f"{tool} capture failed: {e}")
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _capture_linux(mon):
    """
    Linux: Detect Wayland vs X11 and use the appropriate capture method.

    Wayland: Use grim (with output mapping) or gnome-screenshot.
    X11: Use ImageGrab.grab with bbox (backed by XCB).
    """
    bbox = (mon.x, mon.y, mon.x + mon.width, mon.y + mon.height)

    if _is_wayland():
        _log("INFO", "Detected Wayland session")
        tool = _find_tool(['gnome-screenshot'])

        
        img = _capture_wayland_tool('gnome-screenshot')
        if img:
           cropped = img.crop(bbox)
           return cropped

        _log("WARN", "All Wayland capture methods failed")
        return None
    else:
        # X11 path
        _log("INFO", f"X11 session, bbox: {bbox}")
        try:
            return ImageGrab.grab(bbox=bbox)
        except Exception as e:
            _log("ERROR", f"Linux ImageGrab.grab with bbox failed: {e}")
            # Fallback: capture full screen and crop
            _log("INFO", "Falling back to full screen grab + crop")
            try:
                full = ImageGrab.grab()
                cropped = full.crop(bbox)
                return cropped
            except Exception as e2:
                _log("ERROR", f"Linux fallback also failed: {e2}")
                return None
