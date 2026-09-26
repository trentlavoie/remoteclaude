"""Every env-derived setting the launcher reads, plus the audit log line, the project
listing and the TTL cache decorator the rest of the tree shares.

Split out of rc_launcher.py so the git / desk / session / file-share clusters can be their
own modules: launch() needs SHARE and both clusters need log_event, NAME_RE and the env
globals, so extracting a cluster while importing them from the launcher would have made
this repo's only import cycle. Read these as `cfg.NAME` at call time, never
`from rc_config import NAME` — one binding per name is what lets a test redirect PARENT or
SHARE once and have every module follow.

Defaults match this machine; the LaunchAgent sets the rest. Refuses nothing here: the
server checks for the token at startup.
"""

import contextlib
import functools
import hashlib
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable
import tempfile
from datetime import datetime
from pathlib import Path

from rc_claude import MT

# realpath'd, like SHARE below: the desk scan and takeover compare cwds read back from
# lsof / /proc (always physical) against this prefix, and a symlinked or trailing-slashed
# ~/projects would silently match nothing.
PARENT = os.path.realpath(
    os.path.expanduser(os.environ.get("RC_PROJECTS_PARENT", "~/projects"))
)
GIT = os.environ.get("RC_GIT_BIN", "git")


def _read_token() -> str:
    """Auth token from the 0600 file install.sh writes — the only place it lives. The
    service files stopped carrying the secret, so `launchctl print` / `systemctl show`
    can't leak it and rotation is write-file + kickstart, no plist surgery. No env
    fallback: an env-carried token is readable via ps/launchctl and inherited by every
    child of the HTTP process — the channel the 2026-08-16 remediation closed."""
    tf = Path(
        os.path.expanduser(
            os.environ.get("RC_LAUNCHER_TOKEN_FILE", "~/.config/rc-launcher/token")
        )
    )
    with contextlib.suppress(OSError):
        return tf.read_text().strip()
    return ""


TOKEN = _read_token()
# Additional project roots beyond PARENT, runtime-editable so a root added from the phone
# survives with no service reload. JSON list of absolute paths (JSON, not lines, so a path
# can't inject a second root via an embedded newline), 0600, beside the token.
ROOTS_FILE = Path(
    os.path.expanduser(
        os.environ.get("RC_LAUNCHER_ROOTS_FILE", "~/.config/rc-launcher/roots.json")
    )
)
PORT = int(
    os.environ.get("RC_LAUNCHER_PORT") or "8787"
)  # empty env must not ValueError
# loopback by default: front it with `tailscale serve` (HTTPS). The LAN / subnet-route setup
# opts back in with RC_LAUNCHER_BIND=0.0.0.0.
BIND = os.environ.get("RC_LAUNCHER_BIND", "127.0.0.1")
HOST = socket.gethostname().split(".")[0]
CLAUDE_JSON = os.path.expanduser("~/.claude.json")
# per-project transcripts
CLAUDE_PROJECTS = Path(os.path.expanduser("~/.claude/projects"))


def _build_stamp(root: Path = Path(__file__).parent) -> str:
    """A 12-hex hash over the shipped rc_*.py bundle beside this file: it changes iff any
    shipped source does (all rc_*.py, not only the launcher's import closure — a reload
    re-execs everything), needs no git and no build step, recomputed at import. Blank when
    no source is readable, so /version answers instead of crashing."""
    h = hashlib.sha256()
    try:
        sources = sorted(root.glob("rc_*.py"))
        for p in sources:
            h.update(p.read_bytes())
    except OSError:
        return ""  # a partial hash would read as a legitimate build — blank is honest
    return h.hexdigest()[:12] if sources else ""


VERSION = _build_stamp()
SHARE = os.path.realpath(
    os.path.expanduser(os.environ.get("RC_SHARE_DIR", "~/rc-share"))
)
RCPART_TTL = 6 * 3600  # abandoned .rcpart uploads (no writes in this long) get swept
# SIGINT grace before kill-session
STOP_WAIT = float(os.environ.get("RC_STOP_WAIT", "5"))
# per-project git state is cached this long
GIT_TTL = float(os.environ.get("RC_GIT_TTL", "30"))
# cap a hung `git status`
GIT_STATUS_TIMEOUT = float(os.environ.get("RC_GIT_STATUS_TIMEOUT", "3"))
# desk-session scan is cached this long
DESK_TTL = float(os.environ.get("RC_DESK_TTL", "10"))
LOGIN_TTL = 60.0  # `claude auth status` spawns a process; the phone polls every 5s

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")  # must start alphanumeric: no dot
# (rc-<name>.pane is an untargetable tmux session), no leading _ or - (meta/scratch dirs like
# _archive shouldn't list as projects; a leading - is a shell/tmux arg-injection shape)


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _names(name: str) -> frozenset[str]:
    return frozenset(
        s.strip().lower() for s in os.environ.get(name, "").split(",") if s.strip()
    )


def _mib(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return (int(raw) if raw.isdigit() else default) << 20


# --- web-tier hardening; rc_launcher reads these as cfg.NAME at request time ---
# /files is OPT-IN: unless RC_SHARE_ENABLED=1 its routes 404 and the page hides the link.
SHARE_ENABLED = _flag("RC_SHARE_ENABLED")
# Host names accepted besides localhost, IP literals and this host's own name (the DNS-
# rebinding guard): add the `tailscale serve` name. A leading '.' entry allows subdomains.
ALLOWED_HOSTS = _names("RC_ALLOWED_HOSTS")
# Tailscale-User-Login values allowed IN ADDITION to the token. Only meaningful bound to
# loopback behind `tailscale serve`, which sets the header and drops a client's own copy.
TAILSCALE_USERS = _names("RC_TAILSCALE_USERS")
# Legacy opt-in: let /launch /stop /create /addroot /settings answer GET as well as POST.
ALLOW_GET_ACTIONS = _flag("RC_ALLOW_GET_ACTIONS")
COOKIE_SECURE = _flag("RC_COOKIE_SECURE")  # else Secure iff X-Forwarded-Proto: https
UPLOAD_MAX = _mib("RC_UPLOAD_MAX_MB", 4096)  # per-file cap on a /files upload
SHARE_MIN_FREE = _mib("RC_SHARE_MIN_FREE_MB", 1024)  # an upload never leaves less free


# C0 controls + DEL -> '?': a request-supplied name must not forge a second log line
_CTRL = dict.fromkeys([*range(32), 127], "?")


def log_event(action: str, proj: str, result: str) -> None:
    """One audit line per launch/stop to StandardOutPath (/tmp/rc-launcher.log)."""
    action, proj, result = (str(x).translate(_CTRL) for x in (action, proj, result))
    print(
        f"{datetime.now(MT):%Y-%m-%d %H:%M:%S} MT  {action:<6} {proj} -> {result}",
        flush=True,
    )


# Top-level dirs whose NAME is a category to descend into one level (their children list as
# "group/name"), not a project. OPT-IN: set RC_PROJECT_GROUPS (comma-separated, e.g.
# "work,hobby") to enable two-level listing; empty/unset means a flat ~/projects. install.sh
# writes "" by default, so the code default must be "" too or the two disagree — flat until
# the operator declares their buckets.
GROUPS = frozenset(
    g.strip() for g in os.environ.get("RC_PROJECT_GROUPS", "").split(",") if g.strip()
)


def _forbidden_root(path: str) -> bool:
    """A realpath'd root too broad or too central to add: /, $HOME or any ancestor of it,
    the config dir, or anything overlapping PARENT. A root must be a specific projects dir,
    not a whole tree. (The token already grants code execution, so this is footgun-guarding,
    not a security boundary — but adding / would fork a git per top-level dir every poll.)"""
    home = os.path.realpath(os.path.expanduser("~"))
    if path == "/" or (home + os.sep).startswith(
        path + os.sep
    ):  # /, $HOME, an ancestor
        return True
    if path == os.path.realpath(str(ROOTS_FILE.parent)):
        return True
    parent = os.path.realpath(PARENT)  # realpath so a symlinked PARENT still overlaps
    return (
        path == parent
        or path.startswith(parent + os.sep)
        or parent.startswith(path + os.sep)
    )


def _root_lines() -> list[str]:
    """Raw root paths from the env (RC_PROJECT_ROOTS, comma/colon-separated) then the JSON
    file. A malformed/absent file contributes nothing."""
    env = [
        p
        for p in re.split(r"[,:]", os.environ.get("RC_PROJECT_ROOTS", ""))
        if p.strip()
    ]
    from_file: list[str] = []
    with contextlib.suppress(OSError, json.JSONDecodeError):
        data = json.loads(ROOTS_FILE.read_text())
        if isinstance(data, list):
            from_file = [p for p in data if isinstance(p, str)]
    return env + from_file


def extra_roots() -> dict[str, str]:
    """label -> realpath'd dir for each valid additional root; the label is the basename.
    Read fresh each call (a small JSON read) so a UI-added root shows on the next poll with
    no reload. A root that vanished, is forbidden, whose basename collides with a GROUP or a
    primary project, or repeats an earlier label is skipped — a stale file degrades, never
    crashes. PARENT is never here, so a flat project can't be shadowed."""
    out: dict[str, str] = {}
    for raw in _root_lines():
        path = os.path.realpath(os.path.expanduser(raw.strip()))
        label = os.path.basename(path)
        if (
            not NAME_RE.match(label)
            or label in GROUPS
            or label in out
            or _forbidden_root(path)
            or not os.path.isdir(path)
            or os.path.isdir(os.path.join(PARENT, label))
        ):
            continue
        out[label] = path
    return out


def configured_root_labels() -> set[str]:
    """Basenames of every root the config names — even one currently invalid (a down mount) —
    so create() can't mkdir PARENT/<label> and permanently shadow a root that's just offline."""
    return {
        os.path.basename(os.path.realpath(os.path.expanduser(r.strip())))
        for r in _root_lines()
    }


def add_root(raw: str) -> tuple[str, str | None]:
    """Validate a candidate root and append it to ROOTS_FILE (atomic JSON). Returns a
    (status, reason): added / exists / badpath / collision / failed. Realpath'd and stored
    resolved, so a symlinked root can't later escape a prefix check."""
    if not raw.strip():
        return "badpath", "empty path"
    path = os.path.realpath(os.path.expanduser(raw.strip()))
    if _forbidden_root(path) or not os.path.isdir(path):
        return "badpath", "not a usable directory (or a forbidden/overlapping location)"
    try:
        os.listdir(
            path
        )  # readable? probe once, so a dead mount is rejected here not per poll
    except OSError as e:
        return "badpath", f"unreadable: {e}"
    label = os.path.basename(path)
    if not NAME_RE.match(label):
        return "badpath", "the directory name must start with a letter or digit"
    if label in GROUPS or os.path.isdir(os.path.join(PARENT, label)):
        return "collision", f"'{label}' already names a group or a project"
    current = extra_roots()
    if current.get(label) == path:
        return "exists", None
    if label in current:
        return "collision", f"'{label}' is already a root ({current[label]})"
    for other in current.values():  # no nesting: it makes a dir list under two ids
        if (
            path == other
            or path.startswith(other + os.sep)
            or other.startswith(path + os.sep)
        ):
            return "collision", f"overlaps an existing root ({other})"
    try:
        existing: list = []
        with contextlib.suppress(OSError, json.JSONDecodeError):
            data = json.loads(ROOTS_FILE.read_text())
            existing = data if isinstance(data, list) else []
        ROOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(ROOTS_FILE.parent), prefix="roots.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump([*existing, path], f)
            os.replace(tmp, ROOTS_FILE)
        except OSError as e:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            # strerror, not str(e): the client must not see the config path
            return "failed", e.strerror or "write failed"
    except (
        OSError
    ) as e:  # mkstemp makes 0600 and os.replace preserves it — no chmod needed
        return "failed", e.strerror or "write failed"
    return "added", None


def project_dir(proj: str) -> str:
    """Resolve a project id to its on-disk dir. A "label/name" whose label is an added root
    resolves under that root; a flat name or a "group/name" (groups are subdirs of PARENT)
    resolves under PARENT. Read at call time so a test redirecting PARENT/ROOTS_FILE reaches
    every caller. The name segment is re-validated, so a crafted '..'/absolute part falls
    back to the PARENT join instead of escaping the resolved root."""
    label, sep, name = proj.partition("/")
    if sep and NAME_RE.match(name) and (root := extra_roots().get(label)):
        return os.path.join(root, name)
    return os.path.join(PARENT, proj)


def projects() -> list[str]:
    """Every launchable project, flat or one level under a category. A top-level dir whose
    name is in GROUPS is a category and lists its children as "group/name"; any other dir is
    a flat project. So a half-migrated tree (some flat, some grouped) lists correctly, and a
    project's own subdirs are never taken for projects — descent is one level, into GROUPS
    only, and a category dir never lists as a project itself."""
    try:
        entries = os.listdir(PARENT)
    except FileNotFoundError:
        return []
    out = []
    for e in entries:
        if not (NAME_RE.match(e) and os.path.isdir(project_dir(e))):
            continue
        if e in GROUPS:
            group = project_dir(e)
            with contextlib.suppress(
                OSError
            ):  # vanished/unreadable category contributes none
                out += [
                    f"{e}/{s}"
                    for s in os.listdir(group)
                    if NAME_RE.match(s) and os.path.isdir(os.path.join(group, s))
                ]
        else:
            out.append(e)
    for label, root in extra_roots().items():
        with contextlib.suppress(OSError):  # vanished/unreadable root contributes none
            out += [
                f"{label}/{s}"
                for s in os.listdir(root)
                if NAME_RE.match(s)
                and os.path.isdir(os.path.join(root, s))
                and os.path.realpath(os.path.join(root, s)).startswith(root + os.sep)
            ]
    return sorted(out)


class TTLCache[T]:
    """One cached callable: see ttl_cached()."""

    def __init__(self, fn: Callable[..., T], ttl: Callable[[], float]) -> None:
        self._fn, self._ttl = fn, ttl
        self._cache: dict[tuple, tuple[float, T]] = {}
        self._lock = threading.Lock()
        self._inflight: dict[tuple, threading.Lock] = {}  # single-flight per key
        self._gen = 0  # bumped by invalidate(); a fill from an older gen must not land
        functools.update_wrapper(self, fn)

    def __call__(self, *args) -> T:
        # Single-flight: concurrent misses on one key wait for the first computation
        # instead of each forking the work (the page's 5s poll fires whether or not the
        # last one finished, so a slow scan used to fan out into parallel git forks).
        with self._lock:
            if (hit := self._fresh(args)) is not None:
                return hit
            gen = self._gen
            gate = self._inflight.setdefault(args, threading.Lock())
        with gate:
            with self._lock:
                if (hit := self._fresh(args)) is not None:
                    return hit
            value = self._fn(*args)
            with self._lock:
                # an invalidate() while we computed bumped the generation: this result is
                # pre-invalidate, so it must not overwrite a fresher one or reappear stale.
                if gen == self._gen:
                    self._cache[args] = (time.monotonic() + self._ttl(), value)
                # prune this key's lock so _inflight can't grow without bound
                self._inflight.pop(args, None)
        return value

    def _fresh(self, args: tuple) -> T | None:
        hit = self._cache.get(args)
        return hit[1] if hit and hit[0] > time.monotonic() else None

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
            # any fill now in flight is pre-invalidate; its write will be dropped
            self._gen += 1


def ttl_cached[T](
    ttl: Callable[[], float],
) -> Callable[[Callable[..., T]], TTLCache[T]]:
    """Cache the wrapped function's result per positional-argument tuple for ttl() seconds,
    with an .invalidate() that drops every entry.

    One decorator for the three caches the launcher had grown in three shapes (an lru_cache
    over a time bucket for the login probe, a dict+lock keyed per project for git state, a
    tuple+lock+hand-rolled invalidate for the desk scan) — they cache for the same reason:
    the phone polls /status every few seconds and each of these forks a process. ttl is a
    callable, read per call, so a test can set the TTL to 0 and force a rescan.
    """
    return lambda fn: TTLCache(fn, ttl)
