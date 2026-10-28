#!/usr/bin/env python3
"""babuad - keeps Kodi in "it's just a TV" shape (babuad.service).

* converges Kodi on every (re)start: enables the bundled add-ons, enforces
  the settings in babua_lib.kodi_settings()
* TV switched off (CEC) -> Kodi stops playback -> we blank the screen
* TV switched on / any remote key -> screensaver ends -> last channel plays
* TV switched on (seen by polling it over CEC) -> switch it to our input, once:
  someone who then picks another input on purpose stays there
* TV in standby for 5 minutes while a channel plays -> stop and blank, so the
  box doesn't stream all night (the TV doesn't always tell Kodi it went off)
* after giving up on a channel (e.g. their internet is down), keep trying once a
  minute while the TV is on, so the picture comes back by itself; Kodi's
  "playback aborted" boxes from those attempts are closed straight away
* a stream that dies is retried a few times before giving up
* playlist reloads are only done while nothing is playing
* after a clip sent with `babua show`, the channel comes back by itself
"""

import os
import socket
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import babua_lib as B  # noqa: E402

PLAYER_STATE = os.path.join(B.STATE_DIR, "player.json")
RETRY_DELAYS = (3, 10, 30)     # seconds before each retry of a dead stream
STOP_SETTLE = 3                # seconds to wait after a stop before acting
STABLE_AFTER = 60              # a stream that played this long counts as healthy
VERIFY_AFTER = 20              # a retry that shows no picture by then has failed
TV_POLL = 10                   # seconds between CEC power-status polls of the TV
TV_OFF_AFTER = 3               # unanswered polls in a row that count as "TV off"
TV_ON_SETTLE = 5               # after claiming the input, let Kodi's own wake-up resume first
TV_OFF_STOP = 300              # TV in standby this long while playing -> stop (no streaming all night)
OPEN_GRACE = 15                # a channel opened this recently is still starting: don't open another
RECOVER_EVERY = 60             # after giving up, try the channel again this often...
RECOVER_FOR = 12 * 3600        # ...for this long, while the TV is on (it pauses in standby)
DOWN_ALERT_EVERY = 3600        # at most one "Channel down" alert an hour (flapping internet)
CEC_OK_PATH = os.path.join(B.RUN_DIR, "cec_ok")   # touched when the TV answers over CEC; health reads it


class Daemon:
    def __init__(self):
        self.cfg = B.load_config()
        self.kodi = None
        self.current_channel = None
        self.av_started_at = 0.0
        self.retries = 0
        self.pending = None        # (due_monotonic, kind, data)
        self.quiet_until = 0.0
        self.state = B.read_json(PLAYER_STATE)
        self.tv_was_off = None     # None until the first answer: never grab the input at start
        self.tv_silent = 0
        self.standby_since = None  # monotonic time of the first "standby" answer in a row
        self.next_tv_poll = 0.0
        self.last_open = -OPEN_GRACE  # monotonic time of babuad's last channel open
        self.recover_until = 0.0   # > now: given up on a channel, retrying once a minute
        self.message_seen = False  # a Kodi message box was up at the last housekeeping check
        self.tidy_at = 0.0         # > 0: close Kodi's error box from a failed attempt at this time

    # ------------------------------------------------------------------ util
    def save_state(self):
        B.write_json(PLAYER_STATE, self.state)

    def playing(self):
        return self.kodi.is_playing()

    def blank(self):
        if B.cfg_bool(self.cfg, "BLANK_WHEN_STOPPED", True):
            self.kodi.call("GUI.ActivateScreenSaver")

    def play_last(self, reason, retry=False):
        now = time.monotonic()
        if not retry and now - self.last_open < OPEN_GRACE:
            # Kodi is still opening the last one (it isn't "playing" yet);
            # a second open makes it abort one and show an error box
            B.log(f"not opening again ({reason}): a channel is still starting")
            return
        channel = self.current_channel or self.kodi.last_channel_id()
        if channel is None:
            B.log("no channels yet; nothing to play")
            return
        self.last_open = now
        self.av_started_at = 0.0   # no picture from this open yet
        B.log(f"playing channel {channel} ({reason})")
        try:
            self.kodi.play_channel(channel)
        except B.KodiError as exc:
            # e.g. the channel vanished with a playlist change
            B.log("play failed:", exc)
            fallback = self.kodi.last_channel_id()
            if fallback not in (None, channel):
                self.current_channel = fallback
                try:
                    self.kodi.play_channel(fallback)
                except B.KodiError as exc2:
                    B.log("fallback play failed:", exc2)

    def close_messages(self):
        """Close Kodi's OK boxes (e.g. "playlist playback aborted" after a
        failed open): she can't fix anything with them, and they sit on top
        of the picture. Returns how many were closed."""
        closed = 0
        for _ in range(10):        # failed opens can stack several
            active = self.kodi.call("XBMC.GetInfoBooleans", {"booleans": ["Window.IsActive(okdialog)"]}) or {}
            if not active.get("Window.IsActive(okdialog)"):
                break
            text = self.kodi.call("XBMC.GetInfoLabels", {"labels": ["Control.GetLabel(1)"]}) or {}
            B.log("closing a Kodi message:", text.get("Control.GetLabel(1)", "?"))
            self.kodi.call("Input.Select")   # its only button, OK, has focus
            time.sleep(1)
            closed += 1
        return closed

    def stop_playback(self):
        for player in self.kodi.call("Player.GetActivePlayers") or []:
            self.kodi.call("Player.Stop", {"playerid": player["playerid"]})

    # -------------------------------------------------------------- converge
    def converge(self):
        newly_enabled = []
        for addon in B.MANAGED_ADDONS:
            enabled = self.kodi.addon_enabled(addon)
            if enabled is False:
                self.kodi.set_addon_enabled(addon, True)
                newly_enabled.append(addon)
        if newly_enabled:
            B.log("enabled add-ons:", ", ".join(newly_enabled))
            time.sleep(3)

        wanted = B.kodi_settings(self.cfg)
        # enable the web server only after its password is in place
        order = sorted(wanted, key=lambda k: k == "services.webserver")
        for key in order:
            try:
                if self.kodi.setting(key) != wanted[key]:
                    self.kodi.set_setting(key, wanted[key])
                    B.log("set", key)
            except B.KodiError as exc:
                B.log(f"setting {key} failed: {exc}")

    def maybe_reload_playlist(self):
        if not os.path.exists(B.RELOAD_FLAG) or self.playing():
            return
        B.log("reloading IPTV add-on for new playlist")
        try:
            os.remove(B.RELOAD_FLAG)
        except FileNotFoundError:
            pass
        self.kodi.set_addon_enabled(B.PVR_ADDON, False)
        time.sleep(2)
        self.kodi.set_addon_enabled(B.PVR_ADDON, True)

    # ---------------------------------------------------------------- events
    def on_event(self, method, data):
        now = time.monotonic()
        if method in ("Player.OnPlay", "Player.OnAVStart"):
            item = (data or {}).get("item") or {}
            if item.get("type") == "channel" and item.get("id"):
                self.current_channel = item["id"]
            if method == "Player.OnPlay":
                self.av_started_at = 0.0   # a new open: no picture from it yet
            if now < self.quiet_until:
                B.log("quiet boot: stopping start-up playback")
                self.stop_playback()
                return
            if method == "Player.OnAVStart":
                self.av_started_at = now
                self.pending = None
                if self.recover_until:
                    B.log("picture is back")
                    self.recover_until = 0.0
                    self.retries = 0
                # limit SD card writes: persist at most hourly unless clearing a failure
                if "failure" in self.state or time.time() - self.state.get("last_ok", 0) > 3600:
                    self.state["last_ok"] = int(time.time())
                    self.state.pop("failure", None)
                    self.save_state()

        elif method == "Player.OnStop":
            item = (data or {}).get("item") or {}
            if item.get("type", "channel") != "channel":
                # a clip/photo sent with `babua show` ended or was stopped:
                # go back to what the TV was doing before
                after = "channel"
                try:
                    with open(B.AFTER_MEDIA) as fh:
                        after = fh.read().strip() or "channel"
                    os.remove(B.AFTER_MEDIA)
                except FileNotFoundError:
                    pass
                self.pending = (now + STOP_SETTLE, "resume" if after == "channel" else "stopped", None)
                return
            if now < self.quiet_until:
                return             # the quiet boot stopped it on purpose
            if now < self.recover_until:
                self.tidy_at = now + 4   # a failed recovery attempt: close its error box
                return             # "recover" checks back itself
            if item.get("id") and self.current_channel and item["id"] != self.current_channel:
                return             # the old channel stopping after a switch
            ended = bool((data or {}).get("end"))
            # an open that never showed a picture failed (Kodi reports that as a
            # plain stop, e.g. no internet): retry instead of blanking for good
            kind = "died" if ended or not self.av_started_at else "stopped"
            self.pending = (now + STOP_SETTLE, kind, None)

        elif method == "GUI.OnScreensaverDeactivated":
            self.quiet_until = 0.0
            if self.standby_since:
                return             # TV is off: a button press or closed box isn't her watching
            if self.pending and self.pending[1] in ("retry", "recover"):
                return             # babuad is about to open it itself
            if B.cfg_bool(self.cfg, "RESUME_ON_WAKE", True) and not self.playing():
                self.retries = 0
                self.play_last("woken up")

    def on_pending(self, kind):
        if self.playing():
            return
        if kind == "died" and self.standby_since:
            kind = "stopped"       # TV is off: nobody's watching, don't retry
        if kind == "died" and self.current_channel is not None:
            # av_started_at == 0: no picture since the last retry
            if self.av_started_at and time.monotonic() - self.av_started_at > STABLE_AFTER:
                self.retries = 0
            if self.retries < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[self.retries]
                self.retries += 1
                self.av_started_at = 0.0
                B.log(f"stream ended unexpectedly; retry {self.retries} in {delay}s")
                self.close_messages()
                self.pending = (time.monotonic() + delay, "retry", None)
                return
            self.give_up()
            return
        if kind == "tv_on":
            self.retries = 0
            self.play_last("TV turned on")
            return
        if kind == "resume":
            self.retries = 0
            self.play_last("message finished")
            return
        if kind == "recover":
            now = time.monotonic()
            if now >= self.recover_until:
                B.log("stopped retrying after 2 hours")
                self.recover_until = 0.0
                return
            self.pending = (now + RECOVER_EVERY, "recover", None)
            if self.standby_since:
                return             # TV off: try again when it's on (or "TV turned on" will)
            idle = (self.kodi.call("XBMC.GetInfoBooleans", {"booleans": ["System.IdleTime(60)"]}) or {})
            if idle.get("System.IdleTime(60)") is False:
                return             # she's using the remote (picking a channel): don't grab the screen
            self.close_messages()
            self.play_last("trying again after a failure", retry=True)
            return
        if kind == "retry":
            self.play_last(f"retry {self.retries}", retry=True)
            # A failed open may not produce any event; check back. OnPlay
            # clears this, a new OnStop replaces it.
            self.pending = (time.monotonic() + VERIFY_AFTER, "died", None)
            return
        # deliberate stop (TV off via CEC, TV input switched, remote STOP)
        B.log("playback stopped; blanking the screen until the TV wakes it")
        self.maybe_reload_playlist()
        self.blank()

    def watch_tv(self):
        """Switch the TV to the Pi when it goes from off to on; stop playing
        when it has been in standby for a while."""
        power = B.tv_power()
        if power is None:
            self.tv_silent += 1
            if self.tv_silent >= TV_OFF_AFTER:
                self.tv_was_off = True
            return
        self.tv_silent = 0
        try:
            with open(CEC_OK_PATH, "w") as fh:   # /run (RAM): no SD wear
                fh.write(str(int(time.time())))
        except OSError:
            pass
        if power in ("standby", "to-standby"):
            # only an explicit standby answer counts: a lost reply while they
            # watch must never stop the picture
            now = time.monotonic()
            self.standby_since = self.standby_since or now
            if (now - self.standby_since >= TV_OFF_STOP and B.cfg_bool(self.cfg, "STOP_WHEN_TV_OFF", True)
                    and self.playing()):
                B.log("TV in standby for 5 min: stopping playback")
                self.stop_playback()
        else:
            self.standby_since = None
        if power != "on":
            self.tv_was_off = True
            return
        if not B.cfg_bool(self.cfg, "CLAIM_TV_INPUT", True):
            self.tv_was_off = False
            return
        if self.tv_was_off:
            B.log("TV turned on: switching it to our input")
            if not B.claim_tv_input():
                B.log("couldn't switch the input (no HDMI address yet)")
            if B.cfg_bool(self.cfg, "RESUME_ON_WAKE", True) and not self.pending:
                # The switch usually wakes Kodi, which resumes by itself
                # (OnScreensaverDeactivated); opening a channel at the same
                # moment makes Kodi abort one and show an error dialog.
                self.pending = (time.monotonic() + TV_ON_SETTLE, "tv_on", None)
        self.tv_was_off = False

    def give_up(self):
        name = str(self.current_channel)
        try:
            info = self.kodi.call("PVR.GetChannelDetails", {"channelid": self.current_channel, "properties": ["label"]})
            name = info["channeldetails"]["label"]
        except (B.KodiError, KeyError, TypeError):
            pass
        # often their internet, not the channel: keep trying while the TV is on
        self.recover_until = time.monotonic() + RECOVER_FOR
        self.pending = (time.monotonic() + RECOVER_EVERY, "recover", None)
        online = B.internet_ok()
        B.log(f"giving up on channel {name}" + ("" if online else " (no internet)"))
        self.state["failure"] = {"channel": name, "at": int(time.time())}
        self.retries = 0
        self.close_messages()
        if online:
            msg = self.cfg.get("MSG_CHANNEL_DOWN", "Канал сейчас не работает. Выберите другой.")
        else:
            msg = self.cfg.get("MSG_NO_INTERNET", "Нет интернета. Изображение вернётся само.")
        try:
            self.kodi.notify("IPTV", msg, 15000)
            if online:             # this channel's problem: let her pick another
                self.kodi.call("GUI.ActivateWindow", {"window": "tvchannels"})
        except B.KodiError:
            pass
        alert = time.time() - self.state.get("down_alert_at", 0) > DOWN_ALERT_EVERY
        if alert:
            self.state["down_alert_at"] = int(time.time())
        self.save_state()
        if alert:
            what = "" if online else " (no internet on the box)"
            # queued, sent by the next health run: never blocks babuad on a dead network
            B.queue_notify(self.cfg, "Channel down", f"'{name}' failed {len(RETRY_DELAYS) + 1} times in a row{what}.",
                           3, "tv")

    # ------------------------------------------------------------------ main
    def session(self):
        self.kodi = B.Kodi(timeout=15).connect()
        if not self.kodi.ping():
            raise ConnectionError("no pong")
        B.log("connected to kodi")
        self.converge()

        if os.path.exists(B.QUIET_FLAG):
            os.remove(B.QUIET_FLAG)
            B.log("quiet boot after maintenance reboot")
            self.quiet_until = time.monotonic() + 180
            if self.playing():
                self.stop_playback()
            self.blank()

        last_housekeeping = 0.0
        watch_tv = B.cfg_bool(self.cfg, "CLAIM_TV_INPUT", True) or B.cfg_bool(self.cfg, "STOP_WHEN_TV_OFF", True)
        while True:
            timeout = 30.0
            if self.pending:
                timeout = max(0.1, self.pending[0] - time.monotonic())
            if watch_tv:
                timeout = min(timeout, max(0.1, self.next_tv_poll - time.monotonic()))
            if self.tidy_at:
                timeout = min(timeout, max(0.1, self.tidy_at - time.monotonic()))
            try:
                msg = self.kodi.read_notification(timeout)
            except socket.timeout:
                msg = None
            if msg and "method" in msg:
                self.on_event(msg["method"], (msg.get("params") or {}).get("data"))
            if self.pending and time.monotonic() >= self.pending[0]:
                _, kind, _ = self.pending
                self.pending = None
                self.on_pending(kind)
            if self.tidy_at and time.monotonic() >= self.tidy_at:
                self.tidy_at = 0.0
                self.close_messages()
            if watch_tv and time.monotonic() >= self.next_tv_poll:
                self.next_tv_poll = time.monotonic() + TV_POLL
                self.watch_tv()
            if time.monotonic() - last_housekeeping > 60:
                last_housekeeping = time.monotonic()
                self.maybe_reload_playlist()
                # any other Kodi OK box left up for two checks (1-2 min)
                up = (self.kodi.call("XBMC.GetInfoBooleans", {"booleans": ["Window.IsActive(okdialog)"]})
                      or {}).get("Window.IsActive(okdialog)")
                if up and self.message_seen:
                    self.close_messages()
                    up = False
                self.message_seen = bool(up)


def main():
    B.ensure_dirs()
    daemon = Daemon()
    while True:
        try:
            daemon.session()
        except (OSError, B.KodiError, ValueError) as exc:
            # Kodi starting, restarting or crashed; systemd restarts it.
            B.log("kodi session ended:", repr(exc))
            delay = 5
        except Exception:  # noqa: BLE001 - a bug or an odd reply must not kill the daemon
            B.log("babuad error:", traceback.format_exc().strip().replace("\n", " | "))
            delay = 30
        else:
            delay = 5
        finally:
            if daemon.kodi:
                daemon.kodi.close()
        time.sleep(delay)
        daemon.cfg = B.load_config()


if __name__ == "__main__":
    main()
