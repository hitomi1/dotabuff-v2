"""Dota 2 Game State Integration HTTP server."""

import logging
import threading
from typing import Callable

from flask import Flask, request

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
                 on_match_found: Callable | None = None):
        self.port = port
        self.on_match_found = on_match_found

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

            # Extract all 10 players from the allplayers block
            allplayers = data.get("allplayers", {})
            if len(allplayers) < 10:
                logger.debug(
                    f"GSI: only {len(allplayers)}/10 players visible yet – waiting.")
                return

            steam_ids = {}
            for key, pdata in allplayers.items():
                try:
                    slot = int(key.replace("player", ""))
                    sid  = str(pdata.get("steamid", ""))
                    if sid and sid != "0":
                        steam_ids[slot] = sid
                except ValueError:
                    continue

            if len(steam_ids) < 10:
                logger.debug(
                    f"GSI: only {len(steam_ids)}/10 Steam IDs populated – waiting.")
                return

            # Determine which team the local player is on
            local_slot = next(
                (slot for slot, sid in steam_ids.items() if sid == local_steam64),
                None,
            )

            # Slots 0-4 → one team, 5-9 → other team
            if local_slot is not None:
                my_team_slots   = set(range(0, 5)) if local_slot < 5 else set(range(5, 10))
                enemy_team_slots = set(range(5, 10)) if local_slot < 5 else set(range(0, 5))
            else:
                # Can't determine teams – treat all as enemies except local
                my_team_slots   = {slot for slot, sid in steam_ids.items()
                                   if sid == local_steam64}
                enemy_team_slots = set(steam_ids.keys()) - my_team_slots

            teammates = [steam_ids[s] for s in sorted(my_team_slots)   if s in steam_ids]
            enemies   = [steam_ids[s] for s in sorted(enemy_team_slots) if s in steam_ids]

            if not teammates or not enemies:
                return

            self._current_match_id = match_id
            self._analyzing = True

        logger.info(f"GSI: new match {match_id} – starting analysis.")
        thread = threading.Thread(
            target=self._run_analysis,
            args=(local_steam64, teammates, enemies),
            daemon=True,
        )
        thread.start()

    def _run_analysis(self, local_steam64: str, teammates: list, enemies: list):
        try:
            if self.on_match_found:
                self.on_match_found(local_steam64, teammates, enemies)
        finally:
            with self._lock:
                self._analyzing = False

    # ------------------------------------------------------------------ #

    def run(self):
        self.app.run(host="127.0.0.1", port=self.port,
                     debug=False, use_reloader=False)
