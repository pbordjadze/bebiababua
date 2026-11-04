"""The soak test against a fake live HLS server and a fake Kodi."""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import reset  # first: points babua at a scratch dir

import babua_lib as B  # noqa: E402
import playlist as P  # noqa: E402
import soak as S  # noqa: E402
from test_babuad import FakeKodi, wait_for  # noqa: E402

SEGMENT = b"\x47" * 2000


class LiveHLS(http.server.ThreadingHTTPServer):
    """A live stream: one 0.2 s segment appears every 0.2 s; the last 5 are listed."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.started = time.time()
        self.requests = []
        self.forbid = 0            # answer the next N playlist requests with 403
        self.forbid_after = None   # ... starting at this playlist request
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.server_address[1]}{path}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.requests.append(self.path)
        if self.path.startswith("/live/user/secret/1.m3u8"):
            n = sum(1 for p in srv.requests if p.startswith("/live/"))
            if srv.forbid_after is not None and n >= srv.forbid_after and srv.forbid > 0:
                srv.forbid -= 1
                self.send_response(403)
                self.end_headers()
                return
            head = int((time.time() - srv.started) / 0.2)
            first = max(0, head - 4)
            body = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
            for seq in range(first, head + 1):
                body += ["#EXTINF:0.200000,", srv.url(f"/seg/{seq}.ts?token=secret")]
            data = ("\n".join(body) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-mpegURL")
        elif self.path.startswith("/seg/"):
            data = SEGMENT
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SoakTest(unittest.TestCase):
    def setUp(self):
        reset()
        B.ensure_dirs()
        self.kodi = FakeKodi()
        self.hls = LiveHLS()
        patches = [
            mock.patch.object(B.Kodi.__init__, "__defaults__", ("127.0.0.1", self.kodi.port, 10)),
            mock.patch.object(S, "POLL", 0.05),
            mock.patch.object(S, "PLAYLIST_RETRY", 0.05),
            mock.patch.object(S, "RECONNECT_DELAYS", (0.05, 0.05)),
            mock.patch.object(S, "BETWEEN_CHANNELS", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.hls.shutdown)
        self.addCleanup(self.kodi.close)
        self.tv("standby")
        lines = ["#EXTM3U"]
        for n in (1, 2):
            lines += [f'#EXTINF:-1 tvg-chno="{n}",Channel {n}', self.hls.url("/live/user/secret/1.m3u8")]
        with open(B.PLAYLIST_PATH, "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def tv(self, power):
        B.write_atomic(B.TV_POWER_PATH, json.dumps({"power": power, "at": int(time.time())}))

    def night(self, **kw):
        opts = dict(S.DEFAULTS, channels="1,2", first_min=0.04, each_min=0.03, until="23:59")
        opts.update(kw)
        folder = os.path.join(S.SOAK_DIR, "test")
        with mock.patch.object(S, "parse_until", return_value=time.time() + 3600):
            rc = S.run_night(opts, folder)
        return rc, folder, S.read_events(folder)

    def test_a_quiet_night(self):
        rc, folder, events = self.night()
        self.assertEqual(rc, 0)
        kinds = [e["ev"] for e in events]
        self.assertEqual(kinds[0], "night")
        self.assertEqual(kinds.count("connect"), 2)
        ends = [e for e in events if e["ev"] == "end"]
        self.assertEqual([e["ch"] for e in ends], [1, 2])
        self.assertTrue(all(e["reason"] == "planned time over" and e["bytes"] > 0 for e in ends))
        self.assertEqual(events[-1]["reason"], "plan finished")
        self.assertFalse(os.path.exists(B.SOAK_FLAG))
        with open(os.path.join(folder, "summary.txt")) as fh:
            summary = fh.read()
        self.assertIn("Channel 1", summary)
        self.assertIn("plan finished", summary)
        with open(os.path.join(folder, "events.jsonl")) as fh:
            raw = fh.read()
        for secret in ("http", "secret", "token", "127.0.0.1"):
            self.assertNotIn(secret, raw + summary)

    def test_provider_cut_is_logged_and_reconnected(self):
        self.hls.forbid_after, self.hls.forbid = 4, 3      # reload fails 3x: the player gives up
        rc, folder, events = self.night(channels="1", first_min=0.1)
        drops = [e for e in events if e["ev"] == "drop"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["status"], 403)
        self.assertEqual([e["ev"] for e in events].count("reconnect"), 1)
        self.assertEqual([e["ev"] for e in events].count("connect"), 2)
        summary = S.summarize(events)
        self.assertIn("Drops", summary)
        self.assertIn("403", summary)

    def test_stops_the_moment_the_screen_wakes(self):
        threading.Timer(0.5, self.kodi.emit, ("GUI.OnScreensaverDeactivated", {})).start()
        started = time.time()
        rc, folder, events = self.night(first_min=1, each_min=1)
        self.assertLess(time.time() - started, 3)
        self.assertIn("GUI.OnScreensaverDeactivated", events[-1]["reason"])
        self.assertEqual([e["ch"] for e in events if e["ev"] == "end"], [1])   # the night is over

    def test_stops_when_the_tv_turns_on(self):
        threading.Timer(0.5, self.tv, ("on",)).start()
        started = time.time()
        rc, folder, events = self.night(first_min=1)
        self.assertLess(time.time() - started, 3)
        self.assertIn("the TV is on", events[-1]["reason"])

    def test_does_not_start_while_watched(self):
        self.tv("on")
        self.assertEqual(self.night()[0], 1)
        self.tv("standby")
        self.kodi.playing = 11
        self.assertEqual(self.night()[0], 1)
        self.kodi.playing = None
        os.remove(B.TV_POWER_PATH)                          # babuad silent: can't see the TV
        self.assertEqual(self.night()[0], 1)
        self.assertFalse(os.path.exists(os.path.join(S.SOAK_DIR, "test", "events.jsonl")))

    def test_byte_budget(self):
        with mock.patch.object(S.ChannelRun, "tick", autospec=True,
                               side_effect=lambda self, player, force=False: self.guard.trip("byte budget")):
            rc, folder, events = self.night(first_min=1)
        self.assertIn("byte budget", events[-1]["reason"])


class StopTest(unittest.TestCase):
    def setUp(self):
        reset()
        B.ensure_dirs()

    def test_stop_soak_ends_the_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "soak"])
        self.addCleanup(proc.kill)
        B.write_json(B.SOAK_FLAG, {"pid": proc.pid, "channel": "x"})
        self.assertTrue(B.soak_active())
        self.assertTrue(B.stop_soak("babuad is opening a channel", wait=2))
        self.assertIsNotNone(proc.wait(timeout=2))
        self.assertIsNone(B.soak_active())
        self.assertFalse(B.stop_soak("again"))

    def test_stale_flag_is_ignored(self):
        B.write_json(B.SOAK_FLAG, {"pid": 1, "channel": "x"})            # pid 1 isn't a soak test
        self.assertIsNone(B.soak_active())

    def test_playlist_rebuild_waits_for_the_soak(self):
        with mock.patch.object(B, "soak_active", return_value={"pid": 1}), \
                mock.patch.object(B, "kodi_try", return_value=False), \
                mock.patch.object(P, "fetch_sources", side_effect=AssertionError("fetched")):
            self.assertEqual(P.install(mock.Mock(force=True, config=None, secrets=None)), 0)
            self.assertEqual(P.check_streams(mock.Mock()), 0)

    def test_schedule_waits_while_watched_then_runs_once(self):
        today = time.strftime("%Y-%m-%d")
        with mock.patch.object(S, "run_night", return_value=0) as run, \
                mock.patch.object(S, "nobody_watching", return_value="the TV is on"):
            S.main(["schedule", today])
            S.main(["scheduled"])                     # 01:00: they're up: try again at 02:00
        self.assertEqual(run.call_count, 0)
        self.assertNotIn("done", S.read_schedule()[today])

    def test_schedule_runs_once(self):
        today = time.strftime("%Y-%m-%d")
        with mock.patch.object(S, "run_night", return_value=0) as run, \
                mock.patch.object(S, "nobody_watching", return_value=None):
            S.main(["schedule", today, "--channels", "2,3", "--first-min", "120"])
            S.main(["scheduled"])
            S.main(["scheduled"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0]["channels"], "2,3")
        self.assertEqual(run.call_args[0][0]["first_min"], 120)
        self.assertTrue(S.read_schedule()[today]["done"])


class HLSTest(unittest.TestCase):
    def test_parse(self):
        target, segs, end = S.parse_media_playlist(
            "#EXTM3U\n#EXT-X-TARGETDURATION:15\n#EXT-X-MEDIA-SEQUENCE:21370\n"
            "#EXTINF:14.4,\nhttp://h/a.ts?x\n#EXTINF:14.4,\nhttp://h/b.ts?x\n")
        self.assertEqual((target, end), (15, False))
        self.assertEqual([(s[0], s[1]) for s in segs], [(21370, 14.4), (21371, 14.4)])
        master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nlow.m3u8\n"
                  "#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080\nhigh.m3u8\n")
        self.assertEqual(S.pick_variant(master), ("high.m3u8", 4000000, "1920x1080"))
        self.assertIsNone(S.pick_variant("#EXTM3U\n#EXTINF:1,\na.ts\n"))

    def test_player_model_finds_stalls(self):
        log = mock.Mock()
        p = S.Player(log, 2)
        p.add(10, 0)          # playback starts, 10 s buffered
        p.add(10, 5)          # 15 s ahead
        self.assertEqual(p.buffer(6), 14.0)
        p.add(10, 25)         # ran dry at 20, segment came at 25
        self.assertEqual(p.stalls, [5])
        self.assertEqual(p.buffer(25), 10.0)

    def test_clean(self):
        self.assertEqual(S.clean("<urlopen error http://user:pw@h/x/y.m3u8 refused>"), "<urlopen error <url> refused>")


if __name__ == "__main__":
    unittest.main()
