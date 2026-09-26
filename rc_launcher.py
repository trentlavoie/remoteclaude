#!/usr/bin/env python3
"""Remote Control launcher — the HTTP surface.

Tap a project on your phone -> this starts a Claude Code Remote Control
session on the Mac, rooted in that project's directory (so its CLAUDE.md,
.claude/ settings and project MCP load exactly like the VS Code extension).
Each session is held in a detached tmux session so it survives the HTTP
request returning and any SSH/terminal closing.

This module is only the web tier: auth, routing, request framing, and the
/files byte-pushing. The work behind each route lives in rc_sessions (launch,
stop, create, what's live) and rc_share (what a path is allowed to reach); paths
and roots come from rc_config, the fork/worktree toggles from rc_settings. Refuses
to start without the token file.
"""

import contextlib
import functools
import hmac
import ipaddress
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import rc_config as cfg
import rc_desk
import rc_sessions
import rc_settings
import rc_share
from rc_templates import BASE_CSP, HARDENING, page_csp

# The routes that change state: POST only (GET too under RC_ALLOW_GET_ACTIONS), CSRF-checked
_ACTIONS = frozenset({"/launch", "/stop", "/create", "/addroot", "/settings"})
# a request carrying any of these came through a reverse proxy (tailscale serve adds them)
_PROXIED = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded")


def _is_files(path: str) -> bool:
    """The /files subtree, matched the same way by every verb — '/files' itself or
    anything under it, never a '/filesomething' sibling."""
    return path == "/files" or path.startswith("/files/")


def host_allowed(host: str) -> bool:
    """The DNS-rebinding guard: is this Host header one of ours? IP literals always pass (a
    rebinding page reaches us under its own hostname, never an IP), as do localhost, this
    machine's name and RC_ALLOWED_HOSTS (a '.'-led entry matches its subdomains)."""
    h = host.strip().lower()
    h = h[1:].partition("]")[0] if h.startswith("[") else h
    h = (h.partition(":")[0] if h.count(":") == 1 else h).rstrip(".")
    with contextlib.suppress(ValueError):
        return bool(ipaddress.ip_address(h))
    me = cfg.HOST.lower()
    if not h or h in cfg.ALLOWED_HOSTS or h in ("localhost", me, f"{me}.local"):
        return bool(h)
    return any(n[:1] == "." and h.endswith(n) for n in cfg.ALLOWED_HOSTS)


def _cookies(header: str, name: str) -> list[str]:
    """Every value of cookie `name`, parsed leniently: SimpleCookie drops the WHOLE header
    on one malformed pair, so any stray cookie on the host used to 403 the launcher."""
    pairs = (part.partition("=") for part in header.split(";"))
    return [v.strip().strip('"') for k, eq, v in pairs if eq and k.strip() == name]


def _host_checked(verb):
    """Every verb runs behind the Host allowlist (DNS rebinding) — /version included."""

    @functools.wraps(verb)
    def run(self):
        if host_allowed(host := self.headers.get("Host", "")):
            return verb(self)
        cfg.log_event("http", f"refused Host {host[:80]!r}", "421")  # what to allow
        self._send(421, b"misdirected request", close=True)

    return run


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive so a chunked upload reuses ONE connection (a single TCP
    # slow-start) instead of a fresh handshake + slow-start per chunk — the difference
    # between ~line-rate and a per-chunk ramp. Every response sets Content-Length, which
    # is what makes persistent connections framable. timeout reaps idle kept connections.
    protocol_version = "HTTP/1.1"
    timeout = 60
    server_version, sys_version = "rc-launcher", ""  # no Python version in Server:
    _csp: str | None = BASE_CSP  # this response's policy; end_headers resets it

    def _send(
        self,
        code: int,
        body: bytes,
        ctype: str = "text/html; charset=utf-8",
        set_cookie: bool = False,
        close: bool = False,
        extra: tuple = (),
    ):
        # non-2xx used to be invisible (log_message is silenced) — trace it. Path only,
        # never the query: the app's uploads carry ?token=, and a failed request would
        # otherwise write the live token into the world-readable log.
        if code >= 400:
            cfg.log_event(
                "http", f"{self.command} {urlparse(self.path).path}", str(code)
            )
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for header in extra:
            self.send_header(*header)
        if nonce := getattr(body, "nonce", ""):  # a rendered page: allow its own script
            self._csp = page_csp(nonce)
        if set_cookie:
            # Secure whenever the client's leg is HTTPS (tailscale serve says so), or forced
            https = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
            self.send_header(
                "Set-Cookie",
                f"rc_token={cfg.TOKEN}; HttpOnly; SameSite=Strict; Path=/; "
                f"Max-Age=31536000{'; Secure' if https or cfg.COOKIE_SECURE else ''}",
            )
        # a bail-out that never read the request body must end the connection, or that
        # unread body desyncs the next request on the socket
        if close:
            self.close_connection = True
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":  # a HEAD body would desync the kept-alive socket
            self.wfile.write(body)

    def end_headers(self):
        for header in HARDENING:
            self.send_header(*header)
        if self._csp:
            self.send_header("Content-Security-Policy", self._csp)
        self._csp = BASE_CSP
        # advertise the close (set by close=, _guard_body, or a client Connection: close)
        # so the client won't try to reuse a socket we're about to drop.
        if self.close_connection:
            self.send_header("Connection", "close")
        super().end_headers()

    def _json(self, payload: dict):
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _json_error(self, code: int, msg: str, close: bool = False, **extra):
        """A refusal the app can parse. Compact separators keep the wire bytes identical
        to the hand-written literals these eight sites used to carry."""
        body = json.dumps({"error": msg} | extra, separators=(",", ":")).encode()
        self._send(code, body, "application/json", close=close)

    def _authed(self, q: dict) -> bool:
        """Token via ?token= (first contact / bookmark) or the rc_token cookie set
        on that first load, so the token stays out of later request URLs and logs. Compared
        as bytes: a non-ASCII str made compare_digest raise (an unauthenticated traceback).
        With RC_TAILSCALE_USERS set, tailscale serve's identity header must match too."""
        if not cfg.TOKEN:
            return False
        cookie = self.headers.get("Cookie", "")
        offered = q.get("token", [])[:1] + _cookies(cookie, "rc_token")
        want = cfg.TOKEN.encode()  # offered values are never surrogates (latin-1, qs)
        if not any([hmac.compare_digest(v.encode(), want) for v in offered]):
            return False
        login = self.headers.get("Tailscale-User-Login", "").strip().lower()
        if not cfg.TAILSCALE_USERS or login in cfg.TAILSCALE_USERS:
            return True
        # token was right, identity wasn't: log who (never the token) so it can be allowed
        cfg.log_event("http", f"refused Tailscale login {login[:80]!r}", "403")
        return False

    def _same_origin(self) -> bool:
        """The CSRF check on every state change. A browser sends Sec-Fetch-Site (and Origin on
        a POST/PUT/DELETE); a request with neither is a non-browser client (the Android app,
        curl), which has no ambient cookie to abuse. SameSite=Strict alone is not enough:
        same-SITE spans other ports on this host and other nodes of the tailnet."""
        if (site := self.headers.get("Sec-Fetch-Site")) is not None:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "").lower()
        return origin is None or urlparse(origin).netloc.lower() == host

    def _direct_local(self) -> bool:
        """From this host and not through a proxy: the watchdog's own /version probe.
        tailscale serve connects from loopback too, but always adds forwarding headers."""
        with contextlib.suppress(ValueError):
            if ipaddress.ip_address(self.client_address[0]).is_loopback:
                return not any(h in self.headers for h in _PROXIED)
        return False

    def _guard_body(self) -> None:
        """GET/HEAD/DELETE never read a request body; under keep-alive an unread body would
        desync the next request on the socket, so close the connection if one was sent."""
        if self.headers.get("Content-Length") or self.headers.get("Transfer-Encoding"):
            self.close_connection = True

    @_host_checked
    def do_GET(self):
        self._guard_body()
        self._route()

    @_host_checked
    def do_POST(self):
        # the actions read only the query: a small body is read (and dropped) so keep-alive
        # stays framed; anything larger or chunked just ends the connection after
        n = self.headers.get("Content-Length", "")
        if n.isdigit() and int(n) <= 65536 and "Transfer-Encoding" not in self.headers:
            self.rfile.read(int(n))
        else:
            self._guard_body()
        self._route()

    def _route(self):
        """GET and POST: reads answer GET, the _ACTIONS answer POST (CSRF-checked)."""
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # the watchdog's liveness probe: token-free only straight from this host
        if u.path == "/version" and (self._direct_local() or self._authed(q)):
            return self._json({"version": cfg.VERSION})
        action = u.path in _ACTIONS
        if not self._authed(q) or (action and not self._same_origin()):
            return self._send(403, b"forbidden")
        posted = self.command == "POST"
        if action != posted and not (action and cfg.ALLOW_GET_ACTIONS):
            allow = ("Allow", "POST" if action else "GET, HEAD")
            return self._send(405, b"method not allowed", extra=(allow,))
        match u.path:
            case "/":
                return self._send(200, rc_sessions.page(), set_cookie=True)
            case "/status":
                return self._json(rc_sessions.status_payload())
            case "/create":
                return self._create(q.get("proj", [""])[0])
            case "/addroot":
                return self._addroot(q.get("path", [""])[0])
            case "/settings":
                return self._settings(q)
            case "/launch" | "/stop":
                return self._session_verb(u.path, q)
            case path if _is_files(path) and cfg.SHARE_ENABLED:
                return self._files(path)
            case _:
                self._send(404, b"not found")

    def _create(self, proj: str):
        """Make the project, then launch it — one tap on the phone's "+ new" row."""
        try:  # e.g. ENAMETOOLONG: NAME_RE has no length cap. A reason, not a dropped socket
            status, reason = rc_sessions.create(proj)
        except OSError as e:
            status, reason = "failed", e.strerror or "create failed"
        cfg.log_event("create", proj, status)
        payload = {"status": status, "proj": proj}
        if reason:
            payload["reason"] = reason
        if status == "created":
            lstatus, lreason = rc_sessions.launch(proj)
            cfg.log_event("launch", proj, lstatus)
            payload["launch"] = lstatus
            if lreason:
                payload["launch_reason"] = lreason
        return self._json(payload)

    def _addroot(self, path: str):
        """Register a project root at runtime (the '+ root' control), token-gated like every
        route. The candidate path rides in the query, so a refusal logs only the URL path,
        never the query — the dir stays out of the log the way the token does."""
        status, reason = cfg.add_root(path)
        cfg.log_event("addroot", path.strip() or "-", status)
        if status == "added":
            rc_desk.desk_projects.invalidate()  # a new root's desk sessions must badge now
        payload = {"status": status}
        if reason:
            payload["reason"] = reason
        return self._json(payload)

    def _settings(self, q: dict):
        """Persist one launcher toggle (the settings switches): name=fork|worktree, on=0|1."""
        name = q.get("name", [""])[0]
        status, reason = rc_settings.set_toggle(name, q.get("on", [""])[0] == "1")
        cfg.log_event("settings", name or "-", f"{status} {reason or ''}".strip())
        # a failure's OSError text names the config path: it goes to the log, not the client
        reason = "could not save the setting" if status == "failed" else reason
        payload = {"status": status}
        if reason:
            payload["reason"] = reason
        return self._json(payload)

    def _session_verb(self, path: str, q: dict):
        """/launch and /stop. On /stop, desk=1 is the ✕ on a desk-badged row (closes the
        user's desktop claude, an explicit-only action). Otherwise plain stop() closes the
        project's remote-control session however it was started — a launcher tmux session or
        an external `claude --remote-control` — so a legacy ext=1 is now redundant, not wrong."""
        proj = q.get("proj", [""])[0]
        if proj not in cfg.projects():
            return self._json_error(404, "unknown project")
        want_model = ""
        if path == "/stop":
            if q.get("desk", [""])[0] == "1":  # ✕ on a desk-badged row (desktop claude)
                status, reason = rc_sessions.desk_stop(proj)
            else:  # tmux or, failing that, an external RC session — never desk
                status, reason = rc_sessions.stop(proj)
        else:
            want_model = q.get("model", [""])[0]  # optional per-launch model; "" -> pin
            model = rc_settings.resolve_model(want_model)
            if model is None:  # never pass an unrecognized value through to argv
                allowed = " ".join(sorted(rc_settings.MODEL_ALIASES))
                status, reason = (
                    "failed",
                    f"unknown model {want_model!r}; allowed: {allowed} (or full IDs)",
                )
            else:
                status, reason = rc_sessions.launch(proj, model)
        cfg.log_event(path[1:], proj, status)
        if q.get("json", [""])[0] != "1":
            return self._send(200, rc_sessions.page())
        payload = {"status": status, "proj": proj}
        if status == "already":  # launch's reason IS the live kind (tmux|extrc|desk)
            payload["kind"] = reason
            if want_model:  # a live session's model is never changed by an "already"
                payload["note"] = "model not applied; /stop then /launch to switch"
        elif reason:
            payload["reason"] = reason
        return self._json(payload)

    @_host_checked
    def do_PUT(self):
        u = urlparse(self.path)
        if not self._authed(parse_qs(u.query)) or not self._same_origin():
            # PUT carries a body we won't read -> close
            return self._send(403, b"forbidden", close=True)
        if _is_files(u.path) and cfg.SHARE_ENABLED:
            return self._upload(u.path)
        self._send(404, b"not found", close=True)

    @_host_checked
    def do_DELETE(self):
        self._guard_body()
        u = urlparse(self.path)
        if not self._authed(parse_qs(u.query)) or not self._same_origin():
            return self._send(403, b"forbidden")
        if _is_files(u.path) and cfg.SHARE_ENABLED:
            return self._delete(u.path)
        self._send(404, b"not found")

    @_host_checked
    def do_HEAD(self):
        """Report how many bytes of a resumable upload are already on disk, so a client
        can resume from there: X-Rc-Have = size of the target's .rcpart (0 if none)."""
        self._guard_body()
        u = urlparse(self.path)
        if not self._authed(parse_qs(u.query)):
            return self._send(403, b"forbidden")
        have = 0
        if _is_files(u.path):
            if not cfg.SHARE_ENABLED:
                return self._send(404, b"not found")
            rel = u.path.removeprefix("/files")
            _, tmp = rc_share.part_paths(rel, self.headers.get("X-Rc-Id", ""))
            if tmp:
                have = rc_share.have(tmp)
        self.send_response(200)
        self.send_header("X-Rc-Have", str(have))
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _files(self, path: str):
        """Browse/download under SHARE, behind the same token gate.
        share_target() resolves '..' and symlink escapes away, so this can only
        reach files inside SHARE (never ~/projects or $HOME)."""
        rel = path.removeprefix("/files")
        target = rc_share.share_target(rel)
        if target is None:
            return self._send(404, b"not found")
        if os.path.isfile(target):
            return self._stream_file(target)
        if os.path.isdir(target) or target == cfg.SHARE:
            # set the cookie here too: loading /files directly (not via /) must still
            # authenticate the cookie-based upload/HEAD/download/delete requests it fires.
            return self._send(200, rc_share.share_page(target, rel), set_cookie=True)
        return self._send(404, b"not found")

    def _stream_file(self, target: str):
        """Exactly the size fstat'd at open (a file still growing used to overrun its
        Content-Length and desync keep-alive), zero-copy via sendfile. O_NOFOLLOW: a final
        component swapped for a symlink after share_target() checked it is refused."""
        try:
            # O_NONBLOCK: a FIFO swapped in can't park the thread in open(); no-op on a file
            f = open(os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
        except OSError:
            return self._send(404, b"not found")
        with f:
            if not stat.S_ISREG((st := os.fstat(f.fileno())).st_mode):
                return self._send(404, b"not found")
            size = st.st_size
            ctype, disposition, self._csp = rc_share.serve_as(target)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", disposition)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            sent = 0
            with contextlib.suppress(ConnectionError, TimeoutError):
                sent = self.connection.sendfile(f, 0, size)
            if sent != size:  # shrank or dropped mid-body: the framing is gone
                self.close_connection = True

    @staticmethod
    def _uint(raw: str | None, default: int) -> int | None:
        """An optional non-negative-int header: the default when absent, None when
        present-but-invalid (negative, non-numeric, empty) so the caller can 400."""
        if raw is None:
            return default
        return int(raw) if raw.isdigit() else None

    def _upload(self, path: str):
        """Write or RESUME an upload into SHARE. Bytes stream into a .rcpart temp at
        X-Rc-Offset; the partial is KEPT across interruptions so a dropped upload
        resumes (via HEAD -> X-Rc-Have) instead of restarting. When the temp reaches
        X-Rc-Total it's atomically renamed to the final name. Confined like every path."""
        rel = path.removeprefix("/files")
        target, tmp = rc_share.part_paths(rel, self.headers.get("X-Rc-Id", ""))
        if target is None:
            return self._json_error(403, "bad target", close=True)
        folder = os.path.dirname(target)
        if not rc_share.within_share(folder) or not os.path.isdir(folder):
            return self._json_error(404, "no such folder", close=True)
        length = self.headers.get("Content-Length")
        # chunked (or CL+TE, an ambiguous smuggling shape) is never read as a length
        chunked = "Transfer-Encoding" in self.headers
        if chunked or length is None or not length.isdigit():
            return self._json_error(411, "length required", close=True)
        length = int(length)
        offset = self._uint(self.headers.get("X-Rc-Offset"), 0)
        if offset is None:
            return self._json_error(400, "bad offset", close=True)
        total = self._uint(self.headers.get("X-Rc-Total"), offset + length)
        # offset+length past total would write beyond the size the caps were checked against
        if total is None or total <= 0 or total < offset + length:
            return self._json_error(400, "bad total", close=True)
        have = rc_share.have(tmp)
        if offset > have:  # gap: client is ahead of us — tell it what we actually have
            return self._json_error(409, "gap", close=True, have=have)
        if refusal := rc_share.upload_refusal(total, have):  # size cap / disk floor
            return self._json_error(*refusal, close=True)
        remaining = self._drain_body(tmp, offset, length, target)
        # body not fully drained (drop or write error) — end the connection so its
        # leftover bytes can't be read as a next request
        if remaining:
            self.close_connection = True
        now = rc_share.have(tmp)
        if now >= total:
            os.replace(tmp, target)
            cfg.log_event("upload", os.path.relpath(target, cfg.SHARE), "ok")
            return self._json(
                {"ok": True, "done": True, "name": os.path.basename(target)}
            )
        with contextlib.suppress(OSError):
            self._json({"ok": True, "done": False, "have": now})

    def _drain_body(self, tmp: str, offset: int, length: int, target: str) -> int:
        """Stream length bytes of the request body into tmp at offset; returns how many
        bytes were NOT written. Single-writer assumption: the sequential (await-per-file)
        browser/app clients never run two PUTs to the same target+id concurrently, so the
        .rcpart needs no lock. An overlap would corrupt only the partial (reclaimed by the
        sweep) — os.replace keeps the finalized file atomic regardless."""
        remaining = length
        try:
            # O_NOFOLLOW: a .rcpart planted as a symlink must not aim the write outside SHARE
            fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
            with open(fd, "r+b") as f:
                f.seek(offset)
                f.truncate(offset)
                while remaining > 0 and (
                    chunk := self.rfile.read(min(65536, remaining))
                ):
                    f.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionError, TimeoutError):
            pass  # link dropped/stalled mid-body: keep the partial for the next resume
        except (
            OSError
        ) as e:  # a real disk error (ENOSPC/EACCES): log it, keep the partial
            cfg.log_event("upload", os.path.basename(target), f"err {e}")
        return remaining

    def _delete(self, path: str):
        """Delete a file inside SHARE. Same confinement as read/write; only regular
        files (never the root, never a directory)."""
        target = rc_share.share_target(path.removeprefix("/files"))
        if target is None or target == cfg.SHARE or not os.path.isfile(target):
            return self._json_error(403, "bad target")
        os.unlink(target)
        cfg.log_event("delete", os.path.relpath(target, cfg.SHARE), "ok")
        self._json({"ok": True})

    def log_message(self, format: str, *args: object) -> None:
        pass  # access lines are logged by _send (>=400 only), not by http.server


class Server(ThreadingHTTPServer):
    """One thread per connection, at most max_connections at once: a phone needs a handful,
    and the cap stops a slow-drip client from growing the thread count without bound."""

    max_connections = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):
        if self._slots.acquire(blocking=False):
            return super().process_request(request, client_address)
        self.shutdown_request(request)  # full: drop it rather than spawn another thread

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # a client RST / dropped connection mid-request is normal on a lossy link (Starlink):
        # keep it out of the error log (which never rotates). Only real errors get a traceback.
        if not isinstance(sys.exc_info()[1], (ConnectionError, BrokenPipeError)):
            super().handle_error(request, client_address)


if __name__ == "__main__":
    if not cfg.TOKEN:
        raise SystemExit(
            "no launcher token: run install.sh (writes ~/.config/rc-launcher/token)"
        )
    print(
        f"rc-launcher on {cfg.BIND}:{cfg.PORT} parent={cfg.PARENT} spawn={rc_settings.spawn()}"
        f" share={'on' if cfg.SHARE_ENABLED else 'off'}"
    )
    if cfg.TAILSCALE_USERS and cfg.BIND not in ("127.0.0.1", "::1", "localhost"):
        print("warning: RC_TAILSCALE_USERS is spoofable off loopback", flush=True)
    if cfg.SHARE_ENABLED:
        threading.Thread(target=rc_share.sweep_loop, daemon=True).start()
    Server((cfg.BIND, cfg.PORT), Handler).serve_forever()
