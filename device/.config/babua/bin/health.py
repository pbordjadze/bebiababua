#!/usr/bin/env python3
"""Health check + self-repair, every 5 minutes (babua-health.timer).

Each check yields ok/problem. ntfy is only used on transitions (problem
appears / clears) so a long outage produces two messages, not hundreds.
A heartbeat URL (Uptime Kuma push / healthchecks.io) is pinged every run so
the homelab notices when the box goes silent altogether.

  health.py            run checks, repair, notify
  health.py --print    run checks and print them (no repair, no notify)
"""

import os
import shutil
import socket
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import babua_lib as B  # noqa: E402

STATE_PATH = os.path.join(B.STATE_DIR, "health.json")
PLAYLIST_STATUS = os.path.join(B.STATE_DIR, "playlist.json")
PLAYER_STATE = os.path.join(B.STATE_DIR, "player.json")


def check_internet(cfg):
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)):
        try:
            socket.create_connection((host, port), timeout=4).close()
            break
        except OSError:
            continue
    else:
        return False, "no route to the internet"
    try:
        socket.getaddrinfo("libreelec.tv", 443)
    except OSError:
        return False, "internet reachable but DNS fails"
    return True, "ok"


# Their router unreachable this many 5-minute runs in a row: restart Wi-Fi, then
# reboot (a stuck Wi-Fi driver or network stack otherwise needs someone to unplug
# the Pi). Router fine but no internet = their ISP: nothing to repair here.
WIFI_RESTART_AFTER = 3          # ~15 min
REBOOT_AFTER = 9                # ~45 min
REBOOT_MIN_UPTIME = 6 * 3600    # at most one such reboot per 6 h


def default_gateway():
    try:
        with open("/proc/net/route") as fh:
            for line in fh.readlines()[1:]:
                f = line.split()
                if f[1] == "00000000" and int(f[3], 16) & 2:     # default route, RTF_GATEWAY
                    return socket.inet_ntoa(bytes.fromhex(f[2])[::-1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def uptime():
    try:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError):
        return 0.0


def check_router(cfg, state, repair):
    gw = default_gateway()
    ok = bool(gw) and B.run(["ping", "-c", "2", "-W", "3", gw], timeout=15)[0] == 0
    if ok:
        state["router_fails"] = 0
        return True, f"answers ({gw})"
    fails = state.get("router_fails", 0) + 1
    state["router_fails"] = fails
    what = f"router {gw} not answering" if gw else "no Wi-Fi connection"
    if repair and fails >= REBOOT_AFTER and uptime() > REBOOT_MIN_UPTIME:
        B.queue_notify(cfg, "Rebooting", f"{what} for {fails * 5} min; Wi-Fi restart didn't help.", 4, "warning")
        B.log(f"{what} for {fails * 5} min: rebooting")
        B.run(["systemctl", "reboot"])
        return False, f"{what} for {fails * 5} min; rebooting"
    if repair and fails % WIFI_RESTART_AFTER == 0:
        B.log(f"{what} for {fails * 5} min: restarting Wi-Fi")
        B.run(["systemctl", "restart", "iwd"], timeout=60)
        return False, f"{what} for {fails * 5} min; restarted Wi-Fi"
    return False, f"{what} ({fails}x)"


def check_babuad(cfg, state):
    rc, out = B.run(["systemctl", "show", "-p", "ActiveState", "-p", "NRestarts", "babuad"])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    restarts = int(props.get("NRestarts", "0") or 0)
    before = state.get("babuad_restarts", restarts)
    state["babuad_restarts"] = restarts
    if props.get("ActiveState") != "active":
        return False, f"babuad is {props.get('ActiveState', 'unknown')}"
    if restarts - before >= 3:
        return False, f"babuad keeps crashing ({restarts - before} restarts in 5 min)"
    rc, log = B.run(["journalctl", "-u", "babuad", "--since", "-6min", "-o", "cat", "--no-pager"])
    errors = [line for line in log.splitlines() if line.startswith("babuad error:")]
    if errors:
        return False, f"{len(errors)} error(s): {errors[-1][14:140]}"
    return True, "running"


def check_channels(cfg, state):
    n = B.kodi_try(lambda k: len(k.tv_channels()), default=None)
    if n is None:
        return True, "unknown (Kodi not answering)"   # the kodi check reports that
    if n == 0:
        # twice in a row: babuad's add-on reload after a playlist change empties the list for seconds
        state["no_channels"] = state.get("no_channels", 0) + 1
        if state["no_channels"] >= 2:
            return False, "Kodi has no TV channels (IPTV add-on off, or Kodi in safe mode?)"
        return True, "none right now (add-on reloading?)"
    state["no_channels"] = 0
    return True, f"{n} in Kodi"


def check_cec(cfg):
    path = os.path.join(B.RUN_DIR, "cec_ok")
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = uptime()             # /run is new at every boot
    if age > 24 * 3600:
        return False, "TV hasn't answered over HDMI-CEC for a day (Anynet+ off? cable?) - her remote won't work"
    return True, "TV answers"


def wg_handshake_age():
    rc, out = B.run(["wg", "show", "all", "latest-handshakes"])
    if rc != 0 or not out:
        return None
    stamps = [int(line.split()[-1]) for line in out.splitlines() if line.split()[-1].isdigit()]
    if not stamps or max(stamps) == 0:
        return float("inf")
    return time.time() - max(stamps)


def check_tailscale(cfg, repair):
    st = B.tailscale_status()
    if st is None:
        if repair:
            B.run(["systemctl", "restart", "babua-tailscale"], timeout=60)
            return False, "tailscaled not answering; restarted it"
        return False, "tailscaled not answering"
    state = st.get("BackendState")
    if state == "NeedsLogin":
        key = cfg.get("TAILSCALE_AUTHKEY")
        if not key:
            return False, "not logged in - run `babua tailscale-up <authkey>`"
        if repair:
            rc, out = B.tailscale_up(cfg, key)
            return False, "joining the tailnet" if rc == 0 else f"tailscale up failed: {out[-150:]}"
        return False, "not logged in yet"
    if state == "Stopped" and repair:
        # `tailscale down` (by hand, by accident) survives reboots: switch it back on
        rc, out = B.tailscale_up(cfg)
        return False, "tailscale was switched off; switched it back on" if rc == 0 else f"tailscale up failed: {out[-150:]}"
    if state != "Running":
        return False, f"tailscale state {state}"
    me = st.get("Self") or {}
    expiry = me.get("KeyExpiry")
    if expiry:
        try:
            left = time.mktime(time.strptime(expiry[:19], "%Y-%m-%dT%H:%M:%S")) - time.mktime(time.gmtime())
        except ValueError:
            left = None
        if left is not None and left < 14 * 86400:
            return False, f"node key expires {expiry[:10]} - disable key expiry for this machine in the Tailscale admin console"
    ips = ", ".join(me.get("TailscaleIPs") or [])
    return True, f"{me.get('DNSName', '').rstrip('.') or 'connected'} {ips}"


def check_vpn(cfg, repair):
    if B.tailscale_installed():
        ok, detail = check_tailscale(cfg, repair)
        if not cfg.get("WG_ENDPOINT") or not ok:
            return ok, detail
    if not cfg.get("WG_ENDPOINT"):
        return True, "not configured"
    age = wg_handshake_age()
    if age is not None and age < 300:
        return True, f"handshake {int(age)}s ago"
    detail = "tunnel down" if age is None else ("never handshaked" if age == float("inf") else f"last handshake {int(age // 60)} min ago")
    if repair:
        rc, out = B.run(["/bin/sh", os.path.join(os.path.dirname(os.path.abspath(__file__)), "babua-vpn"), "restart"], timeout=60)
        detail += "; reconnect attempted" if rc == 0 else f"; reconnect failed: {out[-120:]}"
    return False, detail


def check_kodi(cfg, state, repair):
    ok = B.kodi_try(lambda k: k.ping(), default=False)
    fails = 0 if ok else state.get("kodi_fails", 0) + 1
    state["kodi_fails"] = fails
    if ok:
        return True, "responding"
    if repair and fails >= 3:
        B.run(["systemctl", "restart", "kodi"], timeout=60)
        state["kodi_fails"] = 0
        return False, "not responding for 15 min; restarted Kodi"
    return False, f"not responding ({fails}x)"


def check_power(cfg):
    rc, out = B.run(["vcgencmd", "get_throttled"])
    if rc != 0 or "=" not in out:
        return True, "unknown"
    flags = int(out.split("=")[1], 16)
    problems = []
    if flags & 0x1:
        problems.append("UNDER-VOLTAGE NOW")
    elif flags & 0x10000:
        problems.append("under-voltage since boot")
    if flags & 0x4:
        problems.append("throttled now")
    if flags & 0x8 or flags & 0x80000:
        problems.append("soft temperature limit hit")
    if problems:
        return False, ", ".join(problems) + f" ({out.split('=')[1]}) - replace the power supply/cable"
    return True, "ok"


def check_temp(cfg):
    rc, out = B.run(["vcgencmd", "measure_temp"])
    try:
        temp = float(out.split("=")[1].split("'")[0])
    except (IndexError, ValueError):
        return True, "unknown"
    return (temp < 80, f"{temp:.0f}C")


def check_storage(cfg):
    free = shutil.disk_usage("/storage").free / 1e6
    return free > 300, f"{free:.0f} MB free"


def check_memory(cfg, repair):
    try:
        with open("/proc/meminfo") as fh:
            info = {line.split(":")[0]: int(line.split()[1]) for line in fh}
    except OSError:
        return True, "unknown"
    avail = info.get("MemAvailable", 0) // 1024
    if avail >= 60:
        return True, f"{avail} MB available"
    detail = f"only {avail} MB available"
    if repair and not B.kodi_try(lambda k: k.is_playing(), default=True):
        B.run(["systemctl", "restart", "kodi"], timeout=60)
        detail += "; restarted idle Kodi"
    return False, detail


def check_playlist(cfg):
    st = B.read_json(PLAYLIST_STATUS)
    if not st:
        return False, "playlist never built"
    age_h = (time.time() - st.get("last_full_success", 0)) / 3600
    problems = []
    if age_h > 26:
        failed = [f"{n}: {s.get('error', '?')}" for n, s in st.get("sources", {}).items() if not s.get("ok")]
        problems.append(f"provider playlist not refreshed for {age_h:.0f} h ({'; '.join(failed)})")
    if st.get("missing"):
        problems.append("not found upstream: " + ", ".join(st["missing"]))
    if st.get("streams_bad"):
        problems.append("streams down at last check: " + ", ".join(st["streams_bad"])
                        + (f" (hidden from her list until they work: {', '.join(st['hidden'])})"
                           if st.get("hidden") else ""))
    if problems:
        return False, " | ".join(problems)
    return True, f"{st.get('channels', '?')} channels"


def check_player(cfg):
    st = B.read_json(PLAYER_STATE)
    fail = st.get("failure")
    if fail:
        return False, f"'{fail['channel']}' gave up at {time.strftime('%H:%M', time.localtime(fail['at']))}"
    return True, "ok"


# Kodi dialogs that only appear when something went wrong (e.g. "playlist
# playback aborted"), by Window.IsActive() name. Not the channel list or the
# volume/seek bars grandma uses.
MESSAGE_DIALOGS = ("okdialog", "yesnodialog", "textviewer", "progressdialog")


def screen_dialog():
    """Text of a message dialog on screen, "" for one without text, None if none."""
    def probe(k):
        active = k.call("XBMC.GetInfoBooleans", {"booleans": [f"Window.IsActive({d})" for d in MESSAGE_DIALOGS]})
        names = [d for d in MESSAGE_DIALOGS if active.get(f"Window.IsActive({d})")]
        if not names:
            return None
        # with no window given, Control.GetLabel reads the topmost modal dialog:
        # 1 = heading, 9 = text (Estuary's DialogConfirm.xml)
        labels = k.call("XBMC.GetInfoLabels", {"labels": ["Control.GetLabel(1)", "Control.GetLabel(9)"]})
        text = " / ".join(v.strip() for v in (labels.get("Control.GetLabel(1)", ""),
                                              labels.get("Control.GetLabel(9)", "")) if v and v.strip())
        return text or names[0]
    return B.kodi_try(probe)


def check_screen(cfg, state):
    """A message dialog left on the TV: reported once it has stayed for a whole
    run (5-10 min), so one someone dismisses doesn't count."""
    text = screen_dialog()
    if text is None:
        state.pop("dialog_runs", None)
        return True, "no dialog"
    runs = state.get("dialog_runs", 0) + 1
    state["dialog_runs"] = runs
    if runs < 2:
        return True, f"dialog just appeared: {text}"[:300]
    return False, f"dialog on screen: {text}"[:300]


def heartbeat(cfg, problems):
    url = cfg.get("HEARTBEAT_URL")
    if not url:
        return
    if "{status}" in url or "{msg}" in url:
        msg = "OK" if not problems else ("; ".join(problems))[:200]
        url = url.replace("{status}", "up").replace("{msg}", urllib.parse.quote(msg))
    try:
        B.http_get(url, timeout=10, retries=0)
    except Exception as exc:  # noqa: BLE001
        B.log("heartbeat failed:", exc)


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


def main():
    printing = "--print" in sys.argv
    repair = not printing
    B.ensure_dirs()
    cfg = B.load_config()
    state = B.read_json(STATE_PATH)
    if state.get("boot_id") not in (None, boot_id()):
        state["kodi_fails"] = 0    # last boot's count: don't restart a Kodi that's still starting

    checks = {
        "internet": lambda: check_internet(cfg),
        "router": lambda: check_router(cfg, state, repair),
        "vpn": lambda: check_vpn(cfg, repair),
        "kodi": lambda: check_kodi(cfg, state, repair),
        "power": lambda: check_power(cfg),
        "temperature": lambda: check_temp(cfg),
        "storage": lambda: check_storage(cfg),
        "memory": lambda: check_memory(cfg, repair),
        "playlist": lambda: check_playlist(cfg),
        "playback": lambda: check_player(cfg),
        "screen": lambda: check_screen(cfg, state),
        "babuad": lambda: check_babuad(cfg, state),
        "channels": lambda: check_channels(cfg, state),
        "cec": lambda: check_cec(cfg),
    }
    results = {}
    for name, fn in checks.items():
        try:
            results[name] = fn()
        except Exception as exc:  # noqa: BLE001
            results[name] = (False, f"check crashed: {exc!r}")

    if printing:
        for name, (ok, detail) in results.items():
            print(f"{'OK ' if ok else 'BAD'}  {name:<12} {detail}")
        return 0

    now = int(time.time())
    alerts = []                    # sent after the state is saved, so a run killed mid-send can't repeat them
    if state.get("boot_id") != boot_id():
        last = state.get("last_run")
        gap = f"; previous check {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}" if last else ""
        rc, up = B.run(["uptime"])
        alerts.append(("Booted", f"Box started ({up}){gap}", 2, "electric_plug"))
        state["boot_id"] = boot_id()

    # internet down makes everything downstream noisy; report only that
    offline = not results["internet"][0]
    previous = state.get("problems", {})
    current = {}
    for name, (ok, detail) in results.items():
        if ok or (offline and name in ("vpn", "playlist")):
            continue
        current[name] = detail
    for name, detail in current.items():
        if name not in previous:
            prio = 4 if name in ("power", "kodi", "internet", "router", "babuad", "channels") else 3
            alerts.append((f"Problem: {name}", detail, prio, "warning"))
    for name in previous:
        if name not in current:
            alerts.append((f"Recovered: {name}", results[name][1] if name in results else "ok", 2, "white_check_mark"))
    state["problems"] = current
    state["last_run"] = now
    state["last_results"] = {k: {"ok": v[0], "detail": v[1]} for k, v in results.items()}
    B.write_json(STATE_PATH, state)

    send = B.notify if not offline else B.queue_notify   # offline: don't hang on DNS, queue
    for alert in alerts:
        send(cfg, *alert)
    if not offline:
        B.flush_outbox(cfg)
        heartbeat(cfg, list(current))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        B.log("health crashed:", repr(exc))
        sys.exit(1)
