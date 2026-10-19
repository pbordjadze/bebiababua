#!/usr/bin/env python3
"""Runs before kodi.service on every boot (babua-prekodi.service).

Writes the files Kodi only reads at start-up: guisettings.xml overrides,
CEC behaviour and the IPTV Simple instance settings. Running every boot makes
the box self-healing: whatever was changed through the UI is put back.
"""

import glob
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import babua_lib as B  # noqa: E402

USERDATA = os.path.join(B.KODI_HOME, "userdata")


def xml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def load_settings_xml(path):
    """Parse a version-2 Kodi settings file; a corrupt one is set aside."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        if root.tag == "settings":
            return tree
    except FileNotFoundError:
        pass
    except ET.ParseError as exc:
        B.log(f"{path} unreadable ({exc}); starting fresh")
        os.replace(path, path + ".corrupt")
    return ET.ElementTree(ET.Element("settings", {"version": "2"}))


def apply_settings_xml(path, values, mode=0o644):
    tree = load_settings_xml(path)
    root = tree.getroot()
    existing = {el.get("id"): el for el in root.findall("setting")}
    changed = []
    for key, value in values.items():
        text = xml_value(value)
        el = existing.get(key)
        if el is None:
            el = ET.SubElement(root, "setting", {"id": key})
        elif (el.text or "") == text:
            continue
        el.attrib.pop("default", None)
        el.text = text
        changed.append(key)
    if changed or not os.path.exists(path):
        ET.indent(tree, "    ")
        B.write_atomic(path, ET.tostring(root, encoding="unicode") + "\n", mode)
    return changed


def apply_cec():
    """CEC settings live in peripheral_data/cec_<name>.xml; on the Pi (Linux
    CEC framework, vendor/product 0) Kodi names it cec_CEC_Adapter.xml."""
    pdir = os.path.join(USERDATA, "peripheral_data")
    os.makedirs(pdir, exist_ok=True)
    files = glob.glob(os.path.join(pdir, "cec_*.xml")) or [os.path.join(pdir, "cec_CEC_Adapter.xml")]
    for path in files:
        try:
            tree = ET.parse(path)
        except (FileNotFoundError, ET.ParseError):
            tree = ET.ElementTree(ET.Element("settings"))
        root = tree.getroot()
        existing = {el.get("id"): el for el in root.findall("setting")}
        dirty = not os.path.exists(path)
        for key, value in B.CEC_SETTINGS.items():
            el = existing.get(key)
            if el is None:
                ET.SubElement(root, "setting", {"id": key, "value": value})
                dirty = True
            elif el.get("value") != value:
                el.set("value", value)
                dirty = True
        if dirty:
            ET.indent(tree, "    ")
            B.write_atomic(path, ET.tostring(root, encoding="unicode") + "\n")
            B.log("cec settings written:", os.path.basename(path))


def enable_addons():
    """Kodi registers add-ons it finds on disk as disabled. babuad enables
    them over JSON-RPC; this is the fallback for when that can't happen
    (Kodi is stopped here, and the database exists from the second boot on).
    Returns whether the PVR add-on is now enabled in the database."""
    pvr_enabled = False
    for db in glob.glob(os.path.join(USERDATA, "Database", "Addons*.db")):
        try:
            con = sqlite3.connect(db, timeout=10)
            with con:
                marks = ",".join("?" * len(B.MANAGED_ADDONS))
                cur = con.execute(f"UPDATE installed SET enabled=1 WHERE enabled=0 AND addonID IN ({marks})",
                                  B.MANAGED_ADDONS)
            if cur.rowcount:
                B.log(f"enabled {cur.rowcount} add-on(s) in {os.path.basename(db)}")
            if con.execute("SELECT 1 FROM installed WHERE addonID=? AND enabled=1", (B.PVR_ADDON,)).fetchone():
                pvr_enabled = True
            con.close()
        except sqlite3.Error as exc:
            B.log(f"{db}: {exc}")
    return pvr_enabled


def iptv_instance_settings(cfg):
    epg = cfg.get("IPTV_EPG_URL", "")
    return {
        "kodi_addon_instance_name": "IPTV",
        "kodi_addon_instance_enabled": True,
        "m3uPathType": 0,                       # local file written by playlist.py
        "m3uPath": B.PLAYLIST_PATH,
        "m3uCache": False,
        "m3uRefreshMode": 0,                    # playlist.py reloads the add-on itself
        "startNum": 1,
        "numberByOrder": False,
        "epgPathType": 1,
        "epgUrl": epg,
        "epgCache": True,
        "epgTimeShift": float(cfg.get("IPTV_EPG_SHIFT_HOURS", "0") or 0),
        "logoPathType": 0,
        "logoPath": os.path.join(B.BABUA_DIR, "logos") + "/",
        "logoFromEpg": 0,                       # our logos win over EPG ones
        "useFFmpegReconnect": True,
        "useInputstreamAdaptiveforHls": B.cfg_bool(cfg, "IPTV_USE_ADAPTIVE_HLS", False),
        "defaultUserAgent": cfg.get("IPTV_USER_AGENT", ""),
        "timeshiftEnabled": False,
        "catchupEnabled": False,
    }


def main():
    B.ensure_dirs()
    os.makedirs(B.SCREENSHOT_DIR, exist_ok=True)
    cfg = B.load_config()

    gui = B.kodi_settings(cfg)
    if not enable_addons():
        # Kodi opens its start window before it loads the JSON-RPC methods, and
        # a TV window with no PVR add-on enabled blocks there on a modal "no PVR
        # add-on" dialog: until someone presses OK, every JSON-RPC call gets
        # "Method not found", so babuad can't enable the add-on (first boot,
        # before Kodi has created its database). Start on Home this time;
        # babuad sets the TV window back once it has enabled the add-on.
        gui["lookandfeel.startupwindow"] = B.HOME_WINDOW
        B.log("PVR add-on not enabled yet: starting Kodi on Home")
    changed = apply_settings_xml(os.path.join(USERDATA, "guisettings.xml"), gui)
    if changed:
        B.log("guisettings updated:", ", ".join(changed))

    apply_cec()

    inst = os.path.join(USERDATA, "addon_data", B.PVR_ADDON, "instance-settings-1.xml")
    os.makedirs(os.path.dirname(inst), exist_ok=True)
    if apply_settings_xml(inst, iptv_instance_settings(cfg), mode=0o600):
        # only matters when run under a live Kodi (post-deploy): babuad
        # restarts the IPTV add-on once nothing is playing
        B.write_atomic(B.RELOAD_FLAG, "settings")

    if not os.path.exists(B.PLAYLIST_PATH):
        # IPTV Simple needs a readable file; playlist.py fills it once online.
        B.write_atomic(B.PLAYLIST_PATH, "#EXTM3U\n", 0o600)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - never block Kodi from starting
        B.log("prekodi failed:", repr(exc))
    sys.exit(0)
