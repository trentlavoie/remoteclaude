# Linux server behind Tailscale

How this fork runs the launcher on a headless Ubuntu 24.04 server with a public IP (a
DigitalOcean droplet), reachable only from your own devices over your tailnet, with HTTPS.

```
iPhone / iPad (Tailscale app, VPN on)
   │  WireGuard, tailnet only
   ▼
tailscaled on the server ── tailscale serve :443 (TLS, *.ts.net cert, adds Tailscale-User-Login)
   │  http://127.0.0.1:8787  (loopback only; nothing on the public IP)
   ▼
rc-launcher.service  (token check; tmux client only)
   │  deploy/rc-tmux → $XDG_RUNTIME_DIR/rc-tmux/tmux.sock
   ▼
rc-tmux.service  (tmux server; every rc-<project> Claude session lives here)
   │  outbound HTTPS
   ▼
Anthropic's Remote Control relay ◄── the Claude app drives the session from here
```

What is exposed where:

| Where | What |
|---|---|
| Public IP | nothing new. The launcher is bound to `127.0.0.1` (pinned in `rc-launcher.service`, not overridable from the env file). |
| Tailnet | `https://<host>.<tailnet>.ts.net/` (port 443, or 8443), to the devices your tailnet policy allows. |
| Anthropic relay | the Claude sessions themselves, exactly as with upstream remoteclaude. |

The token is still the gate: anyone who has it can run code as your user. **If your user
is in the `docker` group, that means root** (a container can mount `/`). So keep the token
off shared devices, and keep the layers below in place (tailnet policy, Host allowlist,
Tailscale identity check). Leaving the `docker` group is the only way to make "the token =
your user" rather than "the token = root".

## 1. Install Tailscale on the server (you run this: it needs sudo)

From an SSH shell on the server (sudo needs a terminal to ask for your password; Claude
Code's `!` prefix has none):

```sh
bash ~/workspace/remoteclaude/deploy/install-tailscale.sh
```

It adds Tailscale's **official apt repository** (signing key fingerprint pinned, source line
checked), installs `tailscale`, joins your tailnet, and sets you as the **operator** so
`tailscale serve` works without sudo afterwards. By hand, it amounts to:

```sh
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
  | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
  | sudo tee /etc/apt/sources.list.d/tailscale.list
sudo apt-get update && sudo apt-get install tailscale
sudo tailscale up --accept-dns=false --hostname=<machine-name>
sudo tailscale set --operator=$USER
```

Why the apt repo and not `curl -fsSL https://tailscale.com/install.sh | sh`: the one-liner
ends up doing the same thing, but it runs a script you have not read, as root, from
whatever the network hands you at that moment. The repo route is a few more commands, every
one of them visible, the key is checked, and updates arrive through normal `apt upgrade`.

`--accept-dns=false` is deliberate: the server keeps its own resolver (Docker and other
services on the server may depend on it). Your phone still resolves the server's `*.ts.net` name
through MagicDNS; the server itself never needs to.

**Name it before you turn on HTTPS.** The machine name becomes `<name>.<tailnet>.ts.net`,
and every certificate issued for it is published in public Certificate Transparency logs:
anyone can learn that the name exists (not reach it). If the default hostname says more
than you like, change it first: `sudo tailscale set --hostname=<neutral-name>` (or
`TS_HOSTNAME=<neutral-name> bash deploy/install-tailscale.sh`). The tailnet part
(`tailXXXX.ts.net`) is random unless you picked a custom one.

## 2. Admin console (browser, once)

At <https://login.tailscale.com/admin>:

1. **DNS**: enable **MagicDNS**, then **HTTPS Certificates**.
2. **Machines → the server → Disable key expiry.** Otherwise the node key expires (180 days
   by default), the server drops off the tailnet, and the launcher becomes unreachable
   until someone re-authenticates it over SSH. The tradeoff: a stolen node key stays valid
   until you revoke it (remove the machine) yourself.
3. **Access controls**: restrict the launcher to your own devices (below).
4. Install the Tailscale app on the iPhone/iPad and sign in with the same account.

### Tailnet policy: only your devices reach port 443 on the server

A new tailnet allows everything between its devices. The rules below allow the launcher's
port (and SSH) only from devices *you own*. Put the server's tailnet IP (`tailscale ip -4`
on the server) in `hosts`, and your Tailscale login in place of `you@example.com`:

```jsonc
{
  "hosts": {
    "rc-server": "100.x.y.z",
  },
  "grants": [
    // the launcher (tailscale serve) and SSH: my own devices only
    { "src": ["you@example.com"], "dst": ["rc-server"], "ip": ["tcp:443", "tcp:22"] },
    // ...keep/add rules for your other machines here. Once the default allow-all rule is
    // gone, anything not granted is denied, so review before saving.
  ],
  "tests": [
    { "src": "you@example.com", "accept": ["rc-server:443"] },
  ],
}
```

Using the older `acls` syntax instead:
`{ "action": "accept", "src": ["you@example.com"], "dst": ["rc-server:443,22"] }`.
Add `tcp:8443` / `:8443` if you serve on the fallback port. Devices you tag (e.g.
`tag:server`) are not "owned by you" and are excluded by these rules, which is what you
want: another server in the tailnet should not reach the launcher.

## 3. Publish the launcher on the tailnet (no sudo)

With the services installed (`./install.sh`):

```sh
~/workspace/remoteclaude/deploy/tailscale-serve.sh
```

It checks that the node is connected, has a MagicDNS name and HTTPS certificates, that no
**Funnel** is on (Funnel would publish to the whole internet; the script never uses it and
refuses to run while one is on), that nothing listens on all interfaces on port 8787, and
that port 443 does not already serve something else. Then it runs
`tailscale serve --bg --https=443 http://127.0.0.1:8787` and prints:

- the URL, `https://<host>.<tailnet>.ts.net/`
- two lines for `~/.config/rc-launcher/rc-launcher.env`: `RC_ALLOWED_HOSTS=...` and
  `RC_TAILSCALE_USERS=...`. Add them, then `./install.sh --reload`.

The first time, `tailscale serve` may print a link to enable Serve for the tailnet: open
it, approve, and it continues. `--status` shows the config; `--off` removes this port's
handler only (it never runs `tailscale serve reset`).

**Port 443 and a web server on the same host.** `tailscale serve` answers on the node's
tailnet address inside tailscaled, so it does not collide with Caddy (or nginx) listening
on the public `:443`. If it ever does, use the fallback port:
`RC_TS_HTTPS_PORT=8443 deploy/tailscale-serve.sh` (URL becomes `https://<host>...:8443/`,
and `RC_ALLOWED_HOSTS` gets the `:8443` form). Test from the phone or another tailnet
device, not from the server itself: a local connection to its own tailnet IP is delivered
by the kernel to whatever listens on `*:443` (e.g. Caddy), not to tailscale serve.

## 4. Lock the launcher to that name and to you

These are web-tier settings in the env file (they need the fork's web-tier change; older
code ignores them):

| Setting | Value | Effect |
|---|---|---|
| `RC_ALLOWED_HOSTS` | `<host>.<tailnet>.ts.net` (+ `…:8443` if used) | Requests with any other `Host` header are refused, so the launcher cannot be reached by an IP or a rebinding DNS name. |
| `RC_TAILSCALE_USERS` | your Tailscale login | `tailscale serve` adds a `Tailscale-User-Login` header naming the device owner; other logins (a shared node, another user) are refused. |

Both are defense in depth, not the gate. Any process on the server can talk to
`127.0.0.1:8787` directly and send whatever headers it likes; the token is what stops it.

## 5. Phone

1. Tailscale app connected.
2. Open `https://<host>.<tailnet>.ts.net/?token=<token>` once. Get the token from an SSH
   session (`cat ~/.config/rc-launcher/token`), paste it, then clear that terminal's
   scrollback. The launcher stores it as an HttpOnly cookie and drops it from the URL.
3. Share → **Add to Home Screen**.

## Why HTTPS when WireGuard already encrypts

- **Secure cookies and a secure context.** Over plain `http://100.x.y.z:8787` the browser
  treats the page as insecure: the auth cookie cannot be `Secure`, and secure-context
  features (service workers, a real home-screen web app, clipboard) are off.
- **The name is checked, not just the path.** A bookmarked `http://100.x.y.z/...` with the
  Tailscale VPN switched off goes to whatever answers at that address on the network you
  are on (100.64.0.0/10 is also carrier-grade NAT space), cookie and all. With HTTPS the
  phone only talks to a server holding the certificate for your `*.ts.net` name.
- **Identity headers.** `Tailscale-User-Login` only exists on requests that came through
  `tailscale serve`, which is what makes `RC_TAILSCALE_USERS` possible.

## 6. Host firewall (optional, recommended)

The launcher itself adds nothing public, but the server should still expose only what it
means to. Prefer a **DigitalOcean Cloud Firewall** (applied before traffic reaches the
droplet, and it also covers ports that Docker publishes, which bypass ufw's rules):

| Inbound | Why |
|---|---|
| TCP 22 | SSH (ideally from your own IPs only; once Tailscale works you can restrict it further) |
| TCP 80, 443 | only if the host serves public sites (e.g. Caddy) |
| UDP 41641 | Tailscale direct connections (optional; without it traffic relays via DERP, slower) |
| UDP 60000–61000 | only if you use mosh |

Everything else: denied. Before saving, list what actually listens publicly
(`sudo ss -ltnup`, anything on `0.0.0.0` / `*` / `[::]`) and decide for each port. With ufw
instead (check what it already allows with `sudo ufw status verbose`):

```sh
sudo ufw default deny incoming
sudo ufw allow OpenSSH            # FIRST, or you lock yourself out
sudo ufw allow 80,443/tcp         # only if you serve public sites
sudo ufw allow 41641/udp          # Tailscale direct connections
sudo ufw allow 60000:61000/udp    # mosh, if used
sudo ufw enable
```

Keep the DigitalOcean web console in mind as the way back in if a firewall change cuts SSH.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Phone can't load the page | Tailscale app connected? `deploy/tailscale-serve.sh --status`; tailnet policy allows 443 from your device? |
| 502 Bad Gateway | the launcher is down: `systemctl --user status rc-launcher`, `journalctl --user -u rc-launcher -n 50` |
| Certificate warning / slow first load | HTTPS Certificates enabled? The first request after enabling waits for the cert. |
| `Access denied: serve config denied` | run once: `sudo tailscale set --operator=$USER` |
| 403 after setting the allowlists | the Host / login in the env file must match what `tailscale-serve.sh` printed |
| Launch fails with "tmux new-session failed" | `systemctl --user status rc-tmux` (the session server is down) |

## Undo

```sh
deploy/tailscale-serve.sh --off   # stop publishing (this port only)
./uninstall.sh                    # services, sessions, hook (keeps token + env file)
sudo tailscale down               # leave the tailnet (or remove the machine in the console)
```
