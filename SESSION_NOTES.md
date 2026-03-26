# Session Notes — 2026-03-26

## Problem solved
Dota 2 GSI does NOT send `allplayers` data to regular (non-spectator) players.
Only `player` (local player) and `map` (match ID, game state) are reliably available.

## Solution implemented
`match_finder.py` — two-strategy player discovery:
1. **STRATZ Live Match API** (preferred) — GraphQL query using match_id
   - Requires free STRATZ token from stratz.com/api (sign in with Steam)
   - Run: `python3 app.py --stratz-token YOUR_TOKEN`
2. **Console log parsing** (fallback) — parses Dota 2's console.log
   - Requires `-condebug` in Dota 2 Steam launch options
   - WSL2 path: `/mnt/d/SteamLibrary/steamapps/common/dota 2 beta/game/dota`

## Current status
- GSI is working: Dota 2 sends data to http://172.25.247.1:5000/gsi ✓
- match_id and local player steamid are received correctly ✓
- Player discovery needs STRATZ token OR console log to work ✗ (pending setup)

## Architecture
- app.py      → Flask web UI + SSE + GSI endpoint (port 5000)
- main.py     → CLI fallback (port 4000)
- gsi_server.py → GSI HTTP listener
- match_finder.py → STRATZ + console log player discovery
- opendota.py → OpenDota API (profiles, matches, heroes)
- display.py  → Rich CLI display

## WSL2 notes
- WSL2 IP: 172.25.247.1 (can change on restart — check with `ip addr show eth0`)
- Dota 2 installed at: D:\SteamLibrary\steamapps\common\dota 2 beta
- GSI cfg at: D:\SteamLibrary\steamapps\common\dota 2 beta\game\dota\cfg\gamestate_integration\
