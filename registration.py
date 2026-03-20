import configparser
import os
import platform
import shutil
import time

import qrcode
import requests

DEFAULT_API_URL = "https://www.vpinleaders.com"


def print_qr(url: str) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)


def _is_batocera() -> bool:
    if platform.system() != "Linux":
        return False
    if platform.uname().node == "BATOCERA":
        return True
    else:
        return False

def default_nvram_base_dir(explicit_value: str = "") -> str:
    value = str(explicit_value or "").strip()
    if value:
        return os.path.abspath(os.path.expanduser(value))
    if _is_batocera():
        return "/userdata/roms/vpinball"
    return ""


def default_log_file_path() -> str:
    if _is_batocera():
        return "/userdata/system/logs/vpinleaders-client.log"
    return os.path.expanduser("~/.vpinleaders/logs/vpinleaders.log")


def seed_config_from_example(config_path: str, example_path: str, api_url: str = DEFAULT_API_URL, nvram_base_dir: str = "") -> str:
    os.makedirs(os.path.dirname(config_path), exist_ok=True)

    if not os.path.exists(config_path):
        if not example_path or not os.path.exists(example_path):
            raise FileNotFoundError(f"config.example.ini not found: {example_path}")
        shutil.copyfile(example_path, config_path)

    cp = configparser.ConfigParser()
    cp.read(config_path)

    if "credentials" not in cp:
        cp["credentials"] = {}
    cp["credentials"]["api_url"] = api_url.rstrip("/")

    if "nvram" not in cp:
        cp["nvram"] = {}
    if nvram_base_dir:
        cp["nvram"]["base_dir"] = nvram_base_dir

    if "logging" not in cp:
        cp["logging"] = {}
    cp["logging"]["file"] = default_log_file_path()

    with open(config_path, "w", encoding="utf-8") as f:
        cp.write(f)
    return config_path


def write_config(config_path: str, machine_id: str, api_key: str, nvram_base_dir: str, api_url: str = DEFAULT_API_URL) -> str:
    cp = configparser.ConfigParser()
    if os.path.exists(config_path):
        cp.read(config_path)

    if "credentials" not in cp:
        cp["credentials"] = {}
    cp["credentials"]["api_url"] = api_url.rstrip("/")
    cp["credentials"]["machine_id"] = machine_id
    cp["credentials"]["api_key"] = api_key

    if "nvram" not in cp:
        cp["nvram"] = {}
    if nvram_base_dir:
        cp["nvram"]["base_dir"] = nvram_base_dir

    if "logging" not in cp:
        cp["logging"] = {}
    cp["logging"]["file"] = default_log_file_path()

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        cp.write(f)
    return config_path


def register(machine_id: str, config_path: str, example_path: str, nvram_base_dir: str = "", api_url: str = DEFAULT_API_URL) -> int:
    machine_id = str(machine_id or "").strip()
    if not machine_id:
        print("Error: --machine-id is required when using --register.")
        return 1

    resolved_nvram_dir = default_nvram_base_dir(nvram_base_dir)
    if not resolved_nvram_dir:
        print("Error: --nvrams-folder is required unless running on Batocera.")
        return 1

    try:
        seed_config_from_example(
            config_path=config_path,
            example_path=example_path,
            api_url=api_url,
            nvram_base_dir=resolved_nvram_dir,
        )
    except Exception as e:
        print(f"Failed to prepare config.ini from example. ({e})")
        return 1

    api_base = api_url.rstrip("/") + "/api"

    try:
        resp = requests.post(
            f"{api_base}/device/pair/start",
            json={"machine_id": machine_id},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"Failed to connect to API or invalid machine_id. ({e})")
        return 1

    data = resp.json()
    pairing_url = data["pairing_url"]
    pairing_code = data["pairing_code"]
    polling_token = data["polling_token"]

    print("\nScan this QR code to register this machine:\n")
    print_qr(pairing_url)
    print(f"\nOr go to: {pairing_url}")
    print(f"Code: {pairing_code}\n")
    print("Waiting for registration...")

    while True:
        time.sleep(3)
        try:
            status_resp = requests.post(
                f"{api_base}/device/pair/status",
                json={"polling_token": polling_token},
                timeout=10,
            )
            status_resp.raise_for_status()
            status = status_resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                print("\nSession not found or invalid.")
                return 1
            continue
        except Exception:
            continue

        state = status.get("status")
        if state == "pending":
            print(".", end="", flush=True)
            continue

        if state == "approved":
            approved_machine_id = str(status.get("machine_id") or machine_id).strip()
            api_key = str(status.get("api_key") or "").strip()
            if not api_key:
                print("\nRegistration failed: API key was not returned by the server.")
                return 1

            written_path = write_config(
                config_path=config_path,
                machine_id=approved_machine_id,
                api_key=api_key,
                nvram_base_dir=resolved_nvram_dir,
                api_url=api_url,
            )

            print("\nRegistration complete.")
            print(f"Config saved to: {written_path}")
            print(f"NVRAM base dir: {resolved_nvram_dir}")
            return 0

        if state == "expired":
            print("\nRegistration session expired. Please try again.")
            return 1
