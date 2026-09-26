"""deploy/tailscale-serve.sh against a fake `tailscale` (and `ss`) on PATH: it publishes the
loopback launcher with `tailscale serve` only, is idempotent, never calls `tailscale funnel`,
refuses while any Funnel is on or while something else owns the port, and prints the URL and
the RC_ALLOWED_HOSTS / RC_TAILSCALE_USERS lines."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
FQDN = "rc-host.tail1234.ts.net"
FAKE_TAILSCALE = r"""#!/bin/sh
d="$FAKE_TS_DIR"
echo "$*" >>"$d/calls"
case "$*" in
  "status --json") cat "$d/status.json" ;;
  "serve status --json") cat "$d/serve.json" 2>/dev/null || echo '{}' ;;
  "serve status") echo "(serve config)" ;;
  serve\ --bg\ *) exit "${FAKE_TS_SERVE_RC:-0}" ;;
  funnel*) exit 99 ;;
esac
"""
FAKE_SS = """#!/bin/sh
cat "$FAKE_TS_DIR/ss" 2>/dev/null || true
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TailscaleServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # PATH is ONLY this dir: the fakes plus the few real tools the script uses. A real
        # `tailscale` elsewhere on the host can never be reached, even when a test removes
        # the fake (an earlier version of this file fell through to /usr/bin/tailscale).
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in ("python3", "awk", "grep", "id", "cat"):
            real = shutil.which(tool)
            assert real, f"{tool} is needed by the test"
            (self.bin / tool).symlink_to(real)
        for name, body in (("tailscale", FAKE_TAILSCALE), ("ss", FAKE_SS)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.lport = _free_port()  # nothing listens: the /version probe just warns
        self.status(state="Running", certs=[FQDN])

    def status(self, state="Running", certs=(), dns=FQDN + "."):
        (self.tmp / "status.json").write_text(
            json.dumps(
                {
                    "BackendState": state,
                    "Self": {"DNSName": dns, "UserID": 42},
                    "User": {"42": {"LoginName": "me@example.com"}},
                    "CertDomains": list(certs),
                }
            )
        )

    def serve_config(self, proxy=None, port=443, funnel=False):
        hp = f"{FQDN}:{port}"
        cfg: dict = {"TCP": {str(port): {"HTTPS": True}}}
        if proxy:
            cfg["Web"] = {hp: {"Handlers": {"/": {"Proxy": proxy}}}}
        if funnel:
            cfg["AllowFunnel"] = {hp: True}
        (self.tmp / "serve.json").write_text(json.dumps(cfg))

    def run_script(self, *args, **env):
        r = subprocess.run(
            [BASH, str(REPO / "deploy/tailscale-serve.sh"), *args],
            env={
                "PATH": str(self.bin),
                "HOME": str(self.tmp),
                "FAKE_TS_DIR": str(self.tmp),
                "RC_LAUNCHER_PORT": str(self.lport),
                **env,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        calls = self.tmp / "calls"
        self.calls = calls.read_text().splitlines() if calls.exists() else []
        self.assertFalse(
            [c for c in self.calls if c.startswith("funnel")], "funnel was called"
        )
        return r

    def serve_calls(self):
        return [c for c in self.calls if c.startswith("serve --")]

    def test_publishes_loopback_launcher_and_prints_the_lock_lines(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        target = f"http://127.0.0.1:{self.lport}"
        self.assertEqual(self.serve_calls(), [f"serve --bg --https=443 {target}"])
        self.assertIn(f"https://{FQDN}/?token=<token>", r.stdout)
        self.assertIn(f"RC_ALLOWED_HOSTS={FQDN}\n", r.stdout)
        self.assertIn("RC_TAILSCALE_USERS=me@example.com", r.stdout)

    def test_idempotent_when_already_serving(self):
        self.serve_config(proxy=f"http://127.0.0.1:{self.lport}")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.serve_calls(), [])
        self.assertIn("already serving", r.stdout)

    def test_alternate_port_for_a_443_conflict(self):
        r = self.run_script(RC_TS_HTTPS_PORT="8443")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            self.serve_calls(),
            [f"serve --bg --https=8443 http://127.0.0.1:{self.lport}"],
        )
        self.assertIn(f"https://{FQDN}:8443/", r.stdout)
        self.assertIn(f"RC_ALLOWED_HOSTS={FQDN},{FQDN}:8443", r.stdout)

    def test_refuses_funnel_in_any_form(self):
        r = self.run_script("--funnel")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("never enables Tailscale Funnel", r.stderr)
        self.assertEqual(self.calls, [])
        self.serve_config(funnel=True)
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Funnel is ON", r.stderr)
        self.assertEqual(self.serve_calls(), [])

    def test_does_not_replace_someone_elses_handler(self):
        self.serve_config(proxy="http://127.0.0.1:3000")
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not replacing", r.stderr)
        self.assertEqual(self.serve_calls(), [])

    def test_refuses_when_the_launcher_port_is_on_all_interfaces(self):
        (self.tmp / "ss").write_text(
            f"LISTEN 0 5 0.0.0.0:{self.lport} 0.0.0.0:* users:((python3))\n"
        )
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ALL interfaces", r.stderr)
        self.assertEqual(self.serve_calls(), [])

    def test_prerequisites_are_explained(self):
        for kwargs, needle in (
            ({"state": "NeedsLogin", "certs": [FQDN]}, "not connected"),
            ({"certs": [], "dns": FQDN + "."}, "HTTPS certificates are off"),
            ({"certs": [FQDN], "dns": ""}, "MagicDNS"),
        ):
            with self.subTest(needle=needle):
                self.status(**kwargs)
                r = self.run_script()
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(needle, r.stderr)
                self.assertEqual(self.serve_calls(), [])

    def test_serve_failure_points_at_the_operator_setting(self):
        r = self.run_script(FAKE_TS_SERVE_RC="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tailscale set --operator=", r.stderr)

    def test_off_removes_only_this_port_and_status_changes_nothing(self):
        r = self.run_script("--off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.serve_calls(), ["serve --https=443 off"])
        (self.tmp / "calls").unlink()
        self.serve_config(funnel=True)
        r = self.run_script("--status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.serve_calls(), [])
        self.assertIn("Funnel (public internet) is ON", r.stdout)

    def test_bad_ports_are_rejected(self):
        for bad in ("0", "65536", "44x", "443 --funnel"):
            with self.subTest(port=bad):
                r = self.run_script(RC_TS_HTTPS_PORT=bad)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self.calls, [])

    def test_missing_tailscale_points_at_the_install_doc(self):
        os.unlink(self.bin / "tailscale")
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("docs/TAILSCALE.md", r.stderr)


if __name__ == "__main__":
    unittest.main()
