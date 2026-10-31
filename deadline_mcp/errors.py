"""Expected failures. The server turns each one into a clear MCP tool error."""


class AssistantError(Exception):
    """Base class: a predictable problem the user can act on."""


class ConfigError(AssistantError):
    """A setting in .env is missing or invalid."""


class FeedError(AssistantError):
    """The Moodle calendar feed could not be read."""


class CalendarError(AssistantError):
    """Google Calendar (or the local stand-in) refused or could not be reached."""


class InputError(AssistantError):
    """A tool argument is well-typed but not usable (e.g. a deadline in the past)."""
