# Verify before deployment

Most of the setup has been tested off-hardware. The image was built and
inspected, every on-box script ran under LibreELEC's own ARM userland in
qemu, the systemd units passed `systemd-analyze verify`, sshd accepted the
options and the firewall rules parsed. The resume/retry logic is tested
against a fake Kodi. What can't be tested without the real TV, provider and
Pi is listed below, roughly in order. ⚠ marks items that could change the
design.

## 1. The subscription ⚠

- [ ] **What is it, actually?** The current app's 150-minute daily limit
      suggests a free tier or a trial rather than a paid subscription. Find
      out whether there's an account with an **M3U / Xtream Codes** login
      (server URL + username + password). Xtream logins work as
      `http://SERVER/get.php?username=U&password=P&type=m3u_plus&output=ts`.
      If the service only works inside its own app (no M3U), this design needs
      a different source for those channels.
- [ ] **Terms:** confirm the provider allows use on another device or player,
      and how many simultaneous connections are allowed. If the limit is one,
      don't watch on the old app while the box plays. The daily stream check
      already skips itself while something is playing.
- [ ] **Geo/IP restrictions:** some Georgian/Russian providers only serve
      certain countries or lock to one IP. Run `tools/babua preview` and
      `tools/babua codecs` from home; if they fail, run them from their
      network (or test `babua playlist` on the box itself).
- [ ] **User-Agent:** some providers only answer their own player. If
      playlists or streams return 403, set `IPTV_USER_AGENT` in
      `site/secrets.env`.
- [ ] **Token rotation:** if stream URLs contain short-lived tokens, the 6-hourly
      refresh may not be enough. Check whether `tools/babua check-streams`
      fails a day after `tools/babua playlist`.

## 2. Streams on a Pi 3 ⚠

The Pi 3 decodes **H.264 up to 1080p in hardware** and nothing else. HEVC/H.265
(often labelled "FHD", "4K" or "HEVC") won't play smoothly. MPEG-2 has no
hardware decoder (the licence is no longer sold); SD MPEG-2 works in
software.

- [ ] `tools/babua codecs` → every channel OK, or CHECK for a known reason.
      For a channel marked BAD, pick an H.264 variant from the provider
      (`tools/babua list-source provider -g NAME` usually shows HD/SD
      variants). If only HEVC exists, that's the one case where routing
      through Jellyfin (transcoding to H.264) is worth it.
- [ ] **Interlaced HD channels** (common for broadcast TV, shown as CHECK) need
      real-life testing. Watch a moving ticker or a sports channel and look
      for combing or judder.
- [ ] **HLS stutter or slow channel switching:** try `IPTV_USE_ADAPTIVE_HLS=1`
      (inputstream.adaptive), then `tools/babua deploy`.
- [ ] **Channel switch time** of a few seconds is normal for IPTV; check it's
      acceptable for her.

## 3. The TV and its remote (HDMI-CEC) ⚠

Samsung calls CEC **Anynet+ (HDMI-CEC)**.

- [ ] Anynet+ is **on** (Settings → General → External Device Manager).
- [ ] **Turning the TV off stops playback** (`tools/babua logs babuad` should show
      `playback stopped; blanking the screen`). If the stream
      keeps running after the TV is off, that only wastes bandwidth; grandma
      won't notice anything.
- [ ] **Turning the TV on resumes the channel by itself.** If the screen stays
      dimmed until she presses a button, the TV isn't announcing itself.
      Usually a single key press is acceptable; explain "press OK if it's
      dark".
- [ ] **Which buttons reach the Pi:** Samsung passes arrows, OK, Return/Exit and
      the play keys. **CH+/CH- and number keys often aren't passed** to
      "playback devices". In that case up/down are her channel buttons. Test
      them all.
- [ ] Kodi's UI language stays Russian. TV menu language following is
      disabled; check it anyway.
- [ ] Make sure the TV powers on to the **HDMI input** where the Pi is, not
      the Smart Hub. If Samsung's "Autorun Smart Hub" or "Autorun last app"
      is on, turn it off.
- [ ] Remove or hide the old IPTV app so the TV doesn't drift back to it.
- [ ] After the TV has been seen once, run `tools/babua edid` **with the TV
      on**. This saves its EDID so the Pi still outputs the right resolution
      when it boots while the TV is off (power cut). Then reboot and check
      the picture.

## 4. Hardware

- [ ] **Power supply: official Raspberry Pi 5.1 V 2.5 A** (or an equivalent
      with a thick cable). Under-voltage is the #1 cause of flaky Pis;
      `tools/babua health` reports it (`power`). Don't power the Pi from the
      TV's USB port.
- [ ] SD card: a "high endurance" type, 8–32 GB. **Flash a second card with
      the same image** and leave it at their house labelled "TV spare". Then
      anyone can recover from a dead card by swapping it.
- [ ] **Ethernet** if at all possible. For Wi-Fi only, set `WIFI_SSID` and
      `WIFI_PSK`; the Pi 3's 2.4 GHz Wi-Fi is marginal for HD streams.
- [ ] Connect the HDMI cable directly to the TV (not through a soundbar)
      unless you've verified CEC works through it.
- [ ] A case with a heatsink; check that `temperature` stays under ~70 °C
      while playing.

## 5. Network, VPN, monitoring

- [ ] The homelab WireGuard server has the `[Peer]` printed by
      `tools/babua init` (docs/HOMELAB.md), and `WG_ENDPOINT` resolves from
      the outside.
- [ ] `WG_ALLOWED_IPS` only covers homelab networks (split tunnel). If it
      included `0.0.0.0/0`, IPTV would be routed through your home
      connection.
- [ ] **Before handing it over:** reboot, then confirm `tools/babua ssh`
      works over the tunnel. Do this on their network, before you leave.
- [ ] `tools/babua ssh babua notify-test` arrives on your phone. Ideally
      `NTFY_URL` is reachable from the internet, not only via the VPN.
- [ ] `HEARTBEAT_URL` is set and the monitor alerts you if pings stop
      (unplug the Pi to test).
- [ ] Router: give the Pi a DHCP reservation (nice to have; nothing depends
      on its LAN IP).

## 6. First-boot acceptance test (at their house)

1. Insert the card, connect Ethernet and HDMI, then power.
2. First boot takes 1–2 min. Kodi starts in Russian on the TV channel list;
   channels appear about 1 min after the network is up.
3. `tools/babua status` → all OK (`playlist` may need one more refresh).
4. Pick a channel. Up/down switches channels, OK shows the list, Back closes
   it.
5. TV off, wait 10 s, TV on → the same channel plays (or after one key press).
6. Pull the Pi's power while it's playing, then plug it back in → it comes back
   playing by itself. You get a "Booted" ntfy.
7. Unplug the Ethernet for 10 min, then reconnect → "Problem: internet" then
   "Recovered", and the channel plays again.
8. `tools/babua screenshot` shows the picture she sees.
