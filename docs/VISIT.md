# Setup visit checklist

Things that need you physically there: the TV/CEC behaviour, the EDID,
power, network, and proving remote access works. Channels don't; they can
be added later from home with `tools/babua deploy`. The subscription
doesn't need to be sorted before the visit.

## Before you leave home (~30 min)

- [ ] `tools/babua init`, then fill in `site/secrets.env`: `WG_*`, `NTFY_*`,
      `HEARTBEAT_URL`. `IPTV_M3U_URL` can stay empty for now; the
      "Тест" channel (99) in `config/channels.toml` gives you a picture.
- [ ] Add the printed `[Peer]` to the homelab WireGuard server (docs/HOMELAB.md).
- [ ] Add a second SSH key (e.g. your phone) to `site/authorized_keys`.
- [ ] `tools/babua check` → `tools/babua image`.
- [ ] Flash **two** SD cards (one is the spare that stays there).
- [ ] Pack: the Pi + official 2.5 A power supply, case, Ethernet cable,
      HDMI cable, and your laptop with the repo and `site/`.
- [ ] Delete `build/out/*.img.gz` after flashing.

## At their house

1. Plug in the Ethernet, then HDMI (straight into the TV), then power. The
   first boot takes 1–2 min.
2. On the TV, turn on **Anynet+ (HDMI-CEC)** and turn off "Autorun Smart
   Hub" / "Autorun last app". Hide the old IPTV app.
3. From your laptop on their Wi-Fi: `BABUA_HOST=babua-tv.local tools/babua status`.
   If `.local` doesn't resolve, use the IP from their router's DHCP list.
4. **VPN:** `tools/babua status` (without `BABUA_HOST`) works over the tunnel.
   Make sure this works before you leave.
5. `tools/babua play 99` → test picture. Then go through VERIFY §3 with her
   remote:
   - up/down, OK, BACK; check whether CH+/− and the number keys do anything
   - TV off → wait 10 s → TV on → does it resume without a key press?
6. `tools/babua edid` with the TV on, then `tools/babua reboot --force` →
   the picture comes back.
7. Pull the power while it's playing → it boots and comes back by itself;
   you get a "Booted" ntfy.
8. `tools/babua health` → check `power` is OK while it plays (no
   under-voltage).
9. `tools/babua show some-photo.jpg` → it appears, then returns to the channel.
10. Show grandma: the TV remote as usual, up/down, OK for the list, BACK to
    close it. "If it's dark, press OK."
11. Put the labelled spare SD card somewhere sensible.

## Later, from home, once the subscription is known

`IPTV_M3U_URL` into `site/secrets.env` → `tools/babua preview` →
`tools/babua codecs` → edit `config/channels.toml` (and remove "Тест") →
`tools/babua deploy`.
