import configparser
import json
import logging
import os
import re
import requests
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, quote, unquote

logger = logging.getLogger(__name__)


class IScoredClient:
    """
    Client for iScored.info game rooms.

    Uses the current iScored API rooted at /api/{gameroom}. iScored gamerooms
    are addressed by username, so [iscored].player_name is also the gameroom.

    Backwards compatibility:
      - Legacy room URL/gameroom keys are honored only when player_name is
        missing.
      - Legacy 'user' is used as the player name when 'player_name' is missing.
    """

    CACHE_FILENAME = "iscored_games.json"

    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self.config = configparser.ConfigParser()
        self.config.read(config_path)

        self.player_name = self._cfg_get("player_name").strip()
        self.selected_gameroom = self._cfg_get("selected_gameroom").strip()
        self.selected_game_id = self._cfg_get("selected_game_id").strip()
        self.selected_game_name = self._cfg_get("selected_game_name").strip()
        configured_user = self._cfg_get("user").strip()
        if not self.player_name:
            # Legacy fallback: older configs used 'user' for the submitting player.
            self.player_name = configured_user

        # iScored gameroom is the username: /api/{username}. Keep legacy room
        # keys only as a fallback for older configs that have no player_name.
        if self.player_name:
            self.room_urls: List[str] = [self.player_name]
        else:
            urls_raw = self._cfg_get("room_urls").strip() or self._cfg_get("room_url").strip()
            gamerooms_raw = (
                urls_raw
                or self._cfg_get("gamerooms").strip()
                or self._cfg_get("gameroom").strip()
                or self._cfg_get("gameroom_name").strip()
                or configured_user
            )
            self.room_urls = [
                name.strip() for name in re.split(r"[,\n]+", gamerooms_raw) if name.strip()
            ]

        # Cache of loaded rooms: url -> room dict (in-memory only, this run).
        self._room_cache: Dict[str, dict] = {}

    # -------------------------------------------------------- on-disk cache

    @property
    def cache_path(self) -> str:
        """Path to the persistent games cache (JSON), kept next to config.ini."""
        return os.path.join(os.path.dirname(os.path.abspath(self.config_path)), self.CACHE_FILENAME)

    def load_games_cache(self) -> List[Dict]:
        """Loads the persisted games list from disk. Returns [] when missing/corrupt."""
        path = self.cache_path
        if not os.path.exists(path):
            logger.info(f"iScored cache: no file at {path}")
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            games = data.get("games") or []
            logger.info(f"iScored cache: loaded {len(games)} game(s) from {path}")
            return games
        except Exception as e:
            logger.warning(f"iScored cache: failed to read {path}: {e}")
            return []

    def save_games_cache(self, games: List[Dict]) -> None:
        """Persists the games list to disk for the next run to pick up."""
        path = self.cache_path
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"games": games}, f, indent=2)
            logger.info(f"iScored cache: saved {len(games)} game(s) to {path}")
        except Exception as e:
            logger.warning(f"iScored cache: failed to write {path}: {e}")

    # ---------------------------------------------------------------- helpers

    def _cfg_get(self, key: str, fallback: str = "") -> str:
        try:
            return self.config.get("iscored", key, fallback=fallback)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    def _split_url(self, url: str) -> Tuple[str, Dict[str, str]]:
        """
        Parses an iScored username or legacy room URL into (base_url, base_params).

        For the current API the normal input is the username. Legacy room URLs
        are still accepted; the gameroom name is the first path component for
        normal room URLs, the second path component for /api/{gameroom} URLs,
        or the 'user' query parameter for room.php/public links.
        """
        if not url:
            raise ValueError("iScored room URL is empty")

        parsed = urlparse(url)
        if parsed.scheme or parsed.netloc:
            if not parsed.scheme or not parsed.netloc:
                raise ValueError(f"iScored room URL is malformed: {url!r}")
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            base_url = "https://www.iscored.info"
            parsed = urlparse(f"{base_url}/{quote(url.strip(), safe='')}")
        params: Dict[str, str] = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if "user" not in params or not params["user"]:
            parts = [unquote(p) for p in parsed.path.split("/") if p]
            path_user = ""
            if parts:
                path_user = parts[1] if parts[0].lower() == "api" and len(parts) > 1 else parts[0]
                if path_user.lower().endswith(".php"):
                    path_user = ""
            if path_user:
                params["user"] = path_user
            elif self.player_name:
                params["user"] = self.player_name

        if "user" not in params or not params["user"]:
            raise ValueError(
                f"iScored URL has no user component (set [iscored].player_name or include the user in the URL): {url}"
            )

        return base_url, params

    def _api_url(self, room: dict, *parts: object) -> str:
        encoded_parts = [quote(str(p).strip(), safe="") for p in parts if str(p).strip()]
        return "/".join([f"{room['base_url']}/api", *encoded_parts])

    @staticmethod
    def _json_or_raise(resp: requests.Response):
        try:
            return resp.json()
        except ValueError:
            preview = (resp.text or "")[:200].strip()
            raise ValueError(f"iScored returned non-JSON response: {preview}")

    @staticmethod
    def _first_present(data: dict, keys: Tuple[str, ...], fallback=None):
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
        return fallback

    @staticmethod
    def _as_bool(value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            value = value.strip().lower()
            if not value:
                return False
            return value in {"1", "true", "yes", "y", "on", "locked", "hidden"}
        return bool(value)

    # ------------------------------------------------------------- readiness

    def is_ready(self) -> bool:
        """True when player_name is configured. The gameroom is derived from it."""
        return bool(self.player_name)

    def get_selected_game(self) -> Tuple[str, str, str]:
        return self.selected_gameroom, self.selected_game_id, self.selected_game_name

    def set_selected_game(self, gameroom: str, game_id: str, game_name: str = "") -> None:
        if "iscored" not in self.config:
            self.config["iscored"] = {}
        self.config["iscored"]["selected_gameroom"] = str(gameroom or "").strip()
        self.config["iscored"]["selected_game_id"] = str(game_id or "").strip()
        self.config["iscored"]["selected_game_name"] = str(game_name or "").strip()
        with open(self.config_path, "w", encoding="utf-8") as fh:
            self.config.write(fh)
        self.selected_gameroom = self.config["iscored"]["selected_gameroom"]
        self.selected_game_id = self.config["iscored"]["selected_game_id"]
        self.selected_game_name = self.config["iscored"]["selected_game_name"]

    def list_all_games(self, request_timeout: int = 5) -> List[Dict]:
        """
        Returns a flat list of every game in the derived iScored gameroom,
        sorted by game name. Each entry includes:
          {id, name, room_name, room_url, isGameLocked, hidden}

        Network errors for individual rooms are logged and skipped — they don't
        break the rest of the list.
        """
        flat: List[Dict] = []
        for url in self.room_urls:
            try:
                room = self.load_game_room(url, request_timeout=request_timeout)
            except Exception as e:
                logger.warning(f"iScored: failed to load room {url}: {e}")
                continue
            room_name = room.get("roomName") or url
            for g in room.get("games") or []:
                flat.append({
                    "id": g.get("id"),
                    "name": g.get("name") or "",
                    "room_name": room_name,
                    "room_url": url,
                    "gameroom": room.get("gameroom") or url,
                    "isGameLocked": bool(g.get("isGameLocked")),
                    "hidden": bool(g.get("hidden")),
                })
        flat.sort(key=lambda g: (g.get("name") or "").lower())
        return flat

    def _fetch_room_metadata(self, url: str, request_timeout: int = 10) -> dict:
        """Fetches gameroom metadata and raw game list from the current /api/ endpoint."""
        base_url, base_params = self._split_url(url)
        gameroom = base_params["user"]

        room_stub = {"base_url": base_url}
        full_url = self._api_url(room_stub, gameroom)
        logger.info(f"iScored: GET {full_url} (gameroom info, timeout={request_timeout}s)")
        resp = requests.get(full_url, timeout=request_timeout)
        logger.info(f"iScored: ← HTTP {resp.status_code} ({len(resp.content)} bytes) from gameroom info")
        resp.raise_for_status()
        info = self._json_or_raise(resp) or []
        if isinstance(info, list):
            settings = {}
            room_id = None
            raw_games = info
        elif isinstance(info, dict):
            settings = info.get("settings") or info.get("roomSettings") or {}
            room_id = self._first_present(info, ("roomID", "roomId", "id"))
            raw_games = (
                info.get("games")
                or info.get("Games")
                or info.get("gameList")
                or info.get("allGames")
                or []
            )
        else:
            raise ValueError(f"iScored gameroom info has unexpected shape: {type(info).__name__}")

        room_name = (
            settings.get("roomName")
            or settings.get("gameroomName")
            or (info.get("roomName") if isinstance(info, dict) else None)
            or (info.get("gameroomName") if isinstance(info, dict) else None)
            or gameroom
        )
        logger.info(
            f"iScored: room loaded — id={room_id} name={room_name!r} gameroom={gameroom!r}"
        )
        return {
            "url": url,
            "base_url": base_url,
            "base_params": base_params,
            "gameroom": gameroom,
            "roomID": room_id,
            "roomName": room_name,
            "settings": settings,
            "raw_info": info,
            "raw_games": raw_games,
        }

    def load_game_room(self, url: str, force_reload: bool = False, request_timeout: int = 10) -> dict:
        """
        Fully loads a game room (metadata + games + scores) and caches it.
        """
        if not force_reload and url in self._room_cache:
            logger.debug(f"iScored: cache hit for {url}")
            return self._room_cache[url]

        logger.info(f"iScored: loading room {url} (force_reload={force_reload})")
        room = self._fetch_room_metadata(url, request_timeout=request_timeout)
        room["games"] = self._fetch_games(room, request_timeout=request_timeout)
        self._room_cache[url] = room
        logger.info(
            f"iScored: room {room.get('roomName')!r} fully loaded — {len(room['games'])} game(s)"
        )
        return room

    def _fetch_games(self, room: dict, request_timeout: int = 10) -> List[dict]:
        raw_info = room.get("raw_info") or {}
        games_data = room.get("raw_games") or []
        if not games_data and isinstance(raw_info, dict):
            games_data = (
                raw_info.get("games")
                or raw_info.get("Games")
                or raw_info.get("gameList")
                or raw_info.get("allGames")
                or []
            )
        if isinstance(games_data, dict):
            games_data = list(games_data.values())
        if not isinstance(games_data, list):
            logger.warning(f"iScored: unexpected games list shape: {type(games_data).__name__}")
            return []

        logger.info(f"iScored: games index returned {len(games_data)} game(s)")
        scores_data: list = []
        try:
            scores_url = self._api_url(room, room["gameroom"], "getAllScores")
            logger.info(f"iScored: GET {scores_url}")
            resp_scores = requests.get(scores_url, timeout=request_timeout)
            logger.info(f"iScored: ← HTTP {resp_scores.status_code} ({len(resp_scores.content)} bytes) from getAllScores")
            if resp_scores.ok:
                raw_scores = self._json_or_raise(resp_scores) or []
                if isinstance(raw_scores, dict):
                    scores_data = (
                        raw_scores.get("games")
                        or raw_scores.get("scores")
                        or raw_scores.get("data")
                        or []
                    )
                elif isinstance(raw_scores, list):
                    scores_data = raw_scores
        except Exception as e:
            logger.warning(f"iScored: failed to fetch scores list: {e}")

        result: List[dict] = []
        for g in games_data:
            if not isinstance(g, dict):
                continue
            gid = self._first_present(g, ("gameID", "GameID", "id", "ID"))
            game_name = self._first_present(g, ("gameName", "GameName", "name", "Name"), "")
            game_scores: list = []
            for s in scores_data:
                if not isinstance(s, dict):
                    continue
                score_gid = self._first_present(s, ("gameID", "GameID", "id", "ID"))
                score_name = self._first_present(s, ("gameName", "GameName", "name", "Name"), "")
                if str(score_gid) == str(gid) or (game_name and score_name == game_name):
                    nested_scores = s.get("scores")
                    game_scores = nested_scores if isinstance(nested_scores, list) else []
                    break
            result.append({
                "id": gid,
                "name": game_name or "",
                "scores": game_scores,
                "isSingleScore": self._as_bool(self._first_present(g, ("singleScore", "isSingleScore"), False)),
                "isMultiScore": self._as_bool(self._first_present(g, ("multiScore", "isMultiScore"), True), default=True),
                "isGameLocked": self._as_bool(self._first_present(g, ("gameLocked", "isGameLocked", "locked", "Locked"), False)),
                "hidden": self._as_bool(self._first_present(g, ("hidden", "Hidden"), False)),
            })
        return result

    # --------------------------------------------------------------- submit

    @staticmethod
    def _normalize_name(s: str) -> str:
        """
        Normalises a game/file name for fuzzy comparison:
          - strip the .vpx extension
          - strip parenthesised suffixes like "(Bally 1995)" or "(MoD)"
          - split camelCase / PascalCase ("AttackFromMars" -> "Attack From Mars")
          - strip version tokens (v1, 1.17, 1.0.4, …)
          - lowercase, keep only alphanumerics, collapse whitespace
        """
        if not s:
            return ""
        s = re.sub(r"\.vpx\b", " ", s, flags=re.I)
        s = re.sub(r"\([^)]*\)", " ", s)               # strip "(Bally 1995)"
        s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)     # camelCase split
        s = re.sub(r"\bv?\d+(\.\d+)*\b", " ", s, flags=re.I)  # strip version tokens
        s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
        s = re.sub(r"\s+", " ", s).strip()
        return s

    _STOP_WORDS = frozenset({"the", "a", "an", "of", "and"})

    def _find_game(self, room: dict, search_name: str) -> Optional[dict]:
        games = room.get("games") or []
        if not games or not search_name:
            return None

        target = self._normalize_name(search_name)
        if not target:
            return None

        # 1) exact normalised match
        for g in games:
            if self._normalize_name(g.get("name") or "") == target:
                return g

        # 2) substring containment (either direction)
        for g in games:
            name = self._normalize_name(g.get("name") or "")
            if name and (target in name or name in target):
                return g

        # 3) token-overlap heuristic — match if the meaningful words overlap.
        target_tokens = set(target.split()) - self._STOP_WORDS
        if not target_tokens:
            return None

        best_game = None
        best_score = 0
        for g in games:
            name_tokens = set(self._normalize_name(g.get("name") or "").split()) - self._STOP_WORDS
            if not name_tokens:
                continue
            common = target_tokens & name_tokens
            score = len(common)
            # require at least half the smaller side to overlap, and at least one token
            threshold = max(1, min(len(target_tokens), len(name_tokens)) // 2)
            if score >= threshold and score > best_score:
                best_score = score
                best_game = g
        return best_game

    def _room_for_gameroom(self, gameroom: str) -> Optional[dict]:
        target = str(gameroom or "").strip()
        if not target:
            return None
        for url in self.room_urls:
            try:
                room = self.load_game_room(url)
            except Exception as e:
                logger.warning(f"iScored: failed to load room {url}: {e}")
                continue
            if str(room.get("gameroom") or "") == target:
                return room
        return None

    def submit_score(self, score: int, rom: str, vpx_file: str = "") -> dict:
        """
        Iterates every configured iScored room, finds the game that matches the
        VPX filename / ROM, and submits to whichever room owns it.

        Returns {success: bool, message: str}. Never raises for ordinary outcomes
        (locked game, room misconfigured, score lower than existing).
        """
        if not self.player_name:
            return {"success": False, "message": "iScored player_name is not configured."}
        if not self.room_urls:
            return {"success": False, "message": "iScored gameroom could not be derived from player_name."}

        match_room: Optional[dict] = None
        match_game: Optional[dict] = None

        if self.selected_gameroom and self.selected_game_id:
            match_room = self._room_for_gameroom(self.selected_gameroom)
            if match_room is None:
                return {
                    "success": False,
                    "message": f"Selected iScored gameroom '{self.selected_gameroom}' could not be loaded.",
                }
            for game in match_room.get("games") or []:
                if str(game.get("id") or "") == str(self.selected_game_id):
                    match_game = game
                    break
            if match_game is None:
                return {
                    "success": False,
                    "message": f"Selected iScored game ID '{self.selected_game_id}' was not found.",
                }
        else:
            # iScored uses friendly game names; the VPX filename usually matches them
            # better than the ROM short name does. Keep this as a fallback when no
            # explicit tray selection has been saved.
            search_name = vpx_file.replace(".vpx", "") if vpx_file else rom
            for url in self.room_urls:
                try:
                    room = self.load_game_room(url)
                except Exception as e:
                    logger.warning(f"iScored: failed to load room {url}: {e}")
                    continue
                game = self._find_game(room, search_name)
                if game is not None:
                    match_room = room
                    match_game = game
                    break

            if match_game is None or match_room is None:
                return {
                    "success": False,
                    "message": f"Game '{search_name}' not found in any configured iScored room.",
                }

        if match_game.get("isGameLocked"):
            return {"success": True, "message": f"iScored: submission is locked for '{match_game['name']}'."}

        # Skip when the player already has a higher score and the room rules forbid replacing it.
        for existing in match_game.get("scores", []) or []:
            if existing.get("name") == self.player_name:
                if match_game.get("isSingleScore"):
                    return {"success": True, "message": "Single-score mode is enabled; skipping."}
                if not match_game.get("isMultiScore"):
                    try:
                        existing_score = int(existing.get("score", 0))
                        if existing_score > score:
                            return {
                                "success": True,
                                "message": "Existing score is higher; skipping.",
                            }
                    except (ValueError, TypeError):
                        pass

        room_name = match_room.get("roomName") or ""

        logger.info(
            f"iScored: submitting '{self.player_name}' to game '{match_game['name']}' "
            f"(ID {match_game.get('id')}) in room '{room_name}'"
        )

        game_ref = match_game.get("id") or match_game.get("name")
        post_url = self._api_url(match_room, match_room["gameroom"], game_ref, "submitScore")
        params = {"playerName": self.player_name, "score": score}
        logger.info(
            f"iScored: POST {post_url}?playerName={self.player_name!r}&score=<hidden>"
        )
        resp = requests.post(post_url, data=params, timeout=10)
        logger.info(f"iScored: ← HTTP {resp.status_code} ({len(resp.content)} bytes) from submitScore")
        message = f"iScored returned HTTP {resp.status_code}"
        try:
            data = self._json_or_raise(resp)
            if isinstance(data, dict):
                if data.get("error") or data.get("message"):
                    message = str(data.get("error") or data.get("message"))
                elif data.get("submittedScore"):
                    submitted = data["submittedScore"]
                    rank = submitted.get("rank") if isinstance(submitted, dict) else None
                    message = f"Submitted to {match_game.get('name')}" + (f" (rank {rank})" if rank else "")
        except Exception:
            if not resp.ok and resp.text:
                message = resp.text[:200]
        return {
            "success": resp.ok,
            "message": message,
        }
