"""Desk-process identification and the kill path, hardened.

What counts as a remote-control or a desk claude is decided on whole argv words (a prompt or
a path that merely mentions remote-control is not an RC server, and headless -p/--print
claude is neither — never badged, never killed); the Linux scan reads /proc with no forks;
kills go through pidfds re-verified after opening, so a recycled pid is never signalled;
RC_TAKEOVER=0 disables the desk ✕ outright.

The Live cases spawn their OWN child processes — a `sleep` exec'd under the name "claude"
from a throwaway dir — and only ever signal those; PARENT is a fresh tmp dir, so no real
session's cwd can fall inside it."""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import rc_config
import rc_desk
import rc_sessions

from tests._harness import MockedToolsCase, desk, keep, proc, restore_globals

LINUX_PIDFD = sys.platform == "linux" and rc_desk._procfs() and rc_desk._pidfd()


class KindTest(unittest.TestCase):
    def test_remote_control_is_matched_by_word_not_substring(self):
        k = rc_desk._kind
        self.assertIs(k(["--remote-control", "host/p"]), True)
        self.assertIs(k(["--model", "m", "remote-control", "--name", "n"]), True)
        self.assertIs(k(["--remote-control=host/p"]), True)
        # a desk claude that merely MENTIONS it: substring matching made these "RC", and
        # the plain /stop external-RC fallback kills whatever is RC
        self.assertIs(k(["fix the remote-control launcher"]), False)
        self.assertIs(k(["--remote-control-session-name-prefix", "x"]), False)
        self.assertIs(k(["--mcp-config", "/p/remote-control.json"]), False)
        self.assertIs(k(["--continue"]), False)

    def test_headless_print_mode_is_neither_kind(self):
        self.assertIsNone(rc_desk._kind(["-p", "summarize"]))
        self.assertIsNone(rc_desk._kind(["--print", "--output-format", "json"]))

    def test_takeover_flag_parsing(self):
        for raw, want in (("1", True), ("0", False), ("off", False), ("FALSE", False)):
            code = "import rc_desk; print(rc_desk.TAKEOVER)"
            out = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                cwd=os.path.dirname(os.path.abspath(rc_desk.__file__)),
                env={**os.environ, "RC_TAKEOVER": raw},
            ).stdout.strip()
            self.assertEqual(out, str(want), raw)


class FallbackScanTest(MockedToolsCase):
    """The pgrep/ps/lsof path (macOS; the harness forces it here)."""

    def test_headless_and_mentioning_claudes_are_classified_right(self):
        root = os.path.join(rc_config.PARENT, "proj")
        self.desk = {
            "111": desk(root, command="claude -p do-the-thing"),  # headless: ignored
            # desk: mentions, never the word (ps joins argv with spaces, so on this
            # path a prompt that IS the bare word reads as the subcommand — /proc is exact)
            "222": desk(root, command="claude --mcp-config /c/remote-control.json"),
            "333": desk(root, command="claude --remote-control h/proj"),  # rc
        }
        self.assertEqual(rc_desk.desktop_sessions("proj"), [222])
        self.assertEqual(rc_desk.remote_sessions("proj"), [333])
        self.assertEqual(rc_desk.desk_projects(), ["proj"])

    def test_plain_stop_never_kills_a_desk_claude_that_mentions_remote_control(self):
        self.desk = {
            "222": desk(
                os.path.join(rc_config.PARENT, "proj"),
                command="claude --remote-control-session-name-prefix me",
            )
        }
        self.responses = {"has-session": proc(returncode=1)}  # no tmux -> ext fallback
        self.assertEqual(rc_sessions.stop("proj"), ("idle", None))
        self.assertEqual(self.killed, [])

    def test_kill_tolerates_a_process_that_is_not_ours(self):
        def eperm(pid, sig):
            self.killed.append((pid, sig))
            raise PermissionError

        os.kill = eperm
        ticks = iter([0.0, 1.0, 10.0])  # past the grace without real waiting
        time.monotonic = lambda: next(ticks, 100.0)  # restore_globals() puts it back
        self.assertEqual(rc_desk._kill_pids([4242]), [4242])  # no raise -> no 500
        self.assertTrue(rc_desk._alive(4242))  # EPERM means it exists

    def test_probe_timeout_degrades_to_nothing(self):
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        subprocess.run = hang
        self.assertEqual(rc_desk._run(["ps"]), "")
        self.assertEqual(rc_desk.desktop_sessions("proj"), [])

    def test_takeover_disabled_never_signals(self):
        keep(self, (rc_desk, "TAKEOVER"))
        rc_desk.TAKEOVER = False
        self.desk = {"111": desk(os.path.join(rc_config.PARENT, "proj"))}
        status, reason = rc_sessions.desk_stop("proj")
        self.assertEqual(status, "failed")
        self.assertIn("RC_TAKEOVER=0", reason)
        self.assertEqual(self.killed, [])
        self.assertFalse(any("pgrep" in c for c in self._cmds()))  # not even scanned


@unittest.skipUnless(LINUX_PIDFD, "Linux procfs + pidfd only")
class LiveProcTest(unittest.TestCase):
    """The real /proc scan and the real pidfd kill, against processes this test owns."""

    def setUp(self):
        restore_globals(self)
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        rc_config.PARENT = os.path.join(self.tmp, "projects")
        self.root = os.path.join(rc_config.PARENT, "proj")
        os.makedirs(self.root)
        rc_config.ROOTS_FILE = Path(self.tmp, "roots.json")
        rc_config.log_event = lambda *a: None
        self.sleep = shutil.which("sleep") or "/bin/sleep"
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)

    def _as_claude(self, exe: str) -> str:
        """exe reachable under the name "claude": its comm reads "claude" once exec'd."""
        link = os.path.join(self.bindir, "claude")
        if not os.path.lexists(link):
            os.symlink(exe, link)
        return link

    def _spawn(self, argv, cwd=None):
        p = subprocess.Popen(argv, cwd=cwd or self.root)
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        want = os.path.basename(argv[0])[:15]
        for _ in range(100):  # until the exec has landed and comm is the new image's
            with open(f"/proc/{p.pid}/comm") as f:
                if f.read().strip() == want:
                    break
            time.sleep(0.01)
        return p

    def test_proc_scan_finds_our_fake_desk_claude_with_no_forks(self):
        p = self._spawn([self._as_claude(self.sleep), "30"])
        calls = []
        real = subprocess.run
        subprocess.run = lambda *a, **k: calls.append(a) or real(*a, **k)
        self.assertIn(p.pid, rc_desk.desktop_sessions("proj"))
        self.assertEqual(calls, [])  # /proc read directly: no pgrep, no ps

    def test_headless_claude_is_invisible_to_the_real_scan(self):
        exe = self._as_claude(os.path.realpath(sys.executable))
        p = self._spawn([exe, "-c", "import time; time.sleep(30)", "-p"])
        self.assertNotIn(p.pid, rc_desk.desktop_sessions("proj"))
        self.assertEqual(rc_desk.takeover("proj"), [])
        self.assertIsNone(p.poll())

    def test_takeover_kills_through_a_pidfd(self):
        p = self._spawn([self._as_claude(self.sleep), "30"])
        self.assertEqual(rc_desk.takeover("proj"), [p.pid])
        self.assertEqual(p.wait(timeout=5), -signal.SIGTERM)

    def test_a_recycled_pid_that_is_not_a_claude_is_never_signalled(self):
        # the scan saw a claude at this pid; by kill time it is something else (here: a
        # plain sleep we own). The post-open re-verification must refuse to signal it.
        p = self._spawn([self.sleep, "30"])
        self.assertEqual(rc_desk._kill_pidfds([p.pid], self.root, False), [])
        time.sleep(0.1)
        self.assertIsNone(p.poll())  # still running

    def test_a_claude_outside_the_project_is_never_signalled(self):
        other = os.path.join(rc_config.PARENT, "projx")  # the sibling-prefix dir
        os.makedirs(other)
        p = self._spawn([self._as_claude(self.sleep), "30"], cwd=other)
        self.assertEqual(rc_desk._kill_pidfds([p.pid], self.root, False), [])
        self.assertEqual(
            rc_desk._kill_pidfds([p.pid], self.root, True), []
        )  # wrong kind
        self.assertIsNone(p.poll())

    def test_straggler_gets_sigkill_after_the_grace(self):
        keep(self, (rc_desk, "GRACE"))
        rc_desk.GRACE = 0.3
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        exe = self._as_claude(os.path.realpath(sys.executable))
        p = subprocess.Popen([exe, "-c", code], cwd=self.root, stdout=subprocess.PIPE)
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.addCleanup(p.stdout.close)
        self.assertEqual(p.stdout.readline().strip(), b"ready")  # SIGTERM now ignored
        self.assertEqual(rc_desk._kill_pidfds([p.pid], self.root, False), [p.pid])
        self.assertEqual(p.wait(timeout=5), -signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
