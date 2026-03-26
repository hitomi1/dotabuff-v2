#!/usr/bin/env python3
"""
Dota 2 Match Analyzer
=====================
Runs in the background and, as soon as a match begins, fetches every
player's Dotabuff page, last 20 matches, top 10 heroes, and main role.

Setup (one-time):
    Copy gamestate_integration_dota2.cfg  into your Dota 2 GSI folder:
      Windows: C:\\Program Files (x86)\\Steam\\steamapps\\common\\dota 2 beta\\game\\dota\\cfg\\gamestate_integration\\
      Linux:   ~/.steam/steam/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/
    Then restart Dota 2.

Usage:
    pip install -r requirements.txt
    python main.py
    python main.py --api-key YOUR_OPENDOTA_KEY   # optional, raises rate limits
"""

import argparse
import logging
import sys

from display import Display
from gsi_server import GSIServer
from opendota import OpenDotaClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Dota 2 Match Analyzer")
    parser.add_argument("--port",    type=int, default=4000,
                        help="GSI listener port (default: 4000)")
    parser.add_argument("--api-key", default=None,
                        help="OpenDota API key (optional, increases rate limit)")
    parser.add_argument("--debug",   action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    display = Display()
    client  = OpenDotaClient(api_key=args.api_key)

    def on_match_found(local_steam64: str, teammates: list, enemies: list):
        display.match_detected(len(teammates), len(enemies))

        all_ids   = list(dict.fromkeys(teammates + enemies))   # preserve order, deduplicate
        player_data = client.get_all_players(all_ids)

        display.results(local_steam64, player_data, teammates, enemies)

    server = GSIServer(port=args.port, on_match_found=on_match_found)

    display.banner()
    logger.info(f"GSI server listening on http://127.0.0.1:{args.port}/")

    try:
        server.run()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        sys.exit(0)


if __name__ == "__main__":
    main()
