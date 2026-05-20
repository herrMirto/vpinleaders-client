"""First-run wizard and tabbed settings dialog for the VPinLeaders client.

Two public entry points:

    run_first_run_wizard(config_path) -> bool
        Launches the linear wizard. Returns True if the user finished, False if
        they cancelled. Writes the resulting state to ``config_path``.

    open_settings_dialog(config_path) -> bool
        Opens the tabbed editor for an already-configured install. Returns True
        if changes were saved, False otherwise.

The module is self-contained: it uses configparser to read/write the ini file
directly.
"""

from __future__ import annotations

import configparser
import os
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from vpinplay_client import generate_machine_id, load_vpinfe_vpinplay_config, normalize_api_url

DEFAULT_VPINPLAY_API_URL = "http://localhost:8888"


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


def _valid_vpinplay_machine_id(value: str) -> bool:
    return len(str(value or "").strip()) >= 64


def _machine_id_or_generated(value: str) -> str:
    value = str(value or "").strip()
    return value if _valid_vpinplay_machine_id(value) else generate_machine_id()


def _vpinplay_values_from_vpinfe_or_config(cp: configparser.ConfigParser) -> dict:
    imported = load_vpinfe_vpinplay_config()
    return {
        "api_url": imported.get("api_url") or normalize_api_url(cp.get("vpinplay", "api_url", fallback=DEFAULT_VPINPLAY_API_URL)),
        "user_id": imported.get("user_id") or cp.get("vpinplay", "user_id", fallback="").strip(),
        "initials": imported.get("initials") or cp.get("vpinplay", "initials", fallback="").strip(),
        "machine_id": _machine_id_or_generated(imported.get("machine_id") or cp.get("vpinplay", "machine_id", fallback="")),
        "source_path": imported.get("source_path", ""),
    }


def _build_page_layout(page: QWizardPage, title: str, subtitle: str) -> QVBoxLayout:
    root = QVBoxLayout(page)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    header = QFrame()
    header.setObjectName("wizardHeader")
    header.setStyleSheet(
        "QFrame#wizardHeader {"
        "background: #fbfbfb;"
        "border-bottom: 1px solid #c7c7c7;"
        "}"
    )
    header_layout = QVBoxLayout(header)
    header_layout.setContentsMargins(24, 18, 24, 18)
    header_layout.setSpacing(6)

    title_label = QLabel(title)
    title_font = title_label.font()
    title_font.setPointSize(title_font.pointSize() + 4)
    title_font.setBold(True)
    title_label.setFont(title_font)

    subtitle_label = QLabel(subtitle)
    subtitle_label.setWordWrap(True)

    header_layout.addWidget(title_label)
    header_layout.addWidget(subtitle_label)
    root.addWidget(header)

    body = QVBoxLayout()
    body.setContentsMargins(24, 28, 24, 24)
    body.setSpacing(10)
    root.addLayout(body, 1)
    return body


def _add_horizontal_separator(layout: QVBoxLayout) -> None:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    layout.addWidget(line)


# ---------------------------------------------------------------------------
# Wizard pages
# ---------------------------------------------------------------------------

class _WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        layout = _build_page_layout(self, "Welcome to VPinLeaders Client", "Let's get you set up.")
        layout.addWidget(QLabel(
            "This wizard will configure the client for the first time.\n\n"
            "You'll choose which leaderboards to send your scores to and provide\n"
            "the setup details for each. Everything can be changed later from the\n"
            "tray icon (Settings…).\n\n"
            "Click Next to continue."
        ))


class _NvramFolderPage(QWizardPage):
    def __init__(self):
        super().__init__()
        layout = _build_page_layout(self, "Tables Folder", "Choose your Visual Pinball tables folder.")
        info = QLabel(
            "Pick the folder that contains your VPX tables. It should follow the "
            "<a href='https://github.com/vpinball/vpinball/blob/master/docs/FileLayout.md'>"
            "VPX 10.8.1 File Layout</a>."
        )
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        layout.addWidget(info)

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
        d = QFileDialog.getExistingDirectory(self, "Select tables folder")
        if d:
            self.path_edit.setText(d)

    def isComplete(self) -> bool:
        return bool(self.path_edit.text().strip())

    def value(self) -> str:
        return self.path_edit.text().strip()


class _IntegrationPickerPage(QWizardPage):
    def __init__(self):
        super().__init__()
        layout = _build_page_layout(self, "Choose integrations", "Pick one or more leaderboards. You can change this later.")
        self.cb_vpinplay = QCheckBox("VPinPlay — syncs to your own local or network instance")
        self.cb_wovp = QCheckBox("WoVP (World of Virtual Pinball) — submits to active challenges")
        self.cb_isc = QCheckBox("iScored — submits to your iScored gameroom")

        for cb in (self.cb_vpinplay, self.cb_wovp, self.cb_isc):
            cb.toggled.connect(self.completeChanged)
            layout.addWidget(cb)

        layout.addStretch(1)
        self.setCommitPage(False)

    def isComplete(self) -> bool:
        # Allow advancing without selecting any — they can always reopen the
        # settings dialog later. But require at least confirming the page.
        return True

    @property
    def want_wovp(self) -> bool:
        return self.cb_wovp.isChecked()

    @property
    def want_iscored(self) -> bool:
        return self.cb_isc.isChecked()

    @property
    def want_vpinplay(self) -> bool:
        return self.cb_vpinplay.isChecked()


class _WoVPPage(QWizardPage):
    def __init__(self):
        super().__init__()
        layout = _build_page_layout(self, "WoVP setup", "Provide your WoVP API key.")
        layout.addWidget(QLabel(
            "1. Sign up / sign in at https://worldofvirtualpinball.com/en\n"
            "2. Generate an API key in your account settings.\n"
            "3. Paste it below. The challenge to play against can be picked\n"
            "   later from the tray menu (WoVP ▸ Challenges)."
        ))

        layout.addSpacing(12)
        _add_horizontal_separator(layout)
        layout.addSpacing(12)

        row = QHBoxLayout()
        row.addWidget(QLabel("API key:"))
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("WoVP API key")
        self.key_edit.textChanged.connect(self.completeChanged)
        row.addWidget(self.key_edit, 1)
        layout.addLayout(row)
        layout.addStretch(1)

    def isComplete(self) -> bool:
        return bool(self.key_edit.text().strip())

    @property
    def api_key(self) -> str:
        return self.key_edit.text().strip()


class _IScoredPage(QWizardPage):
    def __init__(self):
        super().__init__()
        layout = _build_page_layout(self, "iScored setup", "Tell the client your iScored username.")
        layout.addWidget(QLabel(
            "1. Create or sign in to an iScored gameroom at https://iscored.info\n"
            "2. Make sure your gameroom allows score submissions.\n"
            "3. Enter your iScored username."
        ))

        layout.addSpacing(12)
        _add_horizontal_separator(layout)
        layout.addSpacing(12)

        row = QHBoxLayout()
        row.addWidget(QLabel("Username:"))
        self.player_edit = QLineEdit()
        self.player_edit.setPlaceholderText("e.g. Username")
        self.player_edit.textChanged.connect(self.completeChanged)
        row.addWidget(self.player_edit, 1)
        layout.addLayout(row)
        layout.addStretch(1)

    def isComplete(self) -> bool:
        return bool(self.player_edit.text().strip())

    @property
    def player_name(self) -> str:
        return self.player_edit.text().strip()


class _VPinPlayPage(QWizardPage):
    def __init__(self):
        super().__init__()
        layout = _build_page_layout(self, "VPinPlay setup", "Sync scores to your own local or network VPinPlay instance.")
        imported = load_vpinfe_vpinplay_config()
        form = QFormLayout()
        self.api_url_edit = QLineEdit(imported.get("api_url") or DEFAULT_VPINPLAY_API_URL)
        self.user_id_edit = QLineEdit(imported.get("user_id", ""))
        self.initials_edit = QLineEdit(imported.get("initials", ""))
        self.machine_id_edit = QLineEdit(_machine_id_or_generated(imported.get("machine_id", "")))
        self.machine_id_edit.setReadOnly(True)
        self.auto_send_check = QCheckBox("Send VPinPlay scores automatically when a game ends")
        if imported.get("source_path"):
            note_text = "VPinPlay details were loaded from VPinFE."
        else:
            note_text = "Machine ID is generated automatically."
        self.machine_id_note = QLabel(note_text)
        self.machine_id_note.setWordWrap(True)

        for edit in (self.api_url_edit, self.user_id_edit, self.initials_edit, self.machine_id_edit):
            edit.textChanged.connect(self.completeChanged)

        form.addRow("VPinPlay URL:", self.api_url_edit)
        form.addRow("User ID:", self.user_id_edit)
        form.addRow("Initials:", self.initials_edit)
        form.addRow("Machine ID:", self.machine_id_edit)
        form.addRow("", self.machine_id_note)
        layout.addLayout(form)
        layout.addWidget(self.auto_send_check)
        layout.addStretch(1)

    def isComplete(self) -> bool:
        return bool(
            self.api_url_edit.text().strip()
            and self.user_id_edit.text().strip()
            and self.initials_edit.text().strip()
            and _valid_vpinplay_machine_id(self.machine_id_edit.text())
        )

    @property
    def api_url(self) -> str:
        return self.api_url_edit.text().strip() or DEFAULT_VPINPLAY_API_URL

    @property
    def user_id(self) -> str:
        return self.user_id_edit.text().strip()

    @property
    def initials(self) -> str:
        return self.initials_edit.text().strip()

    @property
    def machine_id(self) -> str:
        return self.machine_id_edit.text().strip()

    @property
    def auto_send(self) -> bool:
        return self.auto_send_check.isChecked()

    def apply_values(self, values: dict) -> None:
        self.api_url_edit.setText(values.get("api_url") or DEFAULT_VPINPLAY_API_URL)
        self.user_id_edit.setText(values.get("user_id", ""))
        self.initials_edit.setText(values.get("initials", ""))
        self.machine_id_edit.setText(_machine_id_or_generated(values.get("machine_id", "")))
        if values.get("source_path"):
            self.machine_id_note.setText("VPinPlay details were loaded from VPinFE.")


class _CapturePage(QWizardPage):
    def __init__(self):
        super().__init__()
        layout = _build_page_layout(self, "Capture & hotkey", "Choose which display to capture and the manual-send hotkey.")
        layout.addWidget(QLabel(
            "Screenshots are taken whenever you trigger a manual send. They\n"
            "are attached to WoVP and iScored submissions. Pick the display\n"
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
        layout = _build_page_layout(self, "All set", "Click Finish to start the client.")
        self.summary = QLabel("Reviewing your selections…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addStretch(1)

    def initializePage(self) -> None:
        wiz = self.wizard()
        if not isinstance(wiz, FirstRunWizard):
            return
        bullets = []
        if wiz.picker.want_vpinplay and wiz.vpinplay.isComplete():
            bullets.append("• VPinPlay configured")
        if wiz.picker.want_wovp and wiz.wovp.isComplete():
            bullets.append("• WoVP API key configured")
        if wiz.picker.want_iscored and wiz.iscored.isComplete():
            bullets.append(f"• iScored player '{wiz.iscored.player_name}'")
        if not bullets:
            bullets.append("• No integrations enabled. You can enable them later "
                           "from the tray icon → Settings…")
        bullets.append(f"• Tables folder: {wiz.nvram.value()}")
        self.summary.setText("\n".join(bullets))


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class FirstRunWizard(QWizard):
    PAGE_WELCOME = 0
    PAGE_NVRAM = 1
    PAGE_PICKER = 2
    PAGE_VPINPLAY = 3
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
        self.vpinplay = _VPinPlayPage()
        self.wovp = _WoVPPage()
        self.iscored = _IScoredPage()
        self.capture = _CapturePage()
        self.done_page = _DonePage()

        self.setPage(self.PAGE_WELCOME, self.welcome)
        self.setPage(self.PAGE_NVRAM, self.nvram)
        self.setPage(self.PAGE_PICKER, self.picker)
        self.setPage(self.PAGE_VPINPLAY, self.vpinplay)
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
            if self.picker.want_vpinplay:
                return self.PAGE_VPINPLAY
            if self.picker.want_wovp:
                return self.PAGE_WOVP
            if self.picker.want_iscored:
                return self.PAGE_ISCORED
            return self.PAGE_CAPTURE
        if cur == self.PAGE_VPINPLAY:
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
        cp.remove_section("vpinleaders")
        cp.remove_section("credentials")

        # VPinPlay
        _ensure_section(cp, "vpinplay")
        if self.picker.want_vpinplay and self.vpinplay.isComplete():
            cp["vpinplay"]["enable"] = "true"
            cp["vpinplay"]["api_url"] = normalize_api_url(self.vpinplay.api_url)
            cp["vpinplay"]["user_id"] = self.vpinplay.user_id
            cp["vpinplay"]["initials"] = self.vpinplay.initials
            cp["vpinplay"]["machine_id"] = self.vpinplay.machine_id
            cp["vpinplay"]["auto_send"] = "true" if self.vpinplay.auto_send else "false"
        else:
            cp["vpinplay"]["enable"] = "false"
            cp["vpinplay"]["auto_send"] = "false"

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
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        def tune_form(form: QFormLayout) -> None:
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setHorizontalSpacing(10)
            form.setVerticalSpacing(10)

        def style_integration_box(box: QGroupBox) -> None:
            box.setStyleSheet(
                """
                QGroupBox {
                    background-color: #e8e8e8;
                    border: 1px solid #c8c8c8;
                    border-radius: 6px;
                    margin-top: 10px;
                    padding: 10px 8px 8px 8px;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 4px;
                }
                """
            )

        def add_separator() -> None:
            line = QFrame()
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFrameShadow(QFrame.Shadow.Sunken)
            layout.addWidget(line)

        # VPinPlay
        vpinplay_box = QGroupBox("VPinPlay")
        style_integration_box(vpinplay_box)
        vpinplay_form = QFormLayout(vpinplay_box)
        tune_form(vpinplay_form)
        self.cb_vpinplay = QCheckBox("Enable VPinPlay")
        vpinplay_form.addRow(self.cb_vpinplay)
        self.vpinplay_api_url = QLineEdit()
        vpinplay_form.addRow("VPinPlay URL:", self.vpinplay_api_url)
        self.vpinplay_user_id = QLineEdit()
        vpinplay_form.addRow("User ID:", self.vpinplay_user_id)
        self.vpinplay_initials = QLineEdit()
        vpinplay_form.addRow("Initials:", self.vpinplay_initials)
        self.vpinplay_machine_id = QLineEdit()
        self.vpinplay_machine_id.setReadOnly(True)
        vpinplay_form.addRow("Machine ID:", self.vpinplay_machine_id)
        vpinplay_machine_id_note = QLabel("Machine ID is generated automatically.")
        vpinplay_machine_id_note.setWordWrap(True)
        vpinplay_form.addRow("", vpinplay_machine_id_note)
        self.cb_vpinplay_auto_send = QCheckBox("Send VPinPlay scores automatically when a game ends")
        vpinplay_form.addRow(self.cb_vpinplay_auto_send)
        layout.addWidget(vpinplay_box)
        add_separator()

        # WoVP
        wovp_box = QGroupBox("WoVP")
        style_integration_box(wovp_box)
        wovp_form = QFormLayout(wovp_box)
        tune_form(wovp_form)
        self.cb_wovp = QCheckBox("Enable WoVP")
        wovp_form.addRow(self.cb_wovp)
        self.wovp_api_key = QLineEdit()
        self.wovp_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        wovp_form.addRow("API key:", self.wovp_api_key)
        layout.addWidget(wovp_box)
        add_separator()

        # iScored
        isc_box = QGroupBox("iScored")
        style_integration_box(isc_box)
        isc_form = QFormLayout(isc_box)
        tune_form(isc_form)
        self.cb_iscored = QCheckBox("Enable iScored")
        isc_form.addRow(self.cb_iscored)
        self.iscored_player = QLineEdit()
        isc_form.addRow("Username:", self.iscored_player)
        layout.addWidget(isc_box)

        layout.addStretch(1)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, "Integrations")

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
        form.addRow("Tables folder:", nvram_row)

        layout_note = QLabel(
            "Use the folder that contains your VPX tables. It should follow the "
            "<a href='https://github.com/vpinball/vpinball/blob/master/docs/FileLayout.md'>"
            "VPX 10.8.1 File Layout</a>."
        )
        layout_note.setWordWrap(True)
        layout_note.setOpenExternalLinks(True)
        form.addRow("", layout_note)

        self.log_edit = QLineEdit()
        form.addRow("Log file:", self.log_edit)

        self.tabs.addTab(page, "Paths")

    def _browse_nvram(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select tables folder", self.nvram_edit.text())
        if d:
            self.nvram_edit.setText(d)

    # ---- load / save ----
    def _load(self) -> None:
        cp = _read_config(self.config_path)
        vpinplay_values = _vpinplay_values_from_vpinfe_or_config(cp)
        self.cb_vpinplay.setChecked(_truthy(cp.get("vpinplay", "enable", fallback="false")))
        self.vpinplay_api_url.setText(vpinplay_values["api_url"])
        self.vpinplay_user_id.setText(vpinplay_values["user_id"])
        self.vpinplay_initials.setText(vpinplay_values["initials"])
        self.vpinplay_machine_id.setText(vpinplay_values["machine_id"])
        self.cb_vpinplay_auto_send.setChecked(_truthy(cp.get("vpinplay", "auto_send", fallback="false")))

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
        self.log_edit.setText(cp.get("logging", "file", fallback="~/.vpinscoretracker/logs/vpinscoretracker.log"))

    def _on_save(self) -> None:
        cp = _read_config(self.config_path)
        cp.remove_section("vpinleaders")
        cp.remove_section("credentials")

        _ensure_section(cp, "wovp")
        cp["wovp"]["enable"] = "true" if self.cb_wovp.isChecked() else "false"
        cp["wovp"]["api_key"] = self.wovp_api_key.text().strip()

        _ensure_section(cp, "iscored")
        cp["iscored"]["enable"] = "true" if self.cb_iscored.isChecked() else "false"
        cp["iscored"]["player_name"] = self.iscored_player.text().strip()
        cp["iscored"].pop("room_urls", None)
        cp["iscored"].pop("gamerooms", None)

        _ensure_section(cp, "vpinplay")
        cp["vpinplay"]["enable"] = "true" if self.cb_vpinplay.isChecked() else "false"
        cp["vpinplay"]["api_url"] = normalize_api_url(self.vpinplay_api_url.text())
        cp["vpinplay"]["user_id"] = self.vpinplay_user_id.text().strip()
        cp["vpinplay"]["initials"] = self.vpinplay_initials.text().strip()
        cp["vpinplay"]["machine_id"] = _machine_id_or_generated(self.vpinplay_machine_id.text())
        cp["vpinplay"]["auto_send"] = "true" if self.cb_vpinplay_auto_send.isChecked() else "false"
        if self.cb_vpinplay.isChecked() and not (
            cp["vpinplay"]["api_url"].strip()
            and cp["vpinplay"]["user_id"].strip()
            and cp["vpinplay"]["initials"].strip()
        ):
            QMessageBox.critical(
                self,
                "VPinPlay setup incomplete",
                "Enter the VPinPlay URL, user ID, and initials.",
            )
            return

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

        if integration == "wovp":
            self.setWindowTitle("VPinLeaders Client - WoVP Setup")
            self.page = _WoVPPage()
            self.page.key_edit.setText(cp.get("wovp", "api_key", fallback=""))
        elif integration == "iscored":
            self.setWindowTitle("VPinLeaders Client - iScored Setup")
            self.page = _IScoredPage()
            self.page.player_edit.setText(cp.get("iscored", "player_name", fallback=""))
        elif integration == "vpinplay":
            self.setWindowTitle("VPinLeaders Client - VPinPlay Setup")
            self.page = _VPinPlayPage()
            self.page.apply_values(_vpinplay_values_from_vpinfe_or_config(cp))
            self.page.auto_send_check.setChecked(_truthy(cp.get("vpinplay", "auto_send", fallback="false")))
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

        if self.integration == "wovp":
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
        elif self.integration == "vpinplay":
            if not isinstance(self.page, _VPinPlayPage) or not self.page.isComplete():
                raise ValueError("VPinPlay URL, user ID, and initials are required.")
            _ensure_section(cp, "vpinplay")
            cp["vpinplay"]["enable"] = "true"
            cp["vpinplay"]["api_url"] = normalize_api_url(self.page.api_url)
            cp["vpinplay"]["user_id"] = self.page.user_id
            cp["vpinplay"]["initials"] = self.page.initials
            cp["vpinplay"]["machine_id"] = self.page.machine_id
            cp["vpinplay"]["auto_send"] = "true" if self.page.auto_send else "false"

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
    app.setQuitOnLastWindowClosed(False)
    _qapp_ref = app
    return app


def run_first_run_wizard(config_path: str) -> bool:
    """Run the first-run wizard. Returns True if accepted, False if cancelled."""
    app = _ensure_qapplication()
    wizard = FirstRunWizard(config_path)
    QTimer.singleShot(0, wizard.raise_)
    QTimer.singleShot(0, wizard.activateWindow)
    result = wizard.exec()
    wizard.close()
    wizard.deleteLater()
    app.setQuitOnLastWindowClosed(False)
    app.processEvents()
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
