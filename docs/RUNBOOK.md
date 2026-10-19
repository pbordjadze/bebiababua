# Runbook

All commands run from the repo on your machine. `tools/babua ssh` gives a
shell on the box, where the same tools are on the PATH as `babua ...`.

## "The TV doesn't work"

1. `tools/babua status`. This shows what's playing, whether the screen is
   blanked, and all health checks.
2. `tools/babua screenshot`. This shows what she actually sees.
3. Depending on what you find:

| Symptom | Likely cause | Fix |
|---|---|---|
| Screen dark/dimmed, nothing playing | TV came on without waking Kodi | `tools/babua play N` and tell her "press OK when it's dark". If it happens daily, see VERIFY §3. |
| "Канал сейчас не работает" | That stream is down upstream | `tools/babua check-streams`; if needed, remap the channel in `channels.toml` → `deploy` |
| Every channel fails | Provider down, account expired or IP blocked | `tools/babua logs babua-playlist`; `tools/babua ssh 'cat /storage/.cache/babua/playlist.json'` |
| Picture stutters | Weak network, HEVC stream, or under-voltage | `tools/babua health` (power), `tools/babua codecs`; Wi-Fi under load: `tools/babua ssh 'ping -c 40 -i 0.5 <their router>'` while a channel plays (loss = Wi-Fi, see `iw dev wlan0 link`) |
| Kodi menus in English / wrong look | Someone changed settings | `tools/babua restart-kodi` (prekodi + babuad put them back) |
| Box unreachable | Power, internet or VPN | Check the heartbeat monitor; ask them to unplug the Pi and plug it back in; last resort: swap in the spare SD card |
| Kodi in red/maroon "safe mode" | Kodi crashed repeatedly at start | `tools/babua kodi-log`, fix the cause, then `tools/babua restart-kodi` |

## Changing things

Change config, not the box. Edit the file, then run `tools/babua deploy`. It
renders `device/` + `config/` + `site/`, unpacks the result into `/storage`,
reloads the units, re-applies Kodi settings, rebuilds the playlist and
restarts the VPN only if its config changed. The repo stays the source of
truth, and `tools/babua image` reproduces the same box at any time.

- **Channels:** `config/channels.toml` → `tools/babua preview` → `deploy`.
  Kodi picks up the new list the next time nothing is playing.
- **Logos:** put PNGs in `config/logos/` and reference them as
  `logo = "file.png"`.
- **Provider / credentials / VPN / ntfy:** `site/secrets.env` → `deploy`.
- **CEC behaviour** (`device/.../babua_lib.py: CEC_SETTINGS`): Kodi only reads
  it at start, so run `deploy` and then `restart-kodi`.
- **Kodi settings** (`kodi_settings()` in the same file): `deploy` applies them
  live.

## Updating

- **LibreELEC** (12.x bugfix releases): `tools/babua update 12.2.2`. This
  downloads and checksum-verifies the update and stages it; it installs at
  the next reboot (the weekly one, or `tools/babua reboot` when nothing is
  playing). Auto-update is off on purpose. Read the release notes first, and
  avoid major versions (13.x) until you've checked Pi 3 support and add-on
  compatibility. Also update `tools/versions.json` so fresh images match.
- **Add-ons** are pinned (Kodi never auto-updates them). To update, change the
  URLs and sha256 values in `tools/versions.json` and rebuild the image. For
  a live box: `tools/babua ssh`, then
  `curl -fLO <zip-url> && python3 -m zipfile -e <zip> /storage/.kodi/addons/ && systemctl restart kodi`.

## Disaster recovery

- **Dead SD card / corrupted storage:** flash a new card with
  `tools/babua image` (or use the spare card left at the house). Everything
  comes back: config, channels, keys and host key, so the pinned SSH key
  still matches. Only the "last channel" is lost.
- **Lost SSH key:** `site/authorized_keys` should contain a second key (phone
  or hardware key). Otherwise you'll need a USB keyboard at the box or a new
  image.
- **Factory reset:** don't. Reflash instead.

## Where things are on the box

```
/storage/.config/babua/        babua.conf, channels.toml, secrets.env (0600), bin/
/storage/.cache/babua/         playlist.m3u, playlist.json, health.json, player.json, outbox.jsonl
/storage/.config/system.d/     babua*.service / *.timer
/storage/.config/wireguard/    babua.config (connman WireGuard provider)
/storage/.config/iwd/          main.conf (Wi-Fi: 2.4 GHz only), read via system.d/iwd.service.d/
/storage/.kodi/temp/kodi.log   Kodi log
journalctl -u babuad -u babua-health -u babua-playlist -u kodi
```
