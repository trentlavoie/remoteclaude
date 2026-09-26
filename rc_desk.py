"""Desk (non-remote) claude sessions: find them, badge them, close them.

A resuming remote session would be a second client on the thread a desk session already
holds, which is why the same scan feeds both the launcher's badge and its takeover.
Everything here is a process probe, so the badge path is TTL-cached and the probes
tolerate a missing binary. On Linux the probe reads /proc directly (no forks at all); on
macOS it is pgrep / ps / lsof.
"""

import contextlib
import functools
import os
import select
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator

import rc_config as cfg


def _tool(name: str, *fallbacks: str) -> str:
    """Absolute path to a helper binary. The service runs under a minimal
    launchd/systemd PATH that omits /usr/sbin, so a bare 'lsof' isn't found —
    resolve it up front and fall back to the known locations."""
    return shutil.which(name) or next((p for p in fallbacks if os.path.exists(p)), name)


LSOF = _tool("lsof", "/usr/sbin/lsof", "/usr/bin/lsof")
PGREP = _tool("pgrep", "/usr/bin/pgrep")
PS = _tool("ps", "/bin/ps", "/usr/bin/ps")
# RC_TAKEOVER=0: the launcher never signals a desk claude (the desk ✕ refuses). Default 1,
# matching install.sh. Remote-control processes are unaffected — closing those is the ✕.
TAKEOVER = os.environ.get("RC_TAKEOVER", "1").lower() not in ("0", "false", "no", "off")
PROBE_TIMEOUT = 5.0  # a hung ps/lsof must not hold a /status worker


def _run(cmd: list[str]) -> str:
    """stdout of a helper tool, tolerating a missing (or hung) binary so takeover degrades
    to a no-op instead of aborting the launch it guards."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _pid_cwd(pid: str) -> str | None:
    link = f"/proc/{pid}/cwd"  # Linux: read the cwd symlink; macOS falls to lsof
    if os.path.islink(link):
        with contextlib.suppress(OSError):
            return os.readlink(link)
        return None
    out = _run([LSOF, "-a", "-d", "cwd", "-p", pid, "-Fn"])
    return next((ln[1:] for ln in out.splitlines() if ln.startswith("n")), None)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, but not ours to signal
        return True
    return True


def _kind(args: list[str]) -> bool | None:
    """True for a remote-control claude, False for an interactive desk claude, None for a
    headless one (-p/--print: a script, an agent, a CI job) — which is neither: it can't
    pair with the phone, so it is never badged, never blocks a launch, never killed.
    Matched on whole argv WORDS, not a substring of the command line: a desk claude whose
    prompt or a path mentions "remote-control", or that passes
    --remote-control-session-name-prefix, is not a remote-control server — and the plain
    /stop fallback kills whatever this calls one."""
    if any(a in ("-p", "--print") for a in args):
        return None
    return any(
        a in ("--remote-control", "remote-control") or a.startswith("--remote-control=")
        for a in args
    )


def _procfs() -> bool:
    """Read processes straight from /proc? The same islink probe _pid_cwd keys on, so a
    platform without procfs (macOS) takes the pgrep/ps/lsof tools instead."""
    return os.path.islink("/proc/self/cwd")


def _proc_claude(pid: str) -> tuple[str, bool] | None:
    """(cwd, is_rc) for a live claude at pid, straight from /proc, or None when pid is not
    a claude, is headless, has exited, or isn't ours (another user's cwd is EACCES)."""
    try:
        with open(f"/proc/{pid}/comm", "rb") as f:
            if f.read().strip() != b"claude":
                return None
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = [a.decode(errors="replace") for a in f.read().split(b"\0")[1:] if a]
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
    return None if (kind := _kind(args)) is None else (cwd, kind)


def _claude_pids() -> Iterator[tuple[int, str, bool]]:
    """(pid, cwd, is_rc) of every live claude — is_rc marks a remote-control server. One
    scan feeds every consumer (the desk badge/takeover take the non-RC subset, the
    external-RC badge/stop the RC subset), so their notion of "a claude" can't drift apart:
    a filter fixed in one place but not another would badge sessions the ✕ can't close.
    On Linux that is one pass over /proc — no pgrep, and no 2 `ps` forks per process whose
    command line merely mentions claude (every shell a claude session runs does)."""
    if _procfs():
        for ent in os.listdir("/proc"):
            if ent.isdigit() and (hit := _proc_claude(ent)):
                yield int(ent), *hit
        return
    for pid in _run([PGREP, "-f", "claude"]).split():
        comm = _run([PS, "-o", "comm=", "-p", pid]).strip()
        if os.path.basename(comm) != "claude":  # skip the launcher, tmux, grep, etc.
            continue
        kind = _kind(_run([PS, "-o", "command=", "-p", pid]).split()[1:])
        if kind is not None and (cwd := _pid_cwd(pid)):
            yield int(pid), cwd, kind


def _within(cwd: str, root: str) -> bool:
    return cwd == root or cwd.startswith(root + os.sep)


def _sessions(proj: str, rc: bool) -> list[int]:
    """Live claude pids rooted in proj of the desk (rc=False) or remote-control (rc=True)
    kind. Scoped by cwd, so another project's sessions are never touched."""
    root = cfg.project_dir(proj)
    return [
        pid for pid, cwd, is_rc in _claude_pids() if is_rc is rc and _within(cwd, root)
    ]


def desktop_sessions(proj: str) -> list[int]:
    """Desk (non-RC) claude in proj — the clients a resuming remote session would collide
    with, and what takeover closes."""
    return _sessions(proj, rc=False)


def remote_sessions(proj: str) -> list[int]:
    """Remote-control claude in proj started outside the launcher (a launcher tmux rc-
    session's project shows as running() instead) — what the external-RC ✕ closes."""
    return _sessions(proj, rc=True)


def _rel_project(rel: str) -> str:
    """The project a cwd belongs to: "group/name" when the first path segment is a
    category (matching projects()' shape), else the first segment."""
    parts = rel.split(os.sep)
    if parts[0] in cfg.GROUPS and len(parts) > 1:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _scan(rc: bool) -> list[str]:
    """Projects with a live desk (rc=False) or remote-control (rc=True) claude rooted in
    them, keyed as projects() shapes names. Current Claude Code auto-pairs interactive desk
    sessions with the phone, and an RC session started in a terminal is phone-drivable too —
    both are invisible to the launcher's tmux dots, which is exactly what these badges add."""
    added = [(rp + os.sep, label) for label, rp in cfg.extra_roots().items()]
    parent = cfg.PARENT + os.sep
    out = set()
    for _, cwd, is_rc in _claude_pids():
        if is_rc is not rc:
            continue
        for pre, label in added:
            if cwd.startswith(pre):
                out.add(f"{label}/{cwd.removeprefix(pre).split(os.sep)[0]}")
                break
        else:
            if cwd.startswith(parent):
                out.add(_rel_project(cwd.removeprefix(parent)))
    return sorted(out)


# cached so the 5s /status poll doesn't rescan per viewer per tick;
# .invalidate() makes a just-closed session drop off the next poll.
desk_projects = cfg.ttl_cached(lambda: cfg.DESK_TTL)(lambda: _scan(rc=False))
rc_projects = cfg.ttl_cached(lambda: cfg.DESK_TTL)(lambda: _scan(rc=True))

GRACE = 5.0  # SIGTERM -> SIGKILL grace


def _kill_pidfds(pids: list[int], root: str, rc: bool) -> list[int]:
    """Linux: signal through pidfds, so a pid the kernel recycled between the scan and the
    signal is never hit. Each pidfd pins one process; it is re-verified AFTER opening
    (still a claude of the same kind, still rooted in the project) — past that point every
    signal lands on exactly the process we checked, or on nothing. A pidfd turns readable
    when its process exits, so the grace wait is a poll() on them, not a sleep loop."""
    fds: dict[int, int] = {}
    try:
        for pid in pids:
            with contextlib.suppress(OSError):  # already gone, or not ours to open
                fd = os.pidfd_open(pid)
                hit = _proc_claude(str(pid))
                if hit and hit[1] is rc and _within(hit[0], root):
                    fds[pid] = fd
                else:
                    os.close(fd)
        for fd in fds.values():
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(fd, signal.SIGTERM)
        pending, deadline = set(fds.values()), time.monotonic() + GRACE
        waiter = select.poll()
        for fd in pending:
            waiter.register(fd, select.POLLIN)
        while pending and (left := deadline - time.monotonic()) > 0:
            for fd, _ in waiter.poll(left * 1000):
                pending.discard(fd)
                waiter.unregister(fd)
        for fd in pending:
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
    finally:
        for fd in fds.values():
            os.close(fd)
    return list(fds)


def _kill_pids(pids: list[int]) -> list[int]:
    """SIGTERM, wait, SIGKILL any straggler — graceful so each claude flushes its transcript
    and deregisters before dying; the thread stays resumable. Returns the pids acted on.
    The non-procfs (macOS) path: plain pids, so a pid recycled inside the 5s grace is the
    residual risk there. A process that isn't ours (EPERM) is skipped, never a 500."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    # monotonic: an NTP/DST step must not shorten the SIGKILL grace
    deadline = time.monotonic() + GRACE
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.15)
    for pid in pids:
        if _alive(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    return pids


@functools.cache
def _pidfd() -> bool:
    """pidfds usable here? Python has os.pidfd_open on any Linux build, but a pre-5.3
    kernel answers ENOSYS — which must mean the plain-pid path, not "kill nothing"."""
    try:
        os.close(os.pidfd_open(os.getpid()))
    except (AttributeError, OSError):
        return False
    return True


def _close(proj: str, rc: bool) -> list[int]:
    pids = _sessions(proj, rc)
    if _procfs() and _pidfd():
        return _kill_pidfds(pids, cfg.project_dir(proj), rc)
    return _kill_pids(pids)


def takeover(proj: str) -> list[int]:
    """Close desk claude for proj so a resuming remote session isn't a second client on the
    thread. Returns the pids acted on, for the audit log."""
    return _close(proj, rc=False)


def close_remote(proj: str) -> list[int]:
    """The external-RC ✕: close remote-control sessions for proj started outside the
    launcher, by killing the process (same graceful SIGTERM/SIGKILL as takeover)."""
    return _close(proj, rc=True)
