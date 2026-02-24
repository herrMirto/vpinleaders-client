import sys
import platform

# Force macOS to treat this as a background agent (no Dock icon / no menu bar)
if platform.system() == "Darwin":
    try:
        from AppKit import NSApplication
        ns_app = NSApplication.sharedApplication()
        # 2 = NSApplicationActivationPolicyProhibited
        ns_app.setActivationPolicy_(2)
    except Exception:
        pass

from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal

WIDTH = 320
HEIGHT = 90
DURATION = 3000
FADE_TIME = 500


class ProgressBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.progress = 1.0
        self.setFixedHeight(4)

    def setProgress(self, value):
        self.progress = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, event):
        from PyQt6.QtGui import QPainter, QColor
        painter = QPainter(self)
        painter.setBrush(QColor(255, 255, 255, 64))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(0, 0, int(self.width() * self.progress), self.height())


class NotificationOverlay(QWidget):
    closed = pyqtSignal()

    def __init__(self, title, message):
        super().__init__()
        self.title_text = title
        self.message_text = message

        self._frontmost_app = None
        if platform.system() == "Darwin":
            self._capture_frontmost_app()

        self.init_ui()

    def _capture_frontmost_app(self):
        # Remember which app had focus before we show the notification
        try:
            from AppKit import NSWorkspace
            self._frontmost_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        except Exception:
            self._frontmost_app = None

    def _restore_frontmost_app(self):
        # Give focus back to the previously active app (macOS fix)
        if platform.system() != "Darwin":
            return
        if not self._frontmost_app:
            return
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSApplicationActivateAllWindows
            self._frontmost_app.activateWithOptions_(
                NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows
            )
        except Exception:
            pass

    def init_ui(self):
        # IMPORTANT:
        # - Avoid Qt.ToolTip on macOS (it can trigger weird activation/focus behavior)
        # - X11BypassWindowManagerHint is irrelevant on macOS and can be removed
        flags = (
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )

        self.setWindowFlags(flags)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        if platform.system() == "Darwin":
            # Helps keep it as a "tool-ish" window without messing focus
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)

        self.setStyleSheet("""
            QWidget#MainFrame {
                background-color: rgba(20, 20, 20, 240);
                border-radius: 10px;
                border: 1px solid rgba(255, 255, 255, 20);
            }
            QLabel { color: white; }
            QLabel#Title { font-weight: bold; font-size: 14px; }
            QLabel#Message { font-size: 12px; }
        """)

        self.main_frame = QWidget(self)
        self.main_frame.setObjectName("MainFrame")
        self.main_frame.setFixedSize(WIDTH, HEIGHT)

        layout = QVBoxLayout(self.main_frame)
        layout.setContentsMargins(15, 10, 15, 10)

        self.lbl_title = QLabel(self.title_text)
        self.lbl_title.setObjectName("Title")

        self.lbl_message = QLabel(self.message_text)
        self.lbl_message.setObjectName("Message")
        self.lbl_message.setWordWrap(True)

        self.progress_bar = ProgressBar()

        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_message)
        layout.addStretch()
        layout.addWidget(self.progress_bar)

        screen = QApplication.primaryScreen()
        screen_geo = screen.availableGeometry()

        self.final_x = screen_geo.x() + screen_geo.width() - WIDTH - 20
        self.y_pos = screen_geo.y() + 40
        self.move(self.final_x, self.y_pos)

        self.setWindowOpacity(0.0)
        self.remaining_time = DURATION
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)

        self.animate_in()

    def animate_in(self):
        self.anim = QPropertyAnimation(self, b"windowOpacity", self)
        self.anim.setDuration(FADE_TIME)
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.setEasingCurve(QEasingCurve.Type.OutQuad)

        # Show without activating
        self.show()
        self.raise_()

        self.anim.start()
        self.timer.start(16)

    def animate_out(self):
        self.anim = QPropertyAnimation(self, b"windowOpacity", self)
        self.anim.setDuration(FADE_TIME)
        self.anim.setStartValue(self.windowOpacity())
        self.anim.setEndValue(0.0)
        self.anim.setEasingCurve(QEasingCurve.Type.InQuad)

        def _finish():
            self.hide()

            # Key fix: give focus back to whatever was active before our overlay.
            # Do this AFTER the window is hidden.
            self._restore_frontmost_app()

            # Avoid immediate close() to reduce focus churn
            QTimer.singleShot(250, self.deleteLater)

        self.anim.finished.connect(_finish)
        self.anim.start()

    def tick(self):
        self.remaining_time -= 16
        progress = max(0.0, self.remaining_time / DURATION)
        self.progress_bar.setProgress(progress)

        if self.remaining_time <= 0:
            self.timer.stop()
            self.animate_out()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    notifier = NotificationOverlay("I am a notification.")
    sys.exit(app.exec())
