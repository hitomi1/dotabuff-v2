"""Multi-strategy player discovery for live Dota 2 matches.

Bypasses the GSI limitation where ``allplayers`` data is only available
to spectators.  Two strategies are attempted in order:

1. **STRATZ Live Match API** – GraphQL query using the match_id.
2. **Console-log parsing** – reads Dota 2's ``console.log`` (requires
   the ``-condebug`` launch option).
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

        Tries STRATZ first, then console-log parsing.  Falls back to
        returning only ``local_steam64`` as a teammate if everything fails.
        """
        # Strategy 1: STRATZ
        if self.stratz_token:
            result = self._try_stratz(
                match_id, local_steam64, stratz_retries, stratz_interval
            )
            if result:
                return result

        # Strategy 2: console.log
        if self.dota_path:
            result = self._try_console_log(local_steam64)
            if result:
                return result

        # Fallback: local player only
        logger.warning(
            "Could not discover other players. Analyzing local player only."
        )
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

    # ── Strategy 2: Console-log parsing ──────────────────────────────────

    def _try_console_log(
        self, local_steam64: str
    ) -> tuple[list[str], list[str]] | None:
        """Parse the Dota 2 console.log for recent player connections."""
        log_path = Path(self.dota_path) / "console.log"
        if not log_path.exists():
            logger.warning(
                f"Console log not found at {log_path}. "
                "Add -condebug to Dota 2 launch options."
            )
            return None

        try:
            # Read last 200 KB of the log (recent data only)
            file_size = log_path.stat().st_size
            read_size = min(file_size, 200 * 1024)

            with open(log_path, "r", errors="replace") as f:
                if file_size > read_size:
                    f.seek(file_size - read_size)
                tail = f.read()

        except OSError as exc:
            logger.error(f"Cannot read console log: {exc}")
            return None

        # Extract unique account IDs from SteamID3 patterns
        account_ids = set()
        for m in _STEAMID3_RE.finditer(tail):
            account_ids.add(int(m.group(1)))

        if len(account_ids) < 2:
            logger.info(
                f"Console log: only found {len(account_ids)} Steam IDs."
            )
            return None

        # Convert to steam64
        all_steam64 = [_account_id_to_steam64(aid) for aid in account_ids]
        local_account = _steam64_to_account_id(local_steam64)

        # Simple split: local player + everyone else
        # (Console log doesn't tell us teams, STRATZ does — but this is
        # a fallback, the web UI / CLI can still show all players.)
        teammates = [
            s for s in all_steam64
            if _steam64_to_account_id(s) == local_account
        ]
        enemies = [
            s for s in all_steam64
            if _steam64_to_account_id(s) != local_account
        ]

        if not teammates:
            teammates = [local_steam64]

        logger.info(
            f"Console log: found {len(all_steam64)} players "
            f"({len(teammates)} team, {len(enemies)} other)."
        )
        return teammates, enemies

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
