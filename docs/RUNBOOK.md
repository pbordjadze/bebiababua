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
| Picture stutters | Weak network, HEVC stream, or under-voltage | `tools/babua health` (power), `tools/babua codecs`; Wi-Fi under load: `tools/babua ssh 'ping -c 40 -i 0.5 ROUTER_IP'` while a channel plays (loss = Wi-Fi, see `iw dev wlan0 link`) |
| Kodi menus in English / wrong look | Someone changed settings | `tools/babua restart-kodi` (prekodi + babuad put them back) |
| Box unreachable | Power, internet or VPN | Check the heartbeat monitor; ask them to unplug the Pi and plug it back in (there is no spare SD card) |
| TV on, but showing its own tuner/app, not the Pi | TV powered on into another input | Automatic since babuad's CEC watch (`TV turned on: switching it to our input` in `logs babuad`). By hand: `tools/babua ssh 'cec-ctl -d0 --to 0 --image-view-on; cec-ctl -d0 --active-source phys-addr=1.0.0.0'` (the Pi's address: `cec-ctl -d0 \| grep Physical`) |
| A Kodi dialog over the picture | e.g. "Воспроизведение плейлиста прервано" after two channel opens at once | `tools/babua screenshot` to read it; `tools/babua key select` presses its OK |
| babuad log: `JSONRPC.Ping: ... Method not found` | Kodi's start-up is blocked on a modal dialog (it opens the start window before loading its JSON-RPC methods; the TCP server already answers) | `tools/babua screenshot`; `tools/babua key select`. kodi.log lacks `JSONRPC v13.5.0: Successfully initialized` until then. The "no PVR add-on" case is prevented by prekodi. |
| Kodi in red/maroon "safe mode" | Kodi crashed repeatedly at start | `tools/babua kodi-log`, fix the cause, then `tools/babua restart-kodi` |

## "It died" / "It only works for a while": what was it doing?

The box keeps its own records on the SD card, so after a restart you can see
what happened before it:

```sh
tools/babua boots            # each boot: why it started, what it was doing when last alive
tools/babua boots 3 -v       # ... with the previous boot's last journal lines
tools/babua vitals -3h       # once-a-minute vitals; also `vitals 60`, `vitals 14:30`, `vitals '2026-11-03 14:30'`
tools/babua ssh 'journalctl -b -1 -n 200'   # the previous boot's journal (kept since 2026-11-04)
tools/babua ssh 'ls /storage/.cache/babua/kodi-logs'   # every earlier kodi.log, gzipped
```

Reading a boot record:

- **reset** is the Pi's reset register at boot (`vcgencmd get_rsts`):
  `power-on` (0x1000) means the power was cut or dipped; `watchdog` (0x20
  set) means a reboot, or a hang that the hardware watchdog ended.
- **end** is how the previous boot's vitals log ended: `reboot`/`poweroff`
  is a clean shutdown; "stopped mid-run" means it just stopped.
- So: stopped mid-run + power-on = **power lost**; stopped mid-run + watchdog =
  **froze**; a clean end = a planned restart.
- "TV on before, standby at return": the TV lost power too (a switched outlet,
  a breaker, an outage). "TV on before, on at return": only the Pi did (its
  supply or cable). Under-voltage flags in the last minutes point at the
  supply.

A vitals line: time, uptime, temperature, core volts, ARM clock, power flags
(`!` now, `*` since boot; `UV 10s` = under-voltage seen in that minute), load,
CPU, free memory, Kodi's memory, Wi-Fi signal, bitrate, access point (last
bytes of its MAC), failed transmits, ping to the router and to 1.1.1.1, network
receive rate, Tailscale, the TV's power state (from babuad's CEC polls; nothing
is sent to the TV for this), HDMI, and what Kodi plays (with seconds spent
buffering or frozen in that minute). `--json` shows everything.

## Stream soak test

`soak.py` plays channels the way Kodi would (HLS playlist and every
segment) but throws the data away: nothing on the TV, Kodi not involved. It
logs bytes, bitrate per minute, where a player would have stalled, HTTP
errors, drops (with how long the connection had lasted: a provider that cuts
after a fixed time shows up as drops at the same age) and reconnects. About
1.8 GB an hour per channel (~4 Mbit/s).

It uses the one provider connection only while nobody can be watching: it
won't start if the TV is on or something plays, and stops for the rest of the
night the moment Kodi starts playing, the screen wakes (TV on, any key) or
babuad sees the TV on; babuad also stops it before opening a channel, and the
playlist rebuild, the stream check and `babua reboot` wait while it runs.

```sh
tools/babua soak status                  # running? which nights are scheduled
tools/babua soak report                  # the latest run's summary (or: soak report 2026-11-05_0100)
tools/babua soak schedule 2026-11-07 --channels 2,1,3 --first-min 90 --each-min 15 --until 06:25 --budget-gb 9
tools/babua soak unschedule 2026-11-07
tools/babua soak stop
```

`babua-soak.timer` fires at 01:00, 02:00 and 03:00; it runs a night listed in
`soak/schedule.json` (the date is the morning it runs) once, at the first of
those hours when nobody's watching. Results: `/storage/.cache/babua/soak/<date>_<time>/`
(`events.jsonl`, `summary.txt`; no URLs).

## Alerts and what the box already does about them

| Alert | Meaning | The box itself | You |
|---|---|---|---|
| `Problem: internet` | No internet; the router may still answer | Nothing to fix locally. babuad keeps retrying the channel once a minute while the TV is on and shows "Нет интернета…" | Usually their ISP; wait, or have them restart the Eero |
| `Problem: router` | The Pi can't reach their router | Restarts Wi-Fi after ~15 and ~30 min, reboots after ~45 min (at most once per 6 h) | If it persists: Eero down, or the Pi needs unplugging |
| `Channel down` | A channel failed 4 times (says if there was no internet) | Retries once a minute while the TV is on; at most one alert an hour | `tools/babua check-streams` |
| `Problem: playlist` … hidden | The daily check found a channel dead | Hides it (numbers don't shift) and brings it back when it answers | Nothing, unless it stays hidden for days |
| `Problem: screen` | A Kodi message box is up | babuad closes OK boxes after ~2 min; this one is something else | `tools/babua screenshot` |
| `Problem: babuad` | The daemon is down, crash-looping or logging errors | systemd restarts it | `tools/babua logs babuad` |
| `Problem: channels` | Kodi has no TV channels | - | IPTV add-on off or Kodi in safe mode: `tools/babua kodi-log` |
| `Problem: cec` | The TV hasn't answered HDMI-CEC for a day; her remote won't work | - | Anynet+ off, or the HDMI cable moved |
| `Restarted after power loss` | The box came back after its power was cut (reset register: power-on, log stopped mid-run). The message says when it was last alive, what played, and the TV before/after | - | `tools/babua boots -v`. TV off at return too: a switch, breaker or outage cut both. Only the Pi: its supply or cable |
| `Restarted after a hang` | The hardware watchdog reset a frozen box | - | `tools/babua boots -v` (the journal before it) |
| `Booted` | A clean restart (weekly reboot, `babua reboot`) or no record from before | - | - |
| `Problem: telemetry` | The vitals log stopped, or the journal isn't on the SD card | - | `tools/babua logs babua-vitals`; `mount \| grep /var/log` |
| `Health check not running` (from the homelab) | The box is online but its health check has stopped | - | `systemctl status babua-health.timer` |

A frozen Pi resets itself: `babua-watchdog.service` turns on systemd's hardware
watchdog (~15 s).

The weekly reboot (Monday 04:30, `babua-reboot.timer`) is skipped, until the
next Monday, if something plays, the TV is on (or babuad can't say), a soak
test runs, the box has been up less than a day, or the clock isn't set. It
never starts a channel afterwards. Until 2026-11-04 it never ran: it waited
for a systemd-timesyncd flag, and LibreELEC sets the clock with connman.
`tools/babua ssh 'journalctl -u babua-reboot'` shows what it did.

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

- **Dead SD card / corrupted storage:** there is no spare card at the house
  (2026-10). A new card needs a `site/` with current secrets: the provider URL,
  a fresh Tailscale auth key (the first was one-off), and every SSH key that
  should work, including the homelab's (`~/.ssh/babua_tv_ed25519.pub` there).
  With those, `tools/babua image` brings back config, channels and the host
  key (pinned SSH still matches). It joins Tailscale as a new node: remove the
  old `babua-tv` in the admin console first so the name stays the same.
- **Lost SSH key:** `site/authorized_keys` should contain a second key (phone
  or hardware key). Otherwise you'll need a USB keyboard at the box or a new
  image.
- **Factory reset:** don't. Reflash instead.

## Where things are on the box

```
/storage/.config/babua/        babua.conf, channels.toml, secrets.env (0600), bin/
/storage/.cache/babua/         playlist.m3u, playlist.json, health.json, player.json, outbox.jsonl,
                               boots.jsonl (one record per boot)
/storage/.cache/babua/vitals/  vitals.jsonl (a line a minute), vitals-*.jsonl.gz (older, 100 MB cap)
/storage/.cache/babua/kodi-logs/  every earlier kodi.log, gzipped (last 30)
/storage/.cache/babua/soak/    schedule.json, <date>_<time>/events.jsonl + summary.txt
/storage/.cache/log/journal/   the journal, kept across boots (200 MB cap; /var/log is bound here)
/storage/.cache/journald.conf.d/00_settings.conf   turns that on (LibreELEC's own switch)
/storage/.config/system.d/     babua*.service / *.timer
/storage/.config/wireguard/    babua.config (connman WireGuard provider)
/storage/.config/iwd/          main.conf (Wi-Fi: 2.4 GHz only), read via system.d/iwd.service.d/
/storage/.kodi/temp/kodi.log   Kodi log
journalctl -u babuad -u babua-health -u babua-playlist -u babua-vitals -u kodi   (-b -1: the boot before)
```
