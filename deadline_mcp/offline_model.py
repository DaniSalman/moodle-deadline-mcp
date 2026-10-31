"""A keyword-based stand-in for the LLM, for running the agent without an OpenAI key.

It implements the Agents SDK `Model` interface, so everything else (Agent,
Runner, MCP client, MCP server, real tool results) runs exactly as it does
with a real model. It is deliberately simple: it maps a few phrasings to tool
calls and turns tool results into a short answer. Used by the tests and by
`python -m deadline_mcp.chat --offline` for rehearsing the demo for free.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

HELP_TEXT = (
    "(offline model) I understand: \"what's due this week?\", \"what's due for EECE503P?\", "
    "\"sync all my deadlines to my calendar\", and \"add <deadline> to my calendar, remind me 6 hours before\" "
    "after listing deadlines."
)

_COURSE = re.compile(r"\b([A-Za-z]{3,4})\s?(\d{3}[A-Za-z]?)\b")
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?)\b")
_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b")
_DAYS_BEFORE = re.compile(r"\b(\d+|a|one|two|three)\s*days?\s+before\b")
_NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}
_NOT_COURSES = {"next", "last", "past", "over", "with", "from", "into", "only", "due", "for", "the"}
_STOP = {"the", "my", "to", "add", "put", "in", "on", "for", "and", "is", "due", "calendar", "remind", "me", "before", "hours", "hour"}


class OfflineKeywordModel(Model):
    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ) -> ModelResponse:
        items: list[Any] = [{"role": "user", "content": input}] if isinstance(input, str) else list(input)
        if items and _get(items[-1], "type") == "function_call_output":
            return _message(_summarise(_output_text(items[-1])))
        available = {getattr(tool, "name", "") for tool in tools}
        choice = _choose_tool(_latest_user_text(items), items)
        if choice is None or choice[0] not in available:
            return _message(HELP_TEXT)
        return _tool_call(*choice)

    async def stream_response(self, *args, **kwargs):  # pragma: no cover - not used
        raise NotImplementedError("The offline model only supports Runner.run, not streaming.")
        yield


def _choose_tool(text: str, items: list[Any]) -> tuple[str, dict[str, Any]] | None:
    lowered = text.lower()
    args: dict[str, Any] = {}
    course = _course(text)
    hours = _hours(lowered)

    wants_write = re.search(r"\b(add|put|sync|remind|schedule)\b", lowered)
    if wants_write and ("sync" in lowered or re.search(r"\b(all|every|everything)\b", lowered)):
        args["days_ahead"] = _days(lowered)
        if hours is not None:
            args["remind_hours_before"] = hours
        if course:
            args["course"] = course
        return "sync_deadlines_to_calendar", args

    if wants_write:
        known = _match_known_deadline(lowered, items)
        if known:
            args = {"title": known["title"], "due_at": known["due_at"], "course": known["course"], "moodle_uid": known["uid"]}
        elif _ISO.search(text):
            title = re.split(r"\bdue\b|\bon\b|\bat\b", re.sub(r"^\s*(please\s+)?(add|put|schedule)\s+", "", text, flags=re.I))[0]
            args = {"title": title.strip(" ,.") or "Deadline", "due_at": _ISO.search(text).group(1).replace(" ", "T")}
        else:
            return None
        if hours is not None:
            args["remind_hours_before"] = hours
        return "add_deadline_to_calendar", args

    args["days_ahead"] = _days(lowered)
    if course:
        args["course"] = course
    return "get_upcoming_deadlines", args


def _course(text: str) -> str | None:
    for match in _COURSE.finditer(text):
        if match.group(1).lower() not in _NOT_COURSES:
            return (match.group(1) + match.group(2)).upper()
    return None


def _hours(text: str) -> float | None:
    if match := _HOURS.search(text):
        return float(match.group(1))
    if match := _DAYS_BEFORE.search(text):
        word = match.group(1)
        return 24.0 * (int(word) if word.isdigit() else _NUMBERS[word])
    return None


def _days(text: str) -> int:
    text = _DAYS_BEFORE.sub("", text)  # "remind me 1 day before" is not a window
    if match := re.search(r"(\d+)\s*days?", text):
        return max(1, min(60, int(match.group(1))))
    if match := re.search(r"\b(\d+|a|one|two|three|four)\s*weeks?", text):
        word = match.group(1)
        return min(60, 7 * (int(word) if word.isdigit() else _NUMBERS[word]))
    if "today" in text or "tonight" in text:
        return 1
    if "tomorrow" in text:
        return 2
    if "next week" in text:
        return 14
    if "week" in text:
        return 7
    if "month" in text:
        return 30
    return 14


def _match_known_deadline(text: str, items: list[Any]) -> dict[str, Any] | None:
    """Pick the deadline from an earlier get_upcoming_deadlines result that the user named."""
    words = {w for w in re.findall(r"[a-z0-9]+", text) if w not in _STOP}
    best, best_score = None, 0
    for item in items:
        if _get(item, "type") != "function_call_output":
            continue
        try:
            data = json.loads(_output_text(item))
        except (TypeError, ValueError):
            continue
        for deadline in data.get("deadlines", []) if isinstance(data, dict) else []:
            name_words = {w for w in re.findall(r"[a-z0-9]+", deadline["activity"].lower()) if w not in _STOP}
            score = len(words & name_words)
            if score > best_score:
                best, best_score = deadline, score
    return best


def _summarise(text: str) -> str:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return f"The tool reported a problem: {text}"
    if not isinstance(data, dict):
        return text
    if "deadlines" in data:
        if not data["deadlines"]:
            return "You have no deadlines in that period."
        lines = [
            f"- **{d['activity']}** ({d['course'] or 'no course'}): {_when(d['due_at'])}, in {_left(d['due_in_hours'])}"
            for d in data["deadlines"]
        ]
        return f"You have {data['count']} upcoming deadline(s):\n" + "\n".join(lines)
    if "checked" in data:
        lines = [f"- added: {r['summary']} (reminder {_when(r['reminder_at'])})" for r in data["created"]]
        lines += [f"- failed: {f['title']}: {f['error']}" for f in data["failed"]]
        return data["message"] + ("\n" + "\n".join(lines) if lines else "")
    if "status" in data:
        return data["message"]
    return text


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %d %b, %H:%M")


def _left(hours: float) -> str:
    return f"{hours:g} hours" if hours < 48 else f"{hours / 24:.1f} days"


def _latest_user_text(items: list[Any]) -> str:
    for item in reversed(items):
        if _get(item, "role") == "user":
            return _content_text(_get(item, "content"))
    return ""


def _output_text(item: Any) -> str:
    return _content_text(_get(item, "output"))


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text", ""))
    if isinstance(content, list):
        return "".join(_content_text(part) for part in content)
    return ""


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _tool_call(name: str, args: dict[str, Any]) -> ModelResponse:
    call = ResponseFunctionToolCall(
        arguments=json.dumps(args),
        call_id=f"call_{uuid.uuid4().hex[:12]}",
        name=name,
        type="function_call",
        id=f"fc_{uuid.uuid4().hex[:12]}",
        status="completed",
    )
    return ModelResponse(output=[call], usage=Usage(requests=1), response_id=None)


def _message(text: str) -> ModelResponse:
    message = ResponseOutputMessage(
        id=f"msg_{uuid.uuid4().hex[:12]}",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )
    return ModelResponse(output=[message], usage=Usage(requests=1), response_id=None)
