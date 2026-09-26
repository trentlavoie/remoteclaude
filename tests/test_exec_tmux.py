"""The tmux tier's hardening: exact targets (pane verbs need `=name:`), the opt-in dedicated
socket, the scrubbed client env, the shell-inert launch command, and bounded calls.

The LiveTmux cases drive a REAL tmux on a throwaway `-L rc-test-<random>` server that each
test creates (with -f /dev/null, so no user config or plugins load) and kills in cleanup —
the default server and the user's sessions are never touched. They pin the bugs against
tmux itself, not against a mock of what tmux was assumed to do."""

import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock

import rc_config
import rc_guard
import rc_sessions
import rc_settings
import rc_tmux

from tests._harness import MockedToolsCase, env, keep, proc, spawn_ok

HAVE_TMUX = bool(shutil.which("tmux"))


@unittest.skipUnless(HAVE_TMUX, "tmux not installed")
class LiveTmuxTest(unittest.TestCase):
    def setUp(self):
        keep(self, (rc_tmux, "SOCKET"), (rc_tmux, "TMUX"))
        rc_tmux.TMUX = shutil.which("tmux") or "tmux"
        rc_tmux.SOCKET = f"rc-test-{secrets.token_hex(4)}"
        self.addCleanup(
            subprocess.run,
            [rc_tmux.TMUX, "-L", rc_tmux.SOCKET, "kill-server"],
            capture_output=True,
        )
        # start the throwaway server config-free; every rc_tmux call then joins it
        self._new("seed", "cat")

    def _new(self, sess, *cmd, window="w"):
        subprocess.run(
            [rc_tmux.TMUX, "-L", rc_tmux.SOCKET, "-f", "/dev/null", "new-session"]
            + ["-d", "-s", sess, "-n", window, "-x", "80", "-y", "24", *cmd],
            check=True,
            capture_output=True,
        )

    def _pane_text(self, sess):
        time.sleep(0.3)
        return rc_tmux.tmux("capture-pane", "-p", "-t", rc_tmux.pane(sess)).stdout

    def test_bare_exact_target_misses_the_pane_on_real_tmux(self):
        # the bug this fork fixed: on tmux 3.4 `send-keys -t =rc-x` is "can't find pane"
        # (so graceful_stop's C-c never landed and every stop became a SIGHUP kill);
        # `=rc-x:` reaches the session's pane
        self._new("rc-x", "cat")
        bare = rc_tmux.tmux("send-keys", "-t", "=rc-x", "BARE", "Enter")
        fixed = rc_tmux.tmux("send-keys", "-t", rc_tmux.pane("rc-x"), "FIXED", "Enter")
        self.assertEqual(fixed.returncode, 0)
        text = self._pane_text("rc-x")
        self.assertIn("FIXED", text)
        if bare.returncode == 0:  # a tmux that resolves it must at least hit rc-x
            self.assertIn("BARE", text)

    def test_pane_target_never_resolves_a_same_named_window_elsewhere(self):
        # `=rc-x` as a WINDOW target matched the user's window named rc-x in another
        # session; `=rc-x:` is the session, whatever windows are called
        self._new("rc-x", "cat", window="main")
        self._new("user", "cat", window="rc-x")
        out = rc_tmux.tmux(
            "list-panes", "-t", rc_tmux.pane("rc-x"), "-F", "#{session_name}"
        ).stdout.split()
        self.assertEqual(out, ["rc-x"])
        rc_tmux.tmux("send-keys", "-t", rc_tmux.pane("rc-x"), "ONLY-RC", "Enter")
        self.assertNotIn("ONLY-RC", self._pane_text("user"))

    def test_graceful_stop_delivers_ctrl_c_and_closes_without_the_kill(self):
        # a pane process that exits on the first SIGINT: graceful_stop must close it via
        # the C-c alone (the kill-session fallback is what used to fire every time)
        self._new("rc-g", "sh", "-c", "trap 'exit 0' INT; while :; do sleep 1; done")
        time.sleep(0.3)
        with unittest.mock.patch.object(rc_tmux, "tmux", wraps=rc_tmux.tmux) as spy:
            self.assertTrue(rc_tmux.graceful_stop("rc-g", wait=5))
        verbs = [c.args[0] for c in spy.call_args_list]
        self.assertNotIn("kill-session", verbs)

    def test_launch_command_is_shell_inert_through_real_tmux(self):
        # the new-session command is one sh -c string: every word must survive as ONE
        # literal argument — a crafted value can't run a command or split
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        canary = os.path.join(work, "PWNED")
        evil = f"x; touch {canary}; $(touch {canary}) `touch {canary}`"
        out = os.path.join(work, "argv")
        cmd = ["/bin/sh", "-c", 'printf "%s\\n" "$@" > "$0"', out, evil, "a b"]
        rc = subprocess.run(
            rc_tmux.argv("new-session", "-d", "-s", "rc-q", "--", shlex.join(cmd)),
            capture_output=True,
        ).returncode
        self.assertEqual(rc, 0)
        for _ in range(40):
            if os.path.exists(out):
                break
            time.sleep(0.05)
        with open(out) as f:
            self.assertEqual(f.read().splitlines(), [evil, "a b"])
        self.assertFalse(os.path.exists(canary))

    def test_running_sees_only_the_dedicated_server(self):
        self._new("rc-alpha", "cat")
        self.assertEqual(rc_tmux.running(), {"alpha"})  # "seed" is not rc-*


class TmuxUnitTest(unittest.TestCase):
    def setUp(self):
        keep(self, (rc_tmux, "SOCKET"), (rc_tmux, "TMUX"), (subprocess, "run"))
        rc_tmux.TMUX = "tmux"

    def test_argv_adds_the_socket_only_when_configured(self):
        rc_tmux.SOCKET = ""
        self.assertEqual(rc_tmux.argv("ls"), ["tmux", "ls"])  # upstream: default server
        rc_tmux.SOCKET = "rc"
        self.assertEqual(rc_tmux.argv("ls"), ["tmux", "-L", "rc", "ls"])

    def test_socket_name_validation(self):
        self.assertEqual(rc_tmux._socket(""), "")
        self.assertEqual(rc_tmux._socket("rc-launcher_2"), "rc-launcher_2")
        with unittest.mock.patch("sys.stderr"):
            # a path or option-shaped value still isolates, under the fixed name
            for bad in ("../x", "-x", "a b", "x" * 65, "/tmp/sock"):
                self.assertEqual(rc_tmux._socket(bad), "rc-launcher", bad)

    def test_timeout_parse_never_breaks_the_import(self):
        self.assertEqual(rc_tmux._timeout(None), 10.0)
        self.assertEqual(rc_tmux._timeout("2.5"), 2.5)
        for bad in ("", "abc", "0", "-1", "nan", "inf", "1e9"):
            self.assertEqual(rc_tmux._timeout(bad), 10.0, bad)

    def test_every_call_is_bounded(self):
        seen = {}
        subprocess.run = lambda cmd, **kw: seen.update(kw) or proc()
        rc_tmux.tmux("ls")
        self.assertEqual(seen.get("timeout"), rc_tmux.TIMEOUT)

    def test_running_tolerates_a_wedged_server(self):
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        subprocess.run = hang
        self.assertEqual(rc_tmux.running(), set())

    def test_client_env_drops_secrets(self):
        env(self, RC_LAUNCHER_TOKEN="tok", RC_NOTIFY_URL="https://ntfy/x", KEEP="1")
        e = rc_tmux.client_env()
        self.assertNotIn("RC_LAUNCHER_TOKEN", e)
        self.assertNotIn("RC_NOTIFY_URL", e)
        self.assertEqual(e["KEEP"], "1")

    def test_same_server(self):
        rc_tmux.SOCKET = ""
        self.assertTrue(rc_tmux.same_server("/tmp/tmux-1000/default,1,0"))
        rc_tmux.SOCKET = "rc"
        self.assertFalse(rc_tmux.same_server("/tmp/tmux-1000/default,1,0"))
        self.assertTrue(rc_tmux.same_server("/tmp/tmux-1000/rc,1,0"))


class GuardAttachTest(unittest.TestCase):
    def setUp(self):
        keep(self, (rc_tmux, "SOCKET"), (rc_tmux, "TMUX"), (subprocess, "run"))
        rc_tmux.TMUX = "tmux"
        self.calls = []
        subprocess.run = lambda cmd, **kw: self.calls.append((cmd, kw)) or proc()

    def test_attach_from_inside_another_server_nests_without_tmux_env(self):
        # the user sits in their own tmux; the launcher's sessions are on RC_TMUX_SOCKET:
        # switch-client can't cross servers, so attach nested, $TMUX dropped
        rc_tmux.SOCKET = "rc"
        env(self, TMUX="/tmp/tmux-1000/default,9,0")
        rc_guard.attach("rc-alpha")
        cmd, kw = self.calls[-1]
        self.assertEqual(cmd, ["tmux", "-L", "rc", "attach", "-t", "=rc-alpha"])
        self.assertNotIn("TMUX", kw["env"])

    def test_attach_inside_the_same_server_switches(self):
        rc_tmux.SOCKET = "rc"
        env(self, TMUX="/tmp/tmux-1000/rc,9,0")
        rc_guard.attach("rc-alpha")
        cmd, kw = self.calls[-1]
        self.assertEqual(cmd, ["tmux", "-L", "rc", "switch-client", "-t", "=rc-alpha"])
        self.assertIsNone(kw["env"])

    def test_live_sess_tolerates_a_wedged_tmux(self):
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        subprocess.run = hang
        self.assertIsNone(rc_guard.live_sess("/parent/alpha", "/parent"))


class SpawnCommandTest(MockedToolsCase):
    """The launch argv tmux receives: quoted, after `--`, scrubbed env, bounded."""

    def _newsession(self):
        return next(c for c in self.calls if "new-session" in " ".join(map(str, c)))

    def test_command_is_shlex_quoted_after_double_dash(self):
        rc_settings.RESUME, rc_settings.SPAWN = "off", "same-dir"
        self.responses = spawn_ok()
        # a hostile model can't come from the route (allowlisted), but the quoting must
        # not depend on that: whatever the words, sh gets them back verbatim
        evil = "m; touch /tmp/x $(id)"
        self.assertEqual(rc_sessions.launch("proj", evil), ("launched", None))
        ns = self._newsession()
        self.assertEqual(ns[-2], "--")
        self.assertEqual(shlex.split(ns[-1]), rc_sessions.fresh_cmd("proj", evil))

    def test_new_session_gets_scrubbed_env_and_a_timeout(self):
        rc_settings.RESUME = "off"
        env(self, RC_LAUNCHER_TOKEN="secret-tok")
        self.responses = spawn_ok()
        seen, base = {}, self._run

        def run(cmd, **kw):
            if "new-session" in cmd:
                seen.update(kw)
            return base(cmd, **kw)

        subprocess.run = run
        self.assertEqual(rc_sessions.launch("proj"), ("launched", None))
        self.assertNotIn("RC_LAUNCHER_TOKEN", seen["env"])
        self.assertEqual(seen["timeout"], rc_tmux.TIMEOUT)

    def test_new_session_timeout_is_a_failed_launch_not_a_500(self):
        rc_settings.RESUME = "off"

        def run(cmd, **kw):
            self.calls.append(cmd)
            if "new-session" in cmd:
                raise subprocess.TimeoutExpired(cmd, 1)
            return spawn_ok()["has-session"] if "has-session" in cmd else proc()

        subprocess.run = run
        self.assertEqual(
            rc_sessions.launch("proj"), ("failed", "tmux new-session timed out")
        )

    def test_socket_reaches_every_launcher_tmux_call(self):
        keep(self, (rc_tmux, "SOCKET"))
        rc_tmux.SOCKET = "rc-test"
        rc_settings.RESUME = "off"
        self.responses = spawn_ok()
        rc_sessions.launch("proj")
        tmux_calls = [c for c in self.calls if c[0] == rc_tmux.TMUX]
        self.assertTrue(tmux_calls)
        self.assertTrue(all(c[1:3] == ["-L", "rc-test"] for c in tmux_calls))
        self.assertTrue(os.path.isdir(os.path.join(rc_config.PARENT, "proj")))


if __name__ == "__main__":
    unittest.main()
