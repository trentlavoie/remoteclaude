#!/usr/bin/env python3
"""Desk-side launch guard: don't start a desk `claude` blind while a phone
(remote-control) session already owns this project.

The launcher guards the other direction (a phone tap closes the desk session
before resuming); without this, a desk `claude --continue` walks into the thread
the live rc-* session is holding and dies with "No conversation found to
continue". Shell-agnostic core for the rc_guard.sh shim (bash and zsh).

Exit codes are the contract: 0 = proceed with the launch (no live session,
guard skipped, or takeover complete), 2 = proceed but force a FRESH session
(the shim turns this into --new for the caller's resume logic), 1 = do not
launch (quit, the attach WAS the session, or a takeover that failed to clear).
"""

import contextlib
import os
import subprocess
import sys
import termios
import tty

import rc_tmux

PARENT = os.path.expanduser(os.environ.get("RC_PROJECTS_PARENT", "~/projects"))
TAKEOVER_WAIT = 5.0  # seconds to let claude exit on SIGINT before kill-session

PROCEED, ABORT, FRESH = 0, 1, 2
# The caller already chose a session, or isn't starting one: never second-guess.
CALLER_CONTROLS = frozenset(
    {
        "-c",
        "--continue",
        "-r",
        "--resume",
        "-p",
        "--print",
        "--new",
        "-v",
        "--version",
        "-h",
        "--help",
    }
)
CALLER_CONTROL_PREFIXES = ("--resume=", "--session-id")
# Subcommands that never open a conversation thread.
NON_SESSION_SUBCOMMANDS = frozenset(
    {
        "mcp",
        "update",
        "doctor",
        "auth",
        "plugin",
        "install",
        "config",
        "agents",
        "setup-token",
        "migrate-installer",
    }
)


def caller_controls(argv: list[str]) -> bool:
    if argv and argv[0] in NON_SESSION_SUBCOMMANDS:
        return True
    return any(
        a in CALLER_CONTROLS or a.startswith(CALLER_CONTROL_PREFIXES) for a in argv
    )


def _same_dir(a: str, b: str) -> bool:
    with contextlib.suppress(OSError):
        return os.path.samefile(a, b)
    return False


def live_sess(cwd: str, parent: str) -> str | None:
    """The rc tmux session owning cwd's project, or None. Paths are compared in both
    physical and logical form: a trailing slash on RC_PROJECTS_PARENT, a symlinked
    parent, or a symlinked project dir would otherwise silently disable the guard.
    `=name` is load-bearing too: a bare -t prefix-matches, so rc-alpha would match a
    running rc-alpha-sub. A missing tmux means no rc session can exist."""
    # getcwd() is physical; the shell's $PWD is logical. A project dir that is itself
    # a symlink (~/projects/foo -> elsewhere) only matches through the logical form,
    # and that is the name the launcher's rc-foo session carries. Parent is tried in
    # both forms too (macOS /var -> /private/var). join(.., "") = trailing sep, '/'
    # stays '/'.
    candidates = [os.path.realpath(cwd)]
    if (pwd := os.environ.get("PWD")) and _same_dir(pwd, cwd):
        candidates.insert(0, os.path.normpath(pwd))
    parents = {
        os.path.join(p, "")
        for p in (os.path.realpath(parent), os.path.normpath(parent))
    }
    hit = next(((c, p) for c in candidates for p in parents if c.startswith(p)), None)
    if hit is None:
        return None
    inside, root = hit
    proj = inside[len(root) :].split(os.sep, 1)[0]
    sess = rc_tmux.session_name(proj)
    # no tmux, or a wedged server (timeout): no provable session — never block the launch
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        return sess if rc_tmux.has_session(sess) else None
    return None


def read_key() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def state_tag() -> str:
    """rc_status.py's working/waiting glyph for cwd, '' when it has nothing to say
    (or a hung state dir keeps it from saying anything within 2s)."""
    here = os.path.dirname(os.path.abspath(__file__))
    with contextlib.suppress(subprocess.TimeoutExpired):
        r = subprocess.run(
            [sys.executable, os.path.join(here, "rc_status.py")],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return r.stdout.strip()
    return ""


def attach(sess: str) -> None:
    # Inside an existing tmux client, attach refuses ("sessions should be nested with
    # care"); switch-client is the in-tmux equivalent. Not rc_tmux.tmux(): that captures
    # output, and an attach has to own the terminal. switch-client can't cross servers,
    # though: inside the user's own tmux while the launcher runs its own (RC_TMUX_SOCKET),
    # nest an attach instead, with $TMUX dropped so tmux allows it.
    inside, env = os.environ.get("TMUX"), None
    if inside and not rc_tmux.same_server(inside):
        inside, env = None, {k: v for k, v in os.environ.items() if k != "TMUX"}
    verb = "switch-client" if inside else "attach"
    subprocess.run(rc_tmux.argv(verb, "-t", rc_tmux.target(sess)), env=env)


def takeover(sess: str) -> int:
    """Close the remote session so the caller's normal --continue resumes the same
    thread at the desk. If it somehow survives, refuse to launch into it."""
    if rc_tmux.graceful_stop(sess, TAKEOVER_WAIT):
        return PROCEED
    print(
        f"{sess} is still alive after SIGINT and kill-session; not launching.",
        file=sys.stderr,
    )
    return ABORT


def main(argv: list[str]) -> int:
    if not (sys.stdin.isatty() and sys.stderr.isatty()) or caller_controls(argv):
        return PROCEED
    sess = live_sess(os.getcwd(), PARENT)
    if not sess:
        return PROCEED
    tag = state_tag()
    err = sys.stderr
    print(
        f"A phone session is live for this project ({sess}{', ' + tag if tag else ''}).",
        "  [a] attach to it here — one conversation, desk and phone",
        "  [t] take over — close the remote session, resume the thread here",
        "  [f] separate fresh session alongside it",
        "  [q] quit (default)",
        sep="\n",
        file=err,
    )
    print("> ", end="", file=err, flush=True)
    ans = read_key()
    print(ans, file=err)
    match ans:
        case "a":
            attach(sess)
            return ABORT  # the attach WAS the session; don't launch another
        case "t":
            return takeover(sess)
        case "f":
            return FRESH
        case _:
            return ABORT


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
