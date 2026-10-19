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
        if method == "Player.GetActivePlayers":
            return [{"playerid": 1, "type": "video"}] if self.playing else []
        if method == "Player.GetItem":
            return {"item": {"type": "channel", "id": self.playing, "label": "x"}}
        if method == "PVR.GetChannels":
            return {"channels": self.channels}
        if method == "PVR.GetChannelDetails":
            return {"channeldetails": {"label": f"chan{p['channelid']}"}}
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
