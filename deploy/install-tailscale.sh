#!/usr/bin/env bash
# Install Tailscale on Ubuntu from Tailscale's official apt repo and join this host to
# your tailnet. Idempotent: re-running skips whatever is already done.
#
# Needs sudo, and sudo needs a password typed into a real terminal, so run it from an
# SSH shell (not Claude Code's `!` prefix, which has no terminal):
#
#   bash ~/workspace/remoteclaude/deploy/install-tailscale.sh
#
# Env overrides:
#   TS_HOSTNAME   tailnet machine name (default: this host's hostname). It becomes
#                 <name>.<tailnet>.ts.net and, once HTTPS certs are on, is published in
#                 public Certificate Transparency logs.
set -euo pipefail

# Tailscale's package signing key (https://pkgs.tailscale.com). The download is refused
# unless it carries exactly this primary key.
TS_KEY_FPR=2596A99EAAB33821893C0A79458CA832957F5868
KEYRING=/usr/share/keyrings/tailscale-archive-keyring.gpg
SOURCES=/etc/apt/sources.list.d/tailscale.list

die() {
	echo "error: $*" >&2
	exit 1
}

[ "$(id -u)" -ne 0 ] || die "run as your normal user; the script calls sudo itself"
if ! sudo -n true 2>/dev/null && [ ! -t 0 ]; then
	die "sudo needs your password and there is no terminal to type it into.
Run this from an SSH shell instead:  bash $(realpath "$0")"
fi

# shellcheck source=/dev/null
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "this script supports Ubuntu only (found: ${ID:-unknown})"
codename=${VERSION_CODENAME:?no VERSION_CODENAME in /etc/os-release}

echo "==> sudo check (you may be asked for your password)"
sudo -v

if command -v tailscale >/dev/null; then
	echo "==> tailscale already installed: $(tailscale version | head -1)"
else
	echo "==> adding Tailscale apt repo for ubuntu/$codename"
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	base="https://pkgs.tailscale.com/stable/ubuntu/$codename"
	curl -fsSL --proto '=https' --tlsv1.2 "$base.noarmor.gpg" -o "$tmp/key.gpg"
	curl -fsSL --proto '=https' --tlsv1.2 "$base.tailscale-keyring.list" -o "$tmp/ts.list"

	fpr=$(gpg --show-keys --with-colons "$tmp/key.gpg" 2>/dev/null | awk -F: '/^fpr/ {print $10; exit}')
	[ "$fpr" = "$TS_KEY_FPR" ] || die "unexpected signing key fingerprint: ${fpr:-none}"
	grep -qxF "deb [signed-by=$KEYRING] https://pkgs.tailscale.com/stable/ubuntu $codename main" "$tmp/ts.list" ||
		die "unexpected apt source line in $base.tailscale-keyring.list"

	sudo install -m 0644 "$tmp/key.gpg" "$KEYRING"
	sudo install -m 0644 "$tmp/ts.list" "$SOURCES"
	echo "==> installing tailscale"
	sudo apt-get update -qq
	sudo apt-get install -y tailscale
fi

sudo systemctl enable --now tailscaled

state=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("BackendState",""))' || true)
if [ "$state" = Running ]; then
	echo "==> already connected to the tailnet"
else
	echo "==> joining the tailnet: open the login URL printed below and sign in"
	# --accept-dns=false: leave the droplet's resolver alone (Docker, Caddy and Hermes rely
	# on it). Phones still resolve the droplet's ts.net name through MagicDNS.
	sudo tailscale up --accept-dns=false --hostname="${TS_HOSTNAME:-$(hostname)}"
fi

# Lets $USER run `tailscale serve` without sudo (used by deploy/tailscale-serve.sh).
sudo tailscale set --operator="$USER"

echo
tailscale status | head -5
dns=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
cat <<EOF

Done. This machine is: $dns

Remaining steps (browser / phone):
  1. https://login.tailscale.com/admin/dns: enable MagicDNS, then HTTPS Certificates.
  2. Install the Tailscale app on your iPhone and iPad and sign in with the same account.
  3. Tell Claude Code it's done; it runs deploy/tailscale-serve.sh (no sudo needed now).
EOF
