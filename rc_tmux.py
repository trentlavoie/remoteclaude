"""tmux, spoken once — for the launcher and for the desk-side guard.

Each had grown its own binary default, its own `has-session` call and its own `rc-{proj}`
naming; two definitions of "is that session alive" drift apart quietly, and the `=name`
exact-match form below is the kind of hard-won detail that has to be in exactly one place.
graceful_stop() is the one confirming close: the desk guard's takeover and the
launcher's stop() (rc_sessions) both go through it, so a ✕ that reports "stopped" has
looked.

Deliberately a cheap leaf — no rc_config import, no token read, no gethostname: the guard
runs this on every desk `claude`.
"""

import os
import re
import shutil
import subprocess
import sys
import time

# RC_TMUX_BIN wins, then whatever is on PATH (the desk case), then Homebrew's path: the
# service's minimal launchd PATH has no /opt/homebrew/bin, so `tmux` alone isn't found there.
TMUX = os.environ.get("RC_TMUX_BIN") or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
# Every tmux call is bounded: a wedged server must fail the request, not hang a worker.
TIMEOUT = float(os.environ.get("RC_TMUX_TIMEOUT") or "10")


def _socket(raw: str) -> str:
    """RC_TMUX_SOCKET, opt-in: a dedicated tmux server (`tmux -L <name>`) for the launcher's
    sessions, so they never share a server — or its global environment — with the user's own
    sessions, and `list-sessions` never even sees those. Unset/empty keeps upstream behavior
    (the default server). The value is a socket FILE name under the tmux socket dir, so only
    a plain token is accepted; anything else still isolates, under a fixed name, rather than
    silently falling back onto the shared server the operator asked to leave."""
    if not raw or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", raw):
        return raw
    msg = f"rc_tmux: RC_TMUX_SOCKET {raw!r} is not a plain name; using rc-launcher"
    print(msg, file=sys.stderr)
    return "rc-launcher"


SOCKET = _socket(os.environ.get("RC_TMUX_SOCKET", ""))


# Never handed to a tmux client: if that call is what starts the tmux server (always so on a
# fresh RC_TMUX_SOCKET) its env becomes the server's global env, so every session's. The
# token lives only in a 0600 file and in memory, but a legacy unit may still export it; the
# ntfy topic URL is a push capability, not config.
CLIENT_ENV_DROP = frozenset({"RC_LAUNCHER_TOKEN", "RC_NOTIFY_URL"})


def client_env() -> dict[str, str]:
    """The environment for a tmux client that may start the server."""
    return {k: v for k, v in os.environ.items() if k not in CLIENT_ENV_DROP}


def argv(*args: str) -> list[str]:
    """The tmux argv for args, pointed at the launcher's server (RC_TMUX_SOCKET) when set.
    Every tmux invocation in the tree is built here, so none can land on the wrong server."""
    return [TMUX, *(("-L", SOCKET) if SOCKET else ()), *args]


def tmux(*args: str) -> subprocess.CompletedProcess[str]:
    """A tmux control call with its chatter captured, so it stays out of the audit log.
    No OSError guard: a missing (or wedged: TimeoutExpired) tmux must surface on the
    launch/stop paths; running() catches its own because status dots are non-essential."""
    return subprocess.run(argv(*args), capture_output=True, text=True, timeout=TIMEOUT)


def target(sess: str) -> str:
    """The -t for SESSION-scoped verbs (has-session, kill-session, attach, switch-client).
    `=name` is exact: a bare -t prefix-matches, so with rc-alpha absent and rc-alpha-sub
    live, alpha's stop() would C-c the sibling and launch() report "already"."""
    return f"={sess}"


def pane(sess: str) -> str:
    """The -t for WINDOW/PANE-scoped verbs (send-keys, capture-pane, list-panes, set-option
    on remain-on-exit): `=name:` — the exact session, its current window. A bare `=name`
    is wrong for these: tmux resolves it as a window name first (verified on tmux 3.4:
    `list-panes -t =rc-x` listed the user's own window named rc-x in another session), and
    send-keys/capture-pane/set-option reject it outright ("can't find pane"), which
    silently turned every graceful stop into a kill and blinded the prompt/death checks."""
    return f"={sess}:"


def same_server(tmux_env: str) -> bool:
    """Is $TMUX (a client's "socket_path,pid,idx") on the server argv() talks to? A client
    of another server can't switch-client across servers."""
    return os.path.basename(tmux_env.split(",", 1)[0]) == (SOCKET or "default")


def session_name(proj: str) -> str:
    # a group/name project's "/" is a tmux target metachar (send-keys can't resolve it);
    # "+" round-trips and NAME_RE forbids it in a segment, so it's a safe separator
    return f"rc-{proj.replace('/', '+')}"


def has_session(sess: str) -> bool:
    return tmux("has-session", "-t", target(sess)).returncode == 0


def running() -> set[str]:
    """The projects with a live rc-* session, by name."""
    try:
        out = tmux("list-sessions", "-F", "#{session_name}").stdout
    except (OSError, subprocess.SubprocessError):  # no tmux yet / wedged: non-essential
        return set()
    return {
        line.removeprefix("rc-").replace("+", "/")
        for line in out.splitlines()
        if line.startswith("rc-")
    }


def graceful_stop(sess: str, wait: float = 5.0) -> bool:
    """Close sess and report whether it is actually gone.

    Graceful first: Ctrl-C TWICE, close together — claude's TUI answers a single one with
    "Press Ctrl-C again to exit" and stays up (verified live on 2.1.260: one C-c, or two
    4s apart, leave it running; two 0.4s apart exit it), so the single C-c this used to
    send never closed anything and every stop fell through to the kill. A clean exit lets
    claude deregister from Anthropic's relay; an abrupt kill-session sends SIGHUP, which
    the relay can't tell apart from the Mac dropping off the network — so the app keeps
    showing the session "connected" until the relay's inactivity timeout (~10 min) evicts
    it. Wait for the pane to exit on its own, kill-session only as the fallback, then
    confirm: a caller that reports "stopped" without confirming is guessing.
    """
    tmux("send-keys", "-t", pane(sess), "C-c")
    time.sleep(0.3)  # inside the TUI's "again" window, outside its key-repeat debounce
    tmux("send-keys", "-t", pane(sess), "C-c")
    deadline = time.monotonic() + wait
    while has_session(sess) and time.monotonic() < deadline:
        time.sleep(0.25)
    if has_session(sess):
        tmux("kill-session", "-t", target(sess))
    return not has_session(sess)
