"""First-run wizard and tabbed settings dialog for the VPinLeaders client.

Two public entry points:

    run_first_run_wizard(config_path) -> bool
        Launches the linear wizard. Returns True if the user finished, False if
        they cancelled. Writes the resulting state to ``config_path``.

    open_settings_dialog(config_path) -> bool
        Opens the tabbed editor for an already-configured install. Returns True
        if changes were saved, False otherwise.

The module is self-contained: it uses configparser to read/write the ini file
directly, and only imports from the rest of the project lazily (notably
``registration`` for the device-pairing API calls).
"""

from __future__ import annotations

import configparser
import io
import os
import threading
import time
from typing import Optional

import requests
from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

DEFAULT_API_URL = "https://www.vpinleaders.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_config(path: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read(path)
    return cp


def _write_config(cp: configparser.ConfigParser, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        cp.write(fh)


def _ensure_section(cp: configparser.ConfigParser, name: str) -> None:
    if name not in cp:
        cp[name] = {}


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _qpixmap_from_pil(pil_image) -> QPixmap:
    """Convert a Pillow image (e.g. from qrcode) into a QPixmap via PNG bytes."""
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    qimg = QImage.fromData(buf.getvalue(), "PNG")
    return QPixmap.fromImage(qimg)


# ---------------------------------------------------------------------------
# VPinLeaders pairing worker
# ---------------------------------------------------------------------------

class _PairingWorker(QObject):
    """Drives the /device/pair/start + /device/pair/status polling loop."""

    started = pyqtSignal(str, str)        # pairing_url, pairing_code
    qr_ready = pyqtSignal(QPixmap)
    approved = pyqtSignal(str, str)        # machine_id, api_key
    failed = pyqtSignal(str)
    expired = pyqtSignal()

    def __init__(self, machine_id: str, api_url: str):
        super().__init__()
        self.machine_id = machine_id.strip()
        self.api_url = api_url.rstrip("/")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            api_base = self.api_url + "/api"
            resp = requests.post(
                f"{api_base}/device/pair/start",
                json={"machine_id": self.machine_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            pairing_url = data["pairing_url"]
            pairing_code = data["pairing_code"]
            polling_token = data["polling_token"]
        except Exception as exc:
            self.failed.emit(f"Could not start pairing: {exc}")
            return

        self.started.emit(pairing_url, pairing_code)

        try:
            import qrcode
            qr = qrcode.QRCode(border=2, box_size=6)
            qr.add_data(pairing_url)
            qr.make()
            pixmap = _qpixmap_from_pil(qr.make_image(fill_color="black", back_color="white"))
            self.qr_ready.emit(pixmap)
        except Exception:
            pass

        while not self._stop.is_set():
            time.sleep(2.5)
            if self._stop.is_set():
                return
            try:
                status_resp = requests.post(
                    f"{api_base}/device/pair/status",
                    json={"polling_token": polling_token},
                    timeout=10,
                )
                status_resp.raise_for_status()
                status = status_resp.json()
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    self.failed.emit("Pairing session not found.")
                    return
                continue
            except Exception:
                continue

            state = status.get("status")
            if state == "pending":
                continue
            if state == "approved":
                machine_id = str(status.get("machine_id") or self.machine_id).strip()
                api_key = str(status.get("api_key") or "").strip()
                if not api_key:
                    self.failed.emit("Server approved pairing but did not return an API key.")
                    return
                self.approved.emit(machine_id, api_key)
                return
            if state == "expired":
                self.expired.emit()
                return


# ---------------------------------------------------------------------------
# Wizard pages
# ---------------------------------------------------------------------------

class _WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Welcome to VPinLeaders Client")
        self.setSubTitle("Let's get you set up.")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "This wizard will configure the client for the first time.\n\n"
            "You'll choose which leaderboards to send your scores to and provide\n"
            "the credentials for each. Everything can be changed later from the\n"
            "tray icon (Settings…).\n\n"
            "Click Next to continue."
        ))


class _NvramFolderPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("VPX nvram folder")
        self.setSubTitle("Tell the client where your nvram files live.")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "VPinLeaders Client reads scores from the nvram files written by\n"
            "Visual Pinball X. Pick the parent directory containing the table\n"
            "subfolders (typically the one with pinmame/nvram/*.nv inside)."
        ))

        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.textChanged.connect(self.completeChanged)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)

        layout.addStretch(1)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select nvram base directory")
        if d:
            self.path_edit.setText(d)

    def isComplete(self) -> bool:
        return bool(self.path_edit.text().strip())

    def value(self) -> str:
        return self.path_edit.text().strip()


class _IntegrationPickerPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Choose integrations")
        self.setSubTitle("Pick one or more leaderboards. You can change this later.")

        layout = QVBoxLayout(self)
        self.cb_vpin = QCheckBox("VPinLeaders (vpinleaders.com) — pairs this device with your account")
        self.cb_wovp = QCheckBox("WoVP (World of Virtual Pinball) — uses an API key from wovp.com")
        self.cb_isc = QCheckBox("iScored — submits to your iScored gameroom")

        for cb in (self.cb_vpin, self.cb_wovp, self.cb_isc):
            cb.toggled.connect(self.completeChanged)
            layout.addWidget(cb)

        layout.addStretch(1)
        self.setCommitPage(False)

    def isComplete(self) -> bool:
        # Allow advancing without selecting any — they can always reopen the
        # settings dialog later. But require at least confirming the page.
        return True

    @property
    def want_vpin(self) -> bool:
        return self.cb_vpin.isChecked()

    @property
    def want_wovp(self) -> bool:
        return self.cb_wovp.isChecked()

    @property
    def want_iscored(self) -> bool:
        return self.cb_isc.isChecked()


class _VPinLeadersPage(QWizardPage):
    """Captures machine_id and runs the pairing dance."""

    def __init__(self, default_api_url: str = DEFAULT_API_URL):
        super().__init__()
        self.setTitle("VPinLeaders setup")
        self.setSubTitle("Pair this machine with your VPinLeaders account.")

        self._api_url = default_api_url
        self._worker: Optional[_PairingWorker] = None
        self._approved = False
        self.machine_id: str = ""
        self.api_key: str = ""

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "1. Make sure you have an account at https://www.vpinleaders.com\n"
            "2. Pick a name for this machine (e.g. \"living-room-cab\").\n"
            "3. Click Start Pairing, then scan the QR code or open the URL on\n"
            "   any device signed into your account to approve.\n"
        ))

        form = QFormLayout()
        self.machine_edit = QLineEdit()
        self.machine_edit.setPlaceholderText("machine name (letters, digits, dashes)")
        form.addRow("Machine ID:", self.machine_edit)
        layout.addLayout(form)

        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start Pairing")
        self.start_btn.clicked.connect(self._start_pairing)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel_pairing)
        self.cancel_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setMinimumHeight(220)
        layout.addWidget(self.qr_label)

        self.status_label = QLabel("Not yet paired.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)

    # ---- pairing flow ----
    def _start_pairing(self) -> None:
        mid = self.machine_edit.text().strip()
        if not mid:
            QMessageBox.warning(self, "Machine ID required", "Type a machine name first.")
            return

        self._approved = False
        self.api_key = ""
        self.machine_id = mid
        self.start_btn.setEnabled(False)
        self.machine_edit.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.status_label.setText("Contacting VPinLeaders…")
        self.progress.show()

        self._worker = _PairingWorker(mid, self._api_url)
        self._worker.started.connect(self._on_started)
        self._worker.qr_ready.connect(self._on_qr)
        self._worker.approved.connect(self._on_approved)
        self._worker.failed.connect(self._on_failed)
        self._worker.expired.connect(self._on_expired)
        self._worker.start()

    def _cancel_pairing(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None
        self.progress.hide()
        self.qr_label.clear()
        self.status_label.setText("Pairing cancelled.")
        self.cancel_btn.setEnabled(False)
        self.start_btn.setEnabled(True)
        self.machine_edit.setEnabled(True)

    def _on_started(self, pairing_url: str, pairing_code: str) -> None:
        self.status_label.setText(
            f"Open <a href='{pairing_url}'>{pairing_url}</a> on any signed-in device,\n"
            f"or scan the QR code below. Pairing code: <b>{pairing_code}</b>"
        )
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        self.status_label.setOpenExternalLinks(True)

    def _on_qr(self, pixmap: QPixmap) -> None:
        self.qr_label.setPixmap(pixmap.scaled(
            220, 220,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def _on_approved(self, machine_id: str, api_key: str) -> None:
        self._approved = True
        self.machine_id = machine_id
        self.api_key = api_key
        self.progress.hide()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setText(f"✓ Paired successfully as '{machine_id}'.")
        self.cancel_btn.setEnabled(False)
        self.completeChanged.emit()

    def _on_failed(self, message: str) -> None:
        self.progress.hide()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setText(f"✗ {message}")
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.machine_edit.setEnabled(True)

    def _on_expired(self) -> None:
        self.progress.hide()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setText("Session expired before approval. Click Start Pairing to try again.")
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.machine_edit.setEnabled(True)

    # ---- QWizardPage hooks ----
    def isComplete(self) -> bool:
        return self._approved

    def cleanupPage(self) -> None:
        # Stop any in-flight pairing if the user goes Back.
        if self._worker is not None:
            self._worker.stop()
            self._worker = None


class _WoVPPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("WoVP setup")
        self.setSubTitle("Provide your WoVP API key.")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "1. Sign up / sign in at https://wovp.com\n"
            "2. Generate an API key in your account settings.\n"
            "3. Paste it below. The challenge to play against can be picked\n"
            "   later from the tray menu (WoVP ▸ Challenges)."
        ))

        form = QFormLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("WoVP API key")
        self.key_edit.textChanged.connect(self.completeChanged)
        form.addRow("API key:", self.key_edit)
        layout.addLayout(form)
        layout.addStretch(1)

    def isComplete(self) -> bool:
        return bool(self.key_edit.text().strip())

    @property
    def api_key(self) -> str:
        return self.key_edit.text().strip()


class _IScoredPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("iScored setup")
        self.setSubTitle("Tell the client your iScored username.")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "1. Create or sign in to an iScored gameroom at https://iscored.info\n"
            "2. Enable API access in the gameroom settings.\n"
            "3. Enter your iScored username. The API gameroom is derived from\n"
            "   the same username."
        ))

        form = QFormLayout()
        self.player_edit = QLineEdit()
        self.player_edit.setPlaceholderText("e.g. Username")
        self.player_edit.textChanged.connect(self.completeChanged)
        form.addRow("Username:", self.player_edit)
        layout.addLayout(form)
        layout.addStretch(1)

    def isComplete(self) -> bool:
        return bool(self.player_edit.text().strip())

    @property
    def player_name(self) -> str:
        return self.player_edit.text().strip()


class _CapturePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Capture & hotkey")
        self.setSubTitle("Choose which display to capture and the manual-send hotkey.")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Screenshots are taken whenever you trigger a manual send. They\n"
            "are attached to VPinLeaders / WoVP submissions. Pick the display\n"
            "you want captured (usually the playfield)."
        ))

        form = QFormLayout()

        self.screen_combo = QComboBox()
        self._populate_screens()
        form.addRow("Capture display:", self.screen_combo)

        self.hotkey_edit = QLineEdit("cmd+shift+s")
        form.addRow("Manual send hotkey:", self.hotkey_edit)

        self.joy_edit = QLineEdit()
        self.joy_edit.setPlaceholderText("e.g. 4,5 (joystick button indices, optional)")
        form.addRow("Joystick buttons:", self.joy_edit)

        layout.addLayout(form)
        layout.addStretch(1)

    def _populate_screens(self) -> None:
        self.screen_combo.clear()
        app = QApplication.instance()
        if app is None:
            self.screen_combo.addItem("Screen 0 (primary)", 0)
            return
        for idx, screen in enumerate(app.screens()):
            geom = screen.geometry()
            label = f"Screen {idx} — {screen.name() or 'unnamed'} ({geom.width()}×{geom.height()})"
            self.screen_combo.addItem(label, idx)

    @property
    def screen_index(self) -> int:
        return int(self.screen_combo.currentData() or 0)

    @property
    def hotkey(self) -> str:
        return self.hotkey_edit.text().strip()

    @property
    def joystick_buttons(self) -> str:
        return self.joy_edit.text().strip()


class _DonePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("All set")
        self.setSubTitle("Click Finish to start the client.")

        layout = QVBoxLayout(self)
        self.summary = QLabel("Reviewing your selections…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addStretch(1)

    def initializePage(self) -> None:
        wiz = self.wizard()
        if not isinstance(wiz, FirstRunWizard):
            return
        bullets = []
        if wiz.picker.want_vpin and wiz.vpin.isComplete():
            bullets.append(f"• VPinLeaders paired as '{wiz.vpin.machine_id}'")
        if wiz.picker.want_wovp and wiz.wovp.isComplete():
            bullets.append("• WoVP API key configured")
        if wiz.picker.want_iscored and wiz.iscored.isComplete():
            bullets.append(f"• iScored player '{wiz.iscored.player_name}'")
        if not bullets:
            bullets.append("• No integrations enabled. You can enable them later "
                           "from the tray icon → Settings…")
        bullets.append(f"• nvram folder: {wiz.nvram.value()}")
        self.summary.setText("\n".join(bullets))


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class FirstRunWizard(QWizard):
    PAGE_WELCOME = 0
    PAGE_NVRAM = 1
    PAGE_PICKER = 2
    PAGE_VPIN = 3
    PAGE_WOVP = 4
    PAGE_ISCORED = 5
    PAGE_CAPTURE = 6
    PAGE_DONE = 7

    def __init__(self, config_path: str, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.setWindowTitle("VPinLeaders Client – Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(620, 520)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)

        self.welcome = _WelcomePage()
        self.nvram = _NvramFolderPage()
        self.picker = _IntegrationPickerPage()
        self.vpin = _VPinLeadersPage(default_api_url=DEFAULT_API_URL)
        self.wovp = _WoVPPage()
        self.iscored = _IScoredPage()
        self.capture = _CapturePage()
        self.done_page = _DonePage()

        self.setPage(self.PAGE_WELCOME, self.welcome)
        self.setPage(self.PAGE_NVRAM, self.nvram)
        self.setPage(self.PAGE_PICKER, self.picker)
        self.setPage(self.PAGE_VPIN, self.vpin)
        self.setPage(self.PAGE_WOVP, self.wovp)
        self.setPage(self.PAGE_ISCORED, self.iscored)
        self.setPage(self.PAGE_CAPTURE, self.capture)
        self.setPage(self.PAGE_DONE, self.done_page)

    def nextId(self) -> int:
        cur = self.currentId()
        if cur == self.PAGE_WELCOME:
            return self.PAGE_NVRAM
        if cur == self.PAGE_NVRAM:
            return self.PAGE_PICKER
        if cur == self.PAGE_PICKER:
            if self.picker.want_vpin:
                return self.PAGE_VPIN
            if self.picker.want_wovp:
                return self.PAGE_WOVP
            if self.picker.want_iscored:
                return self.PAGE_ISCORED
            return self.PAGE_CAPTURE
        if cur == self.PAGE_VPIN:
            if self.picker.want_wovp:
                return self.PAGE_WOVP
            if self.picker.want_iscored:
                return self.PAGE_ISCORED
            return self.PAGE_CAPTURE
        if cur == self.PAGE_WOVP:
            if self.picker.want_iscored:
                return self.PAGE_ISCORED
            return self.PAGE_CAPTURE
        if cur == self.PAGE_ISCORED:
            return self.PAGE_CAPTURE
        if cur == self.PAGE_CAPTURE:
            return self.PAGE_DONE
        return -1

    def accept(self) -> None:
        try:
            self._save()
        except Exception as exc:
            QMessageBox.critical(self, "Could not save settings", str(exc))
            return
        super().accept()

    def _save(self) -> None:
        cp = _read_config(self.config_path)

        # VPinLeaders
        _ensure_section(cp, "vpinleaders")
        if self.picker.want_vpin and self.vpin.isComplete():
            cp["vpinleaders"]["enable"] = "true"
            cp["vpinleaders"]["api_url"] = DEFAULT_API_URL
            cp["vpinleaders"]["machine_id"] = self.vpin.machine_id
            cp["vpinleaders"]["api_key"] = self.vpin.api_key
        else:
            cp["vpinleaders"]["enable"] = "false"

        # WoVP
        _ensure_section(cp, "wovp")
        if self.picker.want_wovp and self.wovp.isComplete():
            cp["wovp"]["enable"] = "true"
            cp["wovp"]["api_key"] = self.wovp.api_key
        else:
            cp["wovp"]["enable"] = "false"

        # iScored
        _ensure_section(cp, "iscored")
        if self.picker.want_iscored and self.iscored.isComplete():
            cp["iscored"]["enable"] = "true"
            cp["iscored"]["player_name"] = self.iscored.player_name
            cp["iscored"].pop("room_urls", None)
            cp["iscored"].pop("gamerooms", None)
        else:
            cp["iscored"]["enable"] = "false"

        # Capture / hotkeys / nvram
        _ensure_section(cp, "screenshot")
        cp["screenshot"]["screen_to_capture"] = str(self.capture.screen_index)

        _ensure_section(cp, "hotkeys")
        cp["hotkeys"]["keyboard"] = self.capture.hotkey
        cp["hotkeys"]["joystick_buttons"] = self.capture.joystick_buttons

        _ensure_section(cp, "nvram")
        cp["nvram"]["base_dir"] = self.nvram.value()

        _write_config(cp, self.config_path)


# ---------------------------------------------------------------------------
# Settings dialog (post-first-run)
# ---------------------------------------------------------------------------

class SettingsDialog(QDialog):
    """Tabbed editor for an already-configured install."""

    def __init__(self, config_path: str, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.setWindowTitle("VPinLeaders Client – Settings")
        self.setMinimumSize(560, 480)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self._build_integrations_tab()
        self._build_capture_tab()
        self._build_paths_tab()
        self._load()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---- tabs ----
    def _build_integrations_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        # VPinLeaders
        vpin_box = QGroupBox("VPinLeaders")
        vpin_form = QFormLayout(vpin_box)
        self.cb_vpin = QCheckBox("Enable VPinLeaders")
        vpin_form.addRow(self.cb_vpin)
        self.vpin_machine_id = QLineEdit()
        vpin_form.addRow("Machine ID:", self.vpin_machine_id)
        self.vpin_api_key = QLineEdit()
        self.vpin_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        vpin_form.addRow("API key:", self.vpin_api_key)
        self.vpin_register_btn = QPushButton("Register...")
        self.vpin_register_btn.clicked.connect(self._register_vpinleaders)
        vpin_form.addRow(self.vpin_register_btn)
        layout.addWidget(vpin_box)

        # WoVP
        wovp_box = QGroupBox("WoVP")
        wovp_form = QFormLayout(wovp_box)
        self.cb_wovp = QCheckBox("Enable WoVP")
        wovp_form.addRow(self.cb_wovp)
        self.wovp_api_key = QLineEdit()
        self.wovp_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        wovp_form.addRow("API key:", self.wovp_api_key)
        layout.addWidget(wovp_box)

        # iScored
        isc_box = QGroupBox("iScored")
        isc_form = QFormLayout(isc_box)
        self.cb_iscored = QCheckBox("Enable iScored")
        isc_form.addRow(self.cb_iscored)
        self.iscored_player = QLineEdit()
        isc_form.addRow("Username:", self.iscored_player)
        layout.addWidget(isc_box)

        layout.addStretch(1)
        self.tabs.addTab(page, "Integrations")

    def _build_capture_tab(self) -> None:
        page = QWidget()
        form = QFormLayout(page)

        self.screen_combo = QComboBox()
        app = QApplication.instance()
        if app is not None:
            for idx, screen in enumerate(app.screens()):
                geom = screen.geometry()
                label = f"Screen {idx} — {screen.name() or 'unnamed'} ({geom.width()}×{geom.height()})"
                self.screen_combo.addItem(label, idx)
        else:
            self.screen_combo.addItem("Screen 0 (primary)", 0)
        form.addRow("Capture display:", self.screen_combo)

        self.hotkey_edit = QLineEdit()
        form.addRow("Manual send hotkey:", self.hotkey_edit)

        self.joy_edit = QLineEdit()
        form.addRow("Joystick buttons:", self.joy_edit)

        self.tabs.addTab(page, "Capture & hotkey")

    def _build_paths_tab(self) -> None:
        page = QWidget()
        form = QFormLayout(page)

        nvram_row = QHBoxLayout()
        self.nvram_edit = QLineEdit()
        nvram_browse = QPushButton("Browse…")
        nvram_browse.clicked.connect(self._browse_nvram)
        nvram_row.addWidget(self.nvram_edit, 1)
        nvram_row.addWidget(nvram_browse)
        form.addRow("nvram base dir:", nvram_row)

        self.log_edit = QLineEdit()
        form.addRow("Log file:", self.log_edit)

        self.tabs.addTab(page, "Paths")

    def _browse_nvram(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select nvram base directory", self.nvram_edit.text())
        if d:
            self.nvram_edit.setText(d)

    # ---- load / save ----
    def _load(self) -> None:
        cp = _read_config(self.config_path)
        self.cb_vpin.setChecked(_truthy(cp.get("vpinleaders", "enable", fallback="false")))
        self.vpin_machine_id.setText(cp.get("vpinleaders", "machine_id", fallback=""))
        self.vpin_api_key.setText(cp.get("vpinleaders", "api_key", fallback=""))

        self.cb_wovp.setChecked(_truthy(cp.get("wovp", "enable", fallback="false")))
        self.wovp_api_key.setText(cp.get("wovp", "api_key", fallback=""))

        self.cb_iscored.setChecked(_truthy(cp.get("iscored", "enable", fallback="false")))
        self.iscored_player.setText(cp.get("iscored", "player_name", fallback=""))

        try:
            sid = int(cp.get("screenshot", "screen_to_capture", fallback="0") or "0")
        except ValueError:
            sid = 0
        idx = self.screen_combo.findData(sid)
        if idx >= 0:
            self.screen_combo.setCurrentIndex(idx)

        self.hotkey_edit.setText(cp.get("hotkeys", "keyboard", fallback="cmd+shift+s"))
        self.joy_edit.setText(cp.get("hotkeys", "joystick_buttons", fallback=""))

        self.nvram_edit.setText(cp.get("nvram", "base_dir", fallback=""))
        self.log_edit.setText(cp.get("logging", "file", fallback="~/.vpinleaders/logs/vpinleaders.log"))

    def _on_save(self) -> None:
        cp = _read_config(self.config_path)

        _ensure_section(cp, "vpinleaders")
        cp["vpinleaders"]["enable"] = "true" if self.cb_vpin.isChecked() else "false"
        cp["vpinleaders"]["api_url"] = DEFAULT_API_URL
        cp["vpinleaders"]["machine_id"] = self.vpin_machine_id.text().strip()
        cp["vpinleaders"]["api_key"] = self.vpin_api_key.text().strip()

        _ensure_section(cp, "wovp")
        cp["wovp"]["enable"] = "true" if self.cb_wovp.isChecked() else "false"
        cp["wovp"]["api_key"] = self.wovp_api_key.text().strip()

        _ensure_section(cp, "iscored")
        cp["iscored"]["enable"] = "true" if self.cb_iscored.isChecked() else "false"
        cp["iscored"]["player_name"] = self.iscored_player.text().strip()
        cp["iscored"].pop("room_urls", None)
        cp["iscored"].pop("gamerooms", None)

        _ensure_section(cp, "screenshot")
        cp["screenshot"]["screen_to_capture"] = str(int(self.screen_combo.currentData() or 0))

        _ensure_section(cp, "hotkeys")
        cp["hotkeys"]["keyboard"] = self.hotkey_edit.text().strip()
        cp["hotkeys"]["joystick_buttons"] = self.joy_edit.text().strip()

        _ensure_section(cp, "nvram")
        cp["nvram"]["base_dir"] = self.nvram_edit.text().strip()

        _ensure_section(cp, "logging")
        if self.log_edit.text().strip():
            cp["logging"]["file"] = self.log_edit.text().strip()

        try:
            _write_config(cp, self.config_path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not save settings", str(exc))
            return
        self.accept()

    def _register_vpinleaders(self) -> None:
        wizard = IntegrationSetupWizard(self.config_path, "vpinleaders", parent=self)
        QTimer.singleShot(0, wizard.raise_)
        QTimer.singleShot(0, wizard.activateWindow)
        result = wizard.exec()
        del wizard
        if not result:
            return

        cp = _read_config(self.config_path)
        self.cb_vpin.setChecked(_truthy(cp.get("vpinleaders", "enable", fallback="true")))
        self.vpin_machine_id.setText(cp.get("vpinleaders", "machine_id", fallback=""))
        self.vpin_api_key.setText(cp.get("vpinleaders", "api_key", fallback=""))


class IntegrationSetupWizard(QWizard):
    PAGE_SETUP = 0

    def __init__(self, config_path: str, integration: str, parent=None):
        super().__init__(parent)
        self.config_path = config_path
        self.integration = integration
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(620, 460)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)

        cp = _read_config(config_path)

        if integration == "vpinleaders":
            self.setWindowTitle("VPinLeaders Client - VPinLeaders Setup")
            self.page = _VPinLeadersPage(default_api_url=DEFAULT_API_URL)
        elif integration == "wovp":
            self.setWindowTitle("VPinLeaders Client - WoVP Setup")
            self.page = _WoVPPage()
            self.page.key_edit.setText(cp.get("wovp", "api_key", fallback=""))
        elif integration == "iscored":
            self.setWindowTitle("VPinLeaders Client - iScored Setup")
            self.page = _IScoredPage()
            self.page.player_edit.setText(cp.get("iscored", "player_name", fallback=""))
        else:
            raise ValueError(f"Unknown integration: {integration}")

        self.setPage(self.PAGE_SETUP, self.page)

    def nextId(self) -> int:
        return -1

    def accept(self) -> None:
        try:
            self._save()
        except Exception as exc:
            QMessageBox.critical(self, "Could not save settings", str(exc))
            return
        super().accept()

    def _save(self) -> None:
        cp = _read_config(self.config_path)

        if self.integration == "vpinleaders":
            if not isinstance(self.page, _VPinLeadersPage) or not self.page.isComplete():
                raise ValueError("VPinLeaders pairing is not complete.")
            _ensure_section(cp, "vpinleaders")
            cp["vpinleaders"]["enable"] = "true"
            cp["vpinleaders"]["api_url"] = DEFAULT_API_URL
            cp["vpinleaders"]["machine_id"] = self.page.machine_id
            cp["vpinleaders"]["api_key"] = self.page.api_key
        elif self.integration == "wovp":
            if not isinstance(self.page, _WoVPPage) or not self.page.isComplete():
                raise ValueError("WoVP API key is required.")
            _ensure_section(cp, "wovp")
            cp["wovp"]["enable"] = "true"
            cp["wovp"]["api_key"] = self.page.api_key
        elif self.integration == "iscored":
            if not isinstance(self.page, _IScoredPage) or not self.page.isComplete():
                raise ValueError("iScored username is required.")
            _ensure_section(cp, "iscored")
            cp["iscored"]["enable"] = "true"
            cp["iscored"]["player_name"] = self.page.player_name
            cp["iscored"].pop("room_urls", None)
            cp["iscored"].pop("gamerooms", None)

        _write_config(cp, self.config_path)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

# Module-level reference. Keeps the QApplication alive across the wizard /
# dialog calls — PyQt6 will destroy the underlying QApplication if its Python
# wrapper gets garbage-collected, even though Qt itself treats it as a
# singleton.
_qapp_ref: Optional[QApplication] = None


def _ensure_qapplication() -> QApplication:
    global _qapp_ref
    app = QApplication.instance()
    if app is None:
        import sys
        app = QApplication(sys.argv)
    _qapp_ref = app
    return app


def run_first_run_wizard(config_path: str) -> bool:
    """Run the first-run wizard. Returns True if accepted, False if cancelled."""
    app = _ensure_qapplication()
    wizard = FirstRunWizard(config_path)
    QTimer.singleShot(0, wizard.raise_)
    QTimer.singleShot(0, wizard.activateWindow)
    result = wizard.exec()
    del wizard
    return bool(result)


def open_settings_dialog(config_path: str, parent=None) -> bool:
    """Open the tabbed settings editor. Returns True if changes were saved."""
    app = _ensure_qapplication()
    dlg = SettingsDialog(config_path, parent=parent)
    QTimer.singleShot(0, dlg.raise_)
    QTimer.singleShot(0, dlg.activateWindow)
    result = dlg.exec()
    del dlg
    return bool(result)


def open_integration_setup(config_path: str, integration: str, parent=None) -> bool:
    """Open a focused setup flow for one integration and enable it on success."""
    app = _ensure_qapplication()
    wizard = IntegrationSetupWizard(config_path, integration, parent=parent)
    QTimer.singleShot(0, wizard.raise_)
    QTimer.singleShot(0, wizard.activateWindow)
    result = wizard.exec()
    del wizard
    return bool(result)
