"""Write samples/moodle_sample.ics: a Moodle-format export with deadlines a few days from now.

    python scripts/make_sample_feed.py

Then set MOODLE_ICAL_URL=samples/moodle_sample.ics (and CALENDAR_BACKEND=local)
in .env to rehearse without touching Moodle or Google.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deadline_mcp.sample_feed import build_sample_ics  # noqa: E402


def main() -> None:
    target = ROOT / "samples" / "moodle_sample.ics"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(build_sample_ics().encode("utf-8"))
    print(f"Wrote {target.relative_to(ROOT)} (dates are relative to today; re-run it on another day).")


if __name__ == "__main__":
    main()
