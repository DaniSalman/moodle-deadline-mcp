"""The FastMCP server over the real MCP protocol (in-memory transport)."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client

from conftest import SECRET_URL
from deadline_mcp.server import mcp

EXPECTED = {
    "get_upcoming_deadlines": "data",
    "add_deadline_to_calendar": "action",
    "sync_deadlines_to_calendar": "orchestration",
}


async def test_exactly_three_tools_one_per_category(env):
    async with Client(mcp) as client:
        tools = await client.list_tools()
    assert {t.name: t.meta["category"] for t in tools} == EXPECTED
    by_name = {t.name: t for t in tools}
    assert by_name["get_upcoming_deadlines"].annotations.read_only_hint is True
    for name in ("add_deadline_to_calendar", "sync_deadlines_to_calendar"):
        assert by_name[name].annotations.read_only_hint is False
        assert by_name[name].annotations.destructive_hint is False
        assert by_name[name].annotations.idempotent_hint is True


async def test_every_tool_has_description_typed_input_and_output_schema(env):
    async with Client(mcp) as client:
        tools = await client.list_tools()
    for tool in tools:
        assert len(tool.description) > 80
        assert tool.input_schema["type"] == "object"
        for name, prop in tool.input_schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"
        assert tool.output_schema and tool.output_schema["type"] == "object"
    add = next(t for t in tools if t.name == "add_deadline_to_calendar")
    assert set(add.input_schema["required"]) == {"title", "due_at"}


async def test_data_tool_returns_structured_deadlines(env):
    async with Client(mcp) as client:
        result = await client.call_tool("get_upcoming_deadlines", {"days_ahead": 14})
    assert result.is_error is False
    data = result.structured_content
    assert data["count"] == 5 and data["source"] == "file:moodle.ics"
    # The same data is also sent as JSON text, which is what the agent's model reads.
    assert json.loads(result.content[0].text)["count"] == 5


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("get_upcoming_deadlines", {"days_ahead": 0}, "greater than or equal to 1"),  # D7
        ("get_upcoming_deadlines", {"days_ahead": 61}, "less than or equal to 60"),  # D7
        ("add_deadline_to_calendar", {"title": "x", "due_at": "2026-12-01T10:00", "remind_hours_before": 700}, "less than or equal to 672"),  # A7
        ("add_deadline_to_calendar", {"due_at": "2026-12-01T10:00"}, "title"),
        ("add_deadline_to_calendar", {"title": "Old essay is due", "due_at": "2020-01-01T10:00"}, "already passed"),  # A4
        ("add_deadline_to_calendar", {"title": "Essay is due", "due_at": "next friday"}, "ISO 8601"),  # A3
    ],
)
async def test_bad_calls_come_back_as_clear_tool_errors(env, tool, arguments, expected):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    assert result.is_error is True
    assert expected in result.content[0].text
    assert not env["calendar"].exists()  # nothing was written


async def test_missing_feed_is_a_clear_tool_error(env, monkeypatch):
    monkeypatch.setenv("MOODLE_ICAL_URL", "")
    async with Client(mcp) as client:
        for tool in ("get_upcoming_deadlines", "sync_deadlines_to_calendar"):
            result = await client.call_tool(tool, {}, raise_on_error=False)
            assert result.is_error and "MOODLE_ICAL_URL" in result.content[0].text


async def test_google_not_signed_in_is_a_clear_tool_error(env, monkeypatch):
    monkeypatch.setenv("CALENDAR_BACKEND", "google")
    async with Client(mcp) as client:
        result = await client.call_tool("sync_deadlines_to_calendar", {"days_ahead": 14}, raise_on_error=False)
    assert result.is_error and "scripts/google_auth.py" in result.content[0].text


async def test_bad_setting_is_a_clear_tool_error(env, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus")
    async with Client(mcp) as client:
        result = await client.call_tool("get_upcoming_deadlines", {}, raise_on_error=False)
    assert result.is_error and "TIMEZONE" in result.content[0].text


async def test_action_and_orchestration_through_mcp(env):
    async with Client(mcp) as client:
        data = (await client.call_tool("get_upcoming_deadlines", {"days_ahead": 14})).structured_content
        first = data["deadlines"][0]
        added = await client.call_tool(
            "add_deadline_to_calendar",
            {"title": first["title"], "due_at": first["due_at"], "course": first["course"], "moodle_uid": first["uid"]},
        )
        assert added.structured_content["status"] == "created"
        report = (await client.call_tool("sync_deadlines_to_calendar", {"days_ahead": 14})).structured_content
    assert report["checked"] == 5
    assert len(report["created"]) == 4  # the first one was already added above
    assert len(report["already_in_calendar"]) == 1


async def test_settings_resource_hides_the_token(env, monkeypatch):
    monkeypatch.setenv("MOODLE_ICAL_URL", SECRET_URL)
    async with Client(mcp) as client:
        contents = await client.read_resource("deadline-assistant://settings")
    data = json.loads(contents[0].text)
    assert data["moodle_source"] == "lms.aub.edu.lb"
    assert data["tools"] == EXPECTED
    assert "SECRET0TOKEN0123" not in contents[0].text
