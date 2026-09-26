"""The ~/rc-share file share: what a request is allowed to reach, how a directory is
rendered, and the sweep that reclaims abandoned uploads.

This is the only remotely-reachable code that resolves arbitrary paths, so the
confinement predicate lives here once and every read, write and listing goes through it —
a second definition is how a boundary drifts. The HTTP verbs themselves stay in
rc_launcher; this module never sees a request.
"""

import contextlib
import hashlib
import html
import mimetypes
import os
import shutil
import stat
import time
from datetime import datetime
from urllib.parse import quote, unquote

import rc_config as cfg
from rc_claude import MT
from rc_files_page import FILES_PAGE
from rc_templates import fill, js


def within_share(p: str) -> bool:
    """The single definition of 'this real path is inside the share', used by every
    read and write path so the confinement boundary can't drift between them."""
    return p == cfg.SHARE or p.startswith(cfg.SHARE + os.sep)


def share_target(rel: str) -> str | None:
    """Resolve a /files/<rel> request to a path confined to SHARE, or None.

    realpath collapses '..' and resolves symlinks in one shot, so a symlink
    inside the share pointing outside it lands out of the root and is rejected;
    http.server's own handler only blocks lexical '..', not symlink escape.
    """
    rel = unquote(rel)
    if "\x00" in rel:
        return None
    target = os.path.realpath(os.path.join(cfg.SHARE, rel.lstrip("/")))
    return target if within_share(target) else None


def part_paths(rel: str, rid: str = "") -> tuple[str | None, str]:
    """(target, tmp) for a /files write, both confined to SHARE, or (None, '').

    The .rcpart temp is keyed by the client's X-Rc-Id, so a stale partial left from a
    different file of the same name resolves to a *different* temp — the resume starts
    fresh instead of merging new bytes onto old ones and corrupting the result. That
    suffix is the share's own vocabulary: sweep_rcparts() reclaims it and rows_html()
    hides it, so all three live here.
    """
    target = share_target(rel)
    if target is None or target == cfg.SHARE or os.path.isdir(target):
        return None, ""
    name = os.path.basename(
        target
    )  # a temp's own suffix, or a control char, never lands
    if name.endswith(".rcpart") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None, ""
    # sha1 tags the temp by X-Rc-Id — a filename key, not a security digest, so
    # usedforsecurity=False (unchanged output, and it works on FIPS-restricted hosts).
    digest = hashlib.sha1(rid.encode(), usedforsecurity=False).hexdigest()[:12]
    return target, f"{target}{f'.{digest}' if rid else ''}.rcpart"


def have(tmp: str) -> int:
    """Bytes already on disk for a resumable upload's temp (0 if none). lstat, and regular
    files only: a .rcpart planted as a symlink is never measured, resumed or finalized."""
    try:
        st = os.lstat(tmp)
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) else 0


def upload_refusal(total: int, held: int) -> tuple[int, str] | None:
    """(status, reason) when an upload of `total` bytes (`held` already on disk) must be
    refused before its body is read: over the per-file cap, or it would leave the share's
    disk under the free-space floor. None when it may proceed."""
    if total > cfg.UPLOAD_MAX:
        return 413, "too large"
    if shutil.disk_usage(cfg.SHARE).free - (total - held) < cfg.SHARE_MIN_FREE:
        return 507, "insufficient storage"
    return None


# Sandbox for anything served from the share: no script, no plugins, a unique origin.
FILE_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox"


def serve_as(path: str) -> tuple[str, str, str | None]:
    """(Content-Type, Content-Disposition, CSP) for a download. The launcher origin carries
    the auth cookie, so an uploaded .html/.svg rendered inline would be stored XSS with full
    launcher access: only passive media and plain text open inline; everything else is a
    sandboxed octet-stream attachment."""
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    name = f"filename*=UTF-8''{quote(os.path.basename(path))}"
    if ctype == "application/pdf" or (
        ctype.startswith(("image/", "audio/", "video/")) and "svg" not in ctype
    ):
        return ctype, f"inline; {name}", None
    if ctype in ("text/plain", "text/csv", "text/markdown", "application/json"):
        return ctype, f"inline; {name}", FILE_CSP
    return "application/octet-stream", f"attachment; {name}", FILE_CSP


def _norm(rel: str) -> str:
    """rel (still percent-encoded, as the request carried it) with each segment decoded
    and requoted once — the one href shape the crumb, the rows and the upload URL share."""
    return "".join("/" + quote(unquote(seg)) for seg in rel.split("/") if seg)


def share_page(target: str, rel: str) -> bytes:
    return fill(
        FILES_PAGE,
        {
            "__REL__": js(_norm(rel)),
            "__HOST__": html.escape(cfg.HOST),
            "__CRUMB__": crumb_html(rel),
            "__ROWS__": rows_html(target, rel),
        },
    )


def crumb_html(rel: str) -> str:
    """rel arrives still percent-encoded (the raw URL remainder _files hands over).
    Decode each segment, then requote: quoting the encoded form doubled the escapes
    ("my file" -> href /files/my%2520file, a 404, labeled "my%20file")."""
    out = ['<a href="/files">rc-share</a>']
    acc = ""
    for seg in (s for s in rel.split("/") if s):
        seg_dec = unquote(seg)
        acc += "/" + quote(seg_dec)
        out.append(f'<a href="/files{acc}">{html.escape(seg_dec)}</a>')
    return "<span class=sep>/</span>".join(out)


def rows_html(target: str, rel: str) -> str:
    """One <li> per child, dirs first. Each row carries data-d/n/s/t (is-dir, lowercased
    name, size bytes, mtime) so the page can re-sort client-side without a round trip; the
    server default (name, dirs first) is the no-JS fallback. Symlinks whose real target
    escapes SHARE are never listed or linked — the same confinement share_target() enforces."""
    try:
        names = sorted(os.listdir(target))
    except OSError:
        return "<li class=empty>unreadable</li>"  # a permission failure is not "empty"
    base = _norm(rel)  # requoted, so a raw '"' in the request path can't leave the href
    dirs, files = [], []
    for name in names:
        if name.endswith(".rcpart"):  # in-progress/partial upload — hide it
            continue
        full = os.path.join(target, name)
        if not within_share(os.path.realpath(full)):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        href = f"/files{base}/{quote(name)}"
        # from the stat above; os.stat followed symlinks too
        is_dir = stat.S_ISDIR(st.st_mode)
        data = (
            f'data-d="{int(is_dir)}" data-n="{html.escape(name.lower(), quote=True)}" '
            f'data-s="{st.st_size}" data-t="{int(st.st_mtime)}"'
        )
        if is_dir:
            dirs.append(
                f'<li class=dir {data}><a href="{href}">'
                f"<span class=nm>{html.escape(name)}/</span></a></li>"
            )
        else:
            when = f"{datetime.fromtimestamp(st.st_mtime, MT):%m/%d %H:%M}"
            files.append(
                f'<li {data}><a href="{href}"><span class=nm>{html.escape(name)}'
                f"</span><span class=meta>{human_size(st.st_size)} &middot; "
                f"{when}</span></a></li>"
            )
    rows = dirs + files
    return "\n".join(rows) if rows else "<li class=empty>empty</li>"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def sweep_rcparts() -> int:
    """Remove abandoned .rcpart temps under SHARE (an interrupted upload never resumed).
    Keyed on mtime, so an in-progress or actively-resuming upload — which keeps writing —
    is never swept. Returns how many were removed."""
    cutoff = time.time() - cfg.RCPART_TTL
    parts = (
        os.path.join(root, name)
        for root, _, files in os.walk(cfg.SHARE)
        for name in files
        if name.endswith(".rcpart")
    )
    n = 0
    for p in parts:
        with contextlib.suppress(OSError):
            if os.path.getmtime(p) < cutoff:
                os.unlink(p)
                n += 1
    return n


def sweep_loop() -> None:
    """The launcher's background sweeper thread."""
    while True:
        if swept := sweep_rcparts():
            cfg.log_event("sweep", "rcparts", str(swept))
        time.sleep(1800)
