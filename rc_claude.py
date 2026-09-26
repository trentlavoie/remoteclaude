"""Shared claude(1) contract: the CLAUDE binary path, `claude auth status`, the trust flag in
claude's own ~/.claude.json, and MT.

auth_status() is read by both rc_sessions (the login badge) and rc_healthcheck (the
watchdog) so the external JSON contract and the default binary path have one definition
instead of two that drift; MT is the timezone every timestamp in the tree prints in.
This module depends on nothing in the launcher tree, so rc_healthcheck stays independent
of it — the watchdog still runs if the launcher is broken.
"""

import contextlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

CLAUDE = os.path.expanduser(os.environ.get("RC_CLAUDE_BIN", "~/.local/bin/claude"))
MT = ZoneInfo("America/Denver")


def auth_status(timeout: float = 15) -> tuple[str, str]:
    """('ok' | 'loggedout' | 'unknown', detail): the login state, with detail = the email when
    logged in, the error text when unknown, else ''. Spawns a process, so a caller that polls
    should cache the result."""
    try:
        out = subprocess.run(
            [CLAUDE, "auth", "status"], capture_output=True, text=True, timeout=timeout
        ).stdout
        d = json.loads(out)
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as err:
        return "unknown", str(err)
    return ("ok", d.get("email", "")) if d.get("loggedIn") else ("loggedout", "")


_TRUST_LOCK = threading.Lock()  # the launcher's own concurrent launches, one at a time


def _stamp(path: str) -> tuple[int, int, int]:
    st = os.stat(path)
    return st.st_ino, st.st_size, st.st_mtime_ns


def trust_dir(config: str, key: str) -> str | None:
    """Mark dir `key` trusted (projects[key].hasTrustDialogAccepted) in claude's config file.
    None when done or already trusted or there is no config yet; else why it was skipped.

    ~/.claude.json is claude's own hot file — every running session rewrites it — so: write
    only when the flag is missing; resolve a symlinked dotfile and write its target; a unique
    temp beside it (mkstemp: 0600, the mode this OAuth-bearing file must keep), fsync'd, then
    os.replace, so it is never torn; and optimistic concurrency — if the file changed between
    our read and the replace, a live claude wrote it, so re-read and re-merge rather than
    clobber that write. An unexpected shape is reported, never "repaired"."""
    path = os.path.realpath(config)
    with _TRUST_LOCK:
        for _ in range(3):
            try:
                seen = _stamp(path)
                d = json.loads(Path(path).read_bytes())
            except FileNotFoundError:
                return None
            except (OSError, ValueError) as e:  # unreadable, torn, non-UTF8
                return f"skip: {e}"
            projects = d.setdefault("projects", {}) if isinstance(d, dict) else None
            entry = projects.setdefault(key, {}) if isinstance(projects, dict) else None
            if not isinstance(entry, dict):
                return f"skip: unexpected shape in {config}"
            if entry.get("hasTrustDialogAccepted"):
                return None
            entry.setdefault("allowedTools", [])
            entry.setdefault("mcpServers", {})
            entry["hasTrustDialogAccepted"] = True
            prefix = f".{os.path.basename(path)}.rc"
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=prefix)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(d, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                if _stamp(path) == seen:
                    os.replace(tmp, path)
                    return None
                os.unlink(tmp)  # a concurrent writer: go round again on its version
            except OSError as e:  # disk full / unwritable: don't orphan the temp
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                return f"skip write: {e}"
    return f"skip: {config} kept changing under us"
