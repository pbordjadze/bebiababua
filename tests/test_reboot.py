"""The weekly reboot (babua maintenance-reboot) and `babua reboot`."""

import os
import unittest
from unittest import mock

from helpers import reset  # first: points babua at a scratch dir

import babua_lib as B  # noqa: E402


class RebootTest(unittest.TestCase):
    def setUp(self):
        reset()
        B.ensure_dirs()
        from importlib.machinery import SourceFileLoader
        self.cli = SourceFileLoader("babua_cli", os.path.join(os.path.dirname(B.__file__), "babua")).load_module()
        self.calls = []
        self.state = {"playing": False, "tv": "standby", "soak": None, "uptime": 3 * 86400.0, "synced": True}
        patches = [
            mock.patch.object(self.cli, "sh", side_effect=lambda *cmd: self.calls.append(cmd) or 0),
            mock.patch.object(B, "kodi_try", side_effect=lambda fn, default=None: self.state["playing"]),
            mock.patch.object(B, "read_tv_power", side_effect=lambda: self.state["tv"]),
            mock.patch.object(B, "soak_active", side_effect=lambda: self.state["soak"]),
            mock.patch.object(B, "uptime", side_effect=lambda: self.state["uptime"]),
            mock.patch.object(B, "clock_synced", side_effect=lambda: self.state["synced"]),
            mock.patch.object(B, "load_config", return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def weekly(self, **state):
        self.state.update(state)
        self.calls.clear()
        self.cli.cmd_maintenance_reboot([])
        return ("systemctl", "reboot") in self.calls

    def test_runs_when_idle_with_the_clock_set_by_connman(self):
        self.assertTrue(self.weekly())
        self.assertTrue(os.path.exists(B.QUIET_FLAG))          # no channel starts after it

    def test_never_while_someone_may_be_watching(self):
        self.assertFalse(self.weekly(playing=True))
        self.assertFalse(self.weekly(playing=False, tv="on"))
        self.assertFalse(self.weekly(tv="to-on"))
        self.assertFalse(self.weekly(tv=None))                  # babuad silent: can't tell
        self.assertFalse(self.weekly(tv="standby", soak={"pid": 1}))
        self.assertTrue(self.weekly(soak=None))

    def test_loop_guards(self):
        self.assertFalse(self.weekly(uptime=3600.0))            # rebooted less than a day ago
        self.assertFalse(self.weekly(uptime=3 * 86400.0, synced=False))
        with mock.patch.object(B, "load_config", return_value={"WEEKLY_REBOOT": "0"}):
            self.assertFalse(self.weekly(synced=True))

    def test_manual_reboot_refuses_unless_forced(self):
        self.state.update(tv="on")
        self.assertEqual(self.cli.cmd_reboot([]), 1)
        self.assertEqual(self.calls, [])
        self.cli.cmd_reboot(["--force"])
        self.assertIn(("systemctl", "reboot"), self.calls)



if __name__ == "__main__":
    unittest.main()
