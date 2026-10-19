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

## Messages, photos and clips on their TV

`tools/babua show` plays something on the TV and then returns to the
channel by itself. If the TV is off it refuses (add `--force` to play anyway).
Grandma can end it early with BACK or OK.

```sh
tools/babua show birthday.jpg 30     # photo, 30 s (converted to a clip locally)
tools/babua show hello.mp4           # re-encoded to H.264 720p if the Pi 3 can't play it as is
tools/babua show 'http://...'        # any URL the box can reach
tools/babua say "Позвони мне!"       # small text pop-up, doesn't interrupt the picture
```

Local files need `ffmpeg` on your machine. They are uploaded to
`/storage/babua-media/` (only the latest is kept).

**From Jellyfin:** nothing on the TV depends on Jellyfin; it's just one more
source. Add Jellyfin's network to `WG_ALLOWED_IPS` (and allow it in your
homelab firewall), create a dedicated API key (Dashboard → API Keys), then:

```sh
# ask Jellyfin to transcode to something a Pi 3 decodes
tools/babua show "http://192.168.10.5:8096/Videos/<ItemId>/stream.mp4?VideoCodec=h264&AudioCodec=aac&MaxHeight=720&api_key=<key>"
```

Use Jellyfin's IP: the box uses their DNS, which can't resolve your homelab names. The item id is in the Jellyfin web UI URL (`...details?id=<ItemId>`). I
haven't tested these URL parameters against a live server. If they don't
work, download the file and use `babua show file.mp4`, which transcodes
locally. The API key ends up in Kodi's log on the box, so use a key that
does nothing else.
