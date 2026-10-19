import unittest
from unittest import mock

from helpers import reset  # first: points babua at a scratch dir

import babua_lib as B  # noqa: E402
import health as H  # noqa: E402

OK = (True, "ok")


class HealthTest(unittest.TestCase):
    def setUp(self):
        reset()

    def run_once(self, **results):
        defaults = dict(internet=OK, vpn=OK, kodi=OK, power=OK, temp=OK,
                        storage=OK, memory=OK, playlist=OK, player=OK)
        defaults.update(results)
        sent = []
        with mock.patch.multiple(
            H,
            check_internet=lambda cfg: defaults["internet"],
            check_vpn=lambda cfg, repair: defaults["vpn"],
            check_kodi=lambda cfg, state, repair: defaults["kodi"],
            check_power=lambda cfg: defaults["power"],
            check_temp=lambda cfg: defaults["temp"],
            check_storage=lambda cfg: defaults["storage"],
            check_memory=lambda cfg, repair: defaults["memory"],
            check_playlist=lambda cfg: defaults["playlist"],
            check_player=lambda cfg: defaults["player"],
            heartbeat=lambda cfg, problems: None,
            boot_id=lambda: "boot-1",
        ), mock.patch.object(B, "notify", lambda cfg, title, msg, *a: sent.append(title)), \
                mock.patch.object(B, "flush_outbox", lambda cfg: None):
            H.main()
        return sent

    def test_transitions_only(self):
        self.assertEqual(self.run_once(), ["Booted"])
        self.assertEqual(self.run_once(), [])
        bad = (False, "under-voltage since boot")
        self.assertEqual(self.run_once(power=bad), ["Problem: power"])
        self.assertEqual(self.run_once(power=bad), [])            # no repeat while it persists
        self.assertEqual(self.run_once(), ["Recovered: power"])

    def test_offline_suppresses_downstream_noise(self):
        self.run_once()
        sent = self.run_once(internet=(False, "no route"), vpn=(False, "down"), playlist=(False, "stale"))
        self.assertEqual(sent, ["Problem: internet"])

    def test_tailscale(self):
        running = {"BackendState": "Running",
                   "Self": {"DNSName": "babua-tv.tail1234.ts.net.", "TailscaleIPs": ["100.64.1.2"],
                            "KeyExpiry": "2099-01-01T00:00:00Z"}}
        with mock.patch.object(B, "tailscale_status", return_value=running):
            ok, detail = H.check_tailscale({}, repair=True)
        self.assertTrue(ok)
        self.assertIn("babua-tv.tail1234.ts.net 100.64.1.2", detail)

        soon = {"BackendState": "Running", "Self": {"KeyExpiry": "2000-01-01T00:00:00Z"}}
        with mock.patch.object(B, "tailscale_status", return_value=soon):
            ok, detail = H.check_tailscale({}, repair=True)
        self.assertFalse(ok)
        self.assertIn("key expiry", detail)

        with mock.patch.object(B, "tailscale_status", return_value={"BackendState": "NeedsLogin"}), \
                mock.patch.object(B, "tailscale_up", return_value=(0, "")) as up:
            ok, detail = H.check_tailscale({"TAILSCALE_AUTHKEY": "tskey-x"}, repair=True)
            self.assertFalse(ok)
            up.assert_called_once()
            H.check_tailscale({}, repair=True)            # no key: nothing to do
            self.assertEqual(up.call_count, 1)

    def test_throttle_flags(self):
        with mock.patch.object(B, "run", return_value=(0, "throttled=0x50000")):
            ok, detail = H.check_power({})
        self.assertFalse(ok)
        self.assertIn("under-voltage since boot", detail)
        with mock.patch.object(B, "run", return_value=(0, "throttled=0x0")):
            self.assertTrue(H.check_power({})[0])


if __name__ == "__main__":
    unittest.main()
