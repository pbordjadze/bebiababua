"""babuad against a fake Kodi JSON-RPC server (TCP, like port 9090)."""

import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

from helpers import reset  # first: points babua at a scratch dir

import babua_lib as B  # noqa: E402
import babuad  # noqa: E402

from importlib.machinery import SourceFileLoader  # noqa: E402
CLI = SourceFileLoader("babua_cli", os.path.join(os.path.dirname(B.__file__), "babua")).load_module()


class FakeKodi:
    def __init__(self):
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.clients = []
        self.calls = []
        self.lock = threading.Lock()
        self.playing = None
        self.broken = set()          # channel ids whose streams die at once
        self.addons = {a: False for a in B.MANAGED_ADDONS}
        self.settings = {}
        self.channels = [
            {"channelid": 11, "label": "One", "channelnumber": 1, "lastplayed": ""},
            {"channelid": 12, "label": "Two", "channelnumber": 2, "lastplayed": "2026-10-18 20:00:00"},
        ]
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.clients.append(conn)
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def send(self, conn, obj):
        conn.sendall(json.dumps(obj).encode())

    def emit(self, method, data=None):
        for c in list(self.clients):
            try:
                self.send(c, {"jsonrpc": "2.0", "method": method, "params": {"sender": "xbmc", "data": data}})
            except OSError:
                pass

    def serve(self, conn):
        stream = B.JsonStream(conn)
        while True:
            try:
                req = stream.read(60)
            except (OSError, ConnectionError):
                return
            with self.lock:
                self.calls.append((req["method"], req.get("params")))
            result = self.handle(req["method"], req.get("params") or {})
            self.send(conn, {"jsonrpc": "2.0", "id": req["id"], "result": result})

    def handle(self, method, p):
        if method == "JSONRPC.Ping":
            return "pong"
        if method == "XBMC.GetInfoBooleans":
            return {"System.ScreenSaverActive": self.count("GUI.ActivateScreenSaver") > 0 and not self.playing}
        if method == "Player.GetActivePlayers":
            return [{"playerid": 1, "type": "video"}] if self.playing else []
        if method == "Player.GetItem":
            return {"item": {"type": "channel", "id": self.playing, "label": "x"}}
        if method == "PVR.GetChannels":
            return {"channels": self.channels}
        if method == "PVR.GetChannelDetails":
            return {"channeldetails": {"label": f"chan{p['channelid']}"}}
        if method == "Player.Open" and "file" in p["item"]:
            if self.playing:
                self.emit("Player.OnStop", {"item": {"type": "channel", "id": self.playing}, "end": False})
            self.playing = "clip"
            self.emit("Player.OnPlay", {"item": {"type": "unknown", "file": p["item"]["file"]}})
            self.emit("Player.OnAVStart", {"item": {"type": "unknown"}})
            return "OK"
        if method == "Player.Open":
            cid = p["item"]["channelid"]
            item = {"type": "channel", "id": cid}
            self.emit("Player.OnPlay", {"item": item})
            if cid in self.broken:
                threading.Timer(0.05, self.emit, ("Player.OnStop", {"item": item, "end": True})).start()
            else:
                self.playing = cid
                self.emit("Player.OnAVStart", {"item": item})
            return "OK"
        if method == "Player.Stop":
            item = {"type": "channel", "id": self.playing}
            self.playing = None
            self.emit("Player.OnStop", {"item": item, "end": False})
            return "OK"
        if method == "Addons.GetAddonDetails":
            return {"addon": {"enabled": self.addons[p["addonid"]]}}
        if method == "Addons.SetAddonEnabled":
            self.addons[p["addonid"]] = p["enabled"]
            return "OK"
        if method == "Settings.GetSettingValue":
            return {"value": self.settings.get(p["setting"])}
        if method == "Settings.SetSettingValue":
            self.settings[p["setting"]] = p["value"]
            return True
        return "OK"

    def count(self, method):
        with self.lock:
            return sum(1 for m, _ in self.calls if m == method)

    def opened(self):
        with self.lock:
            return [p["item"]["channelid"] for m, p in self.calls if m == "Player.Open"]

    def close(self):
        for c in self.clients:
            c.close()
        self.srv.close()


def wait_for(cond, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        threading.Event().wait(0.02)
    return False


class BabuadTest(unittest.TestCase):
    def setUp(self):
        reset()
        self.kodi = FakeKodi()
        patches = [
            mock.patch.object(B.Kodi.__init__, "__defaults__", ("127.0.0.1", self.kodi.port, 10)),
            mock.patch.object(babuad, "STOP_SETTLE", 0.1),
            mock.patch.object(babuad, "RETRY_DELAYS", (0.05, 0.05, 0.05)),
            mock.patch.object(babuad, "VERIFY_AFTER", 0.5),
            mock.patch.object(babuad.time, "sleep", lambda s: None),
            mock.patch.object(B, "notify"),
            mock.patch.object(B, "tv_power", return_value=None),
            mock.patch.object(B, "claim_tv_input", return_value=True),
            mock.patch.object(babuad, "OPEN_GRACE", 0.2),
            mock.patch.object(babuad, "RECOVER_EVERY", 0.3),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.notify = B.notify
        B.ensure_dirs()

    def start(self):
        self.daemon = babuad.Daemon()
        t = threading.Thread(target=self._run, daemon=True)
        t.start()
        self.assertTrue(wait_for(lambda: all(self.kodi.addons.values())), "converge did not enable add-ons")

    def _run(self):
        try:
            self.daemon.session()
        except (OSError, ConnectionError, B.KodiError):
            pass

    def tearDown(self):
        self.kodi.close()

    def test_converge(self):
        self.start()
        self.assertTrue(wait_for(lambda: self.kodi.settings.get("lookandfeel.font") == "Arial"))
        self.assertEqual(self.kodi.settings["locale.language"], "resource.language.ru_ru")

    def test_tv_off_blanks_and_wake_resumes_last_channel(self):
        self.start()
        self.kodi.handle("Player.Open", {"item": {"channelid": 12}})   # she is watching 12
        self.kodi.handle("Player.Stop", {})                           # TV off -> CEC stop
        self.assertTrue(wait_for(lambda: self.kodi.count("GUI.ActivateScreenSaver") == 1))
        self.kodi.emit("GUI.OnScreensaverDeactivated", {})            # TV on / key press
        self.assertTrue(wait_for(lambda: self.kodi.playing == 12))

    def test_channel_change_does_not_blank(self):
        self.start()
        self.kodi.handle("Player.Open", {"item": {"channelid": 11}})
        self.kodi.handle("Player.Stop", {})
        self.kodi.handle("Player.Open", {"item": {"channelid": 12}})  # new channel picked at once
        threading.Event().wait(0.4)
        self.assertEqual(self.kodi.count("GUI.ActivateScreenSaver"), 0)

    def test_dead_stream_retried_then_given_up(self):
        self.start()
        self.kodi.broken.add(11)
        self.kodi.emit("Player.OnPlay", {"item": {"type": "channel", "id": 11}})
        self.kodi.emit("Player.OnStop", {"item": {"type": "channel", "id": 11}, "end": True})
        self.assertTrue(wait_for(lambda: self.notify.called, timeout=8), "never gave up")
        self.assertEqual(self.kodi.opened(), [11, 11, 11])
        self.assertIn("chan11", self.notify.call_args[0][2])
        self.assertEqual(B.read_json(os.path.join(B.STATE_DIR, "player.json"))["failure"]["channel"], "chan11")
        # a later successful play clears the failure for the health check
        self.kodi.broken.clear()
        self.kodi.handle("Player.Open", {"item": {"channelid": 12}})
        self.assertTrue(wait_for(lambda: "failure" not in B.read_json(os.path.join(B.STATE_DIR, "player.json"))))

    def test_tv_power_on_claims_input_once(self):
        d = babuad.Daemon()
        d.kodi = B.Kodi().connect()
        self.addCleanup(d.kodi.close)
        states = iter(["on", "standby", "to-on", "on", "on"])
        with mock.patch.object(B, "tv_power", side_effect=lambda: next(states)):
            d.watch_tv()                                   # already on at start: leave it
            B.claim_tv_input.assert_not_called()
            d.watch_tv()                                   # standby
            d.watch_tv()                                   # waking up
            d.watch_tv()                                   # on -> claim, then play if Kodi didn't
            B.claim_tv_input.assert_called_once()
            self.assertEqual(d.pending[1], "tv_on")
            d.on_pending("tv_on")
            self.assertTrue(wait_for(lambda: self.kodi.playing == 12))
            d.watch_tv()                                   # still on: no second claim
            B.claim_tv_input.assert_called_once()

    def test_tv_on_after_kodi_resumed_by_itself_opens_once(self):
        self.start()
        self.daemon.tv_was_off = True
        with mock.patch.object(B, "tv_power", return_value="on"), \
                mock.patch.object(babuad, "TV_ON_SETTLE", 0.3):
            self.daemon.next_tv_poll = 0.0               # poll now
            self.kodi.emit("GUI.OnScreensaverDeactivated", {})   # the switch woke Kodi
            self.assertTrue(wait_for(lambda: B.claim_tv_input.called))
            threading.Event().wait(0.8)                  # past TV_ON_SETTLE
        self.assertEqual(self.kodi.opened(), [12])       # one open, not two

    def test_tv_standby_stops_playback_after_a_while(self):
        d = babuad.Daemon()
        d.kodi = B.Kodi().connect()
        self.addCleanup(d.kodi.close)
        self.kodi.handle("Player.Open", {"item": {"channelid": 12}})
        with mock.patch.object(B, "tv_power", return_value="standby"), \
                mock.patch.object(babuad, "TV_OFF_STOP", 0.3):
            d.watch_tv()                                   # standby starts: keep playing for now
            self.assertEqual(self.kodi.playing, 12)
            threading.Event().wait(0.4)
            d.watch_tv()                                   # still standby after TV_OFF_STOP
            self.assertTrue(wait_for(lambda: self.kodi.playing is None))
        with mock.patch.object(B, "tv_power", side_effect=["standby", None, None, None, "on"]):
            d.standby_since = None
            for _ in range(5):
                d.watch_tv()                               # lost replies never stop anything
        self.assertIsNone(d.standby_since)

    def test_lost_replies_never_stop_playback(self):
        d = babuad.Daemon()
        d.kodi = B.Kodi().connect()
        self.addCleanup(d.kodi.close)
        self.kodi.handle("Player.Open", {"item": {"channelid": 12}})
        with mock.patch.object(B, "tv_power", return_value=None), \
                mock.patch.object(babuad, "TV_OFF_STOP", 0):
            for _ in range(10):
                d.watch_tv()
        self.assertEqual(self.kodi.playing, 12)

    def test_tv_silent_in_standby_then_on_claims_input(self):
        d = babuad.Daemon()
        d.kodi = B.Kodi().connect()
        self.addCleanup(d.kodi.close)
        states = iter(["on", None, None, "on", None, None, None, "on"])
        with mock.patch.object(B, "tv_power", side_effect=lambda: next(states)):
            for _ in range(4):
                d.watch_tv()                               # one lost reply is not "off"
            B.claim_tv_input.assert_not_called()
            for _ in range(4):
                d.watch_tv()                               # 3 unanswered polls = off
            B.claim_tv_input.assert_called_once()

    def test_second_open_while_one_is_starting_is_skipped(self):
        d = babuad.Daemon()
        d.kodi = B.Kodi().connect()
        self.addCleanup(d.kodi.close)
        with mock.patch.object(babuad, "OPEN_GRACE", 15), mock.patch.object(d, "playing", return_value=False):
            d.play_last("TV turned on")
            d.play_last("woken up")                        # same second: Kodi still opening
            self.assertEqual(self.kodi.opened(), [12])
            d.play_last("retry 1", retry=True)             # babuad's own retries aren't held back
            self.assertEqual(self.kodi.opened(), [12, 12])

    def test_failed_retry_keeps_retrying_instead_of_blanking(self):
        self.start()
        self.kodi.handle("Player.Open", {"item": {"channelid": 11}})
        self.assertTrue(wait_for(lambda: self.daemon.av_started_at > 0))
        self.daemon.retries = 1                            # a retry is under way...
        self.daemon.av_started_at = 0.0                    # ...and showed no picture
        self.kodi.playing = None
        self.kodi.emit("Player.OnStop", {"item": {"type": "channel", "id": 11}, "end": False})
        self.assertTrue(wait_for(lambda: self.kodi.opened().count(11) >= 1), "no further retry")
        self.assertEqual(self.kodi.count("GUI.ActivateScreenSaver"), 0)

    def test_outage_recovery_after_giving_up(self):
        self.start()
        self.kodi.broken.add(11)                           # their internet is down
        self.kodi.emit("Player.OnPlay", {"item": {"type": "channel", "id": 11}})
        self.kodi.emit("Player.OnStop", {"item": {"type": "channel", "id": 11}, "end": True})
        self.assertTrue(wait_for(lambda: self.notify.called, timeout=8), "never gave up")
        self.assertTrue(self.daemon.recover_until > 0)
        self.kodi.broken.clear()                           # internet back
        self.assertTrue(wait_for(lambda: self.kodi.playing == 11, timeout=5), "picture never came back")
        self.assertTrue(wait_for(lambda: self.daemon.recover_until == 0))
        self.assertEqual(self.notify.call_count, 1)         # one alert, not one per attempt

    def test_wake_while_tv_in_standby_does_not_play(self):
        self.start()
        self.daemon.standby_since = 1.0
        self.kodi.emit("GUI.OnScreensaverDeactivated", {})
        threading.Event().wait(0.5)
        self.assertEqual(self.kodi.opened(), [])

    def test_wake_with_nothing_played_picks_most_recent(self):
        self.start()
        self.kodi.emit("GUI.OnScreensaverDeactivated", {})
        self.assertTrue(wait_for(lambda: self.kodi.playing == 12))   # lastplayed wins over number

    def test_quiet_boot_after_maintenance_reboot(self):
        open(B.QUIET_FLAG, "w").close()
        self.kodi.playing = 12                                         # Kodi's start-up action
        self.start()
        self.assertTrue(wait_for(lambda: self.kodi.playing is None))
        self.assertFalse(os.path.exists(B.QUIET_FLAG))
        self.kodi.emit("GUI.OnScreensaverDeactivated", {})            # morning: TV on
        self.assertTrue(wait_for(lambda: self.kodi.playing == 12))

    def end_clip(self):
        self.kodi.playing = None
        self.kodi.emit("Player.OnStop", {"item": {"type": "unknown"}, "end": True})

    def test_message_clip_then_channel_comes_back(self):
        self.start()
        self.kodi.handle("Player.Open", {"item": {"channelid": 11}})
        B.write_atomic(B.AFTER_MEDIA, "channel")                       # what `babua show` writes
        self.kodi.handle("Player.Open", {"item": {"file": "/storage/babua-media/show.mp4"}})
        threading.Event().wait(0.3)
        self.assertEqual(self.kodi.playing, "clip")                    # not interrupted
        self.end_clip()
        self.assertTrue(wait_for(lambda: self.kodi.playing == 11))
        self.assertEqual(self.kodi.count("GUI.ActivateScreenSaver"), 0)
        self.assertFalse(self.notify.called)                           # not a "dead stream"

    def test_message_clip_while_tv_off_stays_dark(self):
        self.start()
        B.write_atomic(B.AFTER_MEDIA, "blank")
        self.kodi.handle("Player.Open", {"item": {"file": "/storage/babua-media/show.mp4"}})
        self.end_clip()
        self.assertTrue(wait_for(lambda: self.kodi.count("GUI.ActivateScreenSaver") == 1))
        self.assertIsNone(self.kodi.playing)

    def test_show_cli(self):
        self.start()
        self.kodi.handle("Player.Open", {"item": {"channelid": 12}})
        self.assertEqual(CLI.cmd_show(["/storage/babua-media/show.mp4"]), 0)
        self.assertEqual(self.kodi.playing, "clip")
        self.end_clip()
        self.assertTrue(wait_for(lambda: self.kodi.playing == 12))
        # TV off (blanked, nothing playing): refuses unless forced
        self.kodi.handle("Player.Stop", {})
        self.assertTrue(wait_for(lambda: self.kodi.count("GUI.ActivateScreenSaver") == 1))
        self.assertEqual(CLI.cmd_show(["x.mp4"]), 3)
        self.assertEqual(CLI.cmd_show(["x.mp4", "--force"]), 0)

    def test_playlist_reload_waits_until_idle(self):
        self.start()
        self.kodi.handle("Player.Open", {"item": {"channelid": 12}})
        B.write_atomic(B.RELOAD_FLAG, "x")
        self.daemon_reload_calls = lambda: [p for m, p in self.kodi.calls
                                            if m == "Addons.SetAddonEnabled" and p["addonid"] == B.PVR_ADDON]
        n = len(self.daemon_reload_calls())
        self.kodi.handle("Player.Stop", {})
        self.assertTrue(wait_for(lambda: len(self.daemon_reload_calls()) == n + 2))
        self.assertFalse(os.path.exists(B.RELOAD_FLAG))


if __name__ == "__main__":
    unittest.main()
