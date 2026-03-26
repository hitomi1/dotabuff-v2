"""Rich terminal display for match analysis results."""

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console()


class Display:
    def banner(self):
        console.print(Panel.fit(
            "[bold cyan]Dota 2 Match Analyzer[/bold cyan]\n\n"
            "[dim]Listening on[/dim] [white]http://127.0.0.1:4000[/white]\n\n"
            "[bold]Setup (first time):[/bold]\n"
            "  Copy [yellow]gamestate_integration_dota2.cfg[/yellow] to:\n"
            "  [dim]Windows:[/dim]  [blue]C:\\Program Files (x86)\\Steam\\steamapps\\common"
            "\\dota 2 beta\\game\\dota\\cfg\\gamestate_integration\\[/blue]\n"
            "  [dim]Linux:  [/dim]  [blue]~/.steam/steam/steamapps/common/dota 2 beta/"
            "game/dota/cfg/gamestate_integration/[/blue]\n\n"
            "[dim]Waiting for a match to begin…[/dim]",
            border_style="cyan",
            title="[bold]v1.0[/bold]",
        ))

    def match_detected(self, n_teammates: int, n_enemies: int):
        console.print(
            f"\n[bold green]Match detected![/bold green] "
            f"Fetching data for [cyan]{n_teammates}[/cyan] teammates "
            f"and [red]{n_enemies}[/red] enemies…"
        )

    def results(self, local_steam64: str, player_data: dict,
                teammate_ids: list[str], enemy_ids: list[str]):
        console.rule("[bold cyan]YOUR TEAM[/bold cyan]", style="cyan")
        for sid in teammate_ids:
            data = player_data.get(str(sid))
            is_you = str(sid) == str(local_steam64)
            self._player(data, team="ally", is_you=is_you)

        console.rule("[bold red]ENEMY TEAM[/bold red]", style="red")
        for sid in enemy_ids:
            data = player_data.get(str(sid))
            self._player(data, team="enemy")

        console.rule(style="dim")

    # ------------------------------------------------------------------ #

    def _player(self, data: dict | None, team: str = "ally", is_you: bool = False):
        if not data:
            console.print("[dim]  Player data unavailable (private profile?)[/dim]\n")
            return

        color = "cyan" if team == "ally" else "red"
        profile = data["profile"]
        you_tag = " [bold yellow](YOU)[/bold yellow]" if is_you else ""

        console.print(
            f"\n  [{color}]{profile['name']}[/{color}]{you_tag}  "
            f"[dim]|[/dim]  {profile['rank']}  "
            f"[dim]|[/dim]  Role: [yellow]{data['main_role']}[/yellow]"
        )
        console.print(f"  [blue underline]{profile['dotabuff_url']}[/blue underline]")
        console.print(f"  [dim]{profile['opendota_url']}[/dim]")

        self._matches_table(data["matches"], color)
        self._heroes_table(data["top_heroes"], color)
        console.print()

    def _matches_table(self, matches: list[dict], color: str):
        if not matches:
            console.print("  [dim]No recent matches found.[/dim]")
            return

        t = Table(
            title=f"Last {len(matches)} Matches",
            box=box.SIMPLE_HEAVY,
            title_style=f"bold {color}",
            show_header=True,
            header_style="bold dim",
            padding=(0, 1),
        )
        t.add_column("Date",     width=10, style="dim")
        t.add_column("Hero",     width=18)
        t.add_column("Result",   width=6)
        t.add_column("K/D/A",    width=9)
        t.add_column("Duration", width=8, style="dim")
        t.add_column("Mode",     width=14, style="dim")

        for m in matches:
            res_style = "bold green" if m["result"] == "Win" else "bold red"
            kda = f"{m['kills']}/{m['deaths']}/{m['assists']}"
            t.add_row(
                m["date"],
                m["hero"],
                Text(m["result"], style=res_style),
                kda,
                m["duration"],
                m["game_mode"],
            )

        console.print(t)

    def _heroes_table(self, heroes: list[dict], color: str):
        if not heroes:
            return

        t = Table(
            title="Top 10 Heroes",
            box=box.SIMPLE_HEAVY,
            title_style=f"bold {color}",
            show_header=True,
            header_style="bold dim",
            padding=(0, 1),
        )
        t.add_column("#",       width=3,  style="dim")
        t.add_column("Hero",    width=18)
        t.add_column("Games",   width=6)
        t.add_column("Wins",    width=5)
        t.add_column("Win%",    width=7)

        for i, h in enumerate(heroes, 1):
            try:
                pct = float(h["winrate"].rstrip("%"))
                wr_style = "green" if pct >= 50 else "red"
            except ValueError:
                wr_style = "dim"
            t.add_row(
                str(i),
                h["hero"],
                str(h["games"]),
                str(h["wins"]),
                Text(h["winrate"], style=wr_style),
            )

        console.print(t)
