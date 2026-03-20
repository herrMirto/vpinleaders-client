# Batocera Setup

This guide is for Batocera x86_64 systems running Visual Pinball.

## Before You Start

You need:

- A Batocera 42 `x86_64` system
- Internet access on the Batocera machine
- SSH access to Batocera
- A VPinLeaders account at [vpinleaders.com](https://www.vpinleaders.com)

Important:

- Batocera uses this default NVRAM base folder automatically:
  - `/userdata/roms/vpinball`
- You do not need to create or edit `config.ini` manually
- Registration writes the config file for you

## 1. Sign Up

Create your account first:

- [https://www.vpinleaders.com](https://www.vpinleaders.com)

Do this before running the installer or registration command.

## 2. Install The Client

From an SSH shell on Batocera, run:

```bash
curl -L https://raw.githubusercontent.com/herrmirto/vpinleaders-client/main/batocera/vpinleaders-installer.sh | bash
```

This installs:

- The client binary under:
  - `/userdata/system/vpinleaders-client/current/`
- The Batocera service script under:
  - `/userdata/system/services/VPinLeaders`

## 3. Register Your Cabinet

After installation, register the machine:

```bash
/userdata/system/vpinleaders-client/current/vpinleaders-client --register --machine-id mybatocera--machine
```

What this does:

- Shows a registration QR code / pairing flow
- Links the Batocera machine to your VPinLeaders account
- Writes a config file automatically to:
  - `/userdata/system/configs/vpinleaders-client/config.ini`

## 4. Start The Service

Once registration is complete, start the client:

```bash
/userdata/system/services/VPinLeaders start
```

Check status:

```bash
/userdata/system/services/VPinLeaders status
```

## 5. Verify It Is Running

Check the log file:

```bash
tail -n 100 /userdata/system/logs/vpinleaders-client.log
```

You should see startup lines similar to:

- Config loaded
- NVRAM monitor running
- Waiting for game to start

## Notification Behavior

On Batocera, score notifications are shown as on-screen during gameplay(after the last ball is drained).

## Supported ROMs

Supported tables/ROMs are documented in the main README:

- [NVRAM Maps and supported ROMs](../README.md#nvram-maps)

## Troubleshooting

If registration fails:

- Make sure you already created your account at [vpinleaders.com](https://www.vpinleaders.com)
- Check internet access on Batocera

If the service does not start:

```bash
/userdata/system/services/VPinLeaders status
tail -n 100 /userdata/system/logs/vpinleaders-client.log
```

If you need to restart the client:

```bash
/userdata/system/services/VPinLeaders restart
```

If you need to stop it:

```bash
/userdata/system/services/VPinLeaders stop
```

Feel free to open issues here on Github, but also feel free to reach out to me on discord(herrmirto).
