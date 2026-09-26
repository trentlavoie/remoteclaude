"""rc_guard.py — the desk-side launch guard's detection, skip rules, and menu
actions, on a mocked tmux. The exit-code contract (0 proceed / 2 fresh / 1 abort)
is what the rc_guard.sh shim keys on, so every branch is pinned here."""

import contextlib
import os
import pty
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import unittest.mock
from types import SimpleNamespace

import rc_guard
import rc_tmux

from tests._harness import proc


@contextlib.contextmanager
def _cwd(path):
    with unittest.mock.patch.object(rc_guard.os, "getcwd", lambda: path):
        yield


class GuardHarness(unittest.TestCase):
    def setUp(self):
        self.calls: list[list[str]] = []
        # has-session answers, consumed in order; the last one repeats.
        self.has_session_rcs = [1]
        self.real_state_tag = rc_guard.state_tag  # HelpersTest exercises the real one
        # sys.stdin/stderr are C-level (attributes unassignable): swap the objects.
        self.stdin = SimpleNamespace(isatty=lambda: True)
        self.stderr = unittest.mock.MagicMock(isatty=lambda: True)
        for name, obj in (("stdin", self.stdin), ("stderr", self.stderr)):
            p = unittest.mock.patch.object(rc_guard.sys, name, obj)
            p.start()
            self.addCleanup(p.stop)
        for obj, name in (
            (subprocess, "run"),
            (time, "sleep"),
            (rc_guard, "read_key"),
            (rc_guard, "state_tag"),
            (rc_guard, "PARENT"),
            (rc_guard, "TAKEOVER_WAIT"),
            (rc_tmux, "TMUX"),
        ):
            self.addCleanup(setattr, obj, name, getattr(obj, name))
        self.addCleanup(os.environ.pop, "TMUX", None)
        os.environ.pop("TMUX", None)

        def run(cmd, **kw):
            self.calls.append(cmd)
            if "has-session" in cmd:
                rc = self.has_session_rcs[0]
                if len(self.has_session_rcs) > 1:
                    self.has_session_rcs.pop(0)
                return proc(returncode=rc)
            return proc()

        subprocess.run = run
        time.sleep = lambda s: None
        rc_guard.state_tag = lambda: ""
        rc_guard.PARENT = "/parent"
        rc_guard.TAKEOVER_WAIT = 0.01
        # the assertions below spell the argv out, and the real default is whichever
        # tmux this host resolved at import (/opt/homebrew/bin/tmux on the Mac)
        rc_tmux.TMUX = "tmux"

    def _cmds(self):
        return [" ".join(c) for c in self.calls]


class DetectionTest(GuardHarness):
    def test_project_root_and_subdir_resolve_to_project_session(self):
        self.has_session_rcs = [0]
        for cwd in ("/parent/alpha", "/parent/alpha/deep/er"):
            self.assertEqual(rc_guard.live_sess(cwd, "/parent"), "rc-alpha")

    def test_target_is_exact_match_form(self):
        # A bare -t prefix-matches: a live rc-alpha-sub would count as rc-alpha
        # (verified against a live tmux). The `=` pin is the whole point.
        self.has_session_rcs = [0]
        rc_guard.live_sess("/parent/alpha", "/parent")
        self.assertIn("tmux has-session -t =rc-alpha", self._cmds())

    def test_outside_parent_or_no_session_is_none(self):
        self.assertIsNone(rc_guard.live_sess("/elsewhere/alpha", "/parent"))
        self.has_session_rcs = [1]
        self.assertIsNone(rc_guard.live_sess("/parent/alpha", "/parent"))
        # /parentx is a SIBLING of /parent, not inside it (the cwd-boundary class)
        self.assertIsNone(rc_guard.live_sess("/parentx/alpha", "/parent"))

    def test_trailing_slash_and_symlinked_parent_still_detect(self):
        # Panel + defect-review lead: `parent + os.sep` with a trailing slash never
        # matched, and a symlinked parent never matched getcwd()'s physical path —
        # both silently disabled the guard (fail-open).
        self.has_session_rcs = [0]
        self.assertEqual(rc_guard.live_sess("/parent/alpha", "/parent/"), "rc-alpha")
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real")
            os.makedirs(os.path.join(real, "alpha"))
            link = os.path.join(tmp, "link")
            os.symlink(real, link)
            physical_cwd = os.path.realpath(os.path.join(real, "alpha"))
            self.assertEqual(rc_guard.live_sess(physical_cwd, link), "rc-alpha")

    def test_symlinked_project_dir_matches_through_logical_pwd(self):
        # Second-panel lead: getcwd() is physical, so ~/projects/foo -> /elsewhere
        # never matched parent at all; the shell's logical $PWD does, and rc-foo is
        # the name the launcher gave the session.
        self.has_session_rcs = [0]
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "projects")
            target = os.path.join(tmp, "elsewhere", "foo")
            os.makedirs(parent)
            os.makedirs(target)
            os.symlink(target, os.path.join(parent, "foo"))
            logical = os.path.join(parent, "foo")
            physical = os.path.realpath(target)
            with unittest.mock.patch.dict(os.environ, {"PWD": logical}):
                self.assertEqual(rc_guard.live_sess(physical, parent), "rc-foo")
            # a stale $PWD naming ANOTHER project under parent is ignored, not trusted:
            # without the samefile check it would win and answer rc-other
            stale = os.path.join(parent, "other")
            with unittest.mock.patch.dict(os.environ, {"PWD": stale}):
                self.assertIsNone(rc_guard.live_sess(physical, parent))

    def test_root_parent_still_matches(self):
        self.has_session_rcs = [0]
        self.assertEqual(rc_guard.live_sess("/alpha/x", "/"), "rc-alpha")

    def test_missing_tmux_means_no_session_not_a_crash(self):
        # Without this the traceback exits 1, the shim reads "quit", and every desk
        # launch under ~/projects is blocked on a machine that has no tmux at all.
        def run(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        subprocess.run = run
        self.assertIsNone(rc_guard.live_sess("/parent/alpha", "/parent"))
        with _cwd("/parent/alpha"):
            self.assertEqual(rc_guard.main([]), rc_guard.PROCEED)


class SkipRulesTest(GuardHarness):
    def test_non_tty_stdin_or_stderr_proceeds_without_touching_tmux(self):
        self.stdin.isatty = lambda: False
        self.assertEqual(rc_guard.main([]), rc_guard.PROCEED)
        self.stdin.isatty = lambda: True
        self.stderr.isatty = lambda: False  # menu would be invisible; don't block
        self.has_session_rcs = [0]  # a live session that WOULD prompt on a tty
        with _cwd("/parent/alpha"):
            self.assertEqual(rc_guard.main([]), rc_guard.PROCEED)
        self.assertEqual(self.calls, [])

    def test_caller_controlled_session_flags_skip_the_prompt(self):
        for flag in ("-c", "--continue", "-r", "--resume", "-p", "--print", "--new"):
            self.assertEqual(rc_guard.main([flag, "hi"]), rc_guard.PROCEED)
        self.assertEqual(self.calls, [])

    def test_non_session_invocations_skip_the_prompt(self):
        # `claude --version` in a project with a live rc session must not pop a menu.
        for argv in (
            ["--version"],
            ["-v"],
            ["--help"],
            ["--resume=abc123"],
            ["--session-id", "x"],
            ["mcp", "list"],
            ["doctor"],
            ["update"],
        ):
            self.assertEqual(rc_guard.main(argv), rc_guard.PROCEED, argv)
        self.assertEqual(self.calls, [])
        # a prompt IS a session
        self.assertFalse(rc_guard.caller_controls(["fix the bug"]))

    def test_no_live_session_proceeds(self):
        with _cwd("/parent/alpha"):
            self.assertEqual(rc_guard.main([]), rc_guard.PROCEED)


class HelpersTest(GuardHarness):
    def test_state_tag_runs_rc_status_with_this_interpreter(self):
        seen = []

        def run(cmd, **kw):
            seen.append((cmd, kw))
            return proc(stdout="● rc:working\n")

        subprocess.run = run
        self.assertEqual(self.real_state_tag(), "● rc:working")
        cmd, kw = seen[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("rc_status.py"))
        self.assertEqual(kw.get("timeout"), 2)

    def test_state_tag_timeout_is_empty_not_a_hang(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        subprocess.run = run
        self.assertEqual(self.real_state_tag(), "")

    def test_read_key_single_byte_from_a_real_pty(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)

        # read_key's setraw defaults to TCSAFLUSH, which discards input queued before
        # the raw switch — so a byte written early hangs the read. Wait for raw mode
        # (ICANON clear) rather than racing a timer against a possibly slow runner.
        def write_when_raw():
            while termios.tcgetattr(slave)[3] & termios.ICANON:
                pass
            os.write(master, b"t")

        writer = threading.Thread(target=write_when_raw, daemon=True)
        with (
            os.fdopen(slave) as slave_f,
            unittest.mock.patch.object(rc_guard.sys, "stdin", slave_f),
        ):
            writer.start()
            self.assertEqual(rc_guard.read_key(), "t")


class MenuTest(GuardHarness):
    def setUp(self):
        super().setUp()
        self.has_session_rcs = [0]

    def _choose(self, key):
        rc_guard.read_key = lambda: key
        with _cwd("/parent/alpha"):
            return rc_guard.main([])

    def test_attach_runs_tmux_attach_and_aborts_the_launch(self):
        self.assertEqual(self._choose("a"), rc_guard.ABORT)
        self.assertIn("tmux attach -t =rc-alpha", self._cmds())

    def test_attach_inside_tmux_uses_switch_client(self):
        # `tmux attach` inside an existing client refuses ("sessions should be
        # nested with care"); the user would get an error AND no launch.
        os.environ["TMUX"] = "/tmp/tmux-501/default,123,0"
        self.assertEqual(self._choose("a"), rc_guard.ABORT)
        self.assertIn("tmux switch-client -t =rc-alpha", self._cmds())
        self.assertNotIn("tmux attach -t =rc-alpha", self._cmds())

    def test_takeover_sigint_then_waits_then_proceeds_once_dead(self):
        # detect: alive; after C-c: alive once, then gone -> no kill-session needed
        self.has_session_rcs = [0, 0, 1]
        self.assertEqual(self._choose("t"), rc_guard.PROCEED)
        cmds = self._cmds()
        self.assertIn("tmux send-keys -t =rc-alpha: C-c", cmds)
        self.assertNotIn("tmux kill-session -t =rc-alpha", cmds)

    def test_takeover_kills_when_sigint_is_ignored_and_pins_the_target(self):
        # detect: alive; loop check: alive (wait already expired); pre-kill: alive
        # -> kill-session; post-kill: gone
        self.has_session_rcs = [0, 0, 0, 1]
        rc_guard.TAKEOVER_WAIT = 0
        self.assertEqual(self._choose("t"), rc_guard.PROCEED)
        cmds = self._cmds()
        sigint = cmds.index("tmux send-keys -t =rc-alpha: C-c")
        kill = cmds.index("tmux kill-session -t =rc-alpha")
        self.assertLess(sigint, kill)  # SIGINT (relay deregister) before the kill

    def test_takeover_refuses_to_launch_into_a_session_that_would_not_die(self):
        self.has_session_rcs = [0]  # alive forever
        rc_guard.TAKEOVER_WAIT = 0
        self.assertEqual(self._choose("t"), rc_guard.ABORT)
        self.assertIn("tmux kill-session -t =rc-alpha", self._cmds())

    def test_fresh_returns_the_fresh_code(self):
        self.assertEqual(self._choose("f"), rc_guard.FRESH)

    def test_quit_and_unknown_keys_abort(self):
        for key in ("q", "\r", "x", "\x03"):
            self.calls.clear()
            self.assertEqual(self._choose(key), rc_guard.ABORT)
            self.assertFalse([c for c in self._cmds() if "has-session" not in c])


if __name__ == "__main__":
    unittest.main()
