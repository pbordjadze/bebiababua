import os
import unittest
import xml.etree.ElementTree as ET

from helpers import ROOT, reset  # first: points babua at a scratch dir

import babua_lib as B  # noqa: E402
import prekodi  # noqa: E402

USERDATA = os.path.join(ROOT, "kodi", "userdata")


def settings(path):
    return {el.get("id"): el for el in ET.parse(path).getroot()}


class PrekodiTest(unittest.TestCase):
    def setUp(self):
        reset()
        os.makedirs(B.BABUA_DIR, exist_ok=True)
        with open(os.path.join(B.BABUA_DIR, "secrets.env"), "w") as fh:
            fh.write("KODI_WEB_PASSWORD=pw\nIPTV_EPG_URL=http://epg\n")

    def run_main(self):
        prekodi.main()

    def test_fresh_box(self):
        self.run_main()
        gui = settings(os.path.join(USERDATA, "guisettings.xml"))
        self.assertEqual(gui["lookandfeel.font"].text, "Arial")
        self.assertEqual(gui["locale.language"].text, "resource.language.ru_ru")
        self.assertEqual(gui["pvrplayback.confirmchannelswitch"].text, "false")
        self.assertEqual(gui["services.webserver"].text, "true")
        cec = settings(os.path.join(USERDATA, "peripheral_data", "cec_CEC_Adapter.xml"))
        self.assertEqual(cec["standby_pc_on_tv_standby"].get("value"), "36045")
        self.assertEqual(cec["use_tv_menu_language"].get("value"), "0")
        inst_path = os.path.join(USERDATA, "addon_data", "pvr.iptvsimple", "instance-settings-1.xml")
        inst = settings(inst_path)
        self.assertEqual(inst["m3uPathType"].text, "0")
        self.assertEqual(inst["m3uPath"].text, B.PLAYLIST_PATH)
        self.assertEqual(inst["epgUrl"].text, "http://epg")
        self.assertEqual(oct(os.stat(inst_path).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.exists(B.PLAYLIST_PATH))

    def test_repairs_kodi_written_files(self):
        gui_path = os.path.join(USERDATA, "guisettings.xml")
        os.makedirs(USERDATA, exist_ok=True)
        with open(gui_path, "w") as fh:
            fh.write('<settings version="2">\n'
                     '    <setting id="lookandfeel.font" default="true">Default</setting>\n'
                     '    <setting id="videoplayer.stretch43" default="true">0</setting>\n'
                     '</settings>\n')
        pdir = os.path.join(USERDATA, "peripheral_data")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "cec_CEC_Adapter.xml"), "w") as fh:
            fh.write('<settings><setting id="standby_pc_on_tv_standby" value="13011" />'
                     '<setting id="cec_hdmi_port" value="2" /></settings>')
        self.run_main()
        gui = settings(gui_path)
        self.assertEqual(gui["lookandfeel.font"].text, "Arial")
        self.assertNotIn("default", gui["lookandfeel.font"].attrib)
        self.assertEqual(gui["videoplayer.stretch43"].text, "0")          # untouched
        cec = settings(os.path.join(pdir, "cec_CEC_Adapter.xml"))
        self.assertEqual(cec["standby_pc_on_tv_standby"].get("value"), "36045")
        self.assertEqual(cec["cec_hdmi_port"].get("value"), "2")           # user value kept

    def test_enables_addons_in_kodi_database(self):
        import sqlite3
        dbdir = os.path.join(USERDATA, "Database")
        os.makedirs(dbdir, exist_ok=True)
        con = sqlite3.connect(os.path.join(dbdir, "Addons33.db"))
        con.execute("CREATE TABLE installed (id INTEGER PRIMARY KEY, addonID TEXT, enabled BOOLEAN)")
        con.executemany("INSERT INTO installed (addonID, enabled) VALUES (?, ?)",
                        [("pvr.iptvsimple", 0), ("resource.language.ru_ru", 0), ("some.other.addon", 0)])
        con.commit()
        self.run_main()
        rows = dict(con.execute("SELECT addonID, enabled FROM installed"))
        self.assertEqual(rows, {"pvr.iptvsimple": 1, "resource.language.ru_ru": 1, "some.other.addon": 0})
        con.close()

    def test_corrupt_guisettings_after_power_cut(self):
        gui_path = os.path.join(USERDATA, "guisettings.xml")
        os.makedirs(USERDATA, exist_ok=True)
        with open(gui_path, "w") as fh:
            fh.write('<settings version="2"><setting id="x">trunc')
        self.run_main()
        self.assertEqual(settings(gui_path)["lookandfeel.font"].text, "Arial")
        self.assertTrue(os.path.exists(gui_path + ".corrupt"))

    def test_idempotent(self):
        self.run_main()
        if os.path.exists(B.RELOAD_FLAG):
            os.remove(B.RELOAD_FLAG)
        inst = os.path.join(USERDATA, "addon_data", "pvr.iptvsimple", "instance-settings-1.xml")
        before = os.stat(inst).st_mtime_ns
        self.run_main()
        self.assertEqual(os.stat(inst).st_mtime_ns, before)
        self.assertFalse(os.path.exists(B.RELOAD_FLAG))


if __name__ == "__main__":
    unittest.main()
