import configparser
import hashlib
import logging
import os
import platform
import re
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from requests import HTTPError

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://localhost:8888"
CLIENT_VERSION = "1.0"


def generate_machine_id() -> str:
    return secrets.token_hex(32)


def normalize_api_url(value: str) -> str:
    raw = str(value or "").strip() or DEFAULT_API_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    raw = raw.rstrip("/")
    if raw.endswith("/api/v1/sync"):
        return raw[: -len("/api/v1/sync")]
    if raw.endswith("/api/v1"):
        return raw[: -len("/api/v1")]
    return raw


def vpinfe_ini_path() -> str:
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Darwin":
        return os.path.join(home, "Library", "Application Support", "vpinfe", "vpinfe.ini")
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "vpinfe", "vpinfe", "vpinfe.ini")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "vpinfe", "vpinfe.ini")


def load_vpinfe_vpinplay_config(path: str = "") -> dict:
    ini_path = os.path.expanduser(path or vpinfe_ini_path())
    cp = configparser.ConfigParser()
    if not os.path.exists(ini_path):
        return {}
    cp.read(ini_path)
    if "vpinplay" not in cp:
        return {}

    section = cp["vpinplay"]
    data = {
        "api_url": normalize_api_url(section.get("apiendpoint", "")),
        "user_id": section.get("userid", "").strip(),
        "initials": section.get("initials", "").strip(),
        "machine_id": section.get("machineid", "").strip(),
        "source_path": ini_path,
    }
    return {key: value for key, value in data.items() if value}


class VPinPlayClient:
    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        self.api_url = self.config.get("vpinplay", "api_url", fallback=DEFAULT_API_URL).strip() or DEFAULT_API_URL
        self.user_id = self.config.get("vpinplay", "user_id", fallback="").strip()
        self.initials = self.config.get("vpinplay", "initials", fallback="").strip()
        self.machine_id = self.config.get("vpinplay", "machine_id", fallback="").strip()
        self._apply_vpinfe_config_if_available()
        if self.config.getboolean("vpinplay", "enable", fallback=False) and len(self.machine_id) < 64:
            self.machine_id = self._save_generated_machine_id()

    def is_ready(self) -> bool:
        return bool(self.api_url and self.user_id and self.initials and len(self.machine_id) >= 64)

    def _save_generated_machine_id(self) -> str:
        machine_id = generate_machine_id()
        self._save_config_values(machine_id=machine_id)
        logger.info("VPinPlay: generated a new machine ID")
        return machine_id

    def _save_config_values(
        self,
        api_url: str | None = None,
        user_id: str | None = None,
        initials: str | None = None,
        machine_id: str | None = None,
    ) -> None:
        if "vpinplay" not in self.config:
            self.config["vpinplay"] = {}
        if api_url is not None:
            self.config["vpinplay"]["api_url"] = normalize_api_url(api_url)
        if user_id is not None:
            self.config["vpinplay"]["user_id"] = user_id
        if initials is not None:
            self.config["vpinplay"]["initials"] = initials
        if machine_id is not None:
            self.config["vpinplay"]["machine_id"] = machine_id
        try:
            parent = os.path.dirname(os.path.abspath(os.path.expanduser(self.config_path)))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as fh:
                self.config.write(fh)
        except Exception as exc:
            logger.warning(f"VPinPlay: could not save config values: {exc}")

    def _apply_vpinfe_config_if_available(self) -> None:
        imported = load_vpinfe_vpinplay_config()
        if not imported:
            self.api_url = normalize_api_url(self.api_url)
            return

        changed = False
        api_url = imported.get("api_url") or self.api_url
        user_id = imported.get("user_id") or self.user_id
        initials = imported.get("initials") or self.initials
        machine_id = imported.get("machine_id") or self.machine_id

        if api_url and normalize_api_url(api_url) != normalize_api_url(self.api_url):
            self.api_url = normalize_api_url(api_url)
            changed = True
        else:
            self.api_url = normalize_api_url(self.api_url)
        if user_id and user_id != self.user_id:
            self.user_id = user_id
            changed = True
        if initials and initials != self.initials:
            self.initials = initials
            changed = True
        if machine_id and machine_id != self.machine_id:
            self.machine_id = machine_id
            changed = True

        if changed:
            self._save_config_values(
                api_url=self.api_url,
                user_id=self.user_id,
                initials=self.initials,
                machine_id=self.machine_id,
            )
            logger.info("VPinPlay: imported identity from VPinFE config")

    def _url(self, path: str) -> str:
        return self.api_url.rstrip("/") + "/" + path.lstrip("/")

    def status(self) -> dict:
        resp = requests.get(self._url("/api/v1/vpsdb/status"), timeout=5)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def file_hash(path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _clean_table_name(vpx_file: str) -> str:
        stem = os.path.splitext(os.path.basename(vpx_file or ""))[0]
        stem = re.sub(r"[_\-]+", " ", stem)
        stem = re.sub(r"\bv(?:er(?:sion)?)?\s*\d+(\.\d+)*\b", " ", stem, flags=re.I)
        stem = re.sub(
            r"\b(4k|8k|vr|pup|pov|mod|fs|fss|desktop|dt|vpw|vpxw|hybrid)\b",
            " ",
            stem,
            flags=re.I,
        )
        stem = re.sub(r"\s+", " ", stem).strip()
        return stem

    @staticmethod
    def _without_parentheticals(name: str) -> str:
        return re.sub(r"\([^)]*\)|\[[^]]*\]", " ", name).strip()

    def _find_vpx_path(self, vpx_file: str) -> str:
        target = os.path.basename(vpx_file or "").strip()
        if not target:
            return ""
        if os.path.isabs(vpx_file) and os.path.exists(vpx_file):
            return vpx_file

        base_dir = self.config.get("nvram", "base_dir", fallback="").strip()
        if not base_dir:
            return ""
        root = os.path.expanduser(base_dir)
        if not os.path.isdir(root):
            return ""

        matches = []
        target_lower = target.lower()
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in filenames:
                if filename.lower() == target_lower:
                    matches.append(os.path.join(dirpath, filename))
            if len(matches) > 1:
                break
        return matches[0] if len(matches) == 1 else ""

    @staticmethod
    def _tokens(value: str) -> set:
        return {
            tok
            for tok in re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower()).split()
            if len(tok) >= 3
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = requests.get(self._url(path), params=params or {}, timeout=8)
        resp.raise_for_status()
        return resp.json()

    def resolve_vps_id(self, rom: str, vpx_file: str = "", vpx_path: str = "", filehash: str = "") -> Dict[str, str]:
        if filehash:
            data = self._get(f"/api/v1/tables/by-filehash/{filehash}")
            vps_id = str(data.get("vpsId") or "").strip()
            if vps_id:
                return {"vpsId": vps_id, "method": "filehash", "filehash": filehash}

        clean_name = self._clean_table_name(vpx_file)
        search_terms: List[str] = []
        for term in (clean_name, self._without_parentheticals(clean_name)):
            term = re.sub(r"\s+", " ", term).strip()
            if term and term not in search_terms:
                search_terms.append(term)

        for term in search_terms:
            data = self._get(
                "/api/v1/tables-plus/search",
                {"search_key": "name", "search_term": term, "limit": 5},
            )
            items = data.get("items") or []
            match = self._best_name_match(term, items)
            if match:
                return {"vpsId": match["vpsId"], "method": "tables-plus", "filehash": filehash}

        for term in search_terms:
            data = self._get("/api/v1/vpsdb/search", {"q": term, "limit": 10})
            items = data.get("items") or []
            match = self._best_name_match(term, items)
            if match:
                return {"vpsId": match["vpsId"], "method": "vpsdb-search", "filehash": filehash}

        if rom:
            data = self._get(f"/api/v1/tables/by-rom/{rom}", {"limit": 5})
            items = data.get("items") or []
            if len(items) == 1 and items[0].get("vpsId"):
                return {"vpsId": items[0]["vpsId"], "method": "rom", "filehash": filehash}

        return {"vpsId": "", "method": "", "filehash": filehash}

    def _best_name_match(self, search_term: str, items: List[dict]) -> Optional[dict]:
        if not items:
            return None
        if len(items) == 1 and items[0].get("vpsId"):
            return items[0]

        needle = self._tokens(search_term)
        scored = []
        for item in items:
            name = str(item.get("name") or "")
            hay = self._tokens(name)
            if not hay or not needle:
                continue
            overlap = len(needle & hay)
            coverage = overlap / max(1, len(needle))
            if coverage >= 0.75:
                scored.append((coverage, overlap, item))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
            return scored[0][2]
        return None

    def submit_score_snapshot(self, rom: str, score: int, vpx_file: str = "", vpx_path: str = "") -> dict:
        if not self.is_ready():
            raise ValueError("VPinPlay is not configured.")

        if not vpx_path:
            vpx_path = self._find_vpx_path(vpx_file)

        filehash = ""
        if vpx_path and os.path.exists(vpx_path):
            try:
                filehash = self.file_hash(vpx_path)
            except Exception as exc:
                logger.warning(f"VPinPlay: could not hash VPX file {vpx_path!r}: {exc}")

        resolved = self.resolve_vps_id(rom=rom, vpx_file=vpx_file, vpx_path=vpx_path, filehash=filehash)
        vps_id = resolved.get("vpsId", "")
        if not vps_id:
            raise ValueError(f"Could not resolve VPinPlay VPS ID for {vpx_file or rom}.")

        now = datetime.now(timezone.utc).isoformat()
        filename = os.path.basename(vpx_path or vpx_file or f"{rom}.vpx")
        payload = {
            "source": {"program": "VPin Score Tracker", "programVersion": CLIENT_VERSION},
            "client": {
                "userId": self.user_id,
                "initials": self.initials,
                "machineId": self.machine_id,
            },
            "sentAt": now,
            "tables": [
                {
                    "info": {"vpsId": vps_id, "rom": rom},
                    "user": {
                        "rating": None,
                        "lastRun": now,
                        "startCount": 1,
                        "runTime": 0,
                        "Score": {
                            "rom": rom,
                            "score_type": "FINAL SCORE",
                            "value": int(score),
                        },
                    },
                    "vpxFile": {
                        "filename": filename,
                        "filehash": filehash,
                        "version": "",
                        "releaseDate": "",
                        "saveDate": "",
                        "saveRev": "",
                        "manufacturer": "",
                        "year": "",
                        "type": "",
                        "vbsHash": "",
                        "rom": rom,
                        "detectNfozzy": False,
                        "detectFleep": False,
                        "detectSSF": False,
                        "detectLUT": False,
                        "detectScorebit": False,
                        "detectFastflips": False,
                        "detectFlex": False,
                    },
                    "vpinfe": {"alttitle": None, "altvpsid": None},
                }
            ],
        }

        resp = requests.post(self._url("/api/v1/sync"), json=payload, timeout=10)
        try:
            resp.raise_for_status()
        except HTTPError as exc:
            detail = ""
            try:
                data = resp.json()
                detail = str(data.get("detail") or data)
            except Exception:
                detail = resp.text
            detail = detail.strip()
            if len(detail) > 500:
                detail = detail[:497] + "..."
            raise ValueError(f"VPinPlay rejected the score ({resp.status_code}): {detail}") from exc
        data = resp.json()
        success = data.get("status") == "ok"
        message = f"VPinPlay sync via {resolved.get('method') or 'unknown'}"
        if not success:
            message = self._sync_error_message(data, table_count=len(payload["tables"]))
        return {
            "success": success,
            "message": message,
            "response": data,
            "vpsId": vps_id,
        }

    @staticmethod
    def _sync_error_message(data: dict, table_count: int) -> str:
        summary = data.get("summary") if isinstance(data, dict) else {}
        if not isinstance(summary, dict):
            summary = {}

        errors = int(summary.get("errors") or 0)
        received = int(summary.get("tablesReceived") or 0)
        if errors and received == 0 and table_count:
            return (
                "VPinPlay rejected this client identity. The user ID is likely "
                "already registered in VPinPlay with a different machine ID."
            )
        if errors:
            return f"VPinPlay sync finished with {errors} table error(s)."
        return "VPinPlay sync failed."
