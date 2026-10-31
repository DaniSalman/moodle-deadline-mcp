"""Call each MCP tool directly (no LLM) and show the raw MCP results, failure cases included.

    python scripts/try_tools.py                 # in-process server, read-only + failure cases
    python scripts/try_tools.py --write         # also add one deadline and run a sync (twice)
    python scripts/try_tools.py --url http://127.0.0.1:8765/mcp   # talk to a running server

Without --write nothing is written to any calendar: the failure cases are all
rejected before anything is created.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402

console = Console(highlight=False)


async def call(client: Client, name: str, arguments: dict, why: str) -> dict | None:
    console.rule(f"[bold]{name}[/]  {why}")
    console.print(f"arguments: {json.dumps(arguments)}")
    result = await client.call_tool(name, arguments, raise_on_error=False)
    text = result.content[0].text if result.content else ""
    if result.is_error:
        console.print(Panel(text, title="isError = true (clear error, nothing changed)", border_style="red"))
        return None
    console.print(Panel(json.dumps(result.structured_content, indent=2)[:2500], title="structured result", border_style="green"))
    return result.structured_content


async def main(args: argparse.Namespace) -> None:
    if args.url:
        target = args.url
    else:
        from deadline_mcp.server import mcp

        target = mcp
    async with Client(target) as client:
        tools = await client.list_tools()
        console.print(f"tools/list -> {[t.name for t in tools]}")
        settings = await client.read_resource("deadline-assistant://settings")
        console.print(f"resources/read settings -> {settings[0].text}")

        found = await call(client, "get_upcoming_deadlines", {"days_ahead": 14}, "Data tool")
        await call(client, "get_upcoming_deadlines", {"days_ahead": 0}, "failure: days_ahead out of range")
        await call(client, "add_deadline_to_calendar",
                   {"title": "Old essay is due", "due_at": "2020-01-01T23:59"}, "failure: deadline already passed")
        await call(client, "add_deadline_to_calendar",
                   {"title": "Essay is due", "due_at": "next friday"}, "failure: due_at is not ISO 8601")

        if args.write and found and found["deadlines"]:
            first = found["deadlines"][0]
            await call(client, "add_deadline_to_calendar",
                       {"title": first["title"], "due_at": first["due_at"], "course": first["course"],
                        "moodle_uid": first["uid"], "remind_hours_before": 24}, "Action tool")
            await call(client, "sync_deadlines_to_calendar", {"days_ahead": 14}, "Orchestration tool")
            await call(client, "sync_deadlines_to_calendar", {"days_ahead": 14}, "Orchestration again: nothing new")
        elif not args.write:
            console.print("[dim]Skipped the writing calls; pass --write to run the Action and Orchestration tools.[/]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", help="URL of a running server; default runs the server in-process")
    parser.add_argument("--write", action="store_true", help="also run the Action and Orchestration tools")
    asyncio.run(main(parser.parse_args()))
