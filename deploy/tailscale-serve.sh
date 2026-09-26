#!/usr/bin/env bash
# Publish the launcher on your tailnet only, over HTTPS, with `tailscale serve`:
#
#   https://<host>.<tailnet>.ts.net/  ->  http://127.0.0.1:8787   (the launcher, loopback only)
#
# Idempotent, and no sudo: it needs `sudo tailscale set --operator=$USER` once, which
# deploy/install-tailscale.sh does. It never uses `tailscale funnel` (that would put the
# launcher on the public internet) and refuses to run while any Funnel is on for this node.
#
#   deploy/tailscale-serve.sh            configure (or confirm) and print the URL
#   deploy/tailscale-serve.sh --status   print the URL and the serve config; change nothing
#   deploy/tailscale-serve.sh --off      remove this port's serve config (only this port)
#
# Env: RC_TS_HTTPS_PORT  tailnet HTTPS port (default 443). tailscale serve answers on this
#                        node's tailnet address only, so it does not collide with a web
#                        server (Caddy) on the public :443; if it ever does, use 8443.
#      RC_LAUNCHER_PORT  the launcher's loopback port (default 8787)
# See docs/TAILSCALE.md.
set -euo pipefail

HTTPS_PORT="${RC_TS_HTTPS_PORT:-443}"
LPORT="${RC_LAUNCHER_PORT:-8787}"
TARGET="http://127.0.0.1:${LPORT}"
MODE="${1:-apply}"

die() {
	echo "!! $*" >&2
	exit 1
}

case "$MODE" in
	apply | --status | --off) ;;
	*funnel*) die "refusing: this script never enables Tailscale Funnel (public internet)" ;;
	*) die "usage: $0 [--status | --off]" ;;
esac
for p in "$HTTPS_PORT" "$LPORT"; do
	if ! [[ "$p" =~ ^[0-9]{1,5}$ ]] || [ "$p" -lt 1 ] || [ "$p" -gt 65535 ]; then die "not a port: $p"; fi
done

command -v tailscale >/dev/null ||
	die "tailscale is not installed: see docs/TAILSCALE.md (bash deploy/install-tailscale.sh)"
command -v python3 >/dev/null || die "python3 is needed to read tailscale's JSON"

# --- this node: connected? MagicDNS name? HTTPS certs enabled? whose node? ------------------
status_json="$(tailscale status --json 2>/dev/null)" ||
	die "tailscaled is not reachable (sudo systemctl status tailscaled)"
state="" fqdn="" certs="" login=""
while IFS=$'\t' read -r k v; do
	case "$k" in
		state) state="$v" ;;
		fqdn) fqdn="$v" ;;
		certs) certs="$v" ;;
		login) login="$v" ;;
	esac
done < <(printf '%s' "$status_json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
me = d.get("Self") or {}
user = (d.get("User") or {}).get(str(me.get("UserID", "")), {})
print("state", d.get("BackendState", ""), sep="\t")
print("fqdn", (me.get("DNSName") or "").rstrip("."), sep="\t")
print("certs", ",".join(d.get("CertDomains") or []), sep="\t")
print("login", user.get("LoginName", ""), sep="\t")
')
[ "$state" = Running ] || die "tailscale is not connected (state: ${state:-unknown}); run: sudo tailscale up"
[ -n "$fqdn" ] || die "this node has no MagicDNS name: enable MagicDNS (admin console -> DNS)"
[ -n "$certs" ] ||
	die "HTTPS certificates are off for this tailnet: admin console -> DNS -> HTTPS Certificates"

hostport="$fqdn:$HTTPS_PORT"
url="https://$fqdn/"
[ "$HTTPS_PORT" = 443 ] || url="https://$fqdn:$HTTPS_PORT/"

# --- current serve config: any Funnel at all? what does our port already serve? -------------
serve_json="$(tailscale serve status --json 2>/dev/null || true)"
funnel="" current=""
while IFS=$'\t' read -r k v; do
	case "$k" in
		funnel) funnel="$v" ;;
		current) current="$v" ;;
	esac
done < <(printf '%s' "$serve_json" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read() or "{}")
except ValueError:
    d = {}
def funnels(node):  # every AllowFunnel entry anywhere, incl. foreground sessions
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "AllowFunnel" and isinstance(v, dict):
                yield from (hp for hp, on in v.items() if on)
            else:
                yield from funnels(v)
    elif isinstance(node, list):
        for v in node:
            yield from funnels(v)
web = (d.get("Web") or {}).get(sys.argv[1]) or {}
proxy = ((web.get("Handlers") or {}).get("/") or {}).get("Proxy", "")
print("funnel", ",".join(sorted(set(funnels(d)))), sep="\t")
print("current", proxy or ("other" if web else ""), sep="\t")
' "$hostport")

case "$MODE" in
	--status)
		echo "==> $url -> ${current:-(nothing served on $HTTPS_PORT)}"
		[ -z "$funnel" ] || echo "!! Tailscale Funnel (public internet) is ON for: $funnel"
		tailscale serve status
		exit 0
		;;
	--off)
		# only this port's handler; `tailscale serve reset` would drop every other one too
		tailscale serve --https="$HTTPS_PORT" off || true
		echo "==> tailnet HTTPS port $HTTPS_PORT no longer served"
		exit 0
		;;
esac

if [ -n "$funnel" ]; then
	die "Tailscale Funnel is ON for: $funnel -- that is the public internet. Refusing.
   Turn it off first (e.g. tailscale funnel --https=${funnel##*:} off), then re-run."
fi

# --- the launcher must only listen on loopback -----------------------------------------------
if command -v ss >/dev/null &&
	ss -Hltn "sport = :$LPORT" 2>/dev/null | awk '{print $4}' | grep -Evq '^(127\.[0-9.]+|\[::1\]):'; then
	die "something listens on :$LPORT beyond loopback (all interfaces, or a public/tailnet
   address). The launcher must bind 127.0.0.1 only (rc-launcher.service pins it; check 'ss -ltnp')."
fi

# --- configure (idempotent) ------------------------------------------------------------------
if [ "$current" = "$TARGET" ]; then
	echo "==> already serving: $url -> $TARGET"
elif [ -n "$current" ]; then
	die "tailnet port $HTTPS_PORT already serves '$current'; not replacing it.
   Pick another port (RC_TS_HTTPS_PORT=8443 $0) or remove it: tailscale serve --https=$HTTPS_PORT off"
else
	echo "==> tailscale serve --bg --https=$HTTPS_PORT $TARGET"
	echo "    (the first time, tailscale may print a link to enable Serve for the tailnet: open"
	echo "     it, approve, and this continues)"
	tailscale serve --bg --https="$HTTPS_PORT" "$TARGET" ||
		die "tailscale serve failed. If it said 'Access denied', run once:
   sudo tailscale set --operator=$(id -un)"
fi

if python3 -c 'import sys,urllib.request as u; u.urlopen(sys.argv[1], timeout=3)' \
	"$TARGET/version" 2>/dev/null; then
	echo "==> launcher answering on $TARGET"
else
	echo "!! the launcher is not answering on $TARGET yet: journalctl --user -u rc-launcher -n 50"
fi

hosts="$fqdn"
[ "$HTTPS_PORT" = 443 ] || hosts="$fqdn,$fqdn:$HTTPS_PORT"
cat <<EOF

Launcher URL (tailnet only; first load may take a few seconds while the cert is issued):
    $url?token=<token>        (token: cat ~/.config/rc-launcher/token -- never printed here)

Lock the launcher to this name and your Tailscale identity. Put these in
~/.config/rc-launcher/rc-launcher.env (or pass them to ./install.sh), then ./install.sh --reload:
    RC_ALLOWED_HOSTS=$hosts
    RC_TAILSCALE_USERS=${login:-<your-tailscale-login>}
EOF
