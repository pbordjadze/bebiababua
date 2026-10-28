import http.server
import os
import tempfile
import threading
import unittest
from unittest import mock

from helpers import HERE, read, reset  # first: points babua at a scratch dir

import babua_lib as B  # noqa: E402
import playlist as P  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "provider.m3u")

CHANNELS = os.path.join(HERE, "fixtures", "channels.toml")


class ParseTest(unittest.TestCase):
    def test_parse(self):
        header, entries = P.parse_m3u(read(FIXTURE))
        self.assertEqual(header["url-tvg"], "http://epg.example/guide.xml.gz")
        self.assertEqual(len(entries), 5)
        r2 = entries[0]
        self.assertEqual(r2.name, "Rustavi 2 HD")
        self.assertEqual(r2.attrs["group-title"], "Georgia, HD")  # comma inside quotes
        self.assertEqual(r2.extra, ["#EXTVLCOPT:http-user-agent=SpecialPlayer/1.0"])
        self.assertEqual(entries[3].attrs["group-title"], "Russia")  # from #EXTGRP


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.sources, self.channels = P.load_channels(CHANNELS)
        self.parsed = {"provider": P.parse_m3u(read(FIXTURE))}
        self.cfg = {"DIRECT_URL": "https://direct.example/stream.m3u8"}

    def test_matching_and_numbering(self):
        text, resolved, missing = P.build(self.sources, self.channels, self.parsed, self.cfg)
        self.assertEqual(missing, ["Missing One"])
        self.assertEqual(list(resolved), ["რუსთავი 2", "იმედი", "Первый канал", "НТВ", "Культура", "Direct"])
        r2 = resolved["რუსთავი 2"]
        self.assertTrue(r2.url.endswith("/101.m3u8"))
        self.assertEqual(r2.attrs["tvg-chno"], "1")
        self.assertEqual(r2.attrs["tvg-logo"], "rustavi2.png")      # config overrides provider
        self.assertEqual(r2.attrs["group-title"], "ქართული")
        self.assertIn("#EXTVLCOPT:http-user-agent=SpecialPlayer/1.0", r2.extra)
        self.assertEqual(resolved["იმედი"].attrs["tvg-logo"], "http://logo/imedi.png")
        self.assertIn("#KODIPROP:inputstream=inputstream.adaptive", resolved["Первый канал"].extra)
        self.assertEqual(resolved["Direct"].attrs["tvg-chno"], "20")
        self.assertTrue(text.startswith('#EXTM3U x-tvg-url="http://epg.example/guide.xml.gz"'))
        # round-trips through our own parser
        _, back = P.parse_m3u(text)
        self.assertEqual([e.name for e in back], list(resolved))

    def test_missing_channel_keeps_previous_url(self):
        prev = {"Missing One": P.Entry("Missing One", "http://old.example/x.m3u8")}
        _, resolved, missing = P.build(self.sources, self.channels, self.parsed, self.cfg, prev)
        self.assertEqual(missing, ["Missing One"])
        self.assertEqual(resolved["Missing One"].url, "http://old.example/x.m3u8")

    def test_hidden_channels_left_out_without_renumbering(self):
        text, resolved, _ = P.build(self.sources, self.channels, self.parsed, self.cfg, hidden={"იმედი"})
        self.assertIn("იმედი", resolved)                          # still resolved, for re-testing
        names = [e.name for e in P.parse_m3u(text)[1]]
        self.assertNotIn("იმედი", names)
        self.assertEqual(resolved["Первый канал"].attrs["tvg-chno"], "3")   # numbers don't shift

    def test_explicit_epg_url_suppresses_header(self):
        text, _, _ = P.build(self.sources, self.channels, self.parsed, dict(self.cfg, IPTV_EPG_URL="x"))
        self.assertTrue(text.startswith("#EXTM3U\n"))

    def test_explicit_numbers_dont_shift_the_rest(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('[[channel]]\nname = "T"\nurl = "x"\nnumber = 99\n'
                     '[[channel]]\nname = "A"\nmatch = "a"\n[[channel]]\nname = "B"\nmatch = "b"\n')
        _, chans = P.load_channels(fh.name)
        self.assertEqual([c["number"] for c in chans], [99, 1, 2])

    def test_bad_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('[[channel]]\nname = "A"\nmatch = "a"\n[[channel]]\nname = "A"\nmatch = "b"\n')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            P.load_channels(fh.name)


class Handler(http.server.BaseHTTPRequestHandler):
    fail = False

    def do_GET(self):
        if Handler.fail:
            self.send_error(503)
            return
        body = read(FIXTURE).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class InstallTest(unittest.TestCase):
    """End to end: fetch over HTTP, install, then survive a provider outage."""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def run_install(self):
        conf = tempfile.mkdtemp()
        with open(os.path.join(conf, "channels.toml"), "w", encoding="utf-8") as fh:
            fh.write(read(CHANNELS))
        secrets = os.path.join(conf, "secrets.env")
        with open(secrets, "w") as fh:
            fh.write(f"IPTV_M3U_URL=http://127.0.0.1:{self.srv.server_port}/get.php?username=u&password=p\n")
            fh.write("DIRECT_URL=https://direct.example/s.m3u8\n")
        args = mock.Mock(config=conf, secrets=secrets)
        with mock.patch.object(B.time, "sleep"):
            return P.install(args)

    def test_install_then_outage(self):
        reset()
        Handler.fail = False
        self.assertEqual(self.run_install(), 0)
        first = read(B.PLAYLIST_PATH)
        self.assertIn("Первый канал", first)
        self.assertEqual(oct(os.stat(B.PLAYLIST_PATH).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.exists(B.RELOAD_FLAG))
        status = B.read_json(P.STATUS_PATH)
        self.assertTrue(status["sources_ok"])

        os.remove(B.RELOAD_FLAG)
        Handler.fail = True
        self.assertEqual(self.run_install(), 0)
        self.assertEqual(read(B.PLAYLIST_PATH), first)  # cache used
        self.assertFalse(os.path.exists(B.RELOAD_FLAG))                       # nothing changed
        status = B.read_json(P.STATUS_PATH)
        self.assertFalse(status["sources_ok"])
        self.assertIn("using_cache_from", status["sources"]["provider"])
        self.assertNotIn("password=p", str(status))                         # no credentials leak


    def test_fresh_unchanged_playlist_is_left_alone(self):
        reset()
        Handler.fail = False
        self.run_install()
        conf = tempfile.mkdtemp()
        with open(os.path.join(conf, "channels.toml"), "w", encoding="utf-8") as fh:
            fh.write(read(CHANNELS))
        secrets = os.path.join(conf, "secrets.env")
        with open(secrets, "w") as fh:
            fh.write(f"IPTV_M3U_URL=http://127.0.0.1:{self.srv.server_port}/get.php?username=u&password=p\n")
            fh.write("DIRECT_URL=https://direct.example/s.m3u8\n")
        args = mock.Mock(config=conf, secrets=secrets, force=False)
        with mock.patch.object(B.time, "sleep"):
            P.install(args)                                       # same content: digest recorded
        st = B.read_json(P.STATUS_PATH)
        st["missing"] = []                                        # the fixture has a missing channel
        B.write_json(P.STATUS_PATH, st)
        Handler.fail = True                                       # a fetch now would fail...
        with mock.patch.object(P, "fetch_sources", side_effect=AssertionError("fetched")):
            P.install(args)                                       # ...but none happens
        with open(os.path.join(conf, "channels.toml"), "a", encoding="utf-8") as fh:
            fh.write("\n# changed\n")
        with mock.patch.object(P, "fetch_sources", return_value=({}, {})) as fetched:
            P.install(args)                                       # config changed: rebuild
        self.assertTrue(fetched.called)
        Handler.fail = False

    def names(self):
        return [e.name for e in P.parse_m3u(read(B.PLAYLIST_PATH))[1]]

    def test_dead_channel_hidden_until_it_answers_again(self):
        reset()
        Handler.fail = False
        nothing_playing = mock.patch.object(B, "kodi_try", lambda fn, default=None: False)
        with nothing_playing:
            self.run_install()
            self.assertIn("НТВ", self.names())
            ntv_url = next(e.url for e in P.parse_m3u(read(B.PLAYLIST_PATH))[1] if e.name == "НТВ")
            down = lambda url: (_ for _ in ()).throw(ValueError("down")) if url.split("|")[0] == ntv_url else True
            with mock.patch.object(P, "probe", side_effect=down):
                self.run_check()
            self.assertNotIn("НТВ", self.names())                       # hidden from her list
            self.assertEqual(B.read_json(P.STATUS_PATH)["hidden"], ["НТВ"])
            with mock.patch.object(P, "probe", side_effect=down):
                self.run_install()                                       # 6-hourly rebuild: still dead
            self.assertNotIn("НТВ", self.names())
            with mock.patch.object(P, "probe", return_value=True):
                self.run_install()                                       # answers again
            self.assertIn("НТВ", self.names())
            st = B.read_json(P.STATUS_PATH)
            self.assertEqual(st["hidden"], [])
            self.assertNotIn("НТВ", st["streams_bad"])

    def test_outage_hides_nothing(self):
        reset()
        Handler.fail = False
        with mock.patch.object(B, "kodi_try", lambda fn, default=None: False):
            self.run_install()
            before = self.names()
            with mock.patch.object(P, "probe", side_effect=ValueError("no route")):
                self.run_check()
        self.assertEqual(self.names(), before)
        self.assertEqual(B.read_json(P.STATUS_PATH)["hidden"], [])

    def run_check(self):
        conf = tempfile.mkdtemp()
        with open(os.path.join(conf, "channels.toml"), "w", encoding="utf-8") as fh:
            fh.write(read(CHANNELS))
        secrets = os.path.join(conf, "secrets.env")
        with open(secrets, "w") as fh:
            fh.write(f"IPTV_M3U_URL=http://127.0.0.1:{self.srv.server_port}/get.php?username=u&password=p\n")
            fh.write("DIRECT_URL=https://direct.example/s.m3u8\n")
        args = mock.Mock(config=conf, secrets=secrets)
        with mock.patch.object(B.time, "sleep"), mock.patch.object(P.time, "sleep"):
            return P.check_streams(args)


class Pi3VerdictTest(unittest.TestCase):
    def v(self, codec, w, h, field="progressive"):
        return P.pi3_verdict([{"codec_type": "video", "codec_name": codec, "width": w, "height": h,
                               "field_order": field, "r_frame_rate": "25/1"},
                              {"codec_type": "audio", "codec_name": "aac"}])[0]

    def test_verdicts(self):
        self.assertEqual(self.v("h264", 1920, 1080), "OK")
        self.assertEqual(self.v("h264", 1920, 1080, "tt"), "CHECK")
        self.assertEqual(self.v("h264", 720, 576, "tt"), "OK")
        self.assertEqual(self.v("h264", 3840, 2160), "BAD")
        self.assertEqual(self.v("hevc", 1280, 720), "BAD")
        self.assertEqual(self.v("mpeg2video", 720, 576), "CHECK")
        self.assertEqual(self.v("mpeg2video", 1920, 1080), "BAD")
        self.assertEqual(P.pi3_verdict([{"codec_type": "audio"}])[0], "BAD")


class RedactTest(unittest.TestCase):
    def test_redact(self):
        self.assertEqual(B.redact("http://h.example:8080/live/user/secretpw/1.ts?token=abc"),
                         "http://h.example:8080/…/1.ts?…")
        self.assertNotIn("abc", B.redact("https://x/y?token=abc"))


if __name__ == "__main__":
    unittest.main()
