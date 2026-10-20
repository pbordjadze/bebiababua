# Setup visit checklist

Whatever is on the Pi now doesn't matter. You boot it from **your own,
freshly flashed SD card**, and the old card comes out untouched. Channels
don't need the visit either; they can be added later from home with
`tools/babua deploy`.

## At home, before you go (~30–45 min)

1. **Get the code:** `git pull` (branch `main`), then run `tools/babua init`.
2. **Fill in `site/secrets.env`:**
   - `TAILSCALE_AUTHKEY`: optional. A one-off, pre-approved key from
     the Tailscale admin console (Settings → Keys) makes the box join your
     tailnet by itself. Without it, you join on site (step 7).
   - `NTFY_URL`, `NTFY_TOKEN`; `HEARTBEAT_URL` if you have one ready
   - `WIFI_SSID` / `WIFI_PSK` **only if there's no Ethernet near the TV**.
     You need their Wi-Fi password for that.
   - Leave `IPTV_M3U_URL` empty. Test channel 99 gives you a picture.
3. **Add a backup SSH key** (e.g. your phone's) to `site/authorized_keys`.
4. **Build:** `tools/babua check`, then `tools/babua image`. This needs Linux
   or Docker and produces `build/out/babua-tv-LibreELEC-12.2.1.img.gz`.
5. **Flash two SD cards** (4 GB or more each) with Raspberry Pi Imager:
   *Choose Device* → Raspberry Pi 3, *Choose OS* → **Use Custom** → pick the
   `.img.gz`, *Choose Storage* → the card. When it asks about OS
   customisation, say **No**. Label the second card "TV spare".
6. **Delete** `build/out/*.img.gz` (it contains your secrets).
7. **Pack:** both cards, the official Pi power supply (5.1 V 2.5 A), a long
   enough Ethernet cable, an HDMI cable, a small bag or envelope for their
   old SD card, and your laptop with the repo and `site/`. Bring a card
   reader if you have one, in case you need to reflash on site.

## At their house (~30–45 min)

1. **Check the board.** It should say *Raspberry Pi 3 Model B* or *B+*. If
   it's a Pi 4 or 5, stop and send me a message: it needs a different base
   image, which is a one-line change.
2. **Swap the card.** Unplug the Pi's power. Push the old microSD card out of
   the slot underneath, put it in the bag, and label it. Don't erase it.
   Insert your card.
3. **Plug in:** Ethernet, then HDMI straight into the TV, then power **last**.
4. **Switch the TV to that HDMI input.** You should see the LibreELEC logo,
   then after 1–2 min Kodi (in Russian) on the TV channel list.
   - If there's no picture: the red LED on the Pi means it has power, and a
     flickering green LED means it's reading the card. Try the other HDMI
     input, or reseat the card.
5. **TV settings:** turn on *Anynet+ (HDMI-CEC)* (Settings → General →
   External Device Manager). Turn off *Autorun Smart Hub* / *Autorun last
   app*. Hide the old IPTV app.
6. **Laptop on their Wi-Fi:** run
   `BABUA_HOST=babua-tv.local tools/babua status`. If `.local` doesn't
   resolve, use the Pi's IP from the router's DHCP list.
7. **The must-have: remote access.** If the key wasn't baked in, run
   `babua tailscale-up tskey-auth-...` on the box (generate the key in the
   Tailscale app or admin console). Then, in the admin console, open the
   machine → **Disable key expiry**; otherwise it drops off after 180 days.
   Test: turn your phone's Wi-Fi off and SSH to `babua-tv` over cellular with
   Tailscale on. Everything else can be fixed from home; this can't.
8. **Test picture:** `tools/babua play 99`. Then test with *her* remote:
   - up/down changes channel, OK opens the list, BACK closes it
   - check whether CH+/− and the number keys do anything (note the result)
   - TV off, wait 10 s, TV on: does it play by itself, or does it need one
     press of OK?
   - If the remote does nothing at all, unplug the TV from the wall for a
     minute (a common Samsung CEC hiccup) and check Anynet+ is on.
9. **Lock in the screen mode:** with the TV on, run `tools/babua edid`,
   then `tools/babua reboot --force`. The picture should come back.
10. **Power-cut test:** pull the Pi's plug while it's playing, then plug it
    back in. It should come back by itself, and you get a "Booted" ntfy.
11. **Power check:** `tools/babua health` should show `power` as OK while
    it's playing.
12. **Fun check:** `tools/babua show some-photo.jpg`. It appears, then the
    channel returns.
13. **Show grandma:** the TV remote as usual; up/down for channels; OK for
    the list; BACK to close it. "If it's dark, press OK."
14. **Leave the spare card** somewhere sensible, labelled.

Test channel 99 is a 10-minute clip, not live TV. When it ends you'll see
a few automatic restarts and then the "channel not working" message. That's
expected.

## Later, from home, once the subscription is known

Put `IPTV_M3U_URL` into `site/secrets.env` → `tools/babua preview` →
`tools/babua codecs` → edit `config/channels.toml` (and remove "Тест") →
`tools/babua deploy`.
