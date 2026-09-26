"""The web tier's security envelope, pinned over the real loopback Handler: the auth
comparison and cookie parsing, the Host allowlist (DNS rebinding), POST + same-origin on
every state change (CSRF), the optional Tailscale identity, the headers every response
carries (CSP nonce, nosniff, frame and referrer policy), the opt-in share, the download
disposition that keeps uploaded HTML from running on the launcher origin, and the upload
guards (size cap, disk floor, planted-symlink temps)."""

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import rc_config
import rc_launcher
import rc_page
import rc_sessions
import rc_settings
import rc_templates

from tests._harness import TOKEN, ServerCase, env, keep, restore_globals

_REAL_LOG = rc_config.log_event  # captured at import, before any setUp silences it
SAME = {"Sec-Fetch-Site": "same-origin"}


class WebCase(ServerCase):
    """ServerCase at the shipped defaults for the gate: state changes are POST-only."""

    def setUp(self):
        super().setUp()
        rc_config.ALLOW_GET_ACTIONS = False
        self.logged: list = []
        rc_config.log_event = lambda *a: self.logged.append(a)
        # a hermetic host: no real ~/projects, state dir or process ever reached
        aux = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, aux, True)
        rc_config.PARENT = os.path.join(aux, "projects")
        os.makedirs(rc_config.PARENT)
        rc_sessions.STATE_DIR = Path(aux, "state")
        rc_config.CLAUDE_JSON = os.path.join(aux, "claude.json")
        rc_config.CLAUDE_PROJECTS = Path(aux, "claude-projects")
        subprocess.run = lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
        os.path.islink = lambda p: False

    def raw(self, request: bytes) -> str:
        """Send bytes on a fresh socket and read to EOF (the server closes)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(request)
        chunks = []
        while chunk := s.recv(65536):
            chunks.append(chunk)
        s.close()
        return b"".join(chunks).decode("latin1")


class AuthTest(WebCase):
    def test_non_ascii_token_is_a_clean_403_not_a_crash(self):
        # compare_digest(str, str) raises TypeError on non-ASCII: an unauthenticated request
        # used to kill the handler thread (dropped socket + a traceback in the log)
        self.assertEqual(self.req("GET", "/?token=%C3%A9", cookie=False)[0], 403)
        for bad in ('rc_token="\\351x"', "rc_token=\u00e9"):  # escaped / raw non-ASCII
            hdr = {"Cookie": bad}
            self.assertEqual(
                self.req("GET", "/status", cookie=False, headers=hdr)[0], 403
            )

    def test_a_malformed_sibling_cookie_does_not_hide_the_token(self):
        # SimpleCookie drops the whole header on one bad pair; the gate must not
        hdr = {"Cookie": f"junk; bad key=1; rc_token={TOKEN}"}
        self.assertEqual(self.req("GET", "/status", cookie=False, headers=hdr)[0], 200)

    def test_any_matching_rc_token_cookie_authenticates(self):
        for jar in (
            f"rc_token=stale; rc_token={TOKEN}",
            f"rc_token={TOKEN}; rc_token=x",
        ):
            hdr = {"Cookie": jar}  # a stale cookie on another Path, either order
            self.assertEqual(
                self.req("GET", "/status", cookie=False, headers=hdr)[0], 200
            )
        hdr = {"Cookie": f'rc_token="{TOKEN}"'}  # quoted value
        self.assertEqual(self.req("GET", "/status", cookie=False, headers=hdr)[0], 200)

    def test_cookie_is_secure_behind_https_or_when_forced(self):
        def cookie(**h):
            return self.req("GET", f"/?token={TOKEN}", cookie=False, headers=h)[1][
                "set-cookie"
            ]

        plain = cookie()
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, plain)
        self.assertNotIn("Secure", plain)  # plain-HTTP LAN setup: Secure would drop it
        self.assertIn("; Secure", cookie(**{"X-Forwarded-Proto": "https"}))
        rc_config.COOKIE_SECURE = True
        self.assertIn("; Secure", cookie())

    def test_tailscale_identity_is_required_on_top_of_the_token(self):
        rc_config.TAILSCALE_USERS = frozenset({"trent@example.com"})
        who = "Tailscale-User-Login"
        self.assertEqual(self.req("GET", "/status")[0], 403)  # token alone
        self.assertEqual(self.req("GET", "/status", headers={who: "eve@x.io"})[0], 403)
        ok = self.req("GET", "/status", headers={who: " Trent@Example.com "})
        self.assertEqual(ok[0], 200)
        # and never instead of it
        hdr = {who: "trent@example.com"}
        self.assertEqual(self.req("GET", "/status", cookie=False, headers=hdr)[0], 403)

    def test_tailscale_header_is_ignored_when_no_allowlist(self):
        hdr = {"Tailscale-User-Login": "anyone@x.io"}
        self.assertEqual(self.req("GET", "/status", headers=hdr)[0], 200)

    def test_version_is_token_free_only_straight_from_this_host(self):
        self.assertEqual(self.req("GET", "/version", cookie=False)[0], 200)  # watchdog
        via = {"X-Forwarded-For": "100.64.0.9"}  # through tailscale serve
        self.assertEqual(self.req("GET", "/version", cookie=False, headers=via)[0], 403)
        status, _, body = self.req("GET", "/version", headers=via)  # with the cookie
        self.assertEqual(
            (status, json.loads(body)["version"]), (200, rc_config.VERSION)
        )

    def test_direct_local_is_false_off_loopback(self):
        fake = SimpleNamespace(client_address=("100.64.0.9", 1), headers={})
        self.assertFalse(rc_launcher.Handler._direct_local(fake))
        fake = SimpleNamespace(client_address=("not-an-ip", 1), headers={})
        self.assertFalse(rc_launcher.Handler._direct_local(fake))


class HostTest(WebCase):
    def test_host_allowlist_unit(self):
        rc_config.ALLOWED_HOSTS = frozenset({"box.tail1.ts.net", ".tail2.ts.net"})
        ok = (
            "127.0.0.1:8787",
            "[::1]:8787",
            "LOCALHOST",
            "192.168.1.5:8787",  # an IP literal can't be a rebinding name
            "box.tail1.ts.net",
            "BOX.tail1.ts.net.",
            "x.tail2.ts.net",
            rc_config.HOST,
            f"{rc_config.HOST}.local",
        )
        bad = (
            "",
            ":80",
            "evil.com",
            "tail2.ts.net",
            "ts.net.evil.com",
            "evil.localhost",
        )
        for h in ok:
            self.assertTrue(rc_launcher.host_allowed(h), h)
        for h in bad:
            self.assertFalse(rc_launcher.host_allowed(h), h)

    def test_rebinding_host_is_421_even_with_the_token_and_on_version(self):
        for path in ("/", "/status", "/version"):
            status, hdrs, _ = self.req("GET", path, headers={"Host": "evil.example"})
            self.assertEqual(status, 421, path)
            self.assertEqual(hdrs.get("connection"), "close")
        # the operator can see which name to add to RC_ALLOWED_HOSTS
        self.assertTrue(any("evil.example" in a[1] for a in self.logged))

    def test_configured_name_passes(self):
        rc_config.ALLOWED_HOSTS = frozenset({"box.tail1.ts.net"})
        hdr = {"Host": "box.tail1.ts.net"}
        self.assertEqual(self.req("GET", "/status", headers=hdr)[0], 200)

    def test_rebinding_put_is_refused_before_any_write(self):
        status, hdrs, _ = self.req(
            "PUT", "/files/r.bin", body=b"x", headers={"Host": "evil.example"}
        )
        self.assertEqual((status, hdrs.get("connection")), (421, "close"))
        self.assertFalse(os.path.exists(os.path.join(self.share, "r.bin")))


class CsrfTest(WebCase):
    def settings_file(self) -> bool:
        return rc_settings.SETTINGS_FILE.exists()

    def test_state_change_by_get_is_405_and_does_nothing(self):
        calls: list = []
        subprocess.run = lambda *a, **k: calls.append(a)
        for path in ("/launch?proj=p", "/stop?proj=p", "/create?proj=newp"):
            status, hdrs, _ = self.req("GET", path)
            self.assertEqual((status, hdrs.get("allow")), (405, "POST"), path)
        status, _, _ = self.req("GET", "/settings?name=fork&on=1")
        self.assertEqual(status, 405)
        self.assertEqual(calls, [])  # no spawn, no git init
        self.assertFalse(self.settings_file())
        self.assertFalse(os.path.exists(os.path.join(rc_config.PARENT, "newp")))

    def test_post_to_a_read_route_is_405(self):
        status, hdrs, _ = self.req("POST", "/status", headers=SAME)
        self.assertEqual((status, hdrs.get("allow")), (405, "GET, HEAD"))

    def test_cross_origin_posts_are_refused(self):
        for hdr in (
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},  # another port / another tailnet node
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
        ):
            status, _, _ = self.req("POST", "/settings?name=fork&on=1", headers=hdr)
            self.assertEqual(status, 403, hdr)
        self.assertFalse(self.settings_file())

    def test_same_origin_and_non_browser_posts_pass(self):
        host = f"127.0.0.1:{self.port}"
        for hdr in (SAME, {"Origin": f"http://{host}"}, {}):  # {} = the app / curl
            status, _, body = self.req("POST", "/settings?name=fork&on=1", headers=hdr)
            self.assertEqual((status, json.loads(body)["status"]), (200, "set"), hdr)

    def test_legacy_get_actions_are_opt_in_and_still_origin_checked(self):
        rc_config.ALLOW_GET_ACTIONS = True
        self.assertEqual(self.req("GET", "/settings?name=fork&on=1")[0], 200)
        hdr = {"Sec-Fetch-Site": "cross-site"}  # an <img src> on another site
        self.assertEqual(
            self.req("GET", "/settings?name=fork&on=0", headers=hdr)[0], 403
        )

    def test_cross_origin_put_and_delete_are_refused(self):
        Path(self.share, "keep.txt").write_text("x")
        bad = {"Sec-Fetch-Site": "cross-site", "X-Rc-Offset": "0"}
        status, hdrs, _ = self.req("PUT", "/files/n.bin", body=b"x", headers=bad)
        self.assertEqual((status, hdrs.get("connection")), (403, "close"))
        self.assertEqual(self.req("DELETE", "/files/keep.txt", headers=bad)[0], 403)
        self.assertTrue(os.path.exists(os.path.join(self.share, "keep.txt")))

    def test_post_body_is_drained_or_the_connection_closed(self):
        # a small body is read and keep-alive survives; an oversized one closes the socket
        _, hdrs, _ = self.req("POST", "/settings?name=fork&on=1", b"a=1", SAME)
        self.assertIsNone(hdrs.get("connection"))
        big = b"x" * (65536 + 1)
        _, hdrs, _ = self.req("POST", "/settings?name=fork&on=1", big, SAME)
        self.assertEqual(hdrs.get("connection"), "close")


class HeadersTest(WebCase):
    def assert_hardened(self, hdrs, what):
        self.assertEqual(hdrs.get("x-content-type-options"), "nosniff", what)
        self.assertEqual(hdrs.get("x-frame-options"), "DENY", what)
        self.assertEqual(hdrs.get("referrer-policy"), "no-referrer", what)
        self.assertEqual(hdrs.get("cross-origin-opener-policy"), "same-origin", what)
        self.assertEqual(hdrs.get("cross-origin-resource-policy"), "same-origin", what)
        self.assertIn("frame-ancestors 'none'", hdrs.get("content-security-policy", ""))

    def test_every_response_carries_the_hardening_headers(self):
        for method, path, kw in (
            ("GET", "/status", {}),
            ("GET", "/nope", {}),
            ("GET", "/status", {"cookie": False}),  # 403
            ("GET", "/version", {}),
            ("HEAD", "/files/x", {}),
            ("GET", "/files", {}),
        ):
            status, hdrs, _ = self.req(method, path, **kw)
            self.assert_hardened(hdrs, f"{method} {path} {status}")
            self.assertEqual(hdrs.get("cache-control"), "no-store")
        self.assertNotIn("Python", self.req("GET", "/status")[1].get("server", ""))

    def _page_nonce(self, path):
        _, hdrs, body = self.req("GET", path)
        csp = hdrs["content-security-policy"]
        nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
        self.assertNotIn("unsafe", csp.split("script-src")[1].split(";")[0])
        tags = re.findall(rb"<script[^>]*>", body)
        self.assertEqual(tags, [f"<script nonce={nonce}>".encode()])  # the only script
        return nonce

    def test_pages_allow_only_their_own_nonced_script(self):
        first = self._page_nonce("/")
        self.assertNotEqual(first, self._page_nonce("/"))  # fresh per response
        self._page_nonce("/files")

    def test_head_refusal_sends_no_body_to_desync_keep_alive(self):
        # a 403 HEAD used to write "forbidden" after the headers; pipelined after it, the
        # next response must start cleanly
        resp = self.raw(
            b"HEAD /files/x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            b"GET /version HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        first, _, rest = resp.partition("\r\n\r\n")
        self.assertIn(" 403 ", first.splitlines()[0])
        self.assertTrue(rest.startswith("HTTP/1.1 200"), rest[:40])


class TemplateTest(unittest.TestCase):
    def test_fill_places_the_nonce_only_in_the_template_slot(self):
        out = rc_templates.fill(
            "<script nonce=__NONCE__>var D=__D__</script>", {"__D__": '"__NONCE__"'}
        )
        self.assertTrue(out.nonce)
        self.assertIn(f"<script nonce={out.nonce}>".encode(), out)
        self.assertIn(b'"__NONCE__"', out)  # injected data is never re-scanned
        self.assertEqual(rc_templates.fill("__A__", {"__A__": "x"}).nonce, "")

    def test_page_escapes_names_in_markup_and_posts_its_actions(self):
        page = rc_page.PAGE
        self.assertIn("esc(n)+'</span>'", page)
        self.assertIn("aria-label=\"close '+esc(n)+'\"", page)
        self.assertIn("'\"':'&quot;'", page)  # esc() covers attribute quotes too
        for route in ("/launch", "/stop", "/create", "/settings", "/addroot"):
            self.assertIn(f"post('{route}?", page)
            self.assertNotIn(f"fetch('{route}", page)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        restore_globals(self)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_default_bind_is_loopback(self):
        env = {k: v for k, v in os.environ.items() if k != "RC_LAUNCHER_BIND"}
        out = subprocess.run(
            [sys.executable, "-c", "import rc_config;print(rc_config.BIND)"],
            capture_output=True,
            text=True,
            env=env,
            cwd=Path(__file__).resolve().parent.parent,
        )
        self.assertEqual(out.stdout.strip(), "127.0.0.1")

    def test_env_parsers(self):
        env(self, RC_T_A=" Yes ", RC_T_B="a.ts.net, .B.net ,,", RC_T_C="12", RC_T_D="x")
        self.assertTrue(rc_config._flag("RC_T_A"))
        self.assertFalse(rc_config._flag("RC_T_UNSET"))
        self.assertEqual(rc_config._names("RC_T_B"), frozenset({"a.ts.net", ".b.net"}))
        self.assertEqual(rc_config._mib("RC_T_C", 1), 12 << 20)
        self.assertEqual(rc_config._mib("RC_T_D", 3), 3 << 20)

    def test_log_event_cannot_forge_a_second_line(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _REAL_LOG("create", "x\n2026-01-01 00:00:00 MT  launch evil -> ok", "bad\r")
        self.assertEqual(buf.getvalue().count("\n"), 1)
        self.assertIn("x?2026", buf.getvalue())

    def test_add_root_failure_does_not_echo_the_config_path(self):
        root = os.path.join(self.tmp, "cfg")
        os.makedirs(root)
        os.chmod(root, 0o500)  # mkstemp there fails
        self.addCleanup(os.chmod, root, 0o700)
        rc_config.ROOTS_FILE = Path(root, "roots.json")
        cand = os.path.join(self.tmp, "rcextra7q")
        os.makedirs(cand)
        status, reason = rc_config.add_root(cand)
        self.assertEqual(status, "failed")
        self.assertNotIn(root, reason)


class RouteErrorTest(WebCase):
    def test_create_os_error_is_a_reason_not_a_dropped_socket(self):
        status, _, body = self.req("POST", f"/create?proj={'a' * 300}", headers=SAME)
        d = json.loads(body)
        self.assertEqual((status, d["status"]), (200, "failed"))
        self.assertNotIn(rc_config.PARENT, d["reason"])


class SettingsFailureTest(WebCase):
    def test_settings_failure_reason_is_generic_on_the_wire(self):
        keep(self, (rc_settings, "set_toggle"))
        leak = "[Errno 13] Permission denied: '/home/u/.config/rc-launcher/settings.x'"
        rc_settings.set_toggle = lambda name, on: ("failed", leak)
        _, _, body = self.req("POST", "/settings?name=fork&on=1", headers=SAME)
        self.assertEqual(json.loads(body)["reason"], "could not save the setting")
        self.assertTrue(any(leak in a[2] for a in self.logged))  # the detail is logged


class ConnectionCapTest(unittest.TestCase):
    def test_connections_over_the_cap_are_dropped_not_threaded(self):
        restore_globals(self)
        keep(self, (rc_launcher.Server, "max_connections"))
        rc_launcher.Server.max_connections = 1
        srv = rc_launcher.Server(("127.0.0.1", 0), rc_launcher.Handler)
        threading.Thread(
            target=lambda: srv.serve_forever(poll_interval=0.05), daemon=True
        ).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        held = socket.create_connection(("127.0.0.1", port), timeout=5)  # the one slot
        time.sleep(0.2)
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.assertEqual(extra.recv(100), b"")  # closed at once, no thread spawned
        extra.close()
        held.close()  # frees the slot (the handler times out its read and returns)
        time.sleep(0.3)
        again = socket.create_connection(("127.0.0.1", port), timeout=5)
        again.sendall(b"GET /version HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertTrue(again.recv(100).startswith(b"HTTP/1.1 200"))
        again.close()


if __name__ == "__main__":
    unittest.main()
