"""Shared state vocabulary for the remote-control awareness system.

The session cluster (rc_sessions.py), the status reader (rc_status.py), and the Claude Code
hook (rc_state_hook.py) all import these, so the state names, the rank, the directory, and
the TTL can't drift apart between them. Before this was extracted, a state renamed in the
hook's event map simply fell out of the launcher's rank filter with no error.
"""

import json
import math
import os
import stat
import time
from pathlib import Path
from types import MappingProxyType

STATE_DIR = Path(os.environ.get("RC_STATE_DIR", Path.home() / ".cache" / "rc-state"))
STATE_TTL = float(os.environ.get("RC_STATE_TTL", "3600"))

# turn state -> priority; these keys are the entire state vocabulary
RANK = MappingProxyType({"working": 3, "waiting": 2, "idle": 1})

# Claude Code hook event -> turn state (every value must be a RANK key)
EVENT_STATE = MappingProxyType(
    {
        "UserPromptSubmit": "working",
        "Notification": "waiting",
        "Stop": "idle",
        "SubagentStop": "idle",
        "SessionStart": "idle",
    }
)


MAX_STATE_BYTES = 64 * 1024  # a real state file is ~200 bytes


def _load(f: Path) -> dict | None:
    """One state file as a dict, or None. Opened non-blocking and no-follow, and only a
    small regular file is read: the launcher's /status poll and the shell prompt both land
    here, so a FIFO, a symlink or a huge file dropped in the dir must be skipped, never
    hang or stall them. ValueError covers bad JSON and non-UTF8 alike."""
    try:
        fd = os.open(f, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            raw = fh.read(MAX_STATE_BYTES + 1)
            d = json.loads(raw) if len(raw) <= MAX_STATE_BYTES else None
        except (OSError, ValueError):
            return None
    return d if isinstance(d, dict) else None


def _fresh(d: dict, now: float) -> bool:
    """A known state, a real finite timestamp within STATE_TTL, and string-typed
    project/cwd — the shape every reader indexes into without further checks."""
    ts, st = d.get("ts", 0), d.get("state")
    return (
        isinstance(st, str)  # an unhashable state would TypeError the RANK lookup
        and st in RANK
        and isinstance(ts, (int, float))
        and not isinstance(ts, bool)
        and math.isfinite(ts)
        and now - ts <= STATE_TTL
        and all(isinstance(d.get(k, ""), str) for k in ("project", "cwd"))
    )


def valid_states(state_dir: Path, now: float | None = None) -> list[dict]:
    """State files in state_dir that parse, are fresh (within STATE_TTL), and carry a known
    state — the single read filter behind the launcher's session_states() and rc_status's
    live(), so the on-disk schema and the staleness rule live in one place, not two. A corrupt,
    mistyped or unreadable file is skipped, never raised (rc_status runs in the zsh RPROMPT,
    and a raise here would 500 the launcher's /status). state_dir is a parameter because each
    caller redirects its own STATE_DIR (tests, and the env override resolved at import in
    each module)."""
    now = time.time() if now is None else now
    return [
        d
        for f in state_dir.glob("*.json")
        if (d := _load(f)) is not None and _fresh(d, now)
    ]
