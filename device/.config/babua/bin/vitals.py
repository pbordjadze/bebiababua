#!/usr/bin/env python3
"""Vitals log and boot forensics (babua-vitals.service): so that the next time
the box dies, we know what it was doing and why it came back.

Once a minute one JSON line goes to vitals/vitals.jsonl and is fsync'd at
once, so at most the last minute before a power cut is lost: power (throttled
bits, core volts, clocks, temperature), load and memory, Wi-Fi, pings to the
router and the internet, Tailscale, Kodi (what plays, buffering) and the TV's
power state as babuad last heard it over CEC (nothing is sent to the TV here).
Kodi and the under-voltage alarm are sampled every 10 s in between.

On every boot one record goes to boots.jsonl: why the box started (the Pi's
reset register, and whether the previous boot shut down or just stopped) and
what it was doing when last alive. Kodi's previous log is archived too (Kodi
itself keeps only one).

  vitals.py                    the daemon
  vitals.py --once             print one sample, write nothing
  vitals.py --boot-record      write this boot's record now if it's missing
"""

import glob
import gzip
import json
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import babua_lib as B  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Linux
    fcntl = None

VITALS_DIR = os.path.join(B.STATE_DIR, "vitals")
VITALS_LOG = os.path.join(VITALS_DIR, "vitals.jsonl")
BOOTS_LOG = os.path.join(B.STATE_DIR, "boots.jsonl")
KODI_ARCHIVE = os.path.join(B.STATE_DIR, "kodi-logs")
KODI_OLD_LOG = os.path.join(B.KODI_HOME, "temp", "kodi.old.log")

INTERVAL = 60                  # one line a minute
FAST = 10                      # Kodi and the under-voltage alarm are sampled this often
ROTATE_AT = 2_000_000          # bytes (~2 days); then gzipped next to it
ARCHIVE_MAX = 100_000_000      # all gzipped vitals together (years of them)
KODI_LOGS_KEEP = 30
JOURNAL_TAIL = 40              # previous boot's last journal lines kept in its boot record

UV_ALARM = "/sys/class/hwmon/hwmon0/in0_lcrit_alarm"   # rpi_volt: 1 while under-voltage

# vcgencmd get_throttled
THROTTLED_BITS = (
    (0, "under-voltage now"), (1, "arm freq capped now"), (2, "throttled now"), (3, "soft temp limit now"),
    (16, "under-voltage since boot"), (17, "arm freq capped since boot"),
    (18, "throttled since boot"), (19, "soft temp limit since boot"),
)


def decode_throttled(value):
    if value is None:
        return []
    return [name for bit, name in THROTTLED_BITS if value >> bit & 1]


# vcgencmd get_rsts: the PM_RSTS register as the firmware found it at boot.
# 0x1000 HADPOR (power-on reset); 0x20 HADWRF (full watchdog reset: Linux's
# reboot uses it, and so does the hardware watchdog after a hang). The firmware
# also keeps a boot partition number in bits 0,2,..,10, so only those two bits
# are read.
RSTS_POWER_ON = 0x1000
RSTS_WATCHDOG = 0x20


def decode_rsts(value):
    if value is None:
        return "unknown"
    if value & RSTS_WATCHDOG:
        return "watchdog"          # a reboot, or a hang the hardware watchdog ended
    if value & RSTS_POWER_ON:
        return "power-on"          # power was cut (or dipped low enough to reset the Pi)
    return "unknown"


# --------------------------------------------------------------------------
# readings (each returns None when unavailable; none of them raises)


def vcgencmd(*args):
    rc, out = B.run(["vcgencmd", *args], timeout=5)
    return out if rc == 0 else ""


def _after_eq(text):
    return text.split("=", 1)[1] if "=" in text else ""


def read_throttled():
    try:
        return int(_after_eq(vcgencmd("get_throttled")), 16)
    except ValueError:
        return None


def read_rsts():
    try:
        return int(_after_eq(vcgencmd("get_rsts")), 16)
    except ValueError:
        return None


def read_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return round(int(fh.read()) / 1000, 1)
    except (OSError, ValueError):
        return None


def read_volts():
    m = re.search(r"([\d.]+)V", vcgencmd("measure_volts", "core"))
    return float(m.group(1)) if m else None


def read_mhz(clock):
    try:
        return round(int(_after_eq(vcgencmd("measure_clock", clock))) / 1e6)
    except ValueError:
        return None


def read_file(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def under_voltage_now():
    return read_file(UV_ALARM) == "1"


def loadavg():
    try:
        return [float(x) for x in read_file("/proc/loadavg").split()[:3]]
    except (AttributeError, ValueError):
        return None


def mem_available_mb():
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return None


def cpu_times():
    """(busy, total) jiffies from /proc/stat."""
    try:
        with open("/proc/stat") as fh:
            f = [int(x) for x in fh.readline().split()[1:]]
        idle = f[3] + (f[4] if len(f) > 4 else 0)
        return sum(f) - idle, sum(f)
    except (OSError, ValueError, IndexError):
        return None


def find_pid(comm):
    for d in os.listdir("/proc"):
        if d.isdigit() and read_file(f"/proc/{d}/comm") == comm:
            return int(d)
    return None


def proc_usage(pid):
    """(rss MB, cpu jiffies) of a process."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            f = fh.read().rsplit(")", 1)[1].split()
        rss_pages = int(f[21])
        return round(rss_pages * os.sysconf("SC_PAGE_SIZE") / 1e6), int(f[11]) + int(f[12])
    except (OSError, ValueError, IndexError):
        return None


def default_route():
    """(interface, gateway) of the default route."""
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if f[1] == "00000000" and int(f[3], 16) & 2:
                    return f[0], socket.inet_ntoa(bytes.fromhex(f[2])[::-1])
    except (OSError, ValueError, IndexError):
        pass
    return None, None


def net_bytes(iface):
    rx = read_file(f"/sys/class/net/{iface}/statistics/rx_bytes")
    tx = read_file(f"/sys/class/net/{iface}/statistics/tx_bytes")
    try:
        return int(rx), int(tx)
    except (TypeError, ValueError):
        return None


def parse_iw_link(text):
    """`iw dev wlan0 link` -> signal, bitrates, frequency and the access
    point's BSSID, of which only the last three bytes are kept: enough to see a
    hop between mesh nodes. The network name is never kept."""
    if not text or text.startswith("Not connected"):
        return {"connected": False}
    out = {"connected": True}
    m = re.search(r"Connected to ([0-9a-f:]{17})", text)
    if m:
        out["ap"] = m.group(1)[-8:]
    for key, pattern, conv in (("sig", r"signal:\s*(-?\d+)", int),
                               ("tx_mbit", r"tx bitrate:\s*([\d.]+)", float),
                               ("rx_mbit", r"rx bitrate:\s*([\d.]+)", float),
                               ("freq", r"freq:\s*(\d+)", int)):
        m = re.search(pattern, text)
        if m:
            out[key] = conv(m.group(1))
    return out


def wifi(iface="wlan0"):
    rc, link = B.run(["iw", "dev", iface, "link"], timeout=5)
    if rc != 0:
        return None
    out = parse_iw_link(link)
    rc, dump = B.run(["iw", "dev", iface, "station", "dump"], timeout=5)
    m = re.search(r"tx failed:\s*(\d+)", dump or "")
    if m:
        out["tx_failed_total"] = int(m.group(1))
    return out


def parse_ping(text):
    """busybox or iputils summary -> (loss %, avg ms or None)."""
    loss = re.search(r"(\d+(?:\.\d+)?)% packet loss", text or "")
    rtt = re.search(r"= [\d.]+/([\d.]+)/", text or "")
    return (float(loss.group(1)) if loss else 100.0), (float(rtt.group(1)) if rtt else None)


def ping(host, count=3):
    if not host:
        return None
    rc, out = B.run(["ping", "-c", str(count), "-i", "0.3", "-W", "1", "-w", str(count), "-q", host],
                    timeout=count + 5)
    loss, avg = parse_ping(out)
    return {"loss": loss, "ms": avg}


def tailscale_state():
    """BackendState over tailscaled's local API (cheaper than the CLI every minute)."""
    import http.client

    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(B.TAILSCALE_SOCKET)

    try:
        conn = UnixConnection("local-tailscaled.sock", timeout=5)
        conn.request("GET", "/localapi/v0/status?peers=false", headers={"Sec-Tailscale": "localapi"})
        resp = conn.getresponse()
        data = json.loads(resp.read()) if resp.status == 200 else {}
        conn.close()
    except (OSError, ValueError, http.client.HTTPException):
        return None
    state = data.get("BackendState")
    if state and (data.get("Self") or {}).get("Online") is False:
        state += " (offline)"
    return state


def hdmi_status():
    for path in sorted(glob.glob("/sys/class/drm/card*-HDMI-A-*/status")):
        return read_file(path)
    return None


def disk_free_mb():
    try:
        return int(shutil.disk_usage("/storage" if os.path.isdir("/storage") else B.STATE_DIR).free / 1e6)
    except OSError:
        return None


def journal_persistent():
    """Is the journal being written to the SD card (LibreELEC binds
    /storage/.cache/log onto /var/log when its switch file is there)?"""
    return os.path.ismount("/var/log") and os.path.isdir("/var/log/journal")


def system_ending():
    """'reboot', 'poweroff' or 'halt' while systemd is shutting down, else None."""
    rc, out = B.run(["systemctl", "is-system-running"], timeout=5)
    if out.strip() != "stopping":
        return None
    rc, jobs = B.run(["systemctl", "list-jobs", "--no-legend"], timeout=5)
    for target in ("reboot", "poweroff", "halt", "kexec"):
        if f"{target}.target" in jobs:
            return target
    return "shutdown"


# --------------------------------------------------------------------------
# Kodi, sampled every FAST seconds over one long-lived connection (a new
# connection each time would put two lines into kodi.log every 10 s)


class KodiProbe:
    def __init__(self):
        self.kodi = None
        self.last_time = None
        self.last = {}             # the latest sample (kept across minutes)
        self.reset()

    def reset(self):
        self.samples = 0
        self.down = 0
        self.caching = 0
        self.frozen = 0
        self.uv = 0

    def close(self):
        if self.kodi:
            self.kodi.close()
        self.kodi = None

    def query(self):
        if self.kodi is None:
            self.kodi = B.Kodi(timeout=5).connect()
        k = self.kodi
        info = {"up": True}
        bools = k.call("XBMC.GetInfoBooleans", {"booleans": [
            "System.ScreenSaverActive", "Player.Caching", "Player.Paused"]}) or {}
        info["screensaver"] = bool(bools.get("System.ScreenSaverActive"))
        players = k.call("Player.GetActivePlayers") or []
        if any(p.get("type") == "video" for p in players):
            labels = k.call("XBMC.GetInfoLabels", {"labels": [
                "VideoPlayer.ChannelName", "VideoPlayer.ChannelNumberLabel", "VideoPlayer.Title",
                "Player.Process(videowidth)", "Player.Process(videoheight)", "Player.Process(videofps)",
                "Player.CacheLevel", "Player.Time"]}) or {}
            info.update({
                "playing": labels.get("VideoPlayer.ChannelName") or labels.get("VideoPlayer.Title") or "?",
                "ch": labels.get("VideoPlayer.ChannelNumberLabel") or None,
                "video": "{}x{}@{}".format(labels.get("Player.Process(videowidth)", "?"),
                                           labels.get("Player.Process(videoheight)", "?"),
                                           labels.get("Player.Process(videofps)", "?")),
                "cache": labels.get("Player.CacheLevel") or None,
                "caching": bool(bools.get("Player.Caching")),
                "paused": bool(bools.get("Player.Paused")),
                "time": labels.get("Player.Time"),
            })
        k.notifications.clear()    # Kodi broadcasts events to every client; not needed here
        return info

    def sample(self):
        try:
            info = self.query()
        except (OSError, ValueError, B.KodiError):
            self.close()
            info = {"up": False}
        self.samples += 1
        if under_voltage_now():
            self.uv += 1
        if not info["up"]:
            self.down += 1
        elif info.get("playing"):
            if info["caching"]:
                self.caching += 1
            elif not info["paused"] and info.get("time") and info["time"] == self.last_time:
                self.frozen += 1   # the picture's clock didn't move in 10 s
            self.last_time = info.get("time")
        else:
            self.last_time = None
        self.last = info

    def summary(self):
        """What the last minute looked like; starts a new minute."""
        info = dict(self.last)
        info.pop("time", None)
        info.pop("caching", None)
        info.pop("paused", None)
        if self.down:
            info["down_s"] = self.down * FAST
        if info.get("playing"):
            info["caching_s"] = self.caching * FAST
            info["frozen_s"] = self.frozen * FAST
        uv = self.uv * FAST
        self.reset()
        return info, uv


# --------------------------------------------------------------------------
# the log


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def short_time(ts, with_day=True):
    return time.strftime("%a %d %b %H:%M" if with_day else "%H:%M", time.localtime(ts))


def archives():
    """Gzipped vitals, oldest first."""
    return sorted(glob.glob(os.path.join(VITALS_DIR, "vitals-*.jsonl.gz")))


def rotate(now=None):
    """Gzip the log once it's ROTATE_AT big; drop the oldest archives beyond ARCHIVE_MAX."""
    try:
        if os.path.getsize(VITALS_LOG) < ROTATE_AT:
            return False
    except OSError:
        return False
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now or time.time()))
    dest = os.path.join(VITALS_DIR, f"vitals-{stamp}.jsonl.gz")
    tmp = dest + ".tmp"
    with open(VITALS_LOG, "rb") as src, gzip.open(tmp, "wb") as out:
        shutil.copyfileobj(src, out)
    with open(tmp, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    os.remove(VITALS_LOG)
    B.fsync_dir(VITALS_DIR)
    old = archives()
    total = sum(os.path.getsize(p) for p in old)
    while old and total > ARCHIVE_MAX:
        total -= os.path.getsize(old[0])
        os.remove(old.pop(0))
    return True


def _parse_lines(lines):
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue               # a line cut short by a power cut
    return out


def read_records(since=None, limit=None):
    """Vitals records, oldest first: the newest `limit`, or all since `since` (epoch)."""
    chunks = []
    files = [VITALS_LOG] + archives()[::-1]      # newest first
    have = 0
    for path in files:
        try:
            if path.endswith(".gz"):
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    recs = _parse_lines(fh)
            else:
                with open(path, encoding="utf-8") as fh:
                    recs = _parse_lines(fh)
        except (OSError, EOFError):
            continue
        chunks.append(recs)
        have += len(recs)
        if since is not None and recs and recs[0].get("ts", 0) <= since:
            break
        if since is None and limit is not None and have >= limit:
            break
    records = [r for chunk in reversed(chunks) for r in chunk]
    if since is not None:
        records = [r for r in records if r.get("ts", 0) >= since]
    if limit is not None:
        records = records[-limit:]
    return records


def records_newest_first():
    for path in [VITALS_LOG] + archives()[::-1]:
        try:
            if path.endswith(".gz"):
                with gzip.open(path, "rt", encoding="utf-8") as fh:
                    recs = _parse_lines(fh)
            else:
                with open(path, encoding="utf-8") as fh:
                    recs = _parse_lines(fh)
        except (OSError, EOFError):
            continue
        yield from reversed(recs)


# --------------------------------------------------------------------------
# boot forensics


def previous_boot(current):
    """The last vitals record of the boot before this one, plus the TV state
    and under-voltage seen in its last 10 minutes."""
    last = None
    recent = []
    for rec in records_newest_first():
        if rec.get("boot") == current:
            continue
        if last is None:
            last = rec
        elif rec.get("boot") != last.get("boot") or last.get("ts", 0) - rec.get("ts", 0) > 600:
            break
        recent.append(rec)
        if len(recent) >= 12:
            break
    if last is None:
        return None
    uv = sorted({f for r in recent for f in r.get("thr_flags", []) if f.startswith("under-voltage")})
    return {"last": last, "uv_recent": uv, "uv_s_recent": sum(r.get("uv_s", 0) for r in recent)}


def journal_tail(n=JOURNAL_TAIL):
    rc, out = B.run(["journalctl", "-b", "-1", "-n", str(n), "-o", "short-iso", "--no-pager", "-q"], timeout=30)
    if rc != 0:
        return []
    return [re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "<url>", line)[:300] for line in out.splitlines()]


def describe_prev(last):
    kodi = last.get("kodi") or {}
    if kodi.get("playing"):
        what = f"playing {kodi.get('ch') + ' ' if kodi.get('ch') else ''}'{kodi['playing']}'"
    elif kodi.get("up") is False:
        what = "Kodi not running"
    else:
        what = "nothing playing"
    tv = last.get("tv") or "unknown"
    return f"{what}, TV {tv}"


def hm(seconds):
    seconds = int(max(0, seconds))
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def classify(prev, reset):
    """Why the box started, from how the previous boot ended and the reset register."""
    if prev is None:
        return "no-data"
    end = prev["last"].get("event")
    if end in ("reboot", "poweroff", "halt", "kexec", "shutdown"):
        return "clean"
    if reset == "watchdog":
        return "hang"              # it stopped mid-run and the hardware watchdog reset it
    if reset == "power-on":
        return "power-loss"
    return "unexpected"


CAUSE_TEXT = {
    "clean": "Clean restart",
    "hang": "Hang or crash: the watchdog reset it",
    "power-loss": "Power lost",
    "unexpected": "Unexpected restart",
    "no-data": "Booted",
}


def build_boot_record(now=None):
    now = now or time.time()
    cur = B.boot_id()
    booted_at = now - B.uptime()
    rsts = read_rsts()
    reset = decode_rsts(rsts)
    prev = previous_boot(cur[:8])
    cause = classify(prev, reset)
    rec = {
        "boot": cur[:8], "boot_id": cur, "booted_at": int(booted_at), "booted": iso(booted_at),
        "rsts": None if rsts is None else hex(rsts), "reset": reset, "cause": cause,
        # the TV's state when the Pi came back: on while the Pi was off -> only
        # the Pi lost power; on before, standby now -> the TV lost power as well
        "tv_after": B.read_tv_power() or "unknown", "hdmi_after": hdmi_status(),
    }
    if prev:
        last = prev["last"]
        rec["prev"] = {
            "boot": last.get("boot"), "last_seen": last.get("ts"), "last_seen_iso": last.get("t"),
            "up_s": last.get("up"), "end": last.get("event") or "stopped mid-run",
            "doing": describe_prev(last), "kodi": last.get("kodi"), "tv": last.get("tv"),
            "hdmi": last.get("hdmi"), "thr_flags": last.get("thr_flags"), "temp": last.get("temp"),
            "volts": last.get("volts"), "wifi": last.get("wifi"), "soak": last.get("soak"),
            "uv_recent": prev["uv_recent"], "uv_s_recent": prev["uv_s_recent"],
        }
        rec["off_s"] = int(booted_at - (last.get("ts") or booted_at))
    rec["summary"] = boot_summary(rec)
    rec["journal_tail"] = journal_tail()
    return rec


def boot_summary(rec):
    cause = rec["cause"]
    prev = rec.get("prev")
    back = short_time(rec["booted_at"])
    if not prev:
        return f"Booted {back} (reset: {rec['reset']}); no vitals from the boot before."
    seen = prev.get("last_seen") or rec["booted_at"]
    uv = "under-voltage before: " + (", ".join(prev["uv_recent"]) or ", ".join(
        f for f in prev.get("thr_flags") or [] if f.startswith("under-voltage")) or "none")
    if cause == "clean":
        return (f"Clean {prev['end']} at {short_time(seen)} ({prev['doing']}); "
                f"back {back}, down {hm(rec.get('off_s', 0))}.")
    head = {"power-loss": "Power lost", "hang": "Froze/crashed",
            "unexpected": "Stopped unexpectedly"}[cause]
    tv = f"TV {prev.get('tv') or 'unknown'} before, {rec.get('tv_after', 'unknown')} at return"
    return (f"{head} ~{short_time(seen)} (last sign of life; {prev['doing']}). "
            f"Back {back}, off {hm(rec.get('off_s', 0))}; {tv}. Reset: {rec['reset']}"
            f"{' ' + rec['rsts'] if rec.get('rsts') else ''}; {uv}.")


class _Lock:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a")
        if fcntl:
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        self.fh.close()


def last_boot_record():
    try:
        with open(BOOTS_LOG, encoding="utf-8") as fh:
            recs = _parse_lines(fh.readlines()[-20:])
    except OSError:
        return None
    return recs[-1] if recs else None


def read_boot_records(n=None):
    try:
        with open(BOOTS_LOG, encoding="utf-8") as fh:
            recs = _parse_lines(fh)
    except OSError:
        return []
    return recs[-n:] if n else recs


def ensure_boot_record():
    """This boot's record, written the first time it's asked for once the clock
    is right (and babuad has heard from the TV, or 5 minutes have passed).
    None until then. Never raises."""
    try:
        cur = B.boot_id()
        with _Lock(BOOTS_LOG + ".lock"):
            last = last_boot_record()
            if last and last.get("boot_id") == cur:
                return last
            if not B.clock_synced():
                return None
            if B.read_tv_power() is None and B.uptime() < 300:
                return None        # give babuad a few minutes to hear from the TV first
            rec = build_boot_record()
            B.append_line(BOOTS_LOG, json.dumps(rec, ensure_ascii=False, sort_keys=True))
            B.log("boot record:", rec["summary"])
            return rec
    except Exception as exc:  # noqa: BLE001 - forensics must never break the caller
        B.log("boot record failed:", repr(exc))
        return None


def boot_alert(rec):
    """(title, message, priority, tags) for health's once-per-boot alert."""
    if not rec:
        return None
    cause = rec.get("cause")
    title = {"power-loss": "Restarted after power loss", "hang": "Restarted after a hang",
             "unexpected": "Restarted unexpectedly"}.get(cause, "Booted")
    prio = 4 if cause in ("power-loss", "hang", "unexpected") else 2
    tags = "electric_plug" if cause != "hang" else "warning"
    return title, rec.get("summary", ""), prio, tags


# --------------------------------------------------------------------------
# Kodi's previous log: Kodi renames kodi.log to kodi.old.log at every start,
# and the one before that is gone. Keep each one, gzipped.


def archive_kodi_log():
    try:
        st = os.stat(KODI_OLD_LOG)
    except OSError:
        return None
    os.makedirs(KODI_ARCHIVE, exist_ok=True)
    marker = os.path.join(KODI_ARCHIVE, ".last")
    key = f"{int(st.st_mtime)} {st.st_size}"
    if read_file(marker) == key:
        return None
    dest = os.path.join(KODI_ARCHIVE, time.strftime("kodi-%Y%m%d-%H%M%S.log.gz", time.localtime(st.st_mtime)))
    with open(KODI_OLD_LOG, "rb") as src, gzip.open(dest + ".tmp", "wb") as out:
        shutil.copyfileobj(src, out)
    os.replace(dest + ".tmp", dest)
    B.write_atomic(marker, key)
    for old in sorted(glob.glob(os.path.join(KODI_ARCHIVE, "kodi-*.log.gz")))[:-KODI_LOGS_KEEP]:
        os.remove(old)
    return dest


# --------------------------------------------------------------------------
# the daemon


class Vitals:
    def __init__(self):
        self.kodi = KodiProbe()
        self.prev_cpu = cpu_times()
        self.prev_net = None
        self.prev_net_at = None
        self.prev_failed = None
        self.kodi_pid = None
        self.prev_kodi_cpu = None
        self.boot = B.boot_id()[:8]
        self.stop = threading.Event()

    def record(self, event=None, quick=False):
        """One vitals line. quick: leave out the network probes (at shutdown)."""
        now = time.time()
        thr = read_throttled()
        kodi, uv_s = self.kodi.summary()
        rec = {
            "t": iso(now), "ts": int(now), "up": int(B.uptime()), "boot": self.boot,
            "thr": None if thr is None else hex(thr), "thr_flags": decode_throttled(thr), "uv_s": uv_s,
            "temp": read_temp(), "volts": read_volts(),
            "arm_mhz": read_mhz("arm"), "core_mhz": read_mhz("core"),
            "load": loadavg(), "mem_mb": mem_available_mb(),
            "kodi": kodi, "tv": B.read_tv_power() or "unknown", "hdmi": hdmi_status(),
            "free_mb": disk_free_mb(),
        }
        if not B.clock_synced():
            rec["clock"] = "unset"
        if event:
            rec["event"] = event
        cpu = cpu_times()
        if cpu and self.prev_cpu and cpu[1] > self.prev_cpu[1]:
            rec["cpu_pct"] = round(100 * (cpu[0] - self.prev_cpu[0]) / (cpu[1] - self.prev_cpu[1]))
        self.prev_cpu = cpu
        pid = find_pid("kodi.bin")
        usage = proc_usage(pid) if pid else None
        if usage:
            rec["kodi_mb"] = usage[0]
            if pid == self.kodi_pid and self.prev_kodi_cpu is not None:
                ticks = os.sysconf("SC_CLK_TCK") * INTERVAL
                rec["kodi_cpu_pct"] = round(100 * (usage[1] - self.prev_kodi_cpu) / ticks)
            self.kodi_pid, self.prev_kodi_cpu = pid, usage[1]
        iface, gw = default_route()
        w = wifi(iface or "wlan0") if (iface or "wlan0").startswith("wl") else None
        if w:
            failed = w.pop("tx_failed_total", None)
            if failed is not None and self.prev_failed is not None:
                w["tx_failed"] = failed - self.prev_failed
            self.prev_failed = failed
            rec["wifi"] = w
        nb = net_bytes(iface) if iface else None
        if nb and self.prev_net and self.prev_net_at:
            secs = max(1.0, now - self.prev_net_at)
            rec["rx_kbps"] = round((nb[0] - self.prev_net[0]) * 8 / secs / 1000)
            rec["tx_kbps"] = round((nb[1] - self.prev_net[1]) * 8 / secs / 1000)
        self.prev_net, self.prev_net_at = nb, now
        if not quick:
            rec["ping_gw"] = ping(gw)
            rec["ping_inet"] = ping("1.1.1.1")
            rec["tailscale"] = tailscale_state()
        soak = B.soak_active()
        if soak:
            rec["soak"] = soak.get("channel")
        return rec

    def write(self, rec):
        B.append_line(VITALS_LOG, json.dumps(rec, ensure_ascii=False, separators=(",", ":")))

    def housekeeping(self):
        for job in (rotate, archive_kodi_log, ensure_boot_record):
            try:
                job()
            except Exception as exc:  # noqa: BLE001
                B.log(f"vitals {job.__name__} failed:", repr(exc))

    def run(self):
        os.makedirs(VITALS_DIR, exist_ok=True)
        self.kodi.sample()
        self.write(self.record("start"))
        self.housekeeping()
        next_minute = (int(time.time()) // INTERVAL + 1) * INTERVAL
        while not self.stop.is_set():
            wait = min(FAST, max(0.5, next_minute - time.time()))
            if self.stop.wait(wait):
                break
            self.kodi.sample()
            if time.time() >= next_minute - 1:
                next_minute = (int(time.time()) // INTERVAL + 1) * INTERVAL
                try:
                    self.write(self.record())
                except Exception as exc:  # noqa: BLE001 - one bad reading must not stop the log
                    B.log("vitals record failed:", repr(exc))
                self.housekeeping()
        # stopped: say how, so the next boot can tell a shutdown from a power cut
        ending = system_ending()
        try:
            self.write(self.record(ending or "stop", quick=True))
        except Exception as exc:  # noqa: BLE001
            B.log("vitals final record failed:", repr(exc))
        self.kodi.close()


def main():
    if "--once" in sys.argv:
        v = Vitals()
        v.kodi.sample()
        print(json.dumps(v.record(), ensure_ascii=False, indent=1))
        return 0
    if "--boot-record" in sys.argv:
        rec = ensure_boot_record()
        print(rec["summary"] if rec else "clock not set yet")
        return 0
    B.ensure_dirs()
    v = Vitals()
    signal.signal(signal.SIGTERM, lambda *a: v.stop.set())
    signal.signal(signal.SIGINT, lambda *a: v.stop.set())
    v.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
