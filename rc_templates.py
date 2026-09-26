"""The chunks both launcher pages share, and the two helpers that fill a page in.

Splitting the pages out (rc_page, rc_files_page) left the parts that must NOT diverge —
the colour tokens, the base rules, the pull-to-refresh overlay, the tested upload-resume
policy — here, spliced into each page once at import by shared(). fill() and js() do the
per-request half: the placeholders a page still carries (__PROJECTS__, __ROWS__, ...) are
filled with live data on the way out, plus a fresh CSP nonce for the page's one <script>.
"""

import json
import re
import secrets
from pathlib import Path
from types import MappingProxyType

_UPLOAD = (Path(__file__).parent / "rc_upload.js").read_text(encoding="utf-8")
_DOWNLOAD = (Path(__file__).parent / "rc_download.js").read_text(encoding="utf-8")

# shared by both pages (filled into __PTR__ below) — the pull-to-refresh overlay
_PTR = """(function(){var y0=0,a=false,dy=0,T=64,se=document.scrollingElement||document.documentElement;
var b=document.createElement('div');
b.style.cssText='position:fixed;left:0;right:0;top:0;height:0;overflow:hidden;display:flex;align-items:flex-end;justify-content:center;color:var(--mut);font:12px ui-monospace,monospace;padding-bottom:6px;z-index:9;pointer-events:none;transition:height .12s';
document.body.appendChild(b);
addEventListener('touchstart',function(e){if(se.scrollTop<=0){y0=e.touches[0].clientY;a=true;dy=0;}},{passive:true});
addEventListener('touchmove',function(e){if(!a)return;dy=e.touches[0].clientY-y0;
if(dy>0&&se.scrollTop<=0){b.style.height=Math.min(dy*0.5,46)+'px';b.textContent=dy>T?'\\u21bb release to refresh':'\\u21bb pull to refresh';}else{a=false;b.style.height='0';}},{passive:true});
addEventListener('touchend',function(){if(!a)return;a=false;if(dy>T){b.style.height='46px';b.textContent='\\u21bb refreshing\\u2026';location.reload();}else{b.style.height='0';}});})();"""

# shared color tokens + light-mode overrides, spliced into both pages (filled into __THEME__) so
# the palette can't drift between the launcher and the files page. FILES_PAGE also gets --panel
# and --red (unused there, harmless) rather than maintaining a second, divergent token list.
_THEME = """:root{--bg:#0b0f14;--panel:#121821;--row:#161d28;--row2:#1b2330;--fg:#d7e0ea;--mut:#7c8a9c;--accent:#22c55e;--blue:#2563eb;--name:#a9c4f0;--red:#7f1d1d;--bd:#243040}
@media (prefers-color-scheme:light){:root{--bg:#eef1f5;--panel:#fff;--row:#fff;--row2:#e6eaf0;--fg:#16202e;--mut:#5b6b7c;--accent:#16a34a;--name:#2b4c85;--red:#dc2626;--bd:#d7dee7}}"""

# base rules byte-identical in both pages (filled into __BASE__) — only these three;
# header/h1/.nm deliberately diverge between the pages and stay per-page.
_BASE = """*{box-sizing:border-box}
body{margin:0;font:16px ui-monospace,SFMono-Regular,Menlo,monospace;
background:var(--bg);color:var(--fg)}
ul{list-style:none;margin:0;padding:6px 10px 48px}"""


_CHUNKS = MappingProxyType(
    {
        "__THEME__": _THEME,
        "__BASE__": _BASE,
        "__PTR__": _PTR,
        "__UPLOAD__": _UPLOAD,
        "__DOWNLOAD__": _DOWNLOAD,
    }
)


def shared(template: str) -> str:
    """Splice the shared chunks into a page template — once, at import. A placeholder a
    page doesn't carry is simply absent: only the files page has an upload widget."""
    for placeholder, chunk in _CHUNKS.items():
        template = template.replace(placeholder, chunk)
    return template


def js(x: object) -> str:
    """JSON for splicing into a <script> body: escape '<' so a value containing
    '</script>' (a file/dir named that, dropped into SHARE over SMB) can't break out."""
    return json.dumps(x).replace("<", "\\u003c")


# Every response carries these; its Content-Security-Policy is BASE_CSP unless it is a page
# (page_csp: the nonce'd script) or a share file (rc_share.serve_as: a sandbox).
HARDENING = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
BASE_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class Page(bytes):
    """A filled page, carrying the CSP nonce minted for its <script> so the handler can send
    the matching Content-Security-Policy on exactly this response."""

    nonce = ""


def page_csp(nonce: str) -> str:
    """The launcher pages' policy: only the nonce'd inline script runs (an injected <script>
    or on*= handler can't), same-origin fetches only, never framed. Inline styles stay
    allowed: the pages use style= attributes, and CSS can't run code."""
    return (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


def fill(template: str, values: dict[str, str]) -> bytes:
    """Fill __PLACEHOLDER__s in one pass, so injected data (a project dir named __LOGIN__,
    which NAME_RE permits) can't be re-scanned and rewritten by a later replacement. A
    template's __NONCE__ slot gets a fresh CSP nonce in that same pass, so a nonce can only
    ever land where the template put one, never inside injected data."""
    if nonce := secrets.token_urlsafe(18) if "__NONCE__" in template else "":
        values = values | {"__NONCE__": nonce}
    # longest key first: re alternation takes the first match, so a key that prefixes
    # another (a future __HOST__/__HOSTS__ pair) must not shadow the longer one.
    pat = re.compile("|".join(map(re.escape, sorted(values, key=len, reverse=True))))
    page = Page(pat.sub(lambda m: values[m.group()], template).encode())
    page.nonce = nonce
    return page
