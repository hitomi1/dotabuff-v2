"""Multi-strategy player discovery for live Dota 2 matches.

Bypasses the GSI limitation where ``allplayers`` data is only available
to spectators.  Three strategies are attempted in order:

1. **STRATZ Live Match API** – GraphQL query using the match_id.
2. **Console-log + STRATZ name search** – reads the player table from
   Dota 2's console.log (always present, no launch option needed) and
   resolves each player name to a Steam ID via STRATZ search.
3. **Post-match OpenDota polling** – waits for OpenDota to parse the
   finished match and returns all 10 players.
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

# GraphQL query to fetch live-match players from STRATZ
_LIVE_MATCH_QUERY = """
query($matchId: Long!) {
  live {
    match(id: $matchId) {
      matchId
      players {
        steamAccountId
        isRadiant
        heroId
      }
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
    ):
        self.stratz_token = stratz_token or os.environ.get("STRATZ_TOKEN")
        self.dota_path = self._resolve_dota_path(dota_path)

        if self.stratz_token:
            logger.info("STRATZ token configured — live match API enabled.")
        else:
            logger.warning(
                "No STRATZ token. Set --stratz-token or STRATZ_TOKEN env var "
                "to enable live match lookups."
            )

        if self.dota_path:
            logger.info(f"Dota 2 path: {self.dota_path}")
        else:
            logger.warning(
                "Dota 2 path not found. Console-log fallback disabled. "
                "Pass --dota-path or install Dota 2 in the default location."
            )

    # ── Public API ───────────────────────────────────────────────────────

    def find_players(
        self,
        match_id: str,
        local_steam64: str,
        *,
        stratz_retries: int = 5,
        stratz_interval: float = 10.0,
    ) -> tuple[list[str], list[str]]:
        """Return ``(teammates, enemies)`` lists of Steam-64 IDs.

        Tries three strategies in order, falling back if each fails.
        """
        import concurrent.futures as _cf

        # Run Strategy 1 (STRATZ live) and Strategy 2 (console log) in parallel.
        # Whichever resolves first wins; the other is cancelled.
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
                    # Cancel remaining futures (best-effort)
                    for other in futures.values():
                        other.cancel()
                    return result

        # Strategy 3: poll OpenDota after match ends
        result = self._try_post_match_opendota(match_id, local_steam64)
        if result:
            return result

        logger.warning("All strategies failed. Analyzing local player only.")
        return [local_steam64], []

    # ── Strategy 1: STRATZ Live Match API ────────────────────────────────

    def _try_stratz(
        self,
        match_id: str,
        local_steam64: str,
        retries: int,
        interval: float,
    ) -> tuple[list[str], list[str]] | None:
        """Query STRATZ for the live match.  Retries because STRATZ may
        take a few seconds to pick up the match."""
        headers = {
            "Authorization": f"Bearer {self.stratz_token}",
            "Content-Type": "application/json",
            "User-Agent": "dota2-match-analyzer/2.0",
        }

        for attempt in range(1, retries + 1):
            try:
                logger.info(
                    f"STRATZ: querying match {match_id} "
                    f"(attempt {attempt}/{retries})…"
                )
                resp = requests.post(
                    STRATZ_GQL_URL,
                    json={
                        "query": _LIVE_MATCH_QUERY,
                        "variables": {"matchId": int(match_id)},
                    },
                    headers=headers,
                    timeout=15,
                )

                if resp.status_code == 429:
                    logger.warning("STRATZ: rate-limited. Backing off.")
                    time.sleep(interval * 2)
                    continue

                if resp.status_code != 200:
                    logger.warning(
                        f"STRATZ: HTTP {resp.status_code} — {resp.text[:200]}"
                    )
                    time.sleep(interval)
                    continue

                data = resp.json()
                match_data = (
                    data.get("data", {}).get("live", {}).get("match")
                )

                if not match_data or not match_data.get("players"):
                    logger.info("STRATZ: match not tracked yet, retrying…")
                    time.sleep(interval)
                    continue

                return self._parse_stratz_players(
                    match_data["players"], local_steam64
                )

            except requests.RequestException as exc:
                logger.warning(f"STRATZ request error: {exc}")
                time.sleep(interval)

        logger.warning("STRATZ: exhausted retries — match not found.")
        return None

    def _parse_stratz_players(
        self, players: list[dict], local_steam64: str
    ) -> tuple[list[str], list[str]]:
        """Parse STRATZ player list into (teammates, enemies)."""
        local_account = _steam64_to_account_id(local_steam64)

        # Determine local player's team
        local_is_radiant: bool | None = None
        for p in players:
            if p.get("steamAccountId") == local_account:
                local_is_radiant = p.get("isRadiant")
                break

        teammates: list[str] = []
        enemies: list[str] = []

        for p in players:
            account_id = p.get("steamAccountId")
            if not account_id:
                continue

            steam64 = _account_id_to_steam64(account_id)
            is_radiant = p.get("isRadiant")

            if local_is_radiant is not None:
                if is_radiant == local_is_radiant:
                    teammates.append(steam64)
                else:
                    enemies.append(steam64)
            else:
                # Can't determine teams — put everyone except local in enemies
                if steam64 == local_steam64:
                    teammates.append(steam64)
                else:
                    enemies.append(steam64)

        logger.info(
            f"STRATZ: found {len(teammates)} teammates, "
            f"{len(enemies)} enemies."
        )
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
