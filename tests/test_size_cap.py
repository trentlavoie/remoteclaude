"""The module-size cap (350->375->400->410), enforced in a TRACKED test because
.claude/review.toml (where /gate reads size_cap for per-diff tiering) is gitignored and CI
never sees it. This is the enforcer; keep the two numbers in step. A file over the cap is a
decision to make (split, or raise the cap with a note), not something to let drift silently.
Raised to 410 on 2026-09-13 for the external-RC feature — rc_sessions has now hit the cap
repeatedly with legitimate, tested growth (takeover, settings, external RC), so the COMMITTED
next refactor is a view/lifecycle split (login_status/session_states/status_payload/page ->
their own module), which brings rc_sessions well back down; the bump is a stopgap for it.

rc_launcher alone carries a higher ceiling (OVERRIDES) since the 2026-09-26 web-tier hardening
(Host allowlist, POST+CSRF on every state change, Tailscale identity, CSP/security headers,
connection cap, confined/sandboxed downloads). The committed follow-up is to split the request
gate (host/token/identity/origin predicates) into its own module, bringing it back under CAP."""

import pathlib
import unittest

CAP = 410  # keep in sync with .claude/review.toml size_cap
OVERRIDES = {"rc_launcher.py": 550}  # per-file exceptions, each with a note above


class SizeCapTest(unittest.TestCase):
    def test_shipped_modules_within_cap(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        over = {
            p.name: n
            for p in sorted(root.glob("rc_*.py"))
            if (n := len(p.read_text().splitlines())) > OVERRIDES.get(p.name, CAP)
        }
        self.assertEqual(over, {}, f"shipped modules over the {CAP}-line cap: {over}")


if __name__ == "__main__":
    unittest.main()
