"""Dota 2 Game State Integration HTTP server."""

import logging
import threading
from typing import Callable

from flask import Flask, request

from match_finder import MatchFinder

logger = logging.getLogger(__name__)

# Game states that indicate a real match is starting / in progress
ACTIVE_STATES = {
    "DOTA_GAMERULES_STATE_HERO_SELECTION",
    "DOTA_GAMERULES_STATE_STRATEGY_TIME",
    "DOTA_GAMERULES_STATE_PRE_GAME",
    "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
}

AUTH_TOKEN = "dota2analyzer_token"


class GSIServer:
    """Listens for Dota 2 GSI payloads and fires on_match_found when a new
    match is detected and all 10 player Steam IDs are available."""

    def __init__(self, port: int = 4000,
                 on_match_found: Callable | None = None,
                 match_finder: MatchFinder | None = None):
        self.port = port
        self.on_match_found = on_match_found
        self.match_finder = match_finder

        self._current_match_id: str | None = None
        self._analyzing = False
        self._lock = threading.Lock()

        self.app = Flask(__name__)
        # Silence Flask's per-request logs
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        self.app.add_url_rule("/", "gsi", self._handle, methods=["POST"])

    # ------------------------------------------------------------------ #

    def _handle(self):
        data = request.get_json(silent=True, force=True)
        if not data:
            return "OK", 200

        auth = data.get("auth", {})
        if auth.get("token") != AUTH_TOKEN:
            logger.warning("GSI: invalid auth token – ignoring payload.")
            return "Unauthorized", 401

        try:
            self._process(data)
        except Exception as e:
            logger.error(f"GSI processing error: {e}", exc_info=True)

        return "OK", 200

    def _process(self, data: dict):
        map_data  = data.get("map", {})
        game_state = map_data.get("game_state", "")
        match_id   = str(map_data.get("matchid", ""))

        if game_state not in ACTIVE_STATES or not match_id or match_id == "0":
            return

        with self._lock:
            if match_id == self._current_match_id or self._analyzing:
                return

            # Extract local player's Steam ID
            local_player = data.get("player", {})
            local_steam64 = str(local_player.get("steamid", ""))

            if not local_steam64 or local_steam64 == "0":
                logger.debug("GSI: no local player Steam ID yet – waiting.")
                return

            self._current_match_id = match_id
            self._analyzing = True

        logger.info(f"GSI: new match {match_id} – starting analysis.")
        thread = threading.Thread(
            target=self._run_analysis,
            args=(match_id, local_steam64),
            daemon=True,
        )
        thread.start()

    def _run_analysis(self, match_id: str, local_steam64: str):
        try:
            if self.match_finder:
                logger.info("Discovering players via MatchFinder…")
                teammates, enemies = self.match_finder.find_players(
                    match_id, local_steam64,
                )
            else:
                teammates, enemies = [local_steam64], []

            if self.on_match_found:
                self.on_match_found(local_steam64, teammates, enemies)
        finally:
            with self._lock:
                self._analyzing = False

    # ------------------------------------------------------------------ #

    def run(self):
        self.app.run(host="127.0.0.1", port=self.port,
                     debug=False, use_reloader=False)
