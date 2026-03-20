# VPinLeaders Client

A client application that automatically captures and sends Visual Pinball X (VPX) scores and screenshots to [VPinLeaders](https://www.vpinleaders.com). The application runs in the background and provides a system tray icon for easy configuration and mode switching

- Live PinMAME memory polling
- `.nv` file polling fallback for roms with flush after closing VPX.

## Before You Start

You need:

- A VPinLeaders account at [vpinleaders.com](https://www.vpinleaders.com)
- A VPX 10.8.1 setup 
- The latest GitHub Actions artifact for your operating system

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
./VPinLeaders-macOS-arm64 --register --machine-id YOUR_MACHINE_ID
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
.\VPinLeaders-Windows-x64.exe --register --machine-id YOUR_MACHINE_ID
```

After registration, start it normally:

```powershell
.\VPinLeaders-Windows-x64.exe
```

### Linux x64

1. Unzip `VPinLeaders-Linux-x64.zip`
2. Run registration with the regular binary:

```bash
./VPinLeaders-Linux-x64/VPinLeaders-Linux-x64 --register --machine-id YOUR_MACHINE_ID
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

A sanitized [`config.example.ini`](config.example.ini) is kept in the repo only as a reference/template and for seeding the generated config.

Main config sections:

- `[credentials]` API values
- `[screenshot]` options (used only for manual sends)
- `[hotkeys]` manual keyboard/joystick bindings
- `[nvram]` base NVRAM discovery folder (`base_dir`)
- `[score-mode]`, `[send-mode]`, `[challenge]` as needed

NVRAM monitor settings are internal defaults in code:

- Scan pattern: `**/pinmame/nvram/*.nv;*.nv`
- Live PinMAME: enabled

## Source Run

If you want to run from source instead of the packaged artifacts:

```bash
pip install -r requirements.txt
python3 main.py --register --machine-id YOUR_MACHINE_ID
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

## CLI Helpers

List supported ROMs:

```bash
python3 main.py --list-roms
```

List high scores for one ROM (offline from `.nv` file):

```bash
python3 main.py --list-highscores afm_113b
```

Optional alternate NVRAM base dir:

```bash
python3 main.py --list-highscores afm_113b --base-dir /path/to/nvram-root
```
