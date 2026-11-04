import gzip
import json
import os
import time
import unittest
from unittest import mock

from helpers import reset  # first: points babua at a scratch dir

import babua_lib as B  # noqa: E402
import vitals as V  # noqa: E402

IW_LINK = """Connected to 02:00:5e:aa:bb:cc (on wlan0)
\tSSID: Their Network Name
\tfreq: 2437
\tRX: 2259316274 bytes (1694221 packets)
\tsignal: -49 dBm
\trx bitrate: 72.2 MBit/s
\ttx bitrate: 65.0 MBit/s MCS 6 short GI
"""

BUSYBOX_PING = """PING 192.0.2.1 (192.0.2.1): 56 data bytes

--- 192.0.2.1 ping statistics ---
3 packets transmitted, 2 packets received, 33% packet loss
round-trip min/avg/max = 2.101/3.456/4.200 ms
"""


def rec(boot, ts, **kw):
    r = {"t": V.iso(ts), "ts": ts, "up": 3600, "boot": boot, "thr_flags": [], "kodi": {"up": True}, "tv": "standby"}
    r.update(kw)
    return r


def write_vitals(records):
    for r in records:
        B.append_line(V.VITALS_LOG, json.dumps(r, ensure_ascii=False))


class DecodeTest(unittest.TestCase):
    def test_throttled(self):
        self.assertEqual(V.decode_throttled(0x0), [])
        self.assertEqual(V.decode_throttled(0x50005), ["under-voltage now", "throttled now",
                                                       "under-voltage since boot", "throttled since boot"])
        self.assertEqual(V.decode_throttled(None), [])

    def test_reset_register(self):
        self.assertEqual(V.decode_rsts(0x1000), "power-on")
        self.assertEqual(V.decode_rsts(0x1020), "watchdog")      # a reboot or a hang after a power-on
        self.assertEqual(V.decode_rsts(0x20), "watchdog")
        self.assertEqual(V.decode_rsts(0x0), "unknown")
        self.assertEqual(V.decode_rsts(None), "unknown")

    def test_wifi_keeps_no_network_name(self):
        w = V.parse_iw_link(IW_LINK)
        self.assertEqual(w, {"connected": True, "ap": "aa:bb:cc", "sig": -49, "tx_mbit": 65.0,
                             "rx_mbit": 72.2, "freq": 2437})
        self.assertNotIn("Their Network", json.dumps(w))
        self.assertEqual(V.parse_iw_link("Not connected."), {"connected": False})

    def test_ping(self):
        self.assertEqual(V.parse_ping(BUSYBOX_PING), (33.0, 3.456))
        self.assertEqual(V.parse_ping("3 packets transmitted, 0 packets received, 100% packet loss"),
                         (100.0, None))
        self.assertEqual(V.parse_ping(""), (100.0, None))


class LogTest(unittest.TestCase):
    def setUp(self):
        reset()

    def test_rotate_and_read_back_across_archives(self):
        now = int(time.time())
        with mock.patch.object(V, "ROTATE_AT", 2000):
            for i in range(60):
                write_vitals([rec("aaaaaaaa", now - 6000 + i * 60)])
                V.rotate(now=now - 6000 + i * 60)
        self.assertTrue(V.archives())
        self.assertTrue(all(gzip.open(p).read() for p in V.archives()))
        recs = V.read_records(limit=50)
        self.assertEqual(len(recs), 50)
        self.assertEqual([r["ts"] for r in recs], sorted(r["ts"] for r in recs))
        self.assertEqual(recs[-1]["ts"], now - 6000 + 59 * 60)
        since = V.read_records(since=now - 6000 + 30 * 60)
        self.assertEqual(len(since), 30)

    def test_archive_cap(self):
        with mock.patch.object(V, "ROTATE_AT", 100), mock.patch.object(V, "ARCHIVE_MAX", 600):
            for i in range(20):
                write_vitals([rec("aaaaaaaa", 1800000000 + i, pad="x" * 200)])
                V.rotate(now=1800000000 + i)
        self.assertLessEqual(sum(os.path.getsize(p) for p in V.archives()), 600)

    def test_cut_off_line_is_skipped(self):
        write_vitals([rec("aaaaaaaa", 1800000000)])
        with open(V.VITALS_LOG, "a") as fh:
            fh.write('{"t": "2026-11-0')              # power cut mid-write
        self.assertEqual(len(V.read_records(limit=10)), 1)


class BootRecordTest(unittest.TestCase):
    def setUp(self):
        reset()
        self.now = 1793800000.0                      # Tue 2026-11-03 ~15:06 ET
        patches = [
            mock.patch.object(B, "boot_id", return_value="bbbbbbbb-0000"),
            mock.patch.object(B, "uptime", return_value=60.0),
            mock.patch.object(V, "journal_tail", return_value=["Nov 03 15:04:56 babua-tv kernel: x"]),
            mock.patch.object(V.time, "time", return_value=self.now + 3 * 3600),
            mock.patch.object(B, "read_tv_power", return_value="standby"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def record(self, rsts):
        with mock.patch.object(V, "read_rsts", return_value=rsts):
            return V.ensure_boot_record()

    def test_power_loss_while_watching(self):
        playing = {"up": True, "playing": "რუსთავი 2", "ch": "2"}
        write_vitals([rec("aaaaaaaa", int(self.now) - 120, kodi=playing, tv="on"),
                      rec("aaaaaaaa", int(self.now) - 60, kodi=playing, tv="on", hdmi="connected")])
        r = self.record(0x1000)
        self.assertEqual(r["cause"], "power-loss")
        self.assertEqual(r["reset"], "power-on")
        self.assertEqual(r["prev"]["doing"], "playing 2 'რუსთავი 2', TV on")
        self.assertIn("Power lost", r["summary"])
        self.assertIn("off 3 h 00 min", r["summary"])
        self.assertIn("TV on before, standby at return", r["summary"])     # the TV lost power too
        self.assertIn("under-voltage before: none", r["summary"])
        title, msg, prio, _ = V.boot_alert(r)
        self.assertEqual((title, prio), ("Restarted after power loss", 4))
        self.assertEqual(V.ensure_boot_record()["boot_id"], "bbbbbbbb-0000")   # once per boot
        self.assertEqual(len(V.read_boot_records()), 1)

    def test_under_voltage_before_the_end_is_reported(self):
        write_vitals([rec("aaaaaaaa", int(self.now) - 60, thr_flags=["under-voltage since boot"], uv_s=20)])
        r = self.record(0x1000)
        self.assertIn("under-voltage since boot", r["summary"])
        self.assertEqual(r["prev"]["uv_s_recent"], 20)

    def test_clean_reboot(self):
        write_vitals([rec("aaaaaaaa", int(self.now) - 60), rec("aaaaaaaa", int(self.now), event="reboot")])
        r = self.record(0x1020)
        self.assertEqual(r["cause"], "clean")
        self.assertEqual(V.boot_alert(r)[0], "Booted")

    def test_hang_reset_by_the_watchdog(self):
        write_vitals([rec("aaaaaaaa", int(self.now))])
        self.assertEqual(self.record(0x1020)["cause"], "hang")

    def test_no_vitals_from_the_previous_boot(self):
        write_vitals([rec("bbbbbbbb", int(self.now))])     # only this boot's lines
        r = self.record(0x1000)
        self.assertEqual(r["cause"], "no-data")
        self.assertIn("reset: power-on", r["summary"])

    def test_waits_for_the_clock_and_the_tv(self):
        with mock.patch.object(B, "clock_synced", return_value=False):
            self.assertIsNone(self.record(0x1000))
        with mock.patch.object(B, "read_tv_power", return_value=None):
            self.assertIsNone(self.record(0x1000))                       # babuad hasn't heard the TV yet
            with mock.patch.object(B, "uptime", return_value=400):
                self.assertEqual(self.record(0x1000)["tv_after"], "unknown")   # don't wait forever
        self.assertEqual(len(V.read_boot_records()), 1)

    def test_kodi_log_archived_once(self):
        os.makedirs(os.path.dirname(V.KODI_OLD_LOG), exist_ok=True)
        with open(V.KODI_OLD_LOG, "w") as fh:
            fh.write("2026-11-03 15:04:56.601 T:797 info <general>: last line\n")
        first = V.archive_kodi_log()
        self.assertTrue(first and os.path.exists(first))
        self.assertIsNone(V.archive_kodi_log())
        with gzip.open(first, "rt") as fh:
            self.assertIn("last line", fh.read())


class KodiProbeTest(unittest.TestCase):
    def test_buffering_and_frozen_picture(self):
        probe = V.KodiProbe()
        answers = iter([
            {"up": True, "playing": "Imedi", "ch": "3", "caching": True, "paused": False, "time": "00:10"},
            {"up": True, "playing": "Imedi", "ch": "3", "caching": False, "paused": False, "time": "00:20"},
            {"up": True, "playing": "Imedi", "ch": "3", "caching": False, "paused": False, "time": "00:20"},
            {"up": False},
        ])
        with mock.patch.object(probe, "query", side_effect=lambda: next(answers)), \
                mock.patch.object(V, "under_voltage_now", return_value=False):
            for _ in range(3):
                probe.sample()
            info, uv = probe.summary()
            self.assertEqual((info["caching_s"], info["frozen_s"], uv), (10, 10, 0))
            self.assertNotIn("time", info)
            probe.query = mock.Mock(side_effect=OSError("refused"))
            probe.sample()
            info, _ = probe.summary()
            self.assertEqual(info, {"up": False, "down_s": 10})


class CliTest(unittest.TestCase):
    def setUp(self):
        reset()
        from importlib.machinery import SourceFileLoader
        self.cli = SourceFileLoader("babua_cli", os.path.join(os.path.dirname(B.__file__), "babua")).load_module()

    def test_vitals_line(self):
        r = rec("aaaaaaaa", 1793800000, thr_flags=["under-voltage since boot"], uv_s=10, temp=51.5, volts=1.2,
                arm_mhz=1400, wifi=V.parse_iw_link(IW_LINK), ping_gw={"loss": 0.0, "ms": 2.1},
                ping_inet={"loss": 100.0, "ms": None}, tailscale="Running", hdmi="connected",
                kodi={"up": True, "playing": "იმედი", "ch": "3", "cache": "100", "caching_s": 20, "frozen_s": 0},
                soak=None)
        line = self.cli.format_vitals(r)
        for part in ("51.5C", "under-voltage*", "UV 10s", "wifi -49dBm", "ap ..bb:cc", "gw 2ms", "net LOST",
                     "TV standby", "PLAY 3 იმედი", "buffering 20s"):
            self.assertIn(part, line)
        self.assertIn("== reboot ==", self.cli.format_vitals(dict(r, event="reboot")))

    def test_commands_print(self):
        write_vitals([rec("aaaaaaaa", int(time.time()) - 60)])
        with mock.patch("sys.stdout") as out:
            self.assertEqual(self.cli.cmd_vitals(["-2h"]), 0)
            self.assertEqual(self.cli.cmd_vitals(["5", "--json"]), 0)
            self.assertEqual(self.cli.cmd_boots([]), 0)
        self.assertTrue(out.write.called)
        self.assertAlmostEqual(self.cli.parse_since("-2h"), time.time() - 7200, delta=5)
        self.assertLessEqual(self.cli.parse_since("23:59"), time.time())


if __name__ == "__main__":
    unittest.main()
