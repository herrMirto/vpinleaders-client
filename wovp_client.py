import configparser
import logging
import os
import re
import requests
import tempfile
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CLIENT_VERSION = "1.0"

# Playing platform identifier for Visual Pinball X
PLATFORM_VPX = 0


def _preview(value, limit: int = 800) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


class WovpClient:
    BASE_URL = "https://worldofvirtualpinball.com/api/whsc/v1"
    VALIDATE_URL = f"{BASE_URL}/validate-apikey"
    CHALLENGES_URL = f"{BASE_URL}/challenges/search"
    SCORE_PHOTO_URL = f"{BASE_URL}/scores/submit-photo"
    SCORE_SUBMIT_URL = f"{BASE_URL}/scores/submit"

    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
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
        logger.info(
            "WOVP: submitting score payload "
            f"challengeId={challenge_id} photoTempId={photo_temp_id} "
            f"score={score} playingPlatform={playing_platform} rom={rom!r} vpx_file={vpx_file!r}"
        )

        post_headers = self.headers.copy()
        post_headers["Content-Type"] = "application/json"

        score_resp = requests.post(
            self.SCORE_SUBMIT_URL,
            headers=post_headers,
            json=payload,
        )
        try:
            score_json = score_resp.json()
        except ValueError:
            score_json = None
        logger.info(
            f"WOVP /scores/submit → HTTP {score_resp.status_code}; "
            f"response={_preview(score_json if score_json is not None else score_resp.text)}"
        )

        if score_resp.status_code != 200:
            raise Exception(
                f"WOVP Submit failed with code {score_resp.status_code}: {score_resp.text}"
            )

        if isinstance(score_json, dict):
            data = score_json.get("data")
            errors = score_json.get("errors")
            if isinstance(data, dict):
                errors = errors or data.get("errors")
            success = score_json.get("success")
            if success is False or errors:
                raise Exception(f"WOVP Submit returned errors: {_preview(errors or score_json)}")

        duration = int((time.time() - start_time) * 1000)
        logger.info(f"WOVP: Score of {score} submitted successfully. Took {duration}ms.")

        return score_json if score_json is not None else {"raw": score_resp.text}

    # ─────────────────────────────────────────────────────────────────────────
    # Challenge Management & State
    # ─────────────────────────────────────────────────────────────────────────

    def get_selected_challenge(self) -> Tuple[str, str]:
        """
        Returns the currently selected challenge (id, name).
        Returns ('', '') if none is selected.
        """
        try:
            challenge_id = self.config.get("wovp", "selected_challenge_id", fallback="").strip()
            challenge_name = self.config.get("wovp", "selected_challenge_name", fallback="").strip()
            return (challenge_id, challenge_name)
        except Exception:
            return ("", "")

    def set_selected_challenge(self, challenge_id: str, challenge_name: str):
        """
        Persists the selected WOVP challenge to config.ini.
        """
        if "wovp" not in self.config:
            self.config["wovp"] = {}
        self.config["wovp"]["selected_challenge_id"] = challenge_id
        self.config["wovp"]["selected_challenge_name"] = challenge_name

        try:
            with open(self.config_path, "w") as f:
                self.config.write(f)
            logger.info(f"WOVP: Saved selected challenge {challenge_id} ({challenge_name})")
        except Exception as e:
            logger.error(f"WOVP: Failed to save challenge selection: {e}")

    def is_ready(self) -> bool:
        """
        True when WoVP is fully configured: api_key is present AND a challenge is selected.
        """
        challenge_id, _ = self.get_selected_challenge()
        return bool(self.api_key) and bool(challenge_id)

    @staticmethod
    def table_matches_challenge(vpx_file: str, challenge_name: str) -> bool:
        """
        Returns True when the VPX filename and the WoVP challenge name share at least
        one meaningful word, indicating the player is on the correct table.

        Normalisation applied to both sides before comparison:
          - Strip .vpx extension
          - Remove version tokens (v1, v1.17, 1.17, etc.)
          - Lowercase, strip punctuation
          - Discard words shorter than 3 characters (articles, 'of', 'a', …)

        Returns True (allow) when either string is empty or the check cannot be
        performed — better to submit than to silently block on bad data.
        """
        if not vpx_file or not challenge_name:
            return True

        def _words(s: str) -> set:
            s = os.path.splitext(s)[0]                         # drop .vpx
            s = re.sub(r'\bv?\d+[\d.]*\b', ' ', s, flags=re.I)  # drop versions
            s = re.sub(r'[^a-z0-9 ]', ' ', s.lower())          # alphanum only
            return {w for w in s.split() if len(w) >= 3}

        vpx_words = _words(vpx_file)
        challenge_words = _words(challenge_name)

        if not vpx_words or not challenge_words:
            return True   # nothing useful to compare — allow

        return bool(vpx_words & challenge_words)

    def submit_score_with_screenshot(
        self,
        screenshot_image,  # PIL Image object
        score: int,
        rom: str,
        vpx_file: str = "",
        jpeg_quality: int = 75,
    ) -> dict:
        """
        Convenience method: takes a PIL Image, saves it as a temp JPEG, and submits.
        Handles cleanup automatically.

        Args:
            screenshot_image: PIL Image object to submit
            score: The numeric score
            rom: ROM name (e.g. "sman_261")
            vpx_file: VPX filename (optional)
            jpeg_quality: JPEG compression quality (1-100)

        Returns: Response dict from submit()
        Raises: Exception on failure
        """
        if not screenshot_image:
            raise ValueError("Screenshot image required")

        challenge_id, challenge_name = self.get_selected_challenge()
        if not challenge_id:
            raise ValueError("No challenge selected")

        # Convert to RGB if necessary and save temp JPEG
        wovp_sc = (
            screenshot_image.convert("RGB")
            if hasattr(screenshot_image, "mode") and screenshot_image.mode == "RGBA"
            else screenshot_image
        )

        fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as f:
                wovp_sc.save(f, format="JPEG", quality=jpeg_quality, optimize=True)

            return self.submit(
                screenshot_path=tmp_path,
                score=score,
                rom=rom,
                challenge_id=challenge_id,
                vpx_file=vpx_file,
                playing_platform=PLATFORM_VPX,
            )
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
