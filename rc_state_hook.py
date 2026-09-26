#!/usr/bin/env python3
"""Claude Code hook: record a remote-control session's turn state for local awareness.

Wired (guarded by $RC_REMOTE) into UserPromptSubmit / Notification / Stop /
SubagentStop / SessionStart / SessionEnd in ~/.claude/settings.json. The launcher
tags remote tmux sessions with RC_REMOTE, and the sessions the RC server spawns
inherit it, so this only fires for phone-driven sessions — never a local desk one.

Writes one JSON file per session under RC_STATE_DIR; rc_status.py reads them so a
local shell can tell when a remote turn is live on the shared working tree.
"""

import contextlib
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path

from rc_state import EVENT_STATE as STATE, STATE_DIR, STATE_TTL


def _state_name(sid: object) -> str:
    """A state-file stem for a session id. The id comes from the hook payload, so it is
    confined to one plain filename: a '/', '..' or NUL in it must never make the write
    (or SessionEnd's unlink) reach outside STATE_DIR. Anything but a plain token is hashed."""
    s = str(sid)
    if re.fullmatch(r"[A-Za-z0-9_+-]{1,128}", s):
        return s
    return "h-" + hashlib.sha256(s.encode(errors="replace")).hexdigest()[:32]


def _private_dir() -> Path | None:
    """STATE_DIR, created 0700 and verified: a real directory (not a symlink) that we own.
    Anything else — e.g. an RC_STATE_DIR under a shared /tmp that someone pre-created — gets
    no writes at all rather than a write through someone else's path."""
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(STATE_DIR)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        return None
    if st.st_mode & 0o077:  # an older, umask-made dir: tighten it
        with contextlib.suppress(OSError):
            os.chmod(STATE_DIR, 0o700)
    return STATE_DIR


def _prune(d: Path, now: float) -> None:
    """Bound the dir: a session that died without SessionEnd leaves its file behind, and
    every reader globs them all. A file older than STATE_TTL is already invisible to every
    reader, so dropping it changes nothing but the count. Run on SessionStart only."""
    for f in [*d.glob("*.json"), *d.glob(".*.tmp")]:
        with contextlib.suppress(OSError):
            if now - os.lstat(f).st_mtime > STATE_TTL:
                f.unlink()


def _write(d: Path, name: str, record: dict) -> None:
    """Atomic 0600 write: readers never see a torn file, and os.replace swaps the name
    itself, never following a symlink planted at it."""
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(record))
        os.replace(tmp, d / f"{name}.json")
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def main() -> None:
    """One hook event -> one state file. Runs inside EVERY remote session's turn, so it is
    quiet and cheap: it never writes to stdout (hook stdout on SessionStart and
    UserPromptSubmit becomes model context), and a payload it can't use is dropped."""
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name", "")
    sid = payload.get("session_id") or os.environ.get("RC_REMOTE", "unknown")
    name = _state_name(sid)

    if event == "SessionEnd":
        if (d := _private_dir()) is not None:
            with contextlib.suppress(OSError):
                (d / f"{name}.json").unlink(missing_ok=True)
        return

    # an event the vocabulary lacks must crash, not paint "working"
    state = STATE[event]
    # Notification covers two very different things: a BLOCKED turn (permission
    # request, a question) and the mere "Claude is waiting for your input" idle ping
    # after a turn ends. Under bypassPermissions the idle ping is nearly the only one
    # that fires, so it painted every finished session as amber "waiting". Idle ping
    # -> idle; anything else stays waiting.
    msg = payload.get("message")
    if (
        event == "Notification"
        and isinstance(msg, str)
        and "waiting for your input" in msg.lower()
    ):
        state = "idle"

    if (d := _private_dir()) is None:
        return
    now = time.time()
    if event == "SessionStart":
        _prune(d, now)
    cwd = payload.get("cwd")
    record = {
        "state": state,
        "project": os.environ.get("RC_PROJECT", ""),
        "cwd": cwd if isinstance(cwd, str) and cwd else os.getcwd(),
        "session_id": str(sid),
        "event": event,
        "ts": now,
    }
    _write(d, name, record)


HOOK_COMMAND = '[ -n "$RC_REMOTE" ] && python3 {script}; true'


SETTINGS = os.path.expanduser("~/.claude/settings.json")
EVENTS = (
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "SubagentStop",
    "SessionStart",
    "SessionEnd",
)


def install_hook(repo: str) -> str:
    """Register the state hook on every RC event in settings.json, idempotently — the merge
    install.sh used to embed, now here so uninstall's removal matches it by construction."""
    cmd = hook_command(repo)
    p = Path(SETTINGS)
    text = p.read_text() if p.exists() else ""
    d = (
        json.loads(text) if text.strip() else {}
    )  # 0-byte/whitespace is empty; bad JSON raises
    hooks = d.setdefault("hooks", {})
    added = False
    for ev in EVENTS:
        entries = hooks.setdefault(ev, [])
        if not any(
            h.get("command") == cmd for e in entries for h in e.get("hooks", [])
        ):
            entries.append({"hooks": [{"type": "command", "command": cmd}]})
            added = True
    _write_settings(p, d)
    return f"state hook {'registered' if added else 'already present'} in {p}"


def remove_hook(repo: str) -> str:
    """Remove the state hook from settings.json, leaving any other hooks intact."""
    cmd = hook_command(repo)
    p = Path(SETTINGS)
    if not p.exists():
        return f"no {p}"
    text = p.read_text()
    if not text.strip():
        return f"empty {p}; nothing to remove"
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return f"could not parse {p}; left unchanged"
    hooks = d.get("hooks", {})
    for (
        ev
    ) in EVENTS:  # only the events install_hook touches — the exact inverse, and an
        entries = hooks.get(
            ev
        )  # unrelated event with a non-list value can't abort the removal
        if not isinstance(entries, list):
            continue
        kept = [
            e
            for e in entries
            if not any(h.get("command") == cmd for h in e.get("hooks", []))
        ]
        if kept:
            hooks[ev] = kept
        else:
            hooks.pop(ev, None)
    _write_settings(p, d)
    return f"state hook removed from {p}"


def hook_command(repo: str) -> str:
    """The exact settings.json command install.sh registers and uninstall.sh removes —
    one source, so the two scripts can never disagree on what to match. The script path is
    shell-quoted: every claude session runs this line through sh, so a checkout under a
    path with a space or a metacharacter must stay one inert word. (A plain path quotes to
    itself, so hooks registered before the quoting still match for removal.)"""
    return HOOK_COMMAND.format(script=shlex.quote(f"{repo}/rc_state_hook.py"))


def _write_settings(p: Path, d: dict) -> None:
    """settings.json is read by every claude start: replace it atomically (fsync'd temp in
    the same dir, then os.replace — never a torn file), through a symlinked dotfile to its
    target, keeping the existing file's mode (0600 for a new one)."""
    target = Path(os.path.realpath(p))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".settings.json.rc")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(d, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def cli(argv: list[str]) -> None:
    """--hook-command/--install-hook/--remove-hook <repo> manage the settings.json hook (what
    install.sh/uninstall.sh call); anything else runs the hook itself (the event handler)."""
    match argv:
        case ["--hook-command", repo]:
            print(hook_command(repo))
        case ["--install-hook", repo]:
            print("  ", install_hook(repo))
        case ["--remove-hook", repo]:
            print("  ", remove_hook(repo))
        case _:
            main()


if __name__ == "__main__":
    cli(sys.argv[1:])
