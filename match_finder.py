"""Multi-strategy player discovery for live Dota 2 matches.

Bypasses the GSI limitation where ``allplayers`` data is only available
to spectators.  Four strategies are tried in parallel / sequence:

0. **Steam GetRealtimeStats** – reads the game-server Steam ID from
   console.log and calls the Steam Web API to get all 10 players in
   real-time. Fastest; requires a Steam Web API key and -condebug.
1. **STRATZ match(id) query** – works ~2 min after the match ENDS.
2. **Console-log + STRATZ name search** – parses the player table written
   by the Source engine `status` command if it appears in console.log.
3. **Post-match OpenDota polling** – polls every 60 s for up to 15 min.
"""

import logging
import os
import platform
import re
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Steam-ID arithmetic
_STEAM_ID_OFFSET = 76561197960265728

# Regex for SteamID3 format  [U:1:ACCOUNT_ID]
_STEAMID3_RE = re.compile(r"\[U:1:(\d+)\]")

# Regex for the player-list table in console.log:
#   [Client]    7    00:28   14    0     active  80000 'PlayerName'
#   [Client]    0      BOT    0    0     active      0 'SourceTV'
_PLAYER_ROW_RE = re.compile(
    r"\[Client\]\s+(\d+)\s+(?:BOT|\d+:\d+)\s+\d+\s+\d+\s+\w+\s+\d+\s+'(.+?)'"
)
# Lobby MatchID line
_LOBBY_MATCH_ID_RE = re.compile(r"Lobby MatchID:\s*(\d+)")

# STRATZ player search query
_STRATZ_SEARCH_QUERY = """
query SearchPlayer($query: String!) {
  stratz {
    search(request: {query: $query, searchType: [PLAYERS]}) {
      players {
        id
        name
      }
    }
  }
}
"""

# Default Dota 2 paths per OS
_DEFAULT_DOTA_PATHS: dict[str, list[str]] = {
    "Linux": [
        # Native Linux Steam
        os.path.expanduser(
            "~/.steam/steam/steamapps/common/dota 2 beta/game/dota"
        ),
        os.path.expanduser(
            "~/.local/share/Steam/steamapps/common/dota 2 beta/game/dota"
        ),
        # WSL2 — Windows drives mounted under /mnt/
        "/mnt/d/SteamLibrary/steamapps/common/dota 2 beta/game/dota",
        "/mnt/c/Program Files (x86)/Steam/steamapps/common/dota 2 beta/game/dota",
    ],
    "Windows": [
        r"C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game\dota",
        r"D:\SteamLibrary\steamapps\common\dota 2 beta\game\dota",
    ],
}

STRATZ_GQL_URL = "https://api.stratz.com/graphql"
STEAM_REALTIME_URL = "https://api.steampowered.com/IDOTA2MatchStats_570/GetRealtimeStats/v001/"

# Regex to extract server Steam ID + match_id from console.log line:
# [SteamNetSockets] Received Steam datagram ticket for server steamid:90283504750644245 ... match_id=8745218084
_SERVER_STEAMID_RE = re.compile(
    r"steamid:(\d+)\s+vport\s+\d+\.\s+match_id=(\d+)"
)

# Query match players directly — works for both live and finished matches.
# STRATZ indexes matchmaking games within ~2 min after match end.
_MATCH_QUERY = """
query GetMatch($matchId: Long!) {
  match(id: $matchId) {
    id
    players {
      steamAccountId
      isRadiant
    }
  }
}
"""


def _account_id_to_steam64(account_id: int) -> str:
    return str(account_id + _STEAM_ID_OFFSET)


def _steam64_to_account_id(steam64: str | int) -> int:
    return int(steam64) - _STEAM_ID_OFFSET


class MatchFinder:
    """Discovers all 10 players' Steam-64 IDs for a live match."""

    def __init__(
        self,
        stratz_token: str | None = None,
        dota_path: str | None = None,
        steam_api_key: str | None = None,
    ):
        self.stratz_token  = stratz_token  or os.environ.get("STRATZ_TOKEN")
        self.steam_api_key = steam_api_key or os.environ.get("STEAM_API_KEY")
        self.dota_path     = self._resolve_dota_path(dota_path)

        if self.steam_api_key:
            logger.info("Steam API key configured — GetRealtimeStats enabled (real-time).")
        else:
            logger.warning(
                "No Steam API key. Real-time player lookup disabled. "
                "Get a free key at https://steamcommunity.com/dev/apikey "
                "and set --steam-api-key or STEAM_API_KEY env var."
            )

        if self.stratz_token:
            logger.info("STRATZ token configured — post-match lookup enabled.")
        else:
            logger.warning(
                "No STRATZ token. Set --stratz-token or STRATZ_TOKEN env var."
            )

        if self.dota_path:
            logger.info(f"Dota 2 path: {self.dota_path}")
        else:
            logger.warning(
                "Dota 2 path not found. Console-log parsing disabled. "
                "Pass --dota-path or install Dota 2 in the default location."
            )

    # ── Public API ───────────────────────────────────────────────────────

    def find_players(
        self,
        match_id: str,
        local_steam64: str,
        *,
        stratz_retries: int = 12,
        stratz_interval: float = 10.0,
    ) -> tuple[list[str], list[str]]:
        """Return ``(teammates, enemies)`` lists of Steam-64 IDs.

        Strategy order:
        0. Steam GetRealtimeStats (real-time, needs STEAM_API_KEY + console.log)
        1. STRATZ match(id) — post-match, ~2 min after end
        2. Console-log + STRATZ name search — if status table appears
        3. OpenDota polling — up to 15 min post-match
        """
        import concurrent.futures as _cf

        # Strategy 0: Steam real-time stats (fastest, works during the match)
        if self.steam_api_key and self.dota_path:
            result = self._try_steam_realtime(match_id, local_steam64)
            if result and (result[0] or result[1]):
                return result

        # Run Strategy 1 (STRATZ) and Strategy 2 (console log) in parallel.
        futures: dict = {}
        with _cf.ThreadPoolExecutor(max_workers=2) as pool:
            if self.stratz_token:
                futures["stratz"] = pool.submit(
                    self._try_stratz,
                    match_id, local_steam64, stratz_retries, stratz_interval,
                )
            if self.dota_path:
                futures["console"] = pool.submit(
                    self._try_console_log_with_name_search,
                    match_id, local_steam64,
                )

            for fut in _cf.as_completed(futures.values()):
                result = fut.result()
                if result and (result[0] or result[1]):
                    for other in futures.values():
                        other.cancel()
                    return result

        # Strategy 3: poll OpenDota after match ends
        result = self._try_post_match_opendota(match_id, local_steam64)
        if result:
            return result

        logger.warning("All strategies failed. Analyzing local player only.")
        return [local_steam64], []

    # ── Strategy 0: Steam GetRealtimeStats ───────────────────────────────

    def _get_server_steam_id(self, match_id: str) -> str | None:
        """Read console.log and extract the game-server Steam ID for this match."""
        log_tail = self._read_console_log_tail()
        if not log_tail:
            return None
        for m in _SERVER_STEAMID_RE.finditer(log_tail):
            if m.group(2) == match_id:
                return m.group(1)
        return None

    def _try_steam_realtime(
        self,
        match_id: str,
        local_steam64: str,
        *,
        retries: int = 18,
        interval: float = 10.0,
    ) -> tuple[list[str], list[str]] | None:
        """Query Steam GetRealtimeStats using the server Steam ID from console.log.

        This is the only real-time source — returns all 10 players within
        seconds of match start.  Requires STEAM_API_KEY and -condebug.
        """
        server_steam_id = None
        for attempt in range(1, retries + 1):
            if not server_steam_id:
                server_steam_id = self._get_server_steam_id(match_id)
            if not server_steam_id:
                logger.info(
                    f"Steam: server Steam ID not in console.log yet "
                    f"(attempt {attempt}/{retries}), retrying in {int(interval)}s…"
                )
                time.sleep(interval)
                continue

            try:
                logger.info(
                    f"Steam: GetRealtimeStats for server {server_steam_id} "
                    f"(attempt {attempt}/{retries})…"
                )
                resp = requests.get(
                    STEAM_REALTIME_URL,
                    params={
                        "key": self.steam_api_key,
                        "server_steam_id": server_steam_id,
                    },
                    timeout=10,
                )
                if resp.status_code == 403:
                    logger.warning("Steam: API key invalid or not authorised.")
                    return None
                if resp.status_code != 200:
                    logger.warning(f"Steam GetRealtimeStats HTTP {resp.status_code}")
                    time.sleep(interval)
                    continue

                data = resp.json()
                match_data = data.get("match", {})

                # Verify it's the right match
                api_match_id = str(match_data.get("matchid", ""))
                if api_match_id and api_match_id != match_id:
                    logger.warning(
                        f"Steam: server returned match {api_match_id}, "
                        f"expected {match_id}. Server may have changed."
                    )
                    return None

                teams = match_data.get("teams", [])
                if not teams:
                    logger.info("Steam: match data not ready yet, retrying…")
                    time.sleep(interval)
                    continue

                return self._parse_steam_realtime(teams, local_steam64)

            except requests.RequestException as exc:
                logger.warning(f"Steam GetRealtimeStats error: {exc}")
                time.sleep(interval)

        logger.warning("Steam: exhausted retries for GetRealtimeStats.")
        return None

    def _parse_steam_realtime(
        self, teams: list[dict], local_steam64: str
    ) -> tuple[list[str], list[str]]:
        """Parse the GetRealtimeStats `teams` array into (teammates, enemies)."""
        local_account = _steam64_to_account_id(local_steam64)

        # Find which team the local player is on
        local_team_idx: int | None = None
        for idx, team in enumerate(teams):
            for p in team.get("players", []):
                if p.get("accountid") == local_account:
                    local_team_idx = idx
                    break
            if local_team_idx is not None:
                break

        teammates, enemies = [], []
        for idx, team in enumerate(teams):
            for p in team.get("players", []):
                account_id = p.get("accountid")
                if not account_id:
                    continue
                steam64 = _account_id_to_steam64(account_id)
                if local_team_idx is not None:
                    (teammates if idx == local_team_idx else enemies).append(steam64)
                else:
                    (teammates if steam64 == local_steam64 else enemies).append(steam64)

        logger.info(
            f"Steam GetRealtimeStats: {len(teammates)} teammates, {len(enemies)} enemies."
        )
        return teammates, enemies

    # ── Strategy 1: STRATZ post-match query ──────────────────────────────

    def _try_stratz(
        self,
        match_id: str,
        local_steam64: str,
        retries: int,
        interval: float,
    ) -> tuple[list[str], list[str]] | None:
        """Query STRATZ match(id) endpoint — works for live and finished games.
        STRATZ indexes matchmaking games within ~2 minutes of start."""
        headers = {
            "Authorization": f"Bearer {self.stratz_token}",
            "Content-Type": "application/json",
            "User-Agent": "STRATZ_API",
        }

        for attempt in range(1, retries + 1):
            try:
                logger.info(f"STRATZ: querying match {match_id} (attempt {attempt}/{retries})…")
                resp = requests.post(
                    STRATZ_GQL_URL,
                    json={"query": _MATCH_QUERY, "variables": {"matchId": int(match_id)}},
                    headers=headers,
                    timeout=15,
                )

                if resp.status_code == 429:
                    logger.warning("STRATZ: rate-limited, backing off…")
                    time.sleep(interval * 2)
                    continue

                if resp.status_code != 200:
                    logger.warning(f"STRATZ: HTTP {resp.status_code}")
                    time.sleep(interval)
                    continue

                match_data = resp.json().get("data", {}).get("match")
                players = match_data.get("players", []) if match_data else []

                if not players:
                    logger.info("STRATZ: match not indexed yet, retrying…")
                    time.sleep(interval)
                    continue

                return self._parse_stratz_players(players, local_steam64)

            except requests.RequestException as exc:
                logger.warning(f"STRATZ request error: {exc}")
                time.sleep(interval)

        logger.warning("STRATZ: exhausted retries.")
        return None

    def _parse_stratz_players(
        self, players: list[dict], local_steam64: str
    ) -> tuple[list[str], list[str]]:
        local_account = _steam64_to_account_id(local_steam64)

        local_is_radiant: bool | None = None
        for p in players:
            if p.get("steamAccountId") == local_account:
                local_is_radiant = p.get("isRadiant")
                break

        teammates, enemies = [], []
        for p in players:
            account_id = p.get("steamAccountId")
            if not account_id:
                continue
            steam64 = _account_id_to_steam64(account_id)
            is_radiant = p.get("isRadiant")
            if local_is_radiant is not None:
                (teammates if is_radiant == local_is_radiant else enemies).append(steam64)
            else:
                (teammates if steam64 == local_steam64 else enemies).append(steam64)

        logger.info(f"STRATZ match query: {len(teammates)} teammates, {len(enemies)} enemies.")
        return teammates, enemies

    # ── Strategy 2: Console-log player table + STRATZ name search ────────

    def _read_console_log_tail(self) -> str | None:
        """Read the last 300 KB of console.log."""
        log_path = Path(self.dota_path) / "console.log"
        if not log_path.exists():
            logger.warning(f"console.log not found at {log_path}")
            return None
        try:
            size = log_path.stat().st_size
            read_size = min(size, 300 * 1024)
            with open(log_path, "r", errors="replace") as f:
                if size > read_size:
                    f.seek(size - read_size)
                return f.read()
        except OSError as exc:
            logger.error(f"Cannot read console.log: {exc}")
            return None

    def _parse_player_table(
        self, log_tail: str, match_id: str
    ) -> dict[int, str] | None:
        """Parse the '---------players--------' table for a specific match.

        Returns {slot: player_name} for slots 1-10 (excluding SourceTV slot 0),
        or None if the table for this match isn't found.
        """
        # Find all lobby sections and pick the one matching match_id
        # Look for the pattern: players table followed by Lobby MatchID
        sections = log_tail.split("---------players--------")
        for section in reversed(sections[1:]):   # newest first
            match_id_found = _LOBBY_MATCH_ID_RE.search(section)
            if not match_id_found:
                continue
            if match_id_found.group(1) != match_id:
                continue

            players: dict[int, str] = {}
            for m in _PLAYER_ROW_RE.finditer(section):
                slot = int(m.group(1))
                name = m.group(2).strip()
                if slot == 0:   # skip SourceTV
                    continue
                players[slot] = name

            if len(players) >= 9:
                logger.info(
                    f"Console log: found {len(players)} players for match {match_id}"
                )
                return players

        logger.warning(
            f"Console log: player table for match {match_id} not found."
        )
        return None

    def _stratz_search_name(self, name: str) -> int | None:
        """Search STRATZ for a player by name. Returns account_id or None."""
        if not self.stratz_token:
            return None
        headers = {
            "Authorization": f"Bearer {self.stratz_token}",
            "Content-Type": "application/json",
            "User-Agent": "dota2-match-analyzer/2.0",
        }
        try:
            resp = requests.post(
                STRATZ_GQL_URL,
                json={"query": _STRATZ_SEARCH_QUERY, "variables": {"query": name}},
                headers=headers,
                timeout=10,
            )
            if not resp.ok:
                return None
            players = (
                resp.json()
                .get("data", {})
                .get("stratz", {})
                .get("search", {})
                .get("players", [])
            )
            if players:
                account_id = players[0].get("id")
                found_name = players[0].get("name", "")
                if account_id:
                    logger.debug(f"STRATZ search '{name}' → {account_id} ({found_name})")
                    return account_id
        except requests.RequestException as exc:
            logger.debug(f"STRATZ name search error for '{name}': {exc}")
        return None

    def _try_console_log_with_name_search(
        self, match_id: str, local_steam64: str,
        *,
        retries: int = 12,
        interval: float = 10.0,
    ) -> tuple[list[str], list[str]] | None:
        """Parse console.log player table, resolve names via STRATZ search.

        Retries because the player table is written only after all 10 players
        have fully loaded into the game server (~30-90s after match start).
        """
        slot_names = None
        for attempt in range(1, retries + 1):
            log_tail = self._read_console_log_tail()
            if log_tail:
                slot_names = self._parse_player_table(log_tail, match_id)
            if slot_names:
                break
            logger.info(
                f"Console log: player table not ready yet "
                f"(attempt {attempt}/{retries}), retrying in {int(interval)}s…"
            )
            time.sleep(interval)

        if not slot_names:
            logger.warning(
                f"Console log: player table for match {match_id} never appeared."
            )
            return None

        # Resolve each name to a steam64 ID via STRATZ search
        slot_steam64: dict[int, str] = {}
        for slot, name in slot_names.items():
            account_id = self._stratz_search_name(name)
            if account_id:
                slot_steam64[slot] = _account_id_to_steam64(account_id)
            else:
                logger.debug(f"Slot {slot} '{name}': could not resolve Steam ID")

        if len(slot_steam64) < 2:
            logger.warning(
                f"Console log: only resolved {len(slot_steam64)}/10 Steam IDs via name search."
            )
            return None

        # Determine local player's slot
        local_slot = next(
            (slot for slot, sid in slot_steam64.items() if sid == local_steam64),
            None,
        )

        # Slots 1-5 = one team, 6-10 = other team
        if local_slot is not None:
            my_slots   = set(range(1, 6)) if local_slot <= 5 else set(range(6, 11))
            enemy_slots = set(range(6, 11)) if local_slot <= 5 else set(range(1, 6))
        else:
            # Can't determine — put resolved players as enemies, local as team
            my_slots   = set()
            enemy_slots = set(slot_steam64.keys())

        teammates = [slot_steam64[s] for s in sorted(my_slots)   if s in slot_steam64]
        enemies   = [slot_steam64[s] for s in sorted(enemy_slots) if s in slot_steam64]

        if not teammates:
            teammates = [local_steam64]

        logger.info(
            f"Console+STRATZ: resolved {len(slot_steam64)}/10 players "
            f"({len(teammates)} team, {len(enemies)} enemies)."
        )
        return teammates, enemies

    # ── Strategy 3: Post-match OpenDota polling ───────────────────────────

    def _try_post_match_opendota(
        self,
        match_id: str,
        local_steam64: str,
        *,
        max_wait_minutes: int = 15,
        poll_interval: float = 60.0,
    ) -> tuple[list[str], list[str]] | None:
        """Poll OpenDota until the finished match is available, then return players."""
        url = f"https://api.opendota.com/api/matches/{match_id}"
        local_account = _steam64_to_account_id(local_steam64)
        attempts = max_wait_minutes

        logger.info(
            f"OpenDota: waiting for match {match_id} to finish "
            f"(polling every {int(poll_interval)}s, up to {max_wait_minutes} min)…"
        )

        for attempt in range(1, attempts + 1):
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 404:
                    logger.info(f"OpenDota: match not yet available (attempt {attempt}/{attempts})")
                    time.sleep(poll_interval)
                    continue
                resp.raise_for_status()
                data = resp.json()
                raw_players = data.get("players", [])
                if not raw_players:
                    time.sleep(poll_interval)
                    continue

                # Determine local player's team
                local_is_radiant = next(
                    (p.get("isRadiant") for p in raw_players
                     if p.get("account_id") == local_account),
                    None,
                )

                teammates, enemies = [], []
                for p in raw_players:
                    account_id = p.get("account_id")
                    if not account_id:
                        continue
                    steam64 = _account_id_to_steam64(account_id)
                    if local_is_radiant is not None:
                        if p.get("isRadiant") == local_is_radiant:
                            teammates.append(steam64)
                        else:
                            enemies.append(steam64)
                    else:
                        if account_id == local_account:
                            teammates.append(steam64)
                        else:
                            enemies.append(steam64)

                if not teammates:
                    teammates = [local_steam64]

                logger.info(
                    f"OpenDota post-match: found {len(teammates)+len(enemies)} players."
                )
                return teammates, enemies

            except requests.RequestException as exc:
                logger.warning(f"OpenDota polling error: {exc}")
                time.sleep(poll_interval)

        logger.warning("OpenDota: match never appeared within wait window.")
        return None

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_dota_path(user_path: str | None) -> str | None:
        """Find the Dota 2 game directory."""
        if user_path:
            p = Path(user_path)
            if p.exists():
                return str(p)
            logger.warning(f"Provided Dota path does not exist: {user_path}")

        system = platform.system()
        for candidate in _DEFAULT_DOTA_PATHS.get(system, []):
            if Path(candidate).exists():
                return candidate

        return None
