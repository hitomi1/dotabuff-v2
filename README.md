# Dota 2 Match Analyzer

A background tool that automatically identifies every player in your Dota 2 match the moment hero selection begins. For each teammate and enemy it fetches their **Dotabuff page**, **last 20 matches**, **top 10 most played heroes**, and **main role** — all displayed in a color-coded terminal report.

---

## How it works

1. Dota 2's **Game State Integration (GSI)** sends live match data to a local HTTP server on port `4000`
2. The moment hero selection starts, the tool reads all 10 players' Steam IDs
3. It queries the **OpenDota API** concurrently to fetch each player's stats
4. Results are printed to your terminal — teammates in cyan, enemies in red

---

## Requirements

- Python 3.10+
- A Dota 2 installation (Steam)
- Internet connection

---

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/hitomi1/dotabuff-v2.git
cd dotabuff-v2
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Copy the GSI config into your Dota 2 folder (one-time setup)**

This tells Dota 2 to send game state data to the local server.

**Windows:**
```
C:\Program Files (x86)\Steam\steamapps\common\dota 2 beta\game\dota\cfg\gamestate_integration\
```

**Linux / macOS:**
```
~/.steam/steam/steamapps/common/dota 2 beta/game/dota/cfg/gamestate_integration/
```

Copy `gamestate_integration_dota2.cfg` from this repo into that folder, then **restart Dota 2**.

---

## Usage

```bash
python main.py
```

With an optional OpenDota API key (higher rate limits):

```bash
python main.py --api-key YOUR_OPENDOTA_KEY
```

Custom port:

```bash
python main.py --port 4000
```

The tool runs silently in the background. When you queue and enter hero selection, it will automatically print the full report.

---

## Example output

```
╭──────────────────────────────────────╮
│  Dota 2 Match Analyzer               │
│  Listening on http://127.0.0.1:4000  │
│  Waiting for a match to begin…       │
╰──────────────────────────────────────╯

Match detected! Fetching data for 5 teammates and 5 enemies…

──────────────────── YOUR TEAM ─────────────────────

  PlayerOne  |  Ancient 3  |  Role: Mid Lane
  https://www.dotabuff.com/players/123456789
  https://www.opendota.com/players/123456789

  Last 20 Matches
  Date        Hero           Result  K/D/A      Duration  Mode
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  2026-03-25  Shadow Fiend   Win     14/3/8     32:11     All Pick
  2026-03-24  Invoker        Loss    9/7/11     41:55     All Pick
  ...

  Top 10 Heroes
  #   Hero           Games  Wins  Win%
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  1   Shadow Fiend   142    81    57.0%
  2   Invoker        98     54    55.1%
  ...

──────────────────── ENEMY TEAM ────────────────────
  ...
```

---

## Configuration

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `4000` | Port the GSI server listens on |
| `--api-key` | *(none)* | OpenDota API key for higher rate limits |
| `--debug` | `false` | Enable verbose debug logging |

To get an OpenDota API key (free), sign in at [opendota.com](https://www.opendota.com) and go to **Settings → API Key**.

---

## Project structure

```
dotabuff-v2/
├── main.py                            # Entry point
├── gsi_server.py                      # GSI HTTP listener (Flask)
├── opendota.py                        # OpenDota API client
├── display.py                         # Terminal display (Rich)
├── gamestate_integration_dota2.cfg    # Dota 2 GSI config
└── requirements.txt
```

---

## Notes

- **Private profiles** — players who have set their Steam profile to private or have not consented to OpenDota tracking will show limited or no data. Their Dotabuff link is still provided.
- **Rate limits** — without an API key, OpenDota allows 60 requests/minute. Analyzing 10 players requires ~30 requests, well within the limit.
- The tool only analyzes each match once. If you dodge and re-queue into a new match, it detects the new match ID automatically.

---

## License

MIT
