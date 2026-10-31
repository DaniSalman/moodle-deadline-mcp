"""Terminal chat: the MCP host and client, built with the OpenAI Agents SDK.

    python -m deadline_mcp.chat              # connect to the HTTP server (start it first)
    python -m deadline_mcp.chat --stdio      # launch the server as a subprocess instead
    python -m deadline_mcp.chat --offline    # no OpenAI key: keyword stand-in model
    python -m deadline_mcp.chat --ask "what's due this week?"   # one question, then exit

Every turn prints the round trip: what the model asked for, the tools/call the
MCP client sent, what the server answered, and the final reply.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from agents import Agent, ModelSettings, RunHooks, Runner, set_tracing_disabled
from agents.exceptions import AgentsException
from agents.mcp import MCPServer, MCPServerStdio, MCPServerStreamableHttp
from agents.models import get_default_model
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import DEFAULT_PORT, PROJECT_ROOT
from .offline_model import OfflineKeywordModel

console = Console(highlight=False)
SETTINGS_URI = "deadline-assistant://settings"


# --------------------------------------------------------------------------- agent


def make_instructions(server_settings: dict[str, Any]):
    """Instructions are rebuilt every turn so the model always knows today's date."""
    zone = ZoneInfo(server_settings.get("timezone", "Asia/Beirut"))
    hours = server_settings.get("default_remind_hours", 24)
    calendar = server_settings.get("calendar", "Google Calendar")

    def instructions(context, agent) -> str:
        now = datetime.now(zone)
        return f"""You are a deadline assistant for a university student at AUB. You tell them what is due on Moodle and put deadlines in their calendar with a reminder.

Current date and time: {now:%A %d %B %Y, %H:%M} ({zone.key}).
Default reminder: {hours:g} hours before a deadline, unless the student asks for a different time.
Calendar being written: {calendar}.

How to use your tools:
- A question about what is due -> get_upcoming_deadlines. Choose days_ahead from the question (today = 1, this week = 7, otherwise 14). Only report deadlines the tool returned; never invent one.
- "Add / put / sync all my deadlines" -> sync_deadlines_to_calendar. One call does the whole job.
- One specific deadline -> add_deadline_to_calendar. If the student names a Moodle deadline, first get it with get_upcoming_deadlines (unless it is already in this conversation) and pass its exact title, due_at, course and uid (as moodle_uid), so it is never added twice.
- A tool result with status "updated" means the event already existed and was changed (moved deadline or new reminder); say what changed.
- Only write to the calendar when the student asks for it. If they only ask a question, just answer it.
- If a tool returns an error, say in plain words what went wrong and what the student can do. Do not retry with made-up values.

Style: short and clear. List deadlines soonest first with course, title, day and time, and how much time is left. After writing to the calendar, say how many events were added, already there, or failed, and when the reminders will fire."""

    return instructions


def build_agent(server: MCPServer, model: Any, server_settings: dict[str, Any]) -> Agent:
    return Agent(
        name="Moodle Deadline Assistant",
        instructions=make_instructions(server_settings),
        mcp_servers=[server],  # tools are discovered from the server with tools/list
        model=model,
        # One tool call at a time: two parallel writes could race on the duplicate check.
        model_settings=ModelSettings(parallel_tool_calls=False),
    )


def build_server(args: argparse.Namespace) -> MCPServer:
    if args.stdio:
        return MCPServerStdio(
            name="moodle-deadlines (stdio)",
            params={
                "command": sys.executable,
                "args": ["-m", "deadline_mcp.server", "--stdio"],
                "cwd": str(PROJECT_ROOT),
                "env": dict(os.environ),
            },
            cache_tools_list=True,
            client_session_timeout_seconds=120,
            failure_error_function=connection_failure,
        )
    return MCPServerStreamableHttp(
        name="moodle-deadlines (http)",
        params={"url": args.url, "timeout": 30},
        cache_tools_list=True,
        client_session_timeout_seconds=120,
        failure_error_function=connection_failure,
    )


def connection_failure(context, error: Exception) -> str:
    """Called when a tool call fails in the MCP layer instead of returning a result (e.g. the server stopped)."""
    detail = f"{type(error).__name__}: {str(error)[:200]}"
    console.print(
        f"[red]The MCP call failed ({escape(detail)}).[/] "
        "If the server stopped, restart it with: python -m deadline_mcp.server"
    )
    return (
        f"The tool call failed before the tool could answer ({detail}). The MCP server may have stopped; "
        "tell the student to check it and restart it with `python -m deadline_mcp.server` if needed."
    )


# --------------------------------------------------------------------------- round-trip printing


class RoundTripPrinter(RunHooks):
    """Prints each hop of a turn as it happens."""

    def __init__(self, model_label: str, tool_count: int):
        self.model_label = model_label
        self.tool_count = tool_count
        self.llm_calls = 0

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        self.llm_calls += 1
        console.print(
            f"[magenta]* LLM call {self.llm_calls}[/] -> {self.model_label}: instructions + "
            f"{len(input_items)} conversation item(s) + {self.tool_count} tool schema(s)"
        )

    async def on_llm_end(self, context, agent, response) -> None:
        for item in response.output:
            kind = getattr(item, "type", "")
            if kind == "function_call":
                console.print(
                    f"[magenta]  model asked for a tool call:[/] [bold]{escape(item.name)}[/]({escape(item.arguments)})"
                )
            elif kind == "message":
                console.print("[magenta]  model wrote the final answer[/]")
            elif kind == "reasoning":
                console.print("[dim]  model reasoned privately first[/]")
        usage = response.usage
        if usage.input_tokens or usage.output_tokens:
            console.print(f"[dim]  tokens: {usage.input_tokens} in / {usage.output_tokens} out[/]")

    async def on_tool_start(self, context, agent, tool) -> None:
        arguments = getattr(context, "tool_arguments", "") or ""
        console.print(f"[cyan]> MCP client -> server:[/] tools/call [bold]{escape(tool.name)}[/] {escape(arguments)}")

    async def on_tool_end(self, context, agent, tool, result) -> None:
        console.print(f"[green]< server -> MCP client:[/] {_preview(result)}")


def _text_of(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("text", result))
    if isinstance(result, list):
        return "".join(_text_of(part) for part in result)
    return str(getattr(result, "text", result))


def _preview(result: Any) -> str:
    text = _text_of(result)
    try:
        data = json.loads(text)
    except ValueError:
        return f"[red]tool error:[/] {escape(text)}"
    if isinstance(data, dict) and "deadlines" in data:
        names = escape(", ".join(d["title"] for d in data["deadlines"][:4]))
        more = "..." if data["count"] > 4 else ""
        return f"DeadlineList with {data['count']} deadline(s) [dim]({names}{more})[/]"
    if isinstance(data, dict) and "checked" in data:
        return f"SyncReport: {escape(data['message'])}"
    if isinstance(data, dict) and "status" in data:
        return f"CalendarAddResult: {data['status']} - {escape(data['summary'])}"
    return escape(text[:200])


# --------------------------------------------------------------------------- chat loop


async def run_turn(agent: Agent, history: list[Any], text: str, printer: RoundTripPrinter) -> None:
    printer.llm_calls = 0
    try:
        result = await Runner.run(agent, history + [{"role": "user", "content": text}], hooks=printer, max_turns=8)
    except AgentsException as exc:
        console.print(f"[red]The agent stopped:[/] {escape(str(exc))}")
        return
    except Exception as exc:  # network / API errors from the model provider
        console.print(f"[red]Model request failed ({type(exc).__name__}):[/] {escape(str(exc))}")
        return
    history[:] = result.to_input_list()
    console.print(Panel(Markdown(str(result.final_output)), title="assistant", border_style="green"))
    usage = result.context_wrapper.usage
    console.print(
        f"[dim]{usage.requests} model call(s) this turn, {usage.input_tokens} input + {usage.output_tokens} output tokens. "
        f"Conversation now has {len(history)} items (/context to see them).[/]"
    )


def show_tools(tools: list[Any]) -> None:
    table = Table(title="Tools discovered with tools/list", show_lines=True)
    table.add_column("tool")
    table.add_column("category")
    table.add_column("read-only")
    table.add_column("what the model is told")
    for tool in tools:
        meta = getattr(tool, "meta", None) or {}
        annotations = getattr(tool, "annotations", None)
        read_only = getattr(annotations, "read_only_hint", None) if annotations else None
        table.add_row(
            Text(tool.name),
            Text(str(meta.get("category", "?"))),
            "yes" if read_only else "no",
            Text((tool.description or "").split("\n")[0]),
        )
    console.print(table)


def show_schemas(tools: list[Any]) -> None:
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", {})
        console.print(Panel(Text(json.dumps(schema, indent=2)), title=escape(f"{tool.name} input schema"), border_style="cyan"))


def show_context(history: list[Any]) -> None:
    table = Table(title="What the model receives next turn (besides instructions and tool schemas)")
    table.add_column("#", justify="right")
    table.add_column("item")
    table.add_column("content")
    for index, item in enumerate(history, start=1):
        kind = item.get("type") or item.get("role", "?")
        if kind in ("message", "assistant", "user"):
            kind = f"message ({item.get('role', '?')})"
            body = _text_of(item.get("content"))
        elif kind == "function_call":
            body = f"{item.get('name')}({item.get('arguments')})"
        elif kind == "function_call_output":
            body = _text_of(item.get("output"))
        else:
            body = ""
        table.add_row(str(index), Text(kind), Text((body[:110] + "...") if len(body) > 110 else body))
    console.print(table)


async def chat(args: argparse.Namespace) -> int:
    if args.offline:
        set_tracing_disabled(True)
        model: Any = OfflineKeywordModel()
        model_label = "offline keyword model (no API call)"
    else:
        if not os.getenv("OPENAI_API_KEY"):
            console.print("[red]OPENAI_API_KEY is not set.[/] Put it in .env, or run with --offline to try the pipeline without a model.")
            return 1
        model = os.getenv("OPENAI_MODEL", "").strip() or get_default_model()
        model_label = model
        if args.no_trace:
            set_tracing_disabled(True)

    server = build_server(args)
    where = "a subprocess over stdio" if args.stdio else args.url
    try:
        await server.connect()
    except Exception as exc:
        console.print(f"[red]Could not connect to the MCP server at {escape(where)} ({type(exc).__name__}).[/]")
        if not args.stdio:
            console.print("Start it first in another terminal:  python -m deadline_mcp.server   (or use --stdio)")
        return 1

    try:
        tools = await server.list_tools()
        resource = await server.read_resource(SETTINGS_URI)
        server_settings = json.loads(resource.contents[0].text)
        agent = build_agent(server, model, server_settings)
        printer = RoundTripPrinter(model_label, len(tools))

        console.print(Panel.fit(
            f"[bold]Moodle Deadline Assistant[/]  (MCP host: OpenAI Agents SDK)\n"
            f"MCP server: {server.name} at {where}\n"
            f"Model: {model_label}\n"
            f"Settings resource: tz {server_settings['timezone']}, reminder {server_settings['default_remind_hours']:g} h, "
            f"calendar {server_settings['calendar']}, Moodle {server_settings['moodle_source']}",
            border_style="blue",
        ))
        show_tools(tools)

        history: list[Any] = []
        if args.ask:
            console.print(f"[bold]you >[/] {escape(args.ask)}")
            await run_turn(agent, history, args.ask, printer)
            return 0

        console.print("[dim]Commands: /tools  /schemas  /context  /reset  /quit[/]")
        while True:
            try:
                text = (await asyncio.to_thread(console.input, "[bold]you >[/] ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/tools":
                show_tools(tools)
            elif text == "/schemas":
                show_schemas(tools)
            elif text == "/context":
                show_context(history)
            elif text == "/reset":
                history.clear()
                console.print("[dim]Conversation cleared.[/]")
            else:
                await run_turn(agent, history, text, printer)
        return 0
    finally:
        try:
            await server.cleanup()
        except Exception:
            console.print("[dim]The connection to the MCP server was already closed.[/]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the Moodle Deadline Assistant")
    parser.add_argument("--url", default=f"http://127.0.0.1:{DEFAULT_PORT}/mcp", help="MCP server URL (HTTP mode)")
    parser.add_argument("--stdio", action="store_true", help="launch the MCP server as a subprocess over stdio")
    parser.add_argument("--offline", action="store_true", help="use the keyword stand-in model (no OpenAI key needed)")
    parser.add_argument("--no-trace", action="store_true", help="do not upload traces to the OpenAI dashboard")
    parser.add_argument("--ask", help="ask one question and exit")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(chat(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
