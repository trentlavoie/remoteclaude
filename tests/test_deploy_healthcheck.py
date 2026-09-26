"""rc_healthcheck.py as the headless-Linux deployment uses it: the alert always reaches the
journal (stderr), the phone push only goes to an http(s) URL and never leaks that URL (the
ntfy topic is the secret), a desktop notifier can't hang the run, and a run that found a
problem exits non-zero so systemd marks it failed."""

import http.client
import io
import subprocess
import unittest
import unittest.mock
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

import rc_healthcheck as hc

from tests._harness import keep

SECRET_URL = "https://ntfy.example/rc-s3cr3t-topic"


class NotifyTest(unittest.TestCase):
    def setUp(self):
        keep(
            self,
            (hc, "NOTIFY_URL"),
            (hc.urllib.request, "urlopen"),
            (hc.subprocess, "run"),
            (hc.shutil, "which"),
            (hc.platform, "system"),
        )
        self.pushed: list = []
        self.desktop: list = []
        hc.urllib.request.urlopen = lambda req, timeout=None: self.pushed.append(
            (req, timeout)
        )
        hc.subprocess.run = lambda argv, **k: self.desktop.append((argv, k))
        hc.shutil.which = lambda name: None  # a headless box: no notify-send
        hc.platform.system = lambda: "Linux"

    def _notify(self, title="t", msg="m") -> str:
        err = io.StringIO()
        with redirect_stderr(err):
            hc.notify(title, msg)
        return err.getvalue()

    def test_alert_always_reaches_stderr_even_with_no_channel(self):
        hc.NOTIFY_URL = ""
        out = self._notify("RC launcher: low disk", "/ has 1.0 GiB free")
        self.assertIn("ALERT RC launcher: low disk: / has 1.0 GiB free", out)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.desktop, [])  # no notify-send on PATH -> not attempted

    def test_journal_gets_a_warning_priority_prefix(self):
        hc.NOTIFY_URL = ""
        with unittest.mock.patch.dict(hc.os.environ, {"JOURNAL_STREAM": "8:123"}):
            self.assertTrue(self._notify().startswith("<4>ALERT "))
        with unittest.mock.patch.dict(hc.os.environ, {}, clear=True):
            self.assertTrue(self._notify().startswith("ALERT "))

    def test_https_push_carries_title_body_and_a_timeout(self):
        hc.NOTIFY_URL = SECRET_URL
        self._notify("title", "body")
        (req, timeout), *_ = self.pushed
        self.assertEqual(req.full_url, SECRET_URL)
        self.assertEqual(req.data, b"body")
        self.assertEqual(req.get_header("Title"), "title")
        self.assertEqual(timeout, hc.PUSH_TIMEOUT)

    def test_only_http_schemes_are_pushed_to(self):
        for url in ("file:///etc/passwd", "ftp://x/y", "data:,x", "ntfy.sh/topic"):
            with self.subTest(url=url):
                self.pushed.clear()
                hc.NOTIFY_URL = url
                out = self._notify()
                self.assertEqual(self.pushed, [])
                self.assertIn("RC_NOTIFY_URL ignored", out)
                self.assertNotIn(url, out)
        hc.NOTIFY_URL = "http://100.64.0.9/rc"  # a tailnet ntfy over WireGuard: allowed
        self._notify()
        self.assertEqual(len(self.pushed), 1)

    def test_a_failed_push_is_reported_without_the_url(self):
        hc.NOTIFY_URL = SECRET_URL
        for exc in (
            urllib.error.URLError(f"cannot reach {SECRET_URL}"),
            ValueError(f"unknown url type: {SECRET_URL!r}"),
            http.client.RemoteDisconnected("gone"),
            http.client.BadStatusLine(SECRET_URL),  # an HTTPException, not an OSError
            TimeoutError(),
        ):
            with self.subTest(exc=type(exc).__name__):

                def boom(req, timeout=None, exc=exc):
                    raise exc

                hc.urllib.request.urlopen = boom
                # must not raise: the other probes still have to run
                out = self._notify()
                self.assertIn(f"alert push failed: {type(exc).__name__}", out)
                self.assertNotIn("s3cr3t", out)

    def test_desktop_notifier_that_hangs_or_is_missing_cannot_break_the_run(self):
        hc.NOTIFY_URL = ""
        hc.shutil.which = lambda name: "/usr/bin/notify-send"
        for exc in (subprocess.TimeoutExpired("notify-send", 10), OSError("gone")):

            def hang(argv, exc=exc, **k):
                raise exc

            hc.subprocess.run = hang
            self._notify()  # suppressed
        hc.subprocess.run = lambda argv, **k: self.desktop.append((argv, k))
        self._notify()
        argv, kwargs = self.desktop[-1]
        self.assertEqual(argv[0], "notify-send")
        self.assertEqual(kwargs["timeout"], hc.PUSH_TIMEOUT)

    def test_macos_text_is_passed_as_argv_not_spliced_into_applescript(self):
        hc.NOTIFY_URL = ""
        hc.platform.system = lambda: "Darwin"
        msg = 'x" & (do shell script "touch /tmp/pwned") & "'
        self._notify("t", msg)
        argv, _ = self.desktop[-1]
        self.assertEqual(argv[0], "osascript")
        script = " ".join(a for a in argv[1:-2])
        self.assertNotIn(msg, script)  # never inside the program text
        self.assertEqual(argv[-2:], [msg, "t"])  # only as run-handler arguments


class MainExitStatusTest(unittest.TestCase):
    def setUp(self):
        keep(
            self,
            (hc, "auth_status"),
            (hc, "check_launcher"),
            (hc, "check_disk"),
            (hc, "notify"),
        )
        hc.notify = lambda t, m: None

    def _main(self, login, version, disk_low):
        hc.auth_status = lambda timeout=None: (login, "")
        hc.check_launcher = lambda: version
        hc.check_disk = lambda: disk_low
        with redirect_stdout(io.StringIO()):
            return hc.main()

    def test_problem_count_drives_the_exit_status(self):
        self.assertEqual(self._main("ok", "abc", 0), 0)
        self.assertEqual(self._main("loggedout", "abc", 0), 1)
        self.assertEqual(self._main("ok", "", 0), 1)
        self.assertEqual(self._main("ok", "abc", 2), 1)  # disk counts once
        self.assertEqual(self._main("unknown", "", 1), 3)

    def test_check_disk_counts_low_paths(self):
        from types import SimpleNamespace

        keep(self, (hc.os, "statvfs"))
        hc.os.statvfs = lambda p: SimpleNamespace(f_frsize=1, f_bavail=0)
        self.assertEqual(hc.check_disk(), 2)  # boot volume and the share
        hc.os.statvfs = lambda p: SimpleNamespace(f_frsize=1, f_bavail=10 * 1024**3)
        self.assertEqual(hc.check_disk(), 0)


if __name__ == "__main__":
    unittest.main()
