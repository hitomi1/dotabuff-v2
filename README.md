# Dota 2 Match Analyzer

A local web app that automatically identifies all 10 players the moment your match begins — showing each player's rank, ranked win rate, top heroes, and recent matches in a clean browser UI, inspired by OP.GG and Mobalytics.

No manual input. No browser extensions. Just start the app, play Dota, and your browser updates itself.

![screenshot placeholder]

---

## Features

- Detects your match automatically via Dota 2 **Game State Integration (GSI)**
- Identifies all **10 players** (teammates + enemies) using multiple real-time strategies
- For each player shows:
  - Steam avatar, name, and current rank
  - **Ranked win rate** with games played
  - Recent match win rate (last 20 games) with visual bar
  - Top 10 most played heroes with win %
  - Last 20 matches with hero, K/D/A, duration, result
  - Links to Dotabuff and OpenDota
- Live streaming UI — cards appear one by one as data arrives
- Works on **Windows + WSL2**, Linux, and macOS

---

## How it works

1. Dota 2 sends live game state to a local HTTP server via GSI
2. When a match is detected, the tool finds all 10 player Steam IDs using:
   - **Steam GetRealtimeStats** — real-time lookup during the match (fastest, needs Steam Web API key)
   - **STRATZ GraphQL** — post-match lookup (~2 min after the game ends)
   - **OpenDota polling** — fallback for up to 15 min after match ends
3. Player stats are fetched concurrently from the OpenDota API
4. Results stream to your browser in real time via Server-Sent Events (SSE)

---

## Requirements

- Python 3.10+
- A Dota 2 installation (Steam)
- A free [STRATZ API token](https://stratz.com/api) (recommended)
- A free [Steam Web API key](https://steamcommunity.com/dev/apikey) (recommended, for real-time detection)

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/yourusername/dotabuff-v2.git
cd dotabuff-v2
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Copy the GSI config into your Dota 2 folder (one-time)**

This tells Dota 2 to send game state data to the local server.

Windows:
```
C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game\dota\cfg\gamestate_integration\
```

Linux / macOS:
```
~/.steam/steam/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/
```

Copy `gamestate_integration_dota2.cfg` from this repo into that folder, then **restart Dota 2**.

**4. (Recommended) Enable real-time player detection**

Add `-condebug` to your Dota 2 launch options in Steam. This lets the app read the game server ID from Dota's console log and identify all 10 players the moment the match starts — without waiting for post-match indexing.

**5. Create a `.env` file with your API keys**

```
STRATZ_TOKEN=your_stratz_token_here
STEAM_API_KEY=your_steam_api_key_here
```

Get a free STRATZ token at [stratz.com/api](https://stratz.com/api).
Get a free Steam Web API key at [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey).

---

## Usage

```bash
python app.py
```

Then open **http://localhost:5000** in your browser.

When you enter hero selection, the page updates automatically. No refresh needed.

**Test with a specific match ID (no game required):**

```
http://localhost:5000/test/<match_id>
```

---

## Configuration

All options can be set in `.env` or passed as CLI flags:

| Flag | Env var | Description |
|------|---------|-------------|
| `--stratz-token` | `STRATZ_TOKEN` | STRATZ token for post-match lookups |
| `--steam-api-key` | `STEAM_API_KEY` | Steam Web API key for real-time detection |
| `--api-key` | `OPENDOTA_API_KEY` | OpenDota API key (optional, raises rate limits) |
| `--port` | — | HTTP port (default: 5000) |
| `--dota-path` | — | Path to Dota 2 `game/dota` directory (auto-detected) |
| `--debug` | — | Enable verbose logging |

---

## Project structure

```
dotabuff-v2/
├── app.py                             # Flask web server + SSE streaming
├── match_finder.py                    # Multi-strategy player discovery
├── opendota.py                        # OpenDota API client
├── gsi_server.py                      # GSI HTTP listener (CLI version)
├── main.py                            # CLI entry point (terminal output)
├── display.py                         # Terminal display (Rich)
├── static/
│   ├── app.js                         # Client-side SSE + card rendering
│   └── style.css                      # Dark theme UI
├── templates/
│   └── index.html                     # Main page
├── gamestate_integration_dota2.cfg    # Dota 2 GSI config
├── .env                               # API keys (not committed)
└── requirements.txt
```

---

## Notes

- **Private profiles** — players with private Steam profiles will show limited data. Their Dotabuff/OpenDota links are still provided.
- **WSL2 users** — the GSI config uses the WSL2 host IP (`172.x.x.x`). If your WSL2 IP is different, update the `uri` field in `gamestate_integration_dota2.cfg`.
- **Rate limits** — without an OpenDota API key, the limit is 60 req/min. Analyzing 10 players uses ~40 requests, well within the free tier.
- The app detects each match only once and ignores repeated GSI pings for the same match ID.

---

## License

MIT
