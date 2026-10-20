# Homelab side

The box only makes outbound connections: Tailscale, HTTPS to ntfy and the
heartbeat. Nothing needs to change on grandma's router.

## Tailscale (remote access)

The image includes Tailscale's static ARM build, which runs as
`babua-tailscale.service`. To join it to your tailnet:

1. Admin console → Settings → Keys → **Generate auth key**: one-off
   (not reusable), not ephemeral, pre-approved if you use device approval.
2. Put the key in `site/secrets.env` as `TAILSCALE_AUTHKEY` before building
   (the box joins ~3 min after first boot), or run
   `babua tailscale-up <key>` on the box.
3. In the admin console, open the machine `babua-tv` → **Disable key expiry**.
   The health check warns you 14 days before expiry if you forget.

It runs with `--accept-dns=false --netfilter-mode=off`, so grandma's DNS and
the LibreELEC firewall stay as they are; the firewall accepts anything
arriving over `tailscale0`. From your devices: `ssh root@babua-tv`
(MagicDNS), and `tools/babua` uses `BABUA_HOST=babua-tv`. Tailnet ACLs can
restrict who may reach it.

## WireGuard (alternative)

Only if you'd rather use your own WireGuard server. `tools/babua init` prints
a `[Peer]` block for your server; then set `WG_ENDPOINT`,
`WG_SERVER_PUBLIC_KEY`, `WG_ADDRESS`, `WG_ALLOWED_IPS` and `BABUA_HOST` in
`site/secrets.env`, or run `babua vpn-setup HOST[:PORT] SERVER_PUBKEY` on the box.
Keep `WG_ALLOWED_IPS` to homelab networks only (split tunnel).

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
internet, vpn, kodi, power, temperature, storage, memory, playlist,
playback and screen (a Kodi error/confirm dialog left on the TV for 5-10 min,
with its text), and `Channel down` when a stream fails 4 times in a row.

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

## Debugging from lenovo-homelab

The homelab (`~/projects/bebiababua`, branch `main`) can reach the box for
diagnosis without the `site/` secrets:

- `ssh babua-tv`: its own key `~/.ssh/babua_tv_ed25519`, host key pinned in
  `~/.ssh/babua_tv_known_hosts` (`~/.ssh/babua_tv.conf`). It was added to the
  box's `/storage/.ssh/authorized_keys` by hand. There's no `site/` (and no
  deploys) yet; whoever creates one with `tools/babua init` must put
  `~/.ssh/babua_tv_ed25519.pub` in `site/authorized_keys`, or `deploy` drops it.
- `babua-tv status | logs [unit] | screenshot | play N | key ... | ssh ...`:
  `tools/babua` with `BABUA_SITE=~/.config/babua-site` (only the box's public
  host key). `deploy` and `image` refuse there; changed files were copied by
  hand (`cat file | ssh babua-tv 'cat > /storage/...'`), then committed.
- Non-interactive SSH has no `/storage/.config/babua/bin` on PATH: use
  `export PATH=$PATH:/storage/.config/babua/bin` first, or full paths.
- Never print `/storage/.config/babua/secrets.env` or the playlist URLs
  (provider credentials).
- They are using the TV: restart babuad freely (playback continues), but not
  Kodi, iwd or the box while something plays. Network-touching changes go
  through a detached `systemd-run` script with a watchdog that reverts if the
  router stops answering, because SSH drops with the network.
- `pkill -f PATTERN` over SSH also matches the remote shell running it and
  ends the session.

Useful checks:

```sh
babua-tv ssh 'iw dev wlan0 link; iw dev wlan0 station dump | grep -E "tx failed|signal"'
babua-tv ssh 'ping -c 40 -i 0.5 ROUTER_IP | tail -2'    # their router; loss = Wi-Fi
babua-tv ssh 'cec-ctl -d0 --to 0 --give-device-power-status; cec-ctl -d0 --to 0 --request-active-source'
babua-tv ssh 'grep -E "stalled|timeout waiting|demuxer|JSONRPC" /storage/.kodi/temp/kodi.log | tail'
```

Alerts: since 2026-10-19 the box's `NTFY_URL` is a private ntfy.sh topic (no
token; the random topic name is the secret), kept on the homelab in
`~/.config/secrets/babua-ntfy-url`. `babua-tv notify-test` sends a test.
Silence is covered from the homelab side: its `babua-heartbeat.timer` pings the
box over Tailscale every 5 min and posts "Offline" to the same topic after
~15 min without an answer (no `HEARTBEAT_URL` on the box).

Their network (2026-10): an Eero mesh. The Pi is held to 2.4 GHz (`device/.config/iwd/main.conf`);
its 5 GHz link to the Eero collapsed under streaming load.
