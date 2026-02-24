# VPinLeaders Client

A desktop client application that automatically captures and sends Visual Pinball X (VPX) scores and screenshots to [VPinLeaders](https://www.vpinleaders.com). The application runs in the background and provides a system tray icon for easy configuration and mode switching.

## Features

- **Automatic Score Capture**: Listens to VPX events via WebSocket and detects when a game ends.
- **Screenshot Support**: Automatically captures the active screen and submits it along with your score as proof.
- **Multiple Modes**:
  - **Score Mode**: Send general scores.
  - **Tournament Mode**: Submits scores to a specific active Challenge ID(Soon).
- **Sending Modes**:
  - **Automatic Send**: Submits the best score as soon as the game ends.
  - **Manual Send**: Only submits the score when triggered by a global hotkey (`Cmd+Shift+S` on macOS, `Ctrl+Shift+S` on Windows/Linux).
- **System Tray Integration**: Easily toggle between modes, update settings, and view connection status from the taskbar menu.

## Requirements

- Python >= 3.12
- [score-server](https://github.com/superhac/vpinball) branch vpinball-score-server.
- VPinLeaders API Key and Machine ID (obtainable from your profile at vpinleaders.com).

## Installation

1. Clone or download this repository.
2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
## Configuration

Before running the client, create your account on [VPinLeaders](https://www.vpinleaders.com) and visit the section 'How to Setup' to download your config.ini file.

## Running the Client

If you're not using of the pre-built versions, start the application by running:

```bash
python main.py
```

The application will appear in your system tray. Although you can switch between 'Score Mode' and 'Challeng Mode', only Score Mode works at the moment.