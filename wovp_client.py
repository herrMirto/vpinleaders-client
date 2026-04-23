import configparser
import logging
import requests
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CLIENT_VERSION = "1.0"

# Playing platform identifier for Visual Pinball X
PLATFORM_VPX = 0


class WovpClient:
    BASE_URL = "https://worldofvirtualpinball.com/api/whsc/v1"
    VALIDATE_URL = f"{BASE_URL}/validate-apikey"
    CHALLENGES_URL = f"{BASE_URL}/challenges/search"
    SCORE_PHOTO_URL = f"{BASE_URL}/scores/submit-photo"
    SCORE_SUBMIT_URL = f"{BASE_URL}/scores/submit"

    def __init__(self, config_path: str = "config.ini"):
        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        try:
            self.api_key = self.config.get("wovp", "api_key")
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.warning(f"WOVP configuration incomplete in config.ini: {e}")
            self.api_key = ""

        self.headers = {
            "X-Client-ID": "vpinleaders-client",
            "Authorization": f"Bearer {self.api_key}",
        }

    def validate_apikey(self) -> dict:
        """
        Validates the API key against the WHSC API.
        Returns user info dict on success, raises Exception on failure.

        Per the API spec, this endpoint uses only the X-Client-ID header
        (no Authorization bearer token) and sends the key in the request body.
        """
        resp = requests.post(
            self.VALIDATE_URL,
            headers={"X-Client-ID": "vpinleaders-client"},
            json={"apikey": self.api_key},
            timeout=10,
        )
        if resp.status_code != 200:
            raise Exception(
                f"API key validation failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()
        if not data.get("success"):
            raise Exception("API key is invalid or was rejected by the server.")
        return data

    def search_challenges(self) -> List[Dict[str, str]]:
        """
        Fetches active in-progress challenges from the WHSC API.
        Returns a list of dicts with 'id' and 'name' keys, sorted by name.
        """
        body = {
            "cultureCode": "en",
            "filters": {
                "statuses": [1],   # 1 = Active
                "inProgress": True,
            },
            "expands": [
                "challenge.pinballTable.minimum",
                "challenge.pinballTableVersion.minimum",
                "challenge.scriptMatchKeywords",
            ],
        }
        resp = requests.post(
            self.CHALLENGES_URL,
            headers=self.headers,
            json=body,
            timeout=15,
        )
        logger.info(f"WOVP /challenges/search → HTTP {resp.status_code}")

        if resp.status_code != 200:
            raise Exception(
                f"Challenges fetch failed ({resp.status_code}): {resp.text}"
            )

        raw = resp.json()

        # Log a compact preview of the raw response to aid debugging.
        raw_preview = str(raw)
        if len(raw_preview) > 500:
            raw_preview = raw_preview[:500] + "…"
        logger.info(f"WOVP challenges raw response: {raw_preview}")

        # The API may return a list at the top level or nest items under a key.
        # Try the most common shapes in order.
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = (
                raw.get("data")
                or raw.get("challenges")
                or raw.get("items")
                or raw.get("results")
                or []
            )
        else:
            items = []

        logger.info(f"WOVP: {len(items)} raw item(s) before parsing")

        result: List[Dict[str, str]] = []
        for item in items:
            # Each item may be the challenge object directly or wrapped under a
            # 'challenge' key depending on the API version.
            ch = item.get("challenge", item) if isinstance(item, dict) else {}

            ch_id = str(ch.get("id") or ch.get("challengeId") or "")
            if not ch_id:
                logger.warning(f"WOVP: skipping item with no id: {str(ch)[:120]}")
                continue

            # Prefer an explicit challenge name, fall back to the pinball table name.
            ch_name = (
                ch.get("name")
                or ch.get("title")
                or (ch.get("pinballTable") or {}).get("name")
                or ch_id
            )
            result.append({"id": ch_id, "name": str(ch_name)})

        result.sort(key=lambda x: x["name"].lower())
        logger.info(f"WOVP: {len(result)} challenge(s) parsed successfully")
        return result

    def submit(
        self,
        screenshot_path: str,
        score: int,
        rom: str,
        challenge_id: str,
        vpx_file: str = "",
        playing_platform: int = PLATFORM_VPX,
    ) -> dict:
        """
        Two-step score submission: upload proof photo, then register the score.

        Args:
            screenshot_path:  Path to the JPEG screenshot file.
            score:            The numeric score to submit.
            rom:              ROM name for the table (e.g. "sman_261").
            challenge_id:     The WHSC challenge UUID to submit the score to.
            vpx_file:         VPX filename (e.g. "Spider-Man.vpx"). Empty if unknown.
            playing_platform: Integer platform identifier (0 = VPX).
        """
        if not self.api_key:
            raise ValueError("WOVP api_key is not configured.")
        if not challenge_id:
            raise ValueError("No challenge selected. Please select a challenge from the menu.")

        start_time = time.time()

        # --- STEP 1: Upload Screenshot ---
        try:
            with open(screenshot_path, "rb") as file_stream:
                photo_resp = requests.post(
                    self.SCORE_PHOTO_URL,
                    headers=self.headers,
                    files={"file": file_stream},
                )
        except IOError as e:
            raise Exception(f"Error reading screenshot file {screenshot_path}: {e}")

        if photo_resp.status_code != 200:
            raise Exception(
                f"WOVP Upload failed with code {photo_resp.status_code}: {photo_resp.text}"
            )

        photo_data = photo_resp.json().get("data", {})
        if photo_data.get("errors"):
            raise Exception(f"WOVP returned errors for the image: {photo_data.get('errors')}")

        photo_temp_id = photo_data.get("photoTempId")
        if not photo_temp_id:
            raise Exception("WOVP did not return photoTempId after image upload.")

        # --- STEP 2: Submit Score ---
        payload = {
            "challengeId": challenge_id,
            "photoTempId": photo_temp_id,
            "score": score,
            "playingPlatform": playing_platform,
            "metadata": {
                "vpinleaders-client-version": CLIENT_VERSION,
                "vpx-file": vpx_file,
                "rom": rom,
            },
        }

        post_headers = self.headers.copy()
        post_headers["Content-Type"] = "application/json"

        score_resp = requests.post(
            self.SCORE_SUBMIT_URL,
            headers=post_headers,
            json=payload,
        )

        if score_resp.status_code != 200:
            raise Exception(
                f"WOVP Submit failed with code {score_resp.status_code}: {score_resp.text}"
            )

        duration = int((time.time() - start_time) * 1000)
        logger.info(f"WOVP: Score of {score} submitted successfully. Took {duration}ms.")

        return score_resp.json()
