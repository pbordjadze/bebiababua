#!/usr/bin/env python3
"""babuad - keeps Kodi in "it's just a TV" shape (babuad.service).

* converges Kodi on every (re)start: enables the bundled add-ons, enforces
  the settings in babua_lib.kodi_settings()
* TV switched off (CEC) -> Kodi stops playback -> we blank the screen
* TV switched on / any remote key -> screensaver ends -> last channel plays
* a stream that dies is retried a few times before giving up
* playlist reloads are only done while nothing is playing
* after a clip sent with `babua show`, the channel comes back by itself
"""

import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import babua_lib as B  # noqa: E402

PLAYER_STATE = os.path.join(B.STATE_DIR, "player.json")
RETRY_DELAYS = (3, 10, 30)     # seconds before each retry of a dead stream
STOP_SETTLE = 3                # seconds to wait after a stop before acting
STABLE_AFTER = 60              # a stream that played this long counts as healthy
VERIFY_AFTER = 20              # a retry that shows no picture by then has failed


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

    # ------------------------------------------------------------------ util
    def save_state(self):
        B.write_json(PLAYER_STATE, self.state)

    def playing(self):
        return self.kodi.is_playing()

    def blank(self):
        if B.cfg_bool(self.cfg, "BLANK_WHEN_STOPPED", True):
            self.kodi.call("GUI.ActivateScreenSaver")

    def play_last(self, reason):
        channel = self.current_channel or self.kodi.last_channel_id()
        if channel is None:
            B.log("no channels yet; nothing to play")
            return
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
            if now < self.quiet_until:
                B.log("quiet boot: stopping start-up playback")
                self.stop_playback()
                return
            if method == "Player.OnAVStart":
                self.av_started_at = now
                self.pending = None
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
            ended = bool((data or {}).get("end"))
            kind = "died" if ended else "stopped"
            self.pending = (now + STOP_SETTLE, kind, None)

        elif method == "GUI.OnScreensaverDeactivated":
            self.quiet_until = 0.0
            if B.cfg_bool(self.cfg, "RESUME_ON_WAKE", True) and not self.playing():
                self.retries = 0
                self.play_last("woken up")

    def on_pending(self, kind):
        if self.playing():
            return
        if kind == "died" and self.current_channel is not None:
            # av_started_at == 0: no picture since the last retry
            if self.av_started_at and time.monotonic() - self.av_started_at > STABLE_AFTER:
                self.retries = 0
            if self.retries < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[self.retries]
                self.retries += 1
                self.av_started_at = 0.0
                B.log(f"stream ended unexpectedly; retry {self.retries} in {delay}s")
                self.pending = (time.monotonic() + delay, "retry", None)
                return
            self.give_up()
            return
        if kind == "resume":
            self.retries = 0
            self.play_last("message finished")
            return
        if kind == "retry":
            self.play_last(f"retry {self.retries}")
            # A failed open may not produce any event; check back. OnPlay
            # clears this, a new OnStop replaces it.
            self.pending = (time.monotonic() + VERIFY_AFTER, "died", None)
            return
        # deliberate stop (TV off via CEC, TV input switched, remote STOP)
        B.log("playback stopped; blanking the screen until the TV wakes it")
        self.maybe_reload_playlist()
        self.blank()

    def give_up(self):
        name = str(self.current_channel)
        try:
            info = self.kodi.call("PVR.GetChannelDetails", {"channelid": self.current_channel, "properties": ["label"]})
            name = info["channeldetails"]["label"]
        except (B.KodiError, KeyError, TypeError):
            pass
        B.log(f"giving up on channel {name}")
        self.state["failure"] = {"channel": name, "at": int(time.time())}
        self.save_state()
        self.retries = 0
        msg = self.cfg.get("MSG_CHANNEL_DOWN", "Канал сейчас не работает. Выберите другой.")
        try:
            self.kodi.notify("IPTV", msg, 10000)
            self.kodi.call("GUI.ActivateWindow", {"window": "tvchannels"})
        except B.KodiError:
            pass
        B.notify(self.cfg, "Channel down", f"'{name}' failed {len(RETRY_DELAYS) + 1} times in a row.", 3, "tv")

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
        while True:
            timeout = 30.0
            if self.pending:
                timeout = max(0.1, self.pending[0] - time.monotonic())
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
            if time.monotonic() - last_housekeeping > 60:
                last_housekeeping = time.monotonic()
                self.maybe_reload_playlist()


def main():
    B.ensure_dirs()
    daemon = Daemon()
    while True:
        try:
            daemon.session()
        except (OSError, B.KodiError, ValueError) as exc:
            # Kodi starting, restarting or crashed; systemd restarts it.
            B.log("kodi session ended:", repr(exc))
        finally:
            if daemon.kodi:
                daemon.kodi.close()
        time.sleep(5)
        daemon.cfg = B.load_config()


if __name__ == "__main__":
    main()
