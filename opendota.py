"""OpenDota API client for fetching player data."""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.opendota.com/api"
STEAM_ID_OFFSET = 76561197960265728

LANE_ROLES = {1: "Safe Lane", 2: "Mid Lane", 3: "Off Lane", 4: "Jungle", 5: "Support"}

GAME_MODES = {
    0: "Unknown", 1: "All Pick", 2: "Captain's Mode", 3: "Random Draft",
    4: "Single Draft", 5: "All Random", 12: "Least Played", 14: "Random Draft",
    16: "Captain's Draft", 22: "All Pick", 23: "Turbo",
}

RANK_NAMES = {
    10: "Herald 1", 11: "Herald 2", 12: "Herald 3", 13: "Herald 4", 14: "Herald 5",
    20: "Guardian 1", 21: "Guardian 2", 22: "Guardian 3", 23: "Guardian 4", 24: "Guardian 5",
    30: "Crusader 1", 31: "Crusader 2", 32: "Crusader 3", 33: "Crusader 4", 34: "Crusader 5",
    40: "Archon 1", 41: "Archon 2", 42: "Archon 3", 43: "Archon 4", 44: "Archon 5",
    50: "Legend 1", 51: "Legend 2", 52: "Legend 3", 53: "Legend 4", 54: "Legend 5",
    60: "Ancient 1", 61: "Ancient 2", 62: "Ancient 3", 63: "Ancient 4", 64: "Ancient 5",
    70: "Divine 1", 71: "Divine 2", 72: "Divine 3", 73: "Divine 4", 74: "Divine 5",
    80: "Immortal",
}


def steam64_to_account_id(steam64_id: str | int) -> int:
    return int(steam64_id) - STEAM_ID_OFFSET


class OpenDotaClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "dota2-match-analyzer/1.0"})
        self.heroes: dict[int, str] = {}
        self._load_heroes()

    def _load_heroes(self):
        data = self._get("/heroes")
        if data:
            self.heroes = {h["id"]: h["localized_name"] for h in data}
            logger.info(f"Loaded {len(self.heroes)} heroes.")
        else:
            logger.warning("Could not load hero list from OpenDota.")

    def _get(self, endpoint: str, params: dict | None = None) -> dict | list | None:
        if params is None:
            params = {}
        if self.api_key:
            params["api_key"] = self.api_key

        url = f"{BASE_URL}{endpoint}"
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    logger.warning(f"Rate limited. Waiting {wait}s…")
                    time.sleep(wait)
                    continue
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                logger.debug(f"Request error ({attempt+1}/3) for {url}: {e}")
                if attempt < 2:
                    time.sleep(1)
        return None

    # ------------------------------------------------------------------ #

    def _hero_name(self, hero_id: int) -> str:
        return self.heroes.get(hero_id, f"Hero #{hero_id}")

    def _parse_matches(self, raw: list) -> list[dict]:
        matches = []
        for m in raw[:20]:
            slot = m.get("player_slot", 0)
            radiant_win = m.get("radiant_win")
            won = (radiant_win is True) == (slot < 128)
            secs = m.get("duration", 0)
            matches.append({
                "match_id": m.get("match_id"),
                "hero": self._hero_name(m.get("hero_id", 0)),
                "result": "Win" if won else "Loss",
                "kills": m.get("kills", 0),
                "deaths": m.get("deaths", 0),
                "assists": m.get("assists", 0),
                "duration": f"{secs // 60}:{secs % 60:02d}",
                "game_mode": GAME_MODES.get(m.get("game_mode", 0), "Unknown"),
                "date": datetime.fromtimestamp(m.get("start_time", 0)).strftime("%Y-%m-%d"),
                "lane_role": m.get("lane_role"),
            })
        return matches

    def _parse_heroes(self, raw: list) -> list[dict]:
        heroes = []
        for h in raw[:10]:
            games = h.get("games", 0)
            wins = h.get("win", 0)
            winrate = f"{wins / games * 100:.1f}%" if games > 0 else "N/A"
            heroes.append({
                "hero": self._hero_name(h.get("hero_id", 0)),
                "games": games,
                "wins": wins,
                "winrate": winrate,
            })
        return heroes

    def _infer_role(self, matches: list[dict]) -> str:
        counts: dict[int, int] = {}
        for m in matches:
            r = m.get("lane_role")
            if r:
                counts[r] = counts.get(r, 0) + 1
        if not counts:
            return "Unknown"
        return LANE_ROLES.get(max(counts, key=counts.get), "Unknown")

    # ------------------------------------------------------------------ #

    def get_player(self, steam64_id: str | int) -> dict:
        account_id = steam64_to_account_id(steam64_id)

        # Fire 3 requests concurrently per player
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_profile = ex.submit(self._get, f"/players/{account_id}")
            f_matches = ex.submit(self._get, f"/players/{account_id}/matches",
                                  {"limit": 20, "significant": 0})
            f_heroes  = ex.submit(self._get, f"/players/{account_id}/heroes",
                                  {"limit": 10})

        profile_raw = f_profile.result()
        matches_raw = f_matches.result() or []
        heroes_raw  = f_heroes.result()  or []

        # Profile
        if profile_raw:
            p = profile_raw.get("profile", {})
            rank_tier = profile_raw.get("rank_tier")
            profile = {
                "name": p.get("personaname", "Unknown"),
                "rank": RANK_NAMES.get(rank_tier, "Unranked"),
                "account_id": account_id,
                "dotabuff_url": f"https://www.dotabuff.com/players/{account_id}",
                "opendota_url": f"https://www.opendota.com/players/{account_id}",
            }
        else:
            profile = {
                "name": "Private / Unknown",
                "rank": "N/A",
                "account_id": account_id,
                "dotabuff_url": f"https://www.dotabuff.com/players/{account_id}",
                "opendota_url": f"https://www.opendota.com/players/{account_id}",
            }

        matches = self._parse_matches(matches_raw)
        top_heroes = self._parse_heroes(heroes_raw)
        main_role = self._infer_role(matches)

        return {
            "steam64_id": str(steam64_id),
            "profile": profile,
            "matches": matches,
            "top_heroes": top_heroes,
            "main_role": main_role,
        }

    def get_all_players(self, steam64_ids: list, max_workers: int = 5) -> dict[str, dict]:
        """Fetch data for multiple players concurrently. Returns {steam64_id: data}."""
        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(self.get_player, sid): str(sid)
                         for sid in steam64_ids}
            for future in as_completed(future_map):
                sid = future_map[future]
                try:
                    results[sid] = future.result()
                except Exception as e:
                    logger.error(f"Failed to get data for {sid}: {e}")
                    results[sid] = None
        return results
