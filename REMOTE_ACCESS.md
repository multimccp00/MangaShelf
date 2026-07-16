# Accessing MangaShelf from your phone (and away from home)

By default the server binds to **localhost only** — nothing can reach it but this
PC. There are three ways to open it up, from safest to riskiest.

## Quick reference

| Where you are | How | Command |
|---|---|---|
| Same PC | default | `web\run-web.ps1` |
| Same Wi-Fi (LAN) | bind to all interfaces | `$env:MANGASHELF_HOST="0.0.0.0"; web\run-web.ps1` |
| Anywhere (recommended) | Tailscale VPN | `web\run-web-remote.ps1` |

The app's whole API (browsing **and** editing) is gated by a per-install secret
token, so even if it's reachable, nothing can be read or changed without it. The
token is generated automatically and lives at `%USERPROFILE%\.mangashelf\web_token.txt`.
It's injected into the page when you open `http://<host>:8000/`, so your phone
gets it for free — you never copy a secret by hand.

---

## Option A — Same Wi-Fi (LAN)

1. Start with LAN binding:
   ```powershell
   $env:MANGASHELF_HOST = "0.0.0.0"
   web\run-web.ps1
   ```
2. Find this PC's LAN IP:
   ```powershell
   ipconfig | Select-String "IPv4"
   ```
   (looks like `192.168.x.x`)
3. On your phone, on the same Wi-Fi, open `http://192.168.x.x:8000`.

If the phone can't connect, allow port 8000 through Windows Firewall for
**Private** networks:
```powershell
New-NetFirewallRule -DisplayName "MangaShelf" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Profile Private
```

This exposes the app to **everyone on your Wi-Fi** (gated by the token). Fine for
a home network; don't do it on public/untrusted Wi-Fi.

---

## Option B — Anywhere, via Tailscale (recommended)

Tailscale puts your phone and PC on a private encrypted network, so your phone
reaches the PC *as if it were home* — **nothing is exposed to the public
internet, no router/port-forwarding changes, no firewall holes.** This is the
best option for "just me and my phone, from anywhere."

**One-time setup**

1. Install Tailscale on this PC: <https://tailscale.com/download>, then run
   `tailscale up` and sign in.
2. Install the Tailscale app on your phone and sign in to the **same account**.

**Each time you want access**

```powershell
web\run-web-remote.ps1
```
It finds this PC's Tailscale IP (a `100.x.y.z` address), binds the server to it
(Tailscale-only — not your LAN, not the public internet), and prints the exact
`http://100.x.y.z:8000/` URL to open on your phone. Make sure Tailscale is
connected on the phone, then open that URL.

---

## Option C — Public internet (not recommended)

Forwarding port 8000 on your router puts the app on the open internet with only
its token for protection, no HTTPS, and no real login. A
[Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
with **Cloudflare Access** in front is far safer if you truly need a public URL,
but for personal use **Tailscale (Option B) is simpler and safer** — prefer it.

---

## Security notes

- The token gates the entire API (reads and writes). Without it: `401`.
- `/api/health` is intentionally left open (it only reports a library count) so a
  launcher/monitor can check the server is up.
- Anyone who can load the page gets the token, so treat the URL like a password —
  don't post screenshots of it, and keep it on networks/people you trust.
- To rotate the token, delete `%USERPROFILE%\.mangashelf\web_token.txt` and
  restart; a new one is generated and re-injected on next page load.
