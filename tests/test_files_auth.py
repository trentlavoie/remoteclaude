"""The /files gate and its connection framing: the secret comparison (?token=, the
rc_token cookie, and the fail-closed empty-token case), per-verb denial, the redacted
audit line for a refusal, and the closes that keep keep-alive honest — an unread request
body must never be left to desync the next request on the socket. The write path itself
is tests/test_upload.py."""

import socket
import unittest

import rc_config

from tests._harness import TOKEN, ServerCase


class FilesAuthTest(ServerCase):
    def test_files_get_sets_auth_cookie(self):
        status, hdrs, _ = self.req("GET", f"/files?token={TOKEN}", cookie=False)
        self.assertEqual(status, 200)
        cookie = hdrs.get("set-cookie", "")
        self.assertIn("rc_token=", cookie)
        self.assertIn("HttpOnly", cookie)  # not readable from page JS
        # CSRF defense for the cookie-authed writes
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)

    def test_valid_token_beats_stale_cookie(self):
        # a correct ?token= must authenticate even carrying a wrong cookie (the ac5b5d0 shape)
        status, _, _ = self.req(
            "GET",
            f"/files?token={TOKEN}",
            cookie=False,
            headers={"Cookie": "rc_token=stalevalue"},
        )
        self.assertEqual(status, 200)

    def test_cookie_authorizes_write_and_no_auth_is_403(self):
        # cookie-only PUT (no ?token=) authenticates — the regression that shipped as a 403
        status, _, _ = self.req(
            "PUT",
            "/files/c.bin",
            body=b"hi",
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "2"},
        )
        self.assertEqual(status, 200)
        status, _, _ = self.req(
            "PUT",
            "/files/n.bin",
            body=b"x",
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1"},
            cookie=False,
        )
        self.assertEqual(status, 403)

    def test_wrong_token_and_cookie_rejected(self):
        self.assertEqual(
            self.req("GET", "/files?token=wrongvalue", cookie=False)[0], 403
        )
        self.assertEqual(
            self.req(
                "GET", "/files", cookie=False, headers={"Cookie": "rc_token=wrongvalue"}
            )[0],
            403,
        )

    def test_no_auth_denied_on_every_verb(self):
        for verb in ("GET", "PUT", "DELETE", "HEAD"):
            hdr = {"X-Rc-Offset": "0", "X-Rc-Total": "1"} if verb == "PUT" else None
            self.assertEqual(
                self.req(verb, "/files/x", cookie=False, headers=hdr)[0], 403, verb
            )

    def test_fail_closed_when_no_token_configured(self):
        # misconfigured server: an empty token must deny, not match empty creds
        rc_config.TOKEN = ""
        try:
            # empty ?token= and an empty rc_token cookie both reach compare_digest("","") — that
            # returns True, so only the `if not TOKEN: return False` guard makes these 403. Sending
            # a *non-empty* token here (as before) 403s on the mismatch regardless, proving nothing.
            self.assertEqual(self.req("GET", "/files/x?token=", cookie=False)[0], 403)
            self.assertEqual(
                self.req(
                    "GET", "/files/x", cookie=False, headers={"Cookie": "rc_token="}
                )[0],
                403,
            )
            # and a real-looking token is still rejected when the server holds none
            self.assertEqual(
                self.req("GET", f"/files/x?token={TOKEN}", cookie=False)[0], 403
            )
        finally:
            rc_config.TOKEN = TOKEN

    def test_unauthorized_put_closes_the_connection(self):
        # the body is never read on a 403, so keep-alive reuse would desync: must close
        status, hdrs, _ = self.req(
            "PUT",
            "/files/n.bin",
            body=b"x",
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1"},
            cookie=False,
        )
        self.assertEqual(status, 403)
        self.assertEqual(hdrs.get("connection"), "close")

    def test_body_bearing_get_closes_connection(self):
        # a GET carrying a body: the handler won't read it, so under keep-alive the leftover
        # would desync the next request — the server must close instead of reusing the socket.
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(
            f"GET /files?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Content-Length: 5\r\n\r\nHELLO".encode()
        )
        resp = s.recv(65536).decode("latin1")
        s.close()
        self.assertIn(" 200 ", resp.splitlines()[0])  # the GET itself succeeded
        # and the server closed the socket
        self.assertIn("connection: close", resp.lower())

    def test_chunked_body_bearing_get_closes(self):
        # a chunked (no Content-Length) body on GET must also close, or it desyncs keep-alive
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(
            f"GET /files?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Transfer-Encoding: chunked\r\n\r\n5\r\nHELLO\r\n0\r\n\r\n".encode()
        )
        resp = s.recv(65536).decode("latin1")
        s.close()
        self.assertIn("connection: close", resp.lower())

    def test_put_without_content_length_is_411_and_closes(self):
        # no length, no chunked: the server cannot size the body, so it must refuse (411)
        # and close rather than leave an unread body to desync the next request
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(
            f"PUT /files/nolen.bin HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: rc_token={TOKEN}\r\n"
            f"X-Rc-Offset: 0\r\nX-Rc-Total: 10\r\n\r\n".encode()
        )
        chunks = []  # read to EOF: the server closes, and the body can trail the headers
        while chunk := s.recv(65536):
            chunks.append(chunk)
        resp = b"".join(chunks).decode("latin1")
        s.close()
        self.assertIn(" 411 ", resp.splitlines()[0])
        self.assertIn("connection: close", resp.lower())
        self.assertIn('"length required"', resp)

    def test_rejected_request_is_logged(self):
        logged: list = []
        rc_config.log_event = lambda *a: logged.append(a)  # capture instead of silence
        self.req(
            "PUT",
            "/files/z",
            body=b"z",
            headers={"X-Rc-Offset": "bad", "X-Rc-Total": "1"},
        )
        # the 400 left an audit line
        self.assertTrue(any(a[0] == "http" for a in logged))

    def test_rejected_request_log_omits_query_token(self):
        # The app's uploads carry ?token= in the query, and the launcher log is a
        # world-readable /tmp file: the >=400 audit line must record the PATH only.
        # Reverting the redaction to raw self.path re-opens the leak the token
        # remediation closed — this pin is what makes that revert fail.
        logged: list = []
        rc_config.log_event = lambda *a: logged.append(a)
        self.req(
            "PUT",
            f"/files/z?token={TOKEN}",
            body=b"z",
            headers={"X-Rc-Offset": "bad", "X-Rc-Total": "1"},
        )
        line = " ".join(map(str, next(a for a in logged if a[0] == "http")))
        self.assertIn("/files/z", line)  # the path is still traceable
        self.assertNotIn(TOKEN, line)  # the credential never reaches the log


if __name__ == "__main__":
    unittest.main()
