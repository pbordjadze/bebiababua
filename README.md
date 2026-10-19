# bebiababua — a TV box for my grandparents

A Raspberry Pi 3 behind their Samsung TV that plays their Georgian and Russian
IPTV channels with no time limits. To them it's just the TV. To me it's a
LibreELEC appliance I can manage completely from home over WireGuard.

**What grandma does:** turns on the TV with her usual Samsung remote. The last
channel she watched starts playing. Up/down changes channel. OK shows the
channel list. Turning the TV off stops the stream. If something goes wrong, any
button brings the picture back.

**What I get:** SSH over WireGuard, ntfy alerts when something breaks (and
again when it recovers), a heartbeat so I hear about it if the box goes
silent, remote screenshots and remote button presses, config-as-code deployed
with one command, and a flashable image I can rebuild at any time.

```
 Samsung remote ──CEC──► Pi 3 (LibreELEC 12 / Kodi 21) ──HDMI──► TV
                          │  IPTV Simple ◄── local playlist ◄── babua playlist.py ◄── provider M3U
                          │  babuad      (TV off → stop/blank, TV on/any key → resume, retries)
                          │  health      (5 min: net, VPN, Kodi, power, playlist → ntfy + heartbeat)
                          └─ WireGuard (outbound, split tunnel) ──► homelab: SSH, ntfy, uptime monitor
```

## Why this design

| Option | Verdict |
|---|---|
| **LibreELEC + Kodi + IPTV Simple** (chosen) | Kodi's best-supported Pi build, with H.264 hardware decoding and HDMI-CEC built in. The system is a read-only squashfs that updates atomically, so power cuts can't corrupt the OS. It ships Python, WireGuard (`connman`), `curl`, `vcgencmd` and systemd user units, so nothing extra needs installing. |
| Raspberry Pi OS + Kodi | More flexible, but the SD card is writable everywhere (power-cut risk), there's more to patch, and Kodi packaging for the Pi 3 is second-class. |
| Another app on the Samsung (Tizen) | Can't be remote-administered. That's the same trap as the current app. |
| Through Jellyfin | Only worth it if the provider sends HEVC-only streams (the Pi 3 can't decode HEVC). Then Jellyfin would transcode to H.264 at the cost of homelab upload bandwidth. `tools/babua codecs` tells you whether that's needed. Direct playback is the default. |
| Tailscale instead of WireGuard | The LibreELEC 12 add-on repo has no Tailscale add-on. WireGuard is built in and dials out, so grandma's router needs no changes. |

Kodi has no Georgian UI translation, so the UI is Russian (Russian is fully
translated). Channel names can be Georgian. Kodi's default font has no
Georgian glyphs, so the box switches to the bundled "Arial" font set, which
covers Georgian, Cyrillic and Latin.

## Quick start

Needs Python ≥ 3.11 and `ssh-keygen` on your machine, plus Docker if your
machine isn't Linux.

```sh
tools/babua init                 # creates site/ (git-ignored): WG keys, SSH host key, Kodi password
$EDITOR site/secrets.env         # provider URL, VPN endpoint, ntfy, heartbeat
$EDITOR site/authorized_keys     # your SSH public key(s); add a backup key too
# add the printed [Peer] to your WireGuard server (docs/HOMELAB.md)

tools/babua list-source provider -g rustavi   # find the provider's exact channel names
$EDITOR config/channels.toml                  # grandma's channels, in her order
tools/babua preview                           # what matches, what doesn't
tools/babua codecs                            # can a Pi 3 decode each stream?
tools/babua image                             # -> build/out/babua-tv-LibreELEC-12.2.1.img.gz
```

Flash the image with Raspberry Pi Imager ("Use custom", **no** OS
customisation) or balenaEtcher. You need an SD card of 4 GB or more; a
"high endurance" card is preferable. Delete the image afterwards, because it
contains your secrets. Then work through [docs/VERIFY.md](docs/VERIFY.md)
before leaving the box with them.

## Day-to-day

```sh
tools/babua status            # what's playing, health, playlist, versions
tools/babua screenshot        # see what grandma sees
tools/babua key up | select | back
tools/babua play 3            # switch her to channel 3
tools/babua say "Привет, бабушка!"
$EDITOR config/channels.toml && tools/babua deploy    # change channels (or any config)
tools/babua logs [-f]         # babua units + Kodi journal
tools/babua update 12.2.2     # stage a LibreELEC update; installs on next (weekly) reboot
```

More situations and their fixes are in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## How it works

- `tools/build_image.py` takes the **official** LibreELEC RPi2/3 image (pinned
  and sha256-checked in `tools/versions.json`). It leaves the boot partition
  byte-identical and writes a pre-filled 3 GiB STORAGE partition containing the
  IPTV add-ons, our scripts, systemd units, keys and settings. There is no
  first-run wizard; the box comes up ready. LibreELEC's own first-boot resize
  is deliberately skipped (it would wipe the pre-filled partition).
- `tools/babua deploy` renders the same tree and unpacks it over SSH, so the
  image and a live box never drift apart.
- On the box (`/storage/.config/babua/bin`, Python stdlib only):
  - `prekodi.py` runs before Kodi on every boot. It re-applies Kodi settings,
    CEC behaviour and IPTV Simple's settings. Anything changed through the UI
    is put back.
  - `babuad.py` talks to Kodi's local JSON-RPC. It enables the bundled add-ons
    and enforces settings. When the TV is turned off it blanks the screen;
    when the TV comes on or any key is pressed it resumes the last channel. It
    retries a dying stream 3 times, then shows a message and sends an alert.
    It reloads the playlist only while nothing is playing.
  - `playlist.py` turns the provider's huge M3U into grandma's short, numbered,
    renamed list. It runs at boot and then every 6 h. If the provider is down
    it keeps the last good copy; if a channel disappears upstream, that channel
    keeps its old URL. There's also a daily stream probe.
  - `health.py` runs every 5 minutes. It checks the internet, the WireGuard
    handshake (reconnecting if needed), Kodi (restarted if hung for 15 min),
    under-voltage, temperature, disk, memory and the playlist. It sends ntfy
    alerts only on problem/recovery transitions and queues them while offline.
    It also pings the heartbeat URL.
  - There's a weekly reboot (Monday 04:30). It's skipped while something is
    playing, and it never starts a channel or wakes the TV afterwards.
- Remote keymap: Back can never leave the TV screens; Stop and Pause are
  ignored, because a frozen picture looks "broken" to grandma.

## Security

- `site/` holds every secret (provider credentials, WG keys, Kodi password)
  and is git-ignored. On the box, secrets are `0600`.
- SSH is key-only (password and keyboard-interactive auth are disabled). The
  host key is generated by `init` and pinned by `tools/babua`, so there's no
  trust-on-first-use.
- The firewall drops everything inbound except the VPN and private LAN
  ranges. Samba, Bluetooth and Avahi are off. Nothing is exposed on grandma's
  router.
- Credentials are redacted from logs, notifications and `preview` output.

## Layout

```
config/          committed behaviour: babua.conf, channels.toml, logos/
site.example/    template for site/ (secrets, keys) - copy via `babua init`
device/          files that land in /storage on the box
tools/           babua (admin CLI), build_image.py, stage.py, versions.json
tests/           unit + fake-Kodi tests (python3 -m unittest discover -s tests)
docs/            VERIFY.md (before deployment), RUNBOOK.md, HOMELAB.md
```
