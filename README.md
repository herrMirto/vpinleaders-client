# VPinLeaders Client

VPinLeaders Client is a score tracker for Visual Pinball X (VPX) cabinets and desktop setups. It watches your games as you play, captures the final score, and helps you submit results to supported leaderboard, challenges, and score-sharing services.

It currently supports:

- VPinLeaders leaderboard
- WoVP challenges
- iScored gamerooms

The client runs in the background monitoring PinMAME/NVRAM directly from Visual Pinball X. Once a game is finished, you can press the configured hotkeys and your score will be sent to one or many integrations that are setup.

## Before You Start

You need:

- A VPinLeaders account at [vpinleaders.com](https://www.vpinleaders.com)
- A VPX 10.8.1 setup 
- The latest GitHub Actions artifact for your operating system

Your tables folder should follow the
[VPX 10.8.1 File Layout](https://github.com/vpinball/vpinball/blob/master/docs/FileLayout.md).

Create your account first:

- [https://www.vpinleaders.com](https://www.vpinleaders.com)

Then open the latest successful workflow run in the repo Actions tab and download the artifact for your platform:

- macOS arm64: `VPinLeaders-macOS-arm64.zip`
- Windows x64: `VPinLeaders-Windows-x64.zip`
- Linux x64: `VPinLeaders-Linux-x64.zip`
- Batocera x64: follow [batocera/README.md](batocera/README.md)

## Platform Setup

### macOS arm64

1. Unzip `VPinLeaders-macOS-arm64.zip`
2. Remove the quarantine attribute:

```bash
xattr -d com.apple.quarantine VPinLeaders-macOS-arm64
```

3. Run the client:

```bash
./VPinLeaders-macOS-arm64 --register --machine-id YOUR_MACHINE_ID --nvrams-folder /Your/tables/folder
```

After registration, start it normally:

```bash
./VPinLeaders-macOS-arm64
```

### Windows x64

1. Download `VPinLeaders-Windows-x64.zip`
2. Right-click the downloaded `.zip`, open `Properties`, and click `Unblock`
3. Unzip it
4. Run registration:

```powershell
.\VPinLeaders-Windows-x64.exe --register --machine-id YOUR_MACHINE_ID --nvrams-folder C:\Your\tables\folder
```

After registration, start it normally:

```powershell
.\VPinLeaders-Windows-x64.exe
```

### Linux x64

1. Unzip `VPinLeaders-Linux-x64.zip`
2. Run registration with the regular binary:

```bash
./VPinLeaders-Linux-x64/VPinLeaders-Linux-x64 --register --machine-id YOUR_MACHINE_ID --nvrams-folder /Your/tables/folder

```

3. Start the app with the bundled launcher:

```bash
./vpinleaders-start.sh
```

The launcher exists because Linux needs extra permission to read live VPX data. It applies the required capability for the packaged binary:

```bash
sudo setcap cap_sys_ptrace=eip /path/to/VPinLeaders-Linux-x64/VPinLeaders-Linux-x64
```

You may be prompted for `sudo` the first time the launcher runs.

## Registration And Configuration

Registration creates the config file automatically in the correct location:

- Linux: `~/.config/vpinleaders-client/config.ini`
- macOS: `~/Library/Application Support/vpinleaders-client/config.ini`
- Windows: `%APPDATA%\vpinleaders-client\config.ini`

After the first run, use the tray menu to open `Settings`. From there you can
enable integrations, choose the display used for screenshots, and register
VPinLeaders again if needed.

### VPinLeaders

VPinLeaders connects your VPX setup to the VPinLeaders leaderboard. Register
the client during first setup, or open `Settings` and click `Register` to start
the QR code flow again.

### WoVP Challenges

WoVP lets the client submit scores to active challenges.

To configure it:

1. Sign in at [worldofvirtualpinball.com](https://worldofvirtualpinball.com/en).
2. Create a WoVP API key in your account settings.
3. Open the client tray menu.
4. Choose `Settings`.
5. Enable `WoVP`.
6. Paste your WoVP API key.
7. Save the settings.

When WoVP is enabled, the client loads active challenges at startup and shows
them in the tray menu. Select the challenge you want to submit to before
sending a score.

WoVP submissions require a screenshot. The client captures the configured
display when you trigger a manual send.

### iScored Gamerooms

iScored lets the client submit scores to an iScored gameroom.

To configure it:

1. In iScored, make sure your gameroom allows score submissions.
2. Open the client tray menu.
3. Choose `Settings`.
4. Enable `iScored`.
5. Enter your iScored username.
6. Save the settings.

After iScored is configured, the client loads your gameroom games and shows
them under `iScored > Games` in the tray menu. Select the game you want to
submit to before sending a score.

iScored submissions use the selected gameroom and game.


## Source Run

If you want to run from source instead of the packaged artifacts:

```bash
pip install -r requirements.txt
python3 main.py --register --machine-id YOUR_MACHINE_ID --nvrams-folder /Your/tables/folder
python3 main.py
```

Optional explicit config path for development/testing only:

```bash
python3 main.py --config /path/to/config.ini
```

## NVRAM Maps

The `nvram-maps/` directory contains JSON map files that describe how each
pinball ROM stores scores and game state inside its non-volatile RAM (`.nv`
files).

This program makes use of content from the
[Pinball Memory Maps](https://github.com/tomlogic/pinmame-nvram-maps) project,
licensed under the GNU Lesser General Public License v3.0 (LGPL-3.0).

The upstream map files are included unmodified where available.  A local fork
is maintained here for two reasons:

1. **Extended maps** — some ROMs required for VPinLeaders are not yet covered
   by the upstream project, or need additional `game_state` fields (live ball,
   player and score tracking) that go beyond its current scope.
2. **Custom conventions** — a few maps deviate slightly from the upstream
   format to accommodate edge cases specific to this client (e.g. live RAM
   addresses vs. `.nv` file offsets for the Frida memory-polling path).

The intention is to upstream all compatible additions back to the
[tomlogic/pinmame-nvram-maps](https://github.com/tomlogic/pinmame-nvram-maps)
project once they are stable and conform to its file-format conventions.
