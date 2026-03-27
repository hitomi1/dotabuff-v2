"""Dota 2 Match Analyzer — Flask web application with SSE streaming."""

import argparse
import json
import logging
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Load .env from project root if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from flask import Flask, Response, render_template, request

from gsi_server import ACTIVE_STATES, AUTH_TOKEN
from match_finder import MatchFinder
from opendota import OpenDotaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.INFO)

app = Flask(__name__)

# ── SSE subscriber registry ────────────────────────────────────────────────────
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()

# ── GSI dedup state ────────────────────────────────────────────────────────────
_current_match_id: str | None = None
_analyzing: bool = False
_gsi_lock = threading.Lock()

# ── OpenDota client & match finder (initialised in main) ──────────────────────
_client: OpenDotaClient | None = None
_finder: MatchFinder | None = None


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=100)
    with _subscribers_lock:
        _subscribers.append(q)
    return q


def _unsubscribe(q: queue.Queue):
    with _subscribers_lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def _broadcast(event_type: str, data: dict):
    """Push an SSE event to every connected client."""
    payload = json.dumps({"type": event_type, "data": data})
    with _subscribers_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass


def _sse_format(payload: str) -> str:
    return f"data: {payload}\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/stream")
def stream():
    q = _subscribe()

    def generate():
        # Send initial status so the client knows it's connected
        _broadcast("status", {"status": "waiting"})
        try:
            while True:
                try:
                    payload = q.get(timeout=25)
                    yield _sse_format(payload)
                except queue.Empty:
                    # Heartbeat to keep the connection alive
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            _unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/test/<match_id>")
def test_match(match_id):
    global _current_match_id, _analyzing
    local_steam64 = "76561198046685971"
    with _gsi_lock:
        if _analyzing:
            return "Analysis already in progress", 409
        _current_match_id = match_id
        _analyzing = True
    thread = threading.Thread(
        target=_run_analysis,
        args=(match_id, local_steam64, True),  # skip_realtime=True for completed matches
        daemon=True,
    )
    thread.start()
    return f"Analysis started for match {match_id}", 200


@app.route("/gsi", methods=["POST"])
def gsi():
    data = request.get_json(silent=True, force=True)
    if not data:
        logger.warning("GSI: received empty/non-JSON payload.")
        return "OK", 200

    auth = data.get("auth", {})
    if auth.get("token") != AUTH_TOKEN:
        logger.warning(f"GSI: invalid auth token '{auth.get('token')}' – ignoring.")
        return "Unauthorized", 401

    map_data  = data.get("map", {})
    game_state = map_data.get("game_state", "—")
    match_id   = map_data.get("matchid", "—")
    n_players  = len(data.get("allplayers", {}))
    logger.info(f"GSI ▶ state={game_state}  match={match_id}  players={n_players}/10")

    try:
        _process_gsi(data)
    except Exception as exc:
        logger.error(f"GSI processing error: {exc}", exc_info=True)

    return "OK", 200


# ── GSI processing ────────────────────────────────────────────────────────────

def _process_gsi(data: dict):
    global _current_match_id, _analyzing

    map_data = data.get("map", {})
    game_state = map_data.get("game_state", "")
    match_id = str(map_data.get("matchid", ""))

    if game_state not in ACTIVE_STATES or not match_id or match_id == "0":
        return

    with _gsi_lock:
        if match_id == _current_match_id or _analyzing:
            return

        local_player = data.get("player", {})
        local_steam64 = str(local_player.get("steamid", ""))

        if not local_steam64 or local_steam64 == "0":
            logger.debug("GSI: no local player Steam ID yet – waiting.")
            return

        _current_match_id = match_id
        _analyzing = True

    logger.info(f"GSI: new match {match_id} – launching analysis thread.")
    thread = threading.Thread(
        target=_run_analysis,
        args=(match_id, local_steam64),
        daemon=True,
    )
    thread.start()


def _run_analysis(match_id: str, local_steam64: str, skip_realtime: bool = False):
    global _analyzing
    try:
        if _client is None or _finder is None:
            logger.error("OpenDota client or MatchFinder not initialised.")
            return

        # Phase 1: discover all players (blocks for up to ~17 min total)
        _broadcast("status", {"status": "discovering"})
        logger.info("Discovering players via MatchFinder…")
        teammates, enemies = _finder.find_players(match_id, local_steam64, skip_realtime=skip_realtime)
    except Exception as exc:
        logger.error(f"Player discovery error: {exc}", exc_info=True)
        teammates, enemies = [local_steam64], []
    finally:
        # Release lock so a new match can be detected during Phase 2 retry
        with _gsi_lock:
            _analyzing = False

    # Phase 2: if only the local player was found (all strategies failed),
    # keep retrying STRATZ every 2 min until the match ends and gets indexed.
    if not enemies and set(teammates) == {local_steam64}:
        logger.info(
            f"Discovery failed for {match_id}; "
            f"will retry STRATZ every 2 min (up to 60 min) after match ends…"
        )
        for attempt in range(1, 31):
            time.sleep(120)
            # A new match started — stop retrying the old one
            if _current_match_id != match_id:
                logger.info(f"New match detected; stopping retry for {match_id}.")
                return
            if _finder is None:
                break
            logger.info(f"STRATZ retry {attempt}/30 for match {match_id}…")
            result = _finder._try_stratz(match_id, local_steam64, retries=3, interval=10)
            if result and (result[0] or result[1]):
                teammates, enemies = result
                logger.info(
                    f"Retry succeeded: {len(teammates)} teammates, {len(enemies)} enemies."
                )
                break
        else:
            logger.warning(f"All STRATZ retries exhausted for {match_id}.")

    # If a new match started while we were retrying, don't overwrite its display
    if _current_match_id != match_id and _current_match_id is not None:
        logger.info(f"Match {match_id} results discarded — new match in progress.")
        return

    try:
        _broadcast("match_detected", {
            "match_id": match_id,
            "n_teammates": len(teammates),
            "n_enemies": len(enemies),
        })

        team_map: dict[str, str] = {}
        for sid in teammates:
            team_map[str(sid)] = "teammate"
        for sid in enemies:
            team_map[str(sid)] = "enemy"

        all_ids = list(dict.fromkeys(teammates + enemies))

        with ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {executor.submit(_client.get_player, sid): str(sid)
                         for sid in all_ids}
            for future in as_completed(future_map):
                sid = future_map[future]
                try:
                    player_data = future.result()
                except Exception as exc:
                    logger.error(f"Failed to fetch player {sid}: {exc}")
                    player_data = None

                if player_data is None:
                    account_id = int(sid) - 76561197960265728
                    player_data = {
                        "profile": {
                            "name": f"Player {sid[-4:]}",
                            "rank": "Unknown",
                            "dotabuff_url": f"https://www.dotabuff.com/players/{account_id}",
                            "opendota_url": f"https://www.opendota.com/players/{account_id}",
                        },
                        "matches": [],
                        "top_heroes": [],
                        "main_role": "Unknown",
                    }

                player_data["team"] = team_map.get(sid, "enemy")
                player_data["is_you"] = (sid == str(local_steam64))
                _broadcast("player_data", player_data)
                logger.info(f"Broadcast player_data for {sid}")

        logger.info(f"Analysis complete for match {match_id}.")
    except Exception as exc:
        logger.error(f"Analysis error for match {match_id}: {exc}", exc_info=True)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _client, _finder

    parser = argparse.ArgumentParser(description="Dota 2 Match Analyzer – Web UI")
    parser.add_argument("--port", type=int, default=5000,
                        help="HTTP port (default: 5000)")
    parser.add_argument("--api-key", default=os.environ.get("OPENDOTA_API_KEY"),
                        help="OpenDota API key (optional, raises rate limit)")
    parser.add_argument("--stratz-token", default=os.environ.get("STRATZ_TOKEN"),
                        help="STRATZ API token for post-match lookups")
    parser.add_argument("--steam-api-key", default=os.environ.get("STEAM_API_KEY"),
                        help="Steam Web API key for real-time player lookup (steamcommunity.com/dev/apikey)")
    parser.add_argument("--dota-path", default=None,
                        help="Path to Dota 2 game/dota directory (for console.log parsing)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    _client = OpenDotaClient(api_key=args.api_key)
    _finder = MatchFinder(
        stratz_token=args.stratz_token,
        steam_api_key=args.steam_api_key,
        dota_path=args.dota_path,
    )

    print(f"Web UI → http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
