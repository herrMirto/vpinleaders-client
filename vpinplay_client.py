import configparser
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://localhost:8888"
CLIENT_VERSION = "1.0"


def generate_machine_id() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


class VPinPlayClient:
    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        self.api_url = self.config.get("vpinplay", "api_url", fallback=DEFAULT_API_URL).strip() or DEFAULT_API_URL
        self.user_id = self.config.get("vpinplay", "user_id", fallback="").strip()
        self.initials = self.config.get("vpinplay", "initials", fallback="").strip()
        self.machine_id = self.config.get("vpinplay", "machine_id", fallback="").strip()

    def is_ready(self) -> bool:
        return bool(self.api_url and self.user_id and self.initials and len(self.machine_id) == 64)

    def ensure_machine_id(self) -> str:
        if len(self.machine_id) == 64:
            return self.machine_id
        self.machine_id = generate_machine_id()
        if "vpinplay" not in self.config:
            self.config["vpinplay"] = {}
        self.config["vpinplay"]["machine_id"] = self.machine_id
        with open(self.config_path, "w", encoding="utf-8") as fh:
            self.config.write(fh)
        return self.machine_id

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
        stem = re.sub(r"\b(v|ver|version)?\d+(\.\d+)*\b", " ", stem, flags=re.I)
        stem = re.sub(r"\b(4k|8k|vr|pup|pov|mod|fs|fss|desktop|dt)\b", " ", stem, flags=re.I)
        stem = re.sub(r"\s+", " ", stem).strip()
        return stem

    @staticmethod
    def _without_parentheticals(name: str) -> str:
        return re.sub(r"\([^)]*\)|\[[^]]*\]", " ", name).strip()

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
            "source": {"program": "VPinLeaders Client", "programVersion": CLIENT_VERSION},
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
        resp.raise_for_status()
        data = resp.json()
        return {
            "success": data.get("status") == "ok",
            "message": f"VPinPlay sync via {resolved.get('method') or 'unknown'}",
            "response": data,
            "vpsId": vps_id,
        }
