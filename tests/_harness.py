"""Shared test scaffolding.

proc()/desk()/respond() are the one canned answer for the mocked subprocess world — the
tmux control calls and the pgrep/ps/lsof desk probes that six tests used to re-implement
inline. restore_globals() snapshots every module attribute and stdlib singleton a test may
reassign and restores it via addCleanup, so a mutated global can't leak into a later test
regardless of run order; keep() is the same restore for the one-off attributes a single
class patches. ServerCase is the loopback-server fixture the handler-level tests drive,
and MockedToolsCase the mocked tmux/git/pgrep world the launch and takeover paths run in.
(This file is not collected: discover only runs test*.py.)
"""

import http.client
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
from itertools import pairwise
from types import SimpleNamespace

import rc_config
import rc_desk
import rc_git
import rc_settings
import rc_launcher
import rc_sessions
from pathlib import Path

TOKEN = "test-token-0123456789"

# Module attrs tests reassign, under the module that owns each one now. The source reads
# them as `cfg.NAME`, never `from rc_config import NAME` — one binding per name is what
# lets a test redirect PARENT or SHARE once and have every module follow.
_ATTRS = {
    rc_config: (
        "SHARE",
        "STOP_WAIT",
        "TOKEN",
        "PARENT",
        "CLAUDE_JSON",
        "ROOTS_FILE",
        "CLAUDE_PROJECTS",
        "GIT",
        "GIT_TTL",
        "DESK_TTL",
        "log_event",
        "SHARE_ENABLED",
        "ALLOWED_HOSTS",
        "TAILSCALE_USERS",
        "ALLOW_GET_ACTIONS",
        "COOKIE_SECURE",
        "UPLOAD_MAX",
        "SHARE_MIN_FREE",
    ),
    rc_sessions: ("STATE_DIR",),
    rc_settings: ("RESUME", "SPAWN", "MODEL", "SETTINGS_FILE"),
}
# Stdlib singletons the subprocess-mock rebinds. These are the very module objects every
# rc_* module imported, so one patch here reaches all of them at once.
_STDLIB = (
    (subprocess, "run"),
    (os, "kill"),
    (time, "sleep"),
    (time, "time"),
    (time, "monotonic"),
    (os.path, "islink"),
    (os, "readlink"),
)
# TTL caches keyed on project names — and names repeat across each test's tmp PARENT, so a
# warm entry from an earlier test would otherwise answer for a different directory.
_CACHES = (
    rc_git.git_state,
    rc_desk.desk_projects,
    rc_desk.rc_projects,
    rc_sessions.login_status,
)


def proc(returncode=0, stdout="", stderr=""):
    """A stand-in for subprocess.CompletedProcess in the mocked orchestration tests."""
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def desk(cwd, comm="claude", command="claude --continue"):
    """One live process as _desk_claude_pids() sees it: what `ps -o comm=` reports, what
    `ps -o command=` reports, and the cwd lsof hands back. The defaults describe a plain
    desk claude — the cases that must be FILTERED name the field that filters them."""
    return comm, command, cwd


def respond(cmd, desk_procs, responses):
    """Answer one mocked subprocess.run. The desk-claude probes (`pgrep -f claude` and
    the per-pid `ps -o comm=` / `ps -o command=` / `lsof -Fn`) come from desk_procs
    {pid: desk(...)}; everything else falls through to the substring table."""
    key = " ".join(map(str, cmd))
    if desk_procs:
        if "pgrep" in key:
            return proc(stdout="\n".join(desk_procs) + "\n")
        # pairwise, not an index: `tmux capture-pane -t =rc-x -p` ends on a bare -p
        pid = next((b for a, b in pairwise(cmd) if a == "-p"), None)
        if pid in desk_procs:
            comm, command, cwd = desk_procs[pid]
            if "comm=" in key:
                return proc(stdout=f"{comm}\n")
            if "command=" in key:
                return proc(stdout=f"{command}\n")
            if "-Fn" in key:
                return proc(stdout=f"n{cwd}\n")
    return next((r for pat, r in responses.items() if pat in key), proc())


def keep(tc, *targets):
    """Restore (obj, name) attributes to their current values when tc finishes — the
    one-liner form of what restore_globals() does over its own fixed lists, for the
    modules a single test class patches."""
    for obj, name in targets:
        tc.addCleanup(setattr, obj, name, getattr(obj, name))


def restore_globals(tc):
    """Register addCleanup handlers that restore every mutable module global and rebindable
    stdlib singleton to its current value, and empty the TTL caches on both sides of the
    test. Call first in setUp, before the test mutates them — which also means its
    restores run LAST (addCleanup is LIFO): a cleanup registered after it in setUp runs
    while the test's patches are still in place, so keep such cleanups patch-agnostic."""
    for module, names in _ATTRS.items():
        for name in names:
            tc.addCleanup(setattr, module, name, getattr(module, name))
    for obj, name in _STDLIB:
        tc.addCleanup(setattr, obj, name, getattr(obj, name))
    for cache in _CACHES:
        cache.invalidate()
        tc.addCleanup(cache.invalidate)


def share_dir(tc):
    """A tmp SHARE for tc, removed on cleanup. realpath'd the way the real SHARE is at
    import: mkdtemp hands back /var/..., confinement checks see /private/var/..."""
    path = os.path.realpath(tempfile.mkdtemp())
    tc.addCleanup(shutil.rmtree, path, True)
    return path


def spawn_ok():
    """The two tmux answers a clean launch needs: no session yet, new pane alive."""
    return {"has-session": proc(returncode=1), "pane_dead": proc(stdout="0\n")}


def env(tc, **values):
    """Set process env vars for the duration of tc. patch.dict restores what was there
    before (rather than deleting the key), so a preset RC_SNAPSHOT survives the test."""
    patch = unittest.mock.patch.dict(os.environ, values)
    patch.start()
    tc.addCleanup(patch.stop)


def serve(tc):
    """Start a loopback rc_launcher.Server on an ephemeral port for tc and register its
    shutdown; returns the bound port. Uses Server (not raw ThreadingHTTPServer) so a client RST
    in a test doesn't dump a framework traceback into the test output."""
    srv = rc_launcher.Server(("127.0.0.1", 0), rc_launcher.Handler)
    # poll_interval: shutdown() waits out one poll, and 33 tests each paid the 0.5s default
    threading.Thread(
        target=lambda: srv.serve_forever(poll_interval=0.05), daemon=True
    ).start()
    tc.addCleanup(srv.server_close)
    tc.addCleanup(srv.shutdown)
    return srv.server_address[1]


class ServerCase(unittest.TestCase):
    """The real Handler over a loopback server, with a tmp SHARE and a known token — the
    same contract a browser/app client speaks. Subclasses extend setUp with whatever extra
    globals their routes read.

    The share is switched ON here (it is opt-in in production) and the disk-free floor off,
    so the /files tests don't depend on this machine's free space. ALLOW_GET_ACTIONS is ON
    only because tests/test_settings.py still drives /settings by GET; the web-tier tests
    (test_routes, test_web_*) turn it back off and speak POST, as the page does."""

    def setUp(self):
        restore_globals(self)
        self.share = rc_config.SHARE = share_dir(self)
        rc_config.ROOTS_FILE = Path(self.share, "roots.json")
        rc_settings.SETTINGS_FILE = Path(self.share, "settings.json")
        rc_config.TOKEN = TOKEN
        rc_config.log_event = lambda *a: None  # keep test traffic out of the real log
        rc_config.SHARE_ENABLED = True
        rc_config.SHARE_MIN_FREE = 0
        rc_config.ALLOW_GET_ACTIONS = True
        self.port = serve(self)

    def req(self, method, path, body=None, headers=None, cookie=True):
        """One request; returns (status, lowercased response headers, body bytes)."""
        h = dict(headers or {})
        if cookie:
            h.setdefault("Cookie", f"rc_token={TOKEN}")
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data


class MockedToolsCase(unittest.TestCase):
    """Every tmux / git / pgrep / ps / lsof call answered from a table instead of a real
    process, with os.kill and time.sleep neutered — so launch, stop and takeover can be
    exercised without spawning or signalling anything. Set self.responses (command
    substring -> proc(...)), self.desk ({pid: desk(...)}) and self.alive (pids that
    survive a SIGTERM); read self.calls and self.killed back."""

    def setUp(self):
        # snapshot + auto-restore every global/singleton reassigned below
        restore_globals(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        rc_config.PARENT = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(rc_config.PARENT, "proj"))
        rc_config.CLAUDE_JSON = os.path.join(self.tmp, "claude.json")
        rc_config.ROOTS_FILE = Path(self.tmp, "roots.json")
        rc_settings.SETTINGS_FILE = Path(self.tmp, "settings.json")
        Path(rc_config.CLAUDE_JSON).write_text("{}")
        # empty: no desk thread
        rc_config.CLAUDE_PROJECTS = Path(self.tmp, "claude-projects")
        rc_config.log_event = lambda *a: None
        self.calls: list = []
        self.responses: dict = {}
        self.desk: dict = {}
        self.killed: list = []  # (pid, sig) seen by os.kill
        self.alive: set = set()  # pids that os.kill(pid, 0) should treat as alive
        subprocess.run = self._run
        os.kill = self._kill
        time.sleep = lambda *a: None
        # force _pid_cwd down the (mocked) lsof path — on Linux it would read the runner's
        # real /proc/<pid>/cwd and bypass the mock entirely
        os.path.islink = lambda p: False

    def _run(self, cmd, **kw):
        self.calls.append(cmd)
        return respond(cmd, self.desk, self.responses)

    def _kill(self, pid, sig):
        self.killed.append((pid, sig))
        if sig == 0 and pid not in self.alive:
            raise ProcessLookupError

    def _cmds(self) -> list[str]:
        return [" ".join(map(str, c)) for c in self.calls]

    def _pgreps(self) -> int:
        """How many desk scans have actually forked pgrep — the observable the TTL cache
        tests assert on."""
        return len([c for c in self._cmds() if "pgrep" in c])
