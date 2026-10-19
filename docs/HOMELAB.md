# Homelab side

The box only makes outbound connections: WireGuard to your server, HTTPS to
ntfy and the heartbeat. Nothing needs to change on grandma's router.

## WireGuard

`tools/babua init` prints a `[Peer]` block. Add it to whatever runs your
WireGuard server (wg-quick, wg-easy, OPNsense, UniFi, ...). With plain
wg-quick it looks like this:

```ini
# /etc/wireguard/wg0.conf on the homelab server
[Interface]
Address = 10.66.66.1/24
ListenPort = 51820
PrivateKey = <server private key>

[Peer]
# babua-tv (grandparents)
PublicKey = <printed by babua init>
PresharedKey = <printed by babua init>
AllowedIPs = 10.66.66.20/32
```

Then fill in `site/secrets.env`:

```sh
WG_ENDPOINT=vpn.example.org          # public DNS name/IP of the server (port-forward UDP 51820)
WG_SERVER_PUBLIC_KEY=<server public key>
WG_ADDRESS=10.66.66.20/24
WG_ALLOWED_IPS=10.66.66.0/24         # add e.g. ,192.168.10.0/24 to reach ntfy/Kuma on your LAN
BABUA_HOST=10.66.66.20
```

Notes:

- The box sends a keepalive every 25 s, so it stays reachable behind
  CGNAT. Your homelab end needs a reachable UDP port. If your home is behind
  CGNAT as well, use a small VPS as the WireGuard hub.
- If your endpoint is a dynamic-DNS name, that's fine: connman re-resolves
  it, and the health check reconnects when the handshake goes stale (more
  than 5 min).
- Restrict what the peer may reach on your side with the firewall, e.g.
  only ntfy and the heartbeat monitor. The box doesn't need anything else.

## ntfy

Create a topic, e.g. `babua-tv`, and a token that can only **write** to it:

```sh
ntfy user add babua-tv
ntfy access babua-tv babua-tv write-only
ntfy token add babua-tv            # -> NTFY_TOKEN
```

`NTFY_URL=https://ntfy.example.org/babua-tv`. Prefer a URL that's reachable
from the internet. If ntfy is only reachable through the VPN, alerts about
the VPN itself are queued on the box and arrive only after the tunnel is back.

What you'll receive: `Booted`, `Problem: <check>` / `Recovered: <check>` for
internet, vpn, kodi, power, temperature, storage, memory, playlist and
playback, and `Channel down` when a stream fails 4 times in a row.

## Heartbeat (finding out the box is offline)

ntfy can't tell you about silence, so point `HEARTBEAT_URL` at a dead-man's
switch:

- **Uptime Kuma**: add a monitor of type *Push* with a heartbeat interval of
  300 s and retries set to 2. Use
  `https://kuma.example.org/api/push/<token>?status={status}&msg={msg}`.
  `{msg}` carries the current problems. Let Kuma notify you via ntfy.
- **healthchecks.io**: `https://hc-ping.com/<uuid>` with period 5 min and
  grace 15 min.

## Remote control apps (optional)

The Kodi web server runs on `http://10.66.66.20:8080` (user `kodi`, password
`KODI_WEB_PASSWORD` from `site/secrets.env`). It's reachable over the VPN and
from their LAN only. The Kore (Android) and Yatse apps on your phone can use
it as a full remote. That's handy while you're on the phone with grandma.
