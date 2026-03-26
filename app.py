"""Dota 2 Match Analyzer — Flask web application with SSE streaming."""

import argparse
import json
import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, Response, render_template, request

from gsi_server import ACTIVE_STATES, AUTH_TOKEN
from opendota import OpenDotaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)

# ── SSE subscriber registry ────────────────────────────────────────────────────
_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()

# ── GSI dedup state ────────────────────────────────────────────────────────────
_current_match_id: str | None = None
_analyzing: bool = False
_gsi_lock = threading.Lock()

# ── OpenDota client (initialised in main) ─────────────────────────────────────
_client: OpenDotaClient | None = None


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


@app.route("/gsi", methods=["POST"])
def gsi():
    data = request.get_json(silent=True, force=True)
    if not data:
        return "OK", 200

    auth = data.get("auth", {})
    if auth.get("token") != AUTH_TOKEN:
        logger.warning("GSI: invalid auth token – ignoring payload.")
        return "Unauthorized", 401

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

        allplayers = data.get("allplayers", {})
        if len(allplayers) < 10:
            logger.debug(f"GSI: only {len(allplayers)}/10 players visible – waiting.")
            return

        steam_ids: dict[int, str] = {}
        for key, pdata in allplayers.items():
            try:
                slot = int(key.replace("player", ""))
                sid = str(pdata.get("steamid", ""))
                if sid and sid != "0":
                    steam_ids[slot] = sid
            except ValueError:
                continue

        if len(steam_ids) < 10:
            logger.debug(f"GSI: only {len(steam_ids)}/10 Steam IDs – waiting.")
            return

        local_slot = next(
            (slot for slot, sid in steam_ids.items() if sid == local_steam64),
            None,
        )

        if local_slot is not None:
            my_team_slots = set(range(0, 5)) if local_slot < 5 else set(range(5, 10))
            enemy_team_slots = set(range(5, 10)) if local_slot < 5 else set(range(0, 5))
        else:
            my_team_slots = {slot for slot, sid in steam_ids.items() if sid == local_steam64}
            enemy_team_slots = set(steam_ids.keys()) - my_team_slots

        teammates = [steam_ids[s] for s in sorted(my_team_slots) if s in steam_ids]
        enemies = [steam_ids[s] for s in sorted(enemy_team_slots) if s in steam_ids]

        if not teammates or not enemies:
            return

        _current_match_id = match_id
        _analyzing = True

    logger.info(f"GSI: new match {match_id} – launching analysis thread.")
    thread = threading.Thread(
        target=_run_analysis,
        args=(match_id, local_steam64, teammates, enemies),
        daemon=True,
    )
    thread.start()


def _run_analysis(match_id: str, local_steam64: str, teammates: list, enemies: list):
    global _analyzing
    try:
        # Immediately notify clients
        _broadcast("match_detected", {
            "match_id": match_id,
            "n_teammates": len(teammates),
            "n_enemies": len(enemies),
        })

        # Map each steam64 id → team label
        team_map: dict[str, str] = {}
        for sid in teammates:
            team_map[str(sid)] = "teammate"
        for sid in enemies:
            team_map[str(sid)] = "enemy"

        all_ids = list(dict.fromkeys(teammates + enemies))

        if _client is None:
            logger.error("OpenDota client not initialised.")
            return

        # Fetch all players concurrently, broadcast each one as it arrives
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {executor.submit(_client.get_player, sid): str(sid)
                         for sid in all_ids}
            for future in as_completed(future_map):
                sid = future_map[future]
                try:
                    player_data = future.result()
                    if player_data is None:
                        continue
                    player_data["team"] = team_map.get(sid, "enemy")
                    player_data["is_you"] = (sid == str(local_steam64))
                    _broadcast("player_data", player_data)
                    logger.info(f"Broadcast player_data for {sid}")
                except Exception as exc:
                    logger.error(f"Failed to fetch player {sid}: {exc}")

        logger.info(f"Analysis complete for match {match_id}.")
    finally:
        with _gsi_lock:
            _analyzing = False


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _client

    parser = argparse.ArgumentParser(description="Dota 2 Match Analyzer – Web UI")
    parser.add_argument("--port", type=int, default=5000,
                        help="HTTP port (default: 5000)")
    parser.add_argument("--api-key", default=None,
                        help="OpenDota API key (optional, raises rate limit)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    _client = OpenDotaClient(api_key=args.api_key)

    print(f"Web UI → http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
