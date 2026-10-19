"""Shared helpers for the babua scripts running on the LibreELEC box.

Stdlib only (LibreELEC ships Python 3.11 without pip). Everything here must
tolerate a missing network, a missing Kodi and a half-written config file:
the box is unattended, so a crash means a visit.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BABUA_DIR = os.environ.get("BABUA_DIR", "/storage/.config/babua")
STATE_DIR = os.environ.get("BABUA_STATE_DIR", "/storage/.cache/babua")
RUN_DIR = os.environ.get("BABUA_RUN_DIR", "/run/babua")
KODI_HOME = os.environ.get("KODI_HOME", "/storage/.kodi")
SCREENSHOT_DIR = os.environ.get("BABUA_SCREENSHOT_DIR", "/storage/screenshots")

PLAYLIST_PATH = os.path.join(STATE_DIR, "playlist.m3u")
RELOAD_FLAG = os.path.join(RUN_DIR, "reload_pending")
QUIET_FLAG = os.path.join(STATE_DIR, "quiet_boot")
AFTER_MEDIA = os.path.join(RUN_DIR, "after_media")   # "channel" | "blank", written by `babua show`
MEDIA_DIR = os.environ.get("BABUA_MEDIA_DIR", "/storage/babua-media")

# Add-ons the image ships unpacked in .kodi/addons. Kodi registers manually
# placed add-ons as *disabled*, so babuad enables them over JSON-RPC.
MANAGED_ADDONS = [
    "inputstream.adaptive",
    "inputstream.ffmpegdirect",
    "inputstream.rtmp",
    "pvr.iptvsimple",
    "resource.language.ru_ru",
]
PVR_ADDON = "pvr.iptvsimple"

TV_CHANNELS_WINDOW = 10700


def log(*parts):
    # journald adds timestamps; keep lines short and greppable.
    print(" ".join(str(p) for p in parts), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# config


def parse_env_file(path):
    """Parse a KEY=VALUE file (shell-ish, no expansion). Missing file -> {}."""
    values = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_config():
    """babua.conf (behaviour) overlaid with secrets.env (site specific)."""
    cfg = parse_env_file(os.path.join(BABUA_DIR, "babua.conf"))
    cfg.update(parse_env_file(os.path.join(BABUA_DIR, "secrets.env")))
    return cfg


def cfg_bool(cfg, key, default=False):
    value = cfg.get(key)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "yes", "true", "on")


def cfg_int(cfg, key, default):
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# state files


def ensure_dirs():
    for d in (STATE_DIR, RUN_DIR):
        os.makedirs(d, exist_ok=True)


def write_atomic(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {} if default is None else default


def write_json(path, obj, mode=0o644):
    write_atomic(path, json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n", mode)


# --------------------------------------------------------------------------
# Kodi JSON-RPC over the local TCP socket (services.esenabled, port 9090)


class KodiError(Exception):
    pass


class JsonStream:
    """Kodi's TCP API sends concatenated JSON objects with no delimiter."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = ""
        self.decoder = json.JSONDecoder()

    def read(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            obj = self._pop()
            if obj is not None:
                return obj
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("no message")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("kodi closed the connection")
            self.buf += chunk.decode("utf-8", "replace")

    def _pop(self):
        text = self.buf.lstrip()
        if not text:
            self.buf = ""
            return None
        try:
            obj, end = self.decoder.raw_decode(text)
        except ValueError:
            return None
        self.buf = text[end:]
        return obj


class Kodi:
    def __init__(self, host="127.0.0.1", port=9090, timeout=10):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.stream = None
        self.next_id = 1
        self.notifications = []

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.stream = JsonStream(self.sock)
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    def call(self, method, params=None, timeout=None):
        if self.sock is None:
            self.connect()
        req_id = self.next_id
        self.next_id += 1
        req = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        self.sock.sendall(json.dumps(req).encode("utf-8"))
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            msg = self.stream.read(max(0.1, deadline - time.monotonic()))
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise KodiError(f"{method}: {msg['error']}")
                return msg.get("result")
            if "method" in msg:
                self.notifications.append(msg)

    def read_notification(self, timeout):
        if self.notifications:
            return self.notifications.pop(0)
        return self.stream.read(timeout)

    # convenience wrappers -------------------------------------------------

    def ping(self):
        return self.call("JSONRPC.Ping", timeout=5) == "pong"

    def is_playing(self):
        return bool(self.call("Player.GetActivePlayers"))

    def playing_channel_id(self):
        players = self.call("Player.GetActivePlayers") or []
        for player in players:
            item = self.call("Player.GetItem", {"playerid": player["playerid"], "properties": []})
            item = (item or {}).get("item", {})
            if item.get("type") == "channel":
                return item.get("id")
        return None

    def tv_channels(self):
        res = self.call("PVR.GetChannels", {
            "channelgroupid": "alltv",
            "properties": ["channelnumber", "lastplayed", "hidden"],
        }, timeout=20) or {}
        return [c for c in res.get("channels", []) if not c.get("hidden")]

    def last_channel_id(self):
        channels = self.tv_channels()
        if not channels:
            return None
        played = [c for c in channels if c.get("lastplayed")]
        if played:
            return max(played, key=lambda c: c["lastplayed"])["channelid"]
        return min(channels, key=lambda c: c.get("channelnumber") or 9999)["channelid"]

    def play_channel(self, channel_id):
        return self.call("Player.Open", {"item": {"channelid": channel_id}}, timeout=30)

    def setting(self, key):
        return self.call("Settings.GetSettingValue", {"setting": key}).get("value")

    def set_setting(self, key, value):
        return self.call("Settings.SetSettingValue", {"setting": key, "value": value})

    def addon_enabled(self, addon_id):
        try:
            res = self.call("Addons.GetAddonDetails", {"addonid": addon_id, "properties": ["enabled"]})
        except KodiError:
            return None  # not installed
        return res["addon"]["enabled"]

    def set_addon_enabled(self, addon_id, enabled):
        return self.call("Addons.SetAddonEnabled", {"addonid": addon_id, "enabled": enabled}, timeout=30)

    def notify(self, title, message, ms=6000):
        return self.call("GUI.ShowNotification", {"title": title, "message": message, "displaytime": ms})


def kodi_try(fn, default=None):
    """Run fn(kodi) against a fresh connection; return default if Kodi is down."""
    try:
        with Kodi() as kodi:
            return fn(kodi)
    except (OSError, KodiError, ValueError) as exc:
        log("kodi unavailable:", exc)
        return default


# --------------------------------------------------------------------------
# Kodi settings we enforce (applied to guisettings.xml before Kodi starts and
# over JSON-RPC once it runs). Keep this the single source of truth.


def kodi_settings(cfg):
    language = cfg.get("KODI_LANGUAGE", "resource.language.ru_ru")
    settings = {
        "locale.language": language,
        # Estuary's default font has no Georgian glyphs; "Arial" (DejaVu based)
        # covers Georgian, Cyrillic and Latin.
        "lookandfeel.font": "Arial",
        "lookandfeel.startupwindow": TV_CHANNELS_WINDOW,
        "lookandfeel.startupaction": 1,            # play last TV channel
        "lookandfeel.enablerssfeeds": False,
        "pvrplayback.confirmchannelswitch": False, # up/down switches at once
        "pvrplayback.signalquality": False,
        "pvrmanager.usebackendchannelnumbers": True,
        "pvrmanager.preselectplayingchannel": True,
        "pvrmenu.displaychannelinfo": 3,           # seconds of channel banner
        "screensaver.mode": "screensaver.xbmc.builtin.dim",
        "screensaver.time": 3,
        "general.addonupdates": 2,                 # never auto-update; I update deliberately
        "general.addonnotifications": False,
        "eventlog.enablednotifications": False,
        "addons.unknownsources": False,
        "services.webserver": bool(cfg.get("KODI_WEB_PASSWORD")),
        "services.webserverauthentication": True,
        "services.webserverusername": "kodi",
        "services.webserverpassword": cfg.get("KODI_WEB_PASSWORD", ""),
        "services.esenabled": True,                # local JSON-RPC used by babuad
        "debug.screenshotpath": SCREENSHOT_DIR + "/",
    }
    return settings


# CEC behaviour (peripheral_data/cec_*.xml, only read at Kodi start).
# Values are Kodi localisation ids, see system/peripherals.xml.
CEC_SETTINGS = {
    "enabled": "1",
    "activate_source": "0",              # never grab the TV input at (re)boot
    "wake_devices": "231",               # never power the TV on (weekly reboot at night)
    "standby_devices": "231",            # never power the TV off
    "standby_tv_on_pc_standby": "0",
    "send_inactive_source": "0",
    "use_tv_menu_language": "0",         # else Kodi follows the TV's (English?) menu language
    "standby_pc_on_tv_standby": "36045", # TV off -> stop playback (default would be "suspend")
    "pause_or_stop_playback_on_deactivate": "36045",  # other input selected -> stop
    "cec_wake_screensaver": "1",
}


TAILSCALE_DIR = os.path.join(BABUA_DIR, "tailscale")
TAILSCALE_SOCKET = "/run/tailscale/tailscaled.sock"


def tailscale_installed():
    return os.path.exists(os.path.join(TAILSCALE_DIR, "tailscale"))


def tailscale(*args, timeout=30):
    return run([os.path.join(TAILSCALE_DIR, "tailscale"), f"--socket={TAILSCALE_SOCKET}", *args], timeout=timeout)


def tailscale_status():
    """`tailscale status --json` as a dict, or None if tailscaled isn't answering."""
    rc, out = tailscale("status", "--json")
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def tailscale_up(cfg, authkey):
    """Join the tailnet. DNS and firewall handling stay with LibreELEC."""
    hostname = cfg.get("HOSTNAME") or socket.gethostname()
    return tailscale("up", f"--authkey={authkey}", f"--hostname={hostname}",
                     "--accept-dns=false", "--netfilter-mode=off", timeout=90)


def wireguard_config(cfg):
    """connman WireGuard provider file (.config/wireguard/babua.config)."""
    lines = [
        "[provider_wireguard]",
        "Type = WireGuard",
        "Name = babua homelab",
        f"Host = {cfg['WG_ENDPOINT']}",
        f"WireGuard.Address = {cfg['WG_ADDRESS']}",
        f"WireGuard.PrivateKey = {cfg['WG_PRIVATE_KEY']}",
        f"WireGuard.PublicKey = {cfg['WG_SERVER_PUBLIC_KEY']}",
        f"WireGuard.AllowedIPs = {cfg['WG_ALLOWED_IPS']}",
        f"WireGuard.EndpointPort = {cfg.get('WG_ENDPOINT_PORT', '51820')}",
        "WireGuard.PersistentKeepalive = 25",
    ]
    if cfg.get("WG_PRESHARED_KEY"):
        lines.append(f"WireGuard.PresharedKey = {cfg['WG_PRESHARED_KEY']}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# notifications (ntfy) with an on-disk outbox so alerts raised while the
# internet is down are delivered once it returns.

OUTBOX = os.path.join(STATE_DIR, "outbox.jsonl")


def _post_ntfy(cfg, msg):
    url = cfg.get("NTFY_URL")
    if not url:
        return True
    headers = {
        "Title": msg["title"].encode("ascii", "replace").decode(),
        "Priority": str(msg.get("priority", 3)),
        "Tags": msg.get("tags", ""),
    }
    token = cfg.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = msg["message"]
    if msg.get("queued_at"):
        body += "\n(queued " + time.strftime("%Y-%m-%d %H:%M", time.localtime(msg["queued_at"])) + ")"
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError) as exc:
        log("ntfy failed:", exc)
        return False


def notify(cfg, title, message, priority=3, tags=""):
    """Send now, or queue for later. Never raises."""
    host = cfg.get("HOSTNAME") or socket.gethostname()
    msg = {"title": f"[{host}] {title}", "message": message, "priority": priority, "tags": tags}
    log("notify:", title, "-", message.replace("\n", " | "))
    try:
        flush_outbox(cfg)
        if not _post_ntfy(cfg, msg):
            msg["queued_at"] = int(time.time())
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(OUTBOX, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - alerting must never kill the caller
        log("notify error:", exc)


def flush_outbox(cfg):
    try:
        with open(OUTBOX, encoding="utf-8") as fh:
            pending = [json.loads(line) for line in fh if line.strip()]
    except (FileNotFoundError, ValueError):
        return
    pending = pending[-50:]
    remaining = []
    for i, msg in enumerate(pending):
        if not _post_ntfy(cfg, msg):
            remaining = pending[i:]
            break
    if remaining:
        write_atomic(OUTBOX, "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in remaining))
    else:
        try:
            os.remove(OUTBOX)
        except FileNotFoundError:
            pass


def http_get(url, timeout=30, user_agent=None, retries=2):
    headers = {"User-Agent": user_agent or "Mozilla/5.0 (babua; LibreELEC) Kodi/21"}
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise last


def redact(url):
    """Hide credentials before URLs reach logs or notifications. Providers put
    them in the query (get.php?username=..) or the path (/live/USER/PASS/1.ts),
    so keep only the host and the last path segment."""
    try:
        parts = urllib.parse.urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
    except ValueError:
        return "<url>"
    last = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return f"{parts.scheme}://{netloc}/…/{last}" + ("?…" if parts.query else "")


def run(cmd, timeout=30):
    """Run a command, return (rc, stdout). Never raises for missing binaries."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
