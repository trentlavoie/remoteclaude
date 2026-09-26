"""Phone-editable launcher toggles, persisted beside the token and remembered across
restarts. The launch path reads resume()/spawn() rather than the RESUME/SPAWN env defaults
directly, so a switch flipped from the phone takes effect with no reload; a missing or torn
settings file falls back to those defaults and never breaks a launch. Caveat: once a toggle
is written, its off state is the plain default (continue / same-dir), so the non-default env
values RC_RESUME=off and RC_SPAWN=session|worktree become unreachable from that point."""

import contextlib
import json
import os
import tempfile
import threading
from pathlib import Path

# serialize the read-merge-write so two /settings taps can't race
_LOCK = threading.Lock()

SPAWN = os.environ.get("RC_SPAWN", "same-dir")  # same-dir | worktree | session
RESUME = os.environ.get("RC_RESUME", "continue")  # continue | fork | off
# Pinned model for every launched session, passed as --model on fresh AND resume launches.
# Resume otherwise keeps the thread's last model (verified 2026-09-19: `--continue --model X`
# overrides it), so without this a resumed thread silently drifts off Sonnet 5.
MODEL = os.environ.get("RC_MODEL", "claude-sonnet-5")
# The models a /launch?model= request may pick. Short aliases map to full IDs; only these
# full IDs ever reach the claude argv (a request value is never passed through as free text).
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "fable": "claude-fable-5-1",
    "haiku": "claude-haiku-4-5-20251001",
}
MODELS = frozenset(MODEL_ALIASES.values())

# Opt-in permission-mode pin for every launched session (RC_PERMISSION_MODE). Unset = pass no
# flag, so a session follows settings.json exactly as a desk claude does (upstream behavior).
# bypassPermissions is deliberately NOT accepted: a phone-driven session with no approvals is
# never something an env typo or copy-paste should produce. Any other unrecognized value
# fails CLOSED to "default" (ask for everything) rather than to settings.json's default.
PERMISSION_MODES = frozenset({"default", "acceptEdits", "plan", "auto", "dontAsk"})


def _permission_mode(raw: str) -> str:
    return raw if not raw or raw in PERMISSION_MODES else "default"


PERMISSION_MODE = _permission_mode(os.environ.get("RC_PERMISSION_MODE", ""))


def permission_args() -> list[str]:
    """`--permission-mode <m>` for the launch argv when a mode is pinned, else nothing."""
    return ["--permission-mode", PERMISSION_MODE] if PERMISSION_MODE else []


def resolve_model(value: str) -> str | None:
    """A /launch?model= value -> a full allowlisted model ID, or None if unrecognized.
    Accepts a full ID or a short alias; empty falls back to the RC_MODEL default (which is
    operator-set, so it is trusted even if it is not in the request allowlist)."""
    if not value:
        return MODEL
    return value if value in MODELS else MODEL_ALIASES.get(value)


SETTINGS_FILE = Path(
    os.path.expanduser(
        os.environ.get(
            "RC_LAUNCHER_SETTINGS_FILE", "~/.config/rc-launcher/settings.json"
        )
    )
)


def _settings() -> dict:
    """The persisted toggles, kept to real bools. Missing/unreadable/non-UTF8/torn/malformed
    all read as {} (ValueError covers JSONDecodeError and read_text's UnicodeDecodeError), and
    non-bool values are dropped, so nothing on disk can break a launch or flip a toggle."""
    with contextlib.suppress(OSError, ValueError):
        if isinstance(data := json.loads(SETTINGS_FILE.read_text()), dict):
            return {k: v for k, v in data.items() if isinstance(v, bool)}
    return {}


def _toggled(key: str, on: str, off: str, default: str) -> str:
    s = _settings()
    return (on if s[key] else off) if key in s else default


def resume() -> str:  # "fork" branches the thread on resume; else "continue" reopens it
    return _toggled("fork", "fork", "continue", RESUME)


def spawn() -> str:  # "worktree" isolates each session in its own tree; else same-dir
    return _toggled("worktree", "worktree", "same-dir", SPAWN)


def set_toggle(name: str, on: bool) -> tuple[str, str | None]:
    """Persist one toggle (fork | worktree) atomically, mirroring add_root's temp-replace."""
    if name not in ("fork", "worktree"):
        return "badname", f"unknown setting {name!r}"
    tmp = None
    try:
        # read-merge-write is one critical section, else a racing tap is lost
        with _LOCK:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(SETTINGS_FILE.parent), prefix="settings."
            )
            with os.fdopen(fd, "w") as f:
                json.dump(_settings() | {name: on}, f)
                f.flush()
                os.fsync(f.fileno())  # durable before the rename makes it the settings
            os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return "failed", str(e)
    return "set", None
