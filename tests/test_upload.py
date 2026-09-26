"""The /files write path: the resumable finalize, the 409 gap, the truncate that keeps a
re-PUT honest, malformed headers, delete and download — plus the .rcpart TTL sweep that
reclaims what an abandoned upload left behind. Driven over the real loopback Handler, the
same contract a browser/app client speaks. The auth and keep-alive framing half of this
same surface is tests/test_files_auth.py."""

import json
import os
import shutil
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

import rc_config
import rc_share

from tests._harness import restore_globals, TOKEN, ServerCase, share_dir


class SweepTest(unittest.TestCase):
    def setUp(self):
        # SHARE must not leak a deleted tmp dir into later classes
        restore_globals(self)
        self.share = rc_config.SHARE = share_dir(self)

    def _aged(self, name: str) -> str:
        p = os.path.join(self.share, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        old = time.time() - rc_config.RCPART_TTL - 60
        os.utime(p, (old, old))
        return p

    def test_removes_only_expired_rcparts(self):
        old = self._aged("old.rcpart")
        nested = self._aged("sub/deep.rcpart")  # proves the os.walk recursion
        keep_txt = self._aged("keep.txt")  # aged, but not a .rcpart
        fresh = os.path.join(self.share, "fresh.rcpart")
        open(fresh, "w").close()  # current mtime — an in-progress upload
        self.assertEqual(rc_share.sweep_rcparts(), 2)
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(nested))
        self.assertTrue(os.path.exists(keep_txt))
        self.assertTrue(os.path.exists(fresh))


class UploadTest(ServerCase):
    def test_full_upload_finalizes_without_leftover(self):
        data = bytes(i % 256 for i in range(1000))
        status, _, body = self.req(
            "PUT",
            "/files/f.bin",
            body=data,
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1000"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["done"])
        self.assertEqual(Path(self.share, "f.bin").read_bytes(), data)
        self.assertFalse(os.path.exists(os.path.join(self.share, "f.bin.rcpart")))

    def test_resume_from_partial(self):
        data = bytes(i % 256 for i in range(1000))
        status, _, body = self.req(
            "PUT",
            "/files/r.bin",
            body=data[:400],
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1000"},
        )
        self.assertEqual(status, 200)
        first = json.loads(body)
        self.assertFalse(first["done"])
        self.assertEqual(first["have"], 400)
        _, hdrs, _ = self.req("HEAD", "/files/r.bin")
        self.assertEqual(hdrs.get("x-rc-have"), "400")
        status, _, body = self.req(
            "PUT",
            "/files/r.bin",
            body=data[400:],
            headers={"X-Rc-Offset": "400", "X-Rc-Total": "1000"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["done"])
        self.assertEqual(Path(self.share, "r.bin").read_bytes(), data)

    def test_resume_identity_isolates_stale_partial(self):
        # a stale partial for x.bin under file-id A
        self.req(
            "PUT",
            "/files/x.bin",
            body=b"A" * 100,
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1000", "X-Rc-Id": "idA"},
        )
        # a HEAD under a DIFFERENT id must see have=0 — it won't resume onto A's bytes
        _, hdrs, _ = self.req("HEAD", "/files/x.bin", headers={"X-Rc-Id": "idB"})
        self.assertEqual(hdrs.get("x-rc-have"), "0")
        # the SAME id still resumes
        _, hdrs, _ = self.req("HEAD", "/files/x.bin", headers={"X-Rc-Id": "idA"})
        self.assertEqual(hdrs.get("x-rc-have"), "100")
        # a realistic client resumes from what HEAD reports. With id-keying idB's HEAD is 0,
        # so it uploads pure B; if the keying were removed, HEAD would report 100 (idA's
        # partial) and this would resume ONTO it (A[0:100]+B[100:]) — which this catches.
        _, hb, _ = self.req("HEAD", "/files/x.bin", headers={"X-Rc-Id": "idB"})
        have = int(hb["x-rc-have"])
        full = b"B" * 500
        _, _, body = self.req(
            "PUT",
            "/files/x.bin",
            body=full[have:],
            headers={"X-Rc-Offset": str(have), "X-Rc-Total": "500", "X-Rc-Id": "idB"},
        )
        self.assertTrue(json.loads(body)["done"])
        self.assertEqual(Path(self.share, "x.bin").read_bytes(), full)

    def test_gap_offset_returns_409(self):
        self.req(
            "PUT",
            "/files/g.bin",
            body=b"x" * 100,
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "500"},
        )
        status, _, body = self.req(
            "PUT",
            "/files/g.bin",
            body=b"y" * 10,
            headers={"X-Rc-Offset": "200", "X-Rc-Total": "500"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["have"], 100)

    def test_offset_beyond_total_is_bad_total_400(self):
        # X-Rc-Total < X-Rc-Offset is incoherent; the guard rejects it. No test sent
        # offset>total, so deleting `total < offset` survived.
        status, _, body = self.req(
            "PUT",
            "/files/o.bin",
            body=b"x" * 10,
            headers={"X-Rc-Offset": "500", "X-Rc-Total": "100"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "bad total")

    def test_malformed_headers_400_and_server_survives(self):
        for off in (
            "abc",
            "-5",
        ):  # non-numeric used to crash the thread; negative was swallowed
            status, _, _ = self.req(
                "PUT",
                "/files/b.bin",
                body=b"x",
                headers={"X-Rc-Offset": off, "X-Rc-Total": "10"},
            )
            self.assertEqual(status, 400, off)
        status, _, _ = self.req(
            "PUT",
            "/files/b.bin",
            body=b"x",
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "0"},
        )
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(os.path.join(self.share, "b.bin.rcpart")))
        status, _, _ = self.req("GET", f"/files?token={TOKEN}", cookie=False)
        self.assertEqual(status, 200)  # the handler thread survived the bad requests

    def test_delete_removes_file_and_refuses_root_and_dir(self):
        self.req(
            "PUT",
            "/files/d.bin",
            body=b"x",
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1"},
        )
        self.assertTrue(os.path.exists(os.path.join(self.share, "d.bin")))
        self.assertEqual(self.req("DELETE", "/files/d.bin")[0], 200)
        self.assertFalse(os.path.exists(os.path.join(self.share, "d.bin")))
        os.makedirs(os.path.join(self.share, "sub"))
        self.assertEqual(self.req("DELETE", "/files")[0], 403)  # the share root
        self.assertEqual(self.req("DELETE", "/files/sub")[0], 403)  # a directory
        # a traversal escape
        self.assertEqual(self.req("DELETE", "/files/..%2f..%2fx")[0], 403)

    def test_download_streams_file(self):
        data = b"hello world " * 50
        Path(self.share, "dl.bin").write_bytes(data)
        status, _, body = self.req("GET", f"/files/dl.bin?token={TOKEN}", cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, data)

    def test_resume_below_have_truncates_the_stale_tail(self):
        # The audit's one demonstrated corruption: without f.truncate(offset) a re-PUT below
        # `have` leaves the old tail in place and reports it as stored (have=500, not 450).
        data = bytes(range(256)) * 2
        self.req(
            "PUT",
            "/files/t.bin",
            body=data[:500],
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1000"},
        )
        status, _, body = self.req(
            "PUT",
            "/files/t.bin",
            body=data[400:450],
            headers={"X-Rc-Offset": "400", "X-Rc-Total": "1000"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["have"], 450)
        _, hdrs, _ = self.req("HEAD", "/files/t.bin")
        self.assertEqual(hdrs.get("x-rc-have"), "450")
        self.assertEqual(Path(self.share, "t.bin.rcpart").read_bytes(), data[:450])

    def test_put_to_escaping_path_is_404(self):
        # share_target() is pinned against five escape forms at the function level
        # (test_confinement); this pins the PUT call site, so a handler that skipped
        # the confinement check could not pass.
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, os.path.join(self.share, "esc"))
        for path in ("/files/../evil.bin", "/files/esc/evil.bin"):
            status, _, _ = self.req(
                "PUT",
                path,
                body=b"x",
                headers={"X-Rc-Offset": "0", "X-Rc-Total": "1"},
            )
            self.assertEqual(status, 403, path)  # the PUT path's "bad target" refusal
        self.assertEqual(os.listdir(outside), [])

    def test_upload_write_error_is_logged_not_finalized(self):
        logged: list = []
        rc_config.log_event = lambda *a: logged.append(a)
        # read-only dir -> the .rcpart open() raises OSError
        os.chmod(self.share, 0o500)
        try:
            status, hdrs, body = self.req(
                "PUT",
                "/files/e.bin",
                body=b"data",
                headers={"X-Rc-Offset": "0", "X-Rc-Total": "4"},
            )
        finally:
            os.chmod(self.share, 0o700)
        self.assertEqual(status, 200)
        self.assertEqual(hdrs.get("connection"), "close")  # unread body: never reuse
        self.assertFalse(json.loads(body)["done"])
        self.assertTrue(any(a[0] == "upload" and "err" in str(a[2]) for a in logged))

    def test_put_unknown_path_404(self):
        status, _, _ = self.req(
            "PUT", "/nope", body=b"x", headers={"X-Rc-Offset": "0", "X-Rc-Total": "1"}
        )
        self.assertEqual(status, 404)

    def test_upload_without_total_header_finalizes(self):
        # no X-Rc-Total -> defaults
        status, _, body = self.req(
            "PUT", "/files/nt.bin", body=b"12345", headers={"X-Rc-Offset": "0"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["done"])
        self.assertEqual(Path(self.share, "nt.bin").read_bytes(), b"12345")

    def test_upload_to_directory_is_bad_target(self):
        os.makedirs(os.path.join(self.share, "adir"))
        status, _, _ = self.req(
            "PUT",
            "/files/adir",
            body=b"x",
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1"},
        )
        self.assertEqual(status, 403)

    def test_upload_to_missing_folder_404(self):
        status, _, _ = self.req(
            "PUT",
            "/files/nodir/f.bin",
            body=b"x",
            headers={"X-Rc-Offset": "0", "X-Rc-Total": "1"},
        )
        self.assertEqual(status, 404)

    def test_files_traversal_get_404(self):
        status, _, _ = self.req(
            "GET", f"/files/..%2f..%2fetc?token={TOKEN}", cookie=False
        )
        self.assertEqual(status, 404)

    def test_delete_non_files_path_404(self):
        self.assertEqual(self.req("DELETE", "/nope")[0], 404)

    def test_dropped_connection_keeps_partial(self):
        # a real mid-body RST must hit the ConnectionError branch that KEEPS the .rcpart —
        # mutate that branch to os.unlink and this assertion fails (the headline resume feature).
        # 76800 distinctive bytes: catches stale-length/corruption
        pattern = bytes(range(256)) * 300
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(
            f"PUT /files/drop.bin HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: rc_token={TOKEN}\r\n"
            f"X-Rc-Offset: 0\r\nX-Rc-Total: 200000\r\nContent-Length: 200000\r\n\r\n".encode()
        )
        s.sendall(pattern)  # more than one 64KB read, well short of total
        time.sleep(0.5)  # let the server read + write the first 64KB chunk
        # RST on close
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        s.close()
        time.sleep(0.5)
        _, hdrs, _ = self.req("HEAD", "/files/drop.bin")
        have = int(hdrs["x-rc-have"])
        # partial survived the drop (ConnectionError branch)
        self.assertGreaterEqual(have, 65536)
        kept = Path(self.share, "drop.bin.rcpart").read_bytes()
        # exact prefix of what was sent, not garbage/stale
        self.assertEqual(kept, pattern[:have])


if __name__ == "__main__":
    unittest.main()
