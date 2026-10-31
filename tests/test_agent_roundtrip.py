"""Full round trip: Agents SDK agent -> MCP client -> server subprocess (stdio) -> back.

The LLM is replaced by the offline keyword model, so no OpenAI key is needed,
but everything else is the real pipeline the chat client uses.
"""

from __future__ import annotations

import argparse
import json

from agents import RunHooks, Runner, set_tracing_disabled

from deadline_mcp.chat import build_agent, build_server
from deadline_mcp.offline_model import OfflineKeywordModel

set_tracing_disabled(True)


class Recorder(RunHooks):
    def __init__(self):
        self.tool_calls: list[tuple[str, dict]] = []

    async def on_tool_start(self, context, agent, tool):
        self.tool_calls.append((tool.name, json.loads(context.tool_arguments or "{}")))


async def _ask(server, questions: list[str]):
    resource = await server.read_resource("deadline-assistant://settings")
    agent = build_agent(server, OfflineKeywordModel(), json.loads(resource.contents[0].text))
    history, answers, recorder = [], [], Recorder()
    for question in questions:
        result = await Runner.run(agent, history + [{"role": "user", "content": question}], hooks=recorder)
        history = result.to_input_list()
        answers.append(str(result.final_output))
    return answers, history, recorder.tool_calls


async def test_question_goes_through_the_mcp_server_and_back(env):
    server = build_server(argparse.Namespace(stdio=True))
    async with server:
        tools = await server.list_tools()
        assert len(tools) == 3
        answers, history, calls = await _ask(server, ["What's due this week for EECE 503P?"])

    assert calls == [("get_upcoming_deadlines", {"days_ahead": 7, "course": "EECE503P"})]
    assert "C5 - Agent Tools" in answers[0]
    # The conversation the model sees next turn: question, tool call, tool result, answer.
    kinds = [item.get("type") or item.get("role") for item in history]
    assert kinds[:3] == ["user", "function_call", "function_call_output"]
    # The MCP result comes back to the model as JSON text inside the function_call_output item.
    tool_output = history[2]["output"]
    text = tool_output if isinstance(tool_output, str) else tool_output[0]["text"]
    assert json.loads(text)["count"] == 1


async def test_list_then_add_one_then_sync_all(env):
    server = build_server(argparse.Namespace(stdio=True))
    async with server:
        answers, _, calls = await _ask(
            server,
            [
                "what's due in the next 14 days?",
                "add the Lab 6 report to my calendar, remind me 6 hours before",
                "now sync all my deadlines to my calendar",
            ],
        )
    assert [name for name, _ in calls] == [
        "get_upcoming_deadlines",
        "add_deadline_to_calendar",
        "sync_deadlines_to_calendar",
    ]
    # The model reused the uid from the earlier Data result, so the sync saw it as a duplicate.
    assert calls[1][1]["moodle_uid"].endswith("@lms.aub.edu.lb")
    assert calls[1][1]["remind_hours_before"] == 6
    assert "4 added, 1 already in the calendar" in answers[2]
    stored = json.loads(env["calendar"].read_text())
    assert len(stored) == 5


async def test_tool_error_reaches_the_model_as_text(env, monkeypatch):
    monkeypatch.setenv("MOODLE_ICAL_URL", "")
    server = build_server(argparse.Namespace(stdio=True))
    async with server:
        answers, _, _ = await _ask(server, ["what's due this week?"])
    assert "problem" in answers[0] and "MOODLE_ICAL_URL" in answers[0]
