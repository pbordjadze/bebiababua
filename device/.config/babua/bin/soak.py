#!/usr/bin/env python3
"""Overnight soak test of the provider's streams, network only (babua-soak.timer).

Fetches a channel the way a player does (HLS playlist reloads, every segment),
throws the data away and logs what the provider does: bytes, bitrate per
minute, where a player would have stalled, HTTP errors, drops and reconnects,
and when and why each run ended. Nothing appears on the TV; Kodi isn't used.

One provider connection at a time. It stops at once, for the rest of the
night, when Kodi starts playing, the screen wakes (TV on, any remote key) or
babuad sees the TV on; babuad also stops it before opening a channel, and the
playlist rebuild and the daily stream check wait while it runs.

Results, without URLs: /storage/.cache/babua/soak/<date>_<time>/events.jsonl
and summary.txt.

  soak.py run [--channels 2,1,3] [--first-min 90] [--each-min 15] [--until 06:25] [--budget-gb 9]
  soak.py scheduled            tonight's entry in soak/schedule.json, if any (the timer: 01:00,
                               or 02:00/03:00 if someone was watching)
  soak.py schedule DATE [run options]   DATE: the morning it runs, YYYY-MM-DD
  soak.py unschedule DATE
  soak.py report [DIR]         summary of a run (default: the latest)
  soak.py status | stop
"""

import argparse
import http.client
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import babua_lib as B  # noqa: E402
import playlist as P  # noqa: E402

SOAK_DIR = os.path.join(B.STATE_DIR, "soak")
SCHEDULE = os.path.join(SOAK_DIR, "schedule.json")
UA = "Mozilla/5.0 (babua; LibreELEC) Kodi/21"
DEFAULT_CHANNELS = "2,1,3,4,5,6,7,8,9,10,11,12"   # the Georgian channels, Rustavi 2 first
POLL = 3                       # seconds between "is anyone watching?" checks (Kodi events: at once)
KODI_GONE = 60                 # Kodi silent this long: stop, nobody can tell if she's watching
READ_TIMEOUT = 15              # no bytes for this long: the request failed
CHUNK = 65536
LIVE_START = 3                 # a player starts this many segments behind the live end (ffmpeg)
RECONNECT_DELAYS = (3, 10, 30, 60)   # like babuad's retries, then once a minute
STUCK_GIVE_UP = 120            # no new segment for this long: reopen, as a player would
SEGMENT_ERRORS_GIVE_UP = 3     # failed segments in a row: reopen
BETWEEN_CHANNELS = 5
PLAYLIST_RETRY = 2             # seconds before retrying a failed playlist request (twice)
WAKE_EVENTS = ("Player.OnPlay", "Player.OnAVStart", "Player.OnResume", "GUI.OnScreensaverDeactivated",
               "System.OnWake", "Input.OnInputRequested")


def clean(text, n=160):
    """Error text without URLs (they carry the provider's credentials)."""
    return re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "<url>", str(text))[:n]


class Abort(Exception):
    """Someone may be watching, it's time, or we were told to stop: end the night."""


class StreamError(Exception):
    def __init__(self, kind, status, reason):
        super().__init__(f"{kind}: {status or ''} {reason}")
        self.kind, self.status, self.reason = kind, status, clean(reason)

    def code(self):
        return str(self.status) if self.status else self.reason.split(":")[0][:40]


# --------------------------------------------------------------------------
# the log


class Log:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.path = os.path.join(folder, "events.jsonl")
        self.writing = False

    def __call__(self, ev, **fields):
        rec = {"t": time.strftime("%H:%M:%S"), "ts": round(time.time(), 1), "ev": ev}
        rec.update(fields)
        self.writing = True
        try:
            B.append_line(self.path, json.dumps(rec, ensure_ascii=False))
        finally:
            self.writing = False
        return rec


# --------------------------------------------------------------------------
# is anyone watching?


class Guard:
    def __init__(self, until):
        self.until = until
        self.event = threading.Event()
        self.reason = None
        self.kodi_seen = time.time()
        threading.Thread(target=self.watch, daemon=True).start()

    def trip(self, reason):
        if not self.event.is_set():
            self.reason = reason
            self.event.set()

    def check(self):
        if self.event.is_set():
            raise Abort(self.reason)

    def sleep(self, seconds):
        if seconds > 0 and self.event.wait(seconds):
            raise Abort(self.reason)
        self.check()

    def other_checks(self):
        if time.time() >= self.until:
            self.trip("time is up")
        tv = B.read_tv_power()
        if tv in ("on", "to-on"):
            self.trip(f"the TV is {tv}")
        if time.time() - self.kodi_seen > KODI_GONE:
            self.trip("Kodi isn't answering, so nobody can tell whether she's watching")

    def watch(self):
        """Kodi's events on one connection (instant), plus a poll every few seconds."""
        while not self.event.is_set():
            kodi = None
            try:
                kodi = B.Kodi(timeout=5).connect()
                next_poll = 0.0
                while not self.event.is_set():
                    if time.monotonic() >= next_poll:
                        next_poll = time.monotonic() + POLL
                        if kodi.is_playing():
                            self.trip("Kodi started playing")
                        self.kodi_seen = time.time()
                        self.other_checks()
                    try:
                        msg = kodi.read_notification(0.5)
                    except socket.timeout:
                        continue
                    if msg.get("method") in WAKE_EVENTS:
                        self.trip(f"Kodi: {msg['method']}")
            except (OSError, ValueError, B.KodiError):
                self.other_checks()
                self.event.wait(2)
            finally:
                if kodi:
                    kodi.close()


def nobody_watching():
    """None if it's fine to start, else why not."""
    tv = B.read_tv_power()
    if tv is None:
        return "babuad hasn't reported the TV's state (can't tell if it's on)"
    if tv in ("on", "to-on"):
        return f"the TV is {tv}"
    try:
        with B.Kodi(timeout=5) as k:
            if k.is_playing():
                return "something is playing"
    except (OSError, ValueError, B.KodiError):
        return "Kodi isn't answering"
    if B.soak_active():
        return "a soak test is already running"
    return None


# --------------------------------------------------------------------------
# HLS


def parse_media_playlist(text):
    """-> (target_duration, [(seq, duration, uri)], endlist)"""
    target, seq, dur, endlist, segments = 10, 0, None, False, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = max(1, int(float(line.split(":", 1)[1])))
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0] or 0)
        elif line.startswith("#EXT-X-ENDLIST"):
            endlist = True
        elif line and not line.startswith("#"):
            segments.append((seq + len(segments), dur if dur is not None else target, line))
            dur = None
    return target, segments, endlist


def pick_variant(text):
    """Master playlist -> (uri, bandwidth, resolution) of the best variant
    (what Kodi picks with no bandwidth limit), or None for a media playlist."""
    best, info = None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            bw = re.search(r"BANDWIDTH=(\d+)", line)
            res = re.search(r"RESOLUTION=(\d+x\d+)", line)
            info = (int(bw.group(1)) if bw else 0, res.group(1) if res else None)
        elif line and not line.startswith("#") and info:
            if best is None or info[0] > best[1]:
                best = (line, info[0], info[1])
            info = None
    return best


class Player:
    """When would a player have run dry? Playback starts when the first
    segment is in and then runs in real time; a stall lasts until the next
    segment arrives."""

    def __init__(self, log, ch):
        self.log, self.ch = log, ch
        self.started = None
        self.media = 0.0
        self.stalled = 0.0
        self.dry_since = None
        self.stalls = []

    def position(self, now):
        return now - self.started - self.stalled

    def update(self, now):
        if self.started is not None and self.dry_since is None and self.position(now) >= self.media:
            self.dry_since = self.started + self.stalled + self.media

    def add(self, seconds, now):
        if self.started is None:
            self.started = now
            self.media = seconds
            return
        self.update(now)
        if self.dry_since is not None:
            gap = now - self.dry_since
            self.stalled += gap
            self.stalls.append(gap)
            self.log("stall", ch=self.ch, secs=round(gap, 1),
                     began=time.strftime("%H:%M:%S", time.localtime(self.dry_since)))
            self.dry_since = None
        self.media += seconds

    def buffer(self, now):
        if self.started is None:
            return 0.0
        self.update(now)
        return 0.0 if self.dry_since is not None else round(self.media - self.position(now), 1)


class ChannelRun:
    def __init__(self, number, name, url, minutes, guard, log, night):
        self.number, self.name, self.url, self.minutes = number, name, url, minutes
        self.guard, self.log, self.night = guard, log, night
        self.bytes = 0
        self.tick_bytes = 0
        self.tick_at = time.time()
        self.tick_rates = []
        self.reconnects = 0
        self.sessions = 0

    # one request, read in chunks so a stop is noticed within milliseconds
    def get(self, url, kind, keep):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        started = time.monotonic()
        parts, n = [], 0
        try:
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
                status, final = resp.status, resp.geturl()
                while True:
                    self.guard.check()
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    n += len(chunk)
                    self.tick_bytes += len(chunk)
                    if keep:
                        parts.append(chunk)
                        if n > 2_000_000:
                            raise StreamError(kind, None, "playlist larger than 2 MB")
        except urllib.error.HTTPError as exc:
            raise StreamError(kind, exc.code, exc.reason) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            raise StreamError(kind, None, getattr(exc, "reason", None) or exc) from None
        finally:
            self.bytes += n
            self.night["bytes"] += n
        return status, final, (b"".join(parts) if keep else None), n, time.monotonic() - started

    def get_playlist(self, url, kind):
        for attempt in range(3):   # a player retries a playlist reload before giving up
            try:
                return self.get(url, kind, keep=True)
            except StreamError as exc:
                if attempt == 2:
                    raise
                self.log("error", ch=self.number, kind=exc.kind, status=exc.status, reason=exc.reason,
                         retry=attempt + 1)
                self.guard.sleep(PLAYLIST_RETRY)

    def tick(self, player, force=False):
        now = time.time()
        if not force and now - self.tick_at < 60:
            return
        secs = max(1.0, now - self.tick_at)
        kbps = round(self.tick_bytes * 8 / secs / 1000)
        self.tick_rates.append(kbps)
        self.log("tick", ch=self.number, bytes=self.tick_bytes, kbps=kbps,
                 buffer_s=player.buffer(now) if player else None, total_mb=round(self.bytes / 1e6, 1))
        self.tick_bytes, self.tick_at = 0, now
        if self.night["bytes"] > self.night["budget"]:
            self.guard.trip(f"byte budget of {self.night['budget'] / 1e9:.1f} GB reached")

    def session(self, end):
        """Open the channel and play it until `end`; raises StreamError when it drops."""
        self.sessions += 1
        status, final, body, _, secs = self.get_playlist(self.url, "open")
        text = body.decode("utf-8", "replace")
        variant = pick_variant(text)
        media_url, media_text = final, text
        if variant:
            media_url = urllib.parse.urljoin(final, variant[0])
            media_text = None
        self.log("connect", ch=self.number, session=self.sessions, status=status, secs=round(secs, 2),
                 redirected=final != self.url, variant=list(variant[1:]) if variant else None)
        started = time.time()
        player = Player(self.log, self.number)
        last_seq, last_new, stuck_logged, seg_errors = None, time.time(), False, 0
        while time.time() < end:
            self.guard.check()
            if media_text is None:
                _, media_final, body, _, _ = self.get_playlist(media_url, "playlist")
                media_text = body.decode("utf-8", "replace")
            else:
                media_final = media_url
            target, segments, endlist = parse_media_playlist(media_text)
            media_text = None
            if last_seq is None:
                new = segments[-LIVE_START:]
            else:
                new = [s for s in segments if s[0] > last_seq]
                if segments and segments[-1][0] < last_seq - 5:
                    self.log("seq_reset", ch=self.number, was=last_seq, now=segments[-1][0])
                    new = segments[-1:]
                elif new and new[0][0] > last_seq + 1:
                    self.log("skipped", ch=self.number, segments=new[0][0] - last_seq - 1)
            now = time.time()
            if new:
                last_new, stuck_logged = now, False
            elif now - last_new > max(3 * target, 30):
                if not stuck_logged:
                    self.log("stuck", ch=self.number, secs=round(now - last_new))
                    stuck_logged = True
                if now - last_new > STUCK_GIVE_UP:
                    raise StreamError("playlist", None, f"no new segment for {round(now - last_new)} s")
            for seq, dur, uri in new:
                if time.time() >= end:
                    break
                try:
                    self.get(urllib.parse.urljoin(media_final, uri), "segment", keep=False)
                except StreamError as exc:
                    seg_errors += 1
                    self.log("error", ch=self.number, kind="segment", status=exc.status, reason=exc.reason)
                    if seg_errors >= SEGMENT_ERRORS_GIVE_UP:
                        raise
                    continue
                seg_errors = 0
                player.add(dur, time.time())
                last_seq = seq
                self.tick(player)
            if endlist:
                raise StreamError("playlist", None, "the provider ended the stream (EXT-X-ENDLIST)")
            self.tick(player)
            self.guard.sleep(min(target if new else max(1.0, target / 2), max(0.0, end - time.time())))
        return time.time() - started, player

    def run(self):
        end = time.time() + self.minutes * 60
        self.log("start", ch=self.number, name=self.name, planned_min=self.minutes)
        failures, reason = 0, "planned time over"
        stalls, drops = [], []
        try:
            while time.time() < end:
                session_start = time.time()
                try:
                    _, player = self.session(end)
                    stalls += player.stalls
                    break
                except StreamError as exc:
                    lasted = round(time.time() - session_start)
                    drops.append(lasted)
                    self.log("drop", ch=self.number, session=self.sessions, after_s=lasted, kind=exc.kind,
                             status=exc.status, reason=exc.reason)
                    if lasted >= 60:
                        failures = 0       # it had been playing: start the back-off again
                    delay = RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS) - 1)]
                    failures += 1
                    self.guard.sleep(min(delay, max(0.0, end - time.time())))
                    if time.time() < end:
                        self.reconnects += 1
                        self.log("reconnect", ch=self.number, n=self.reconnects)
        except Abort as exc:
            reason = f"stopped: {exc}"
            raise
        except Exception as exc:  # noqa: BLE001 - a bug here ends this channel, not the night
            reason = f"soak error: {clean(repr(exc))}"
            B.log("soak error:", reason)
        finally:
            self.tick(None, force=True)
            self.log("end", ch=self.number, name=self.name, reason=reason, bytes=self.bytes,
                     ran_s=round(time.time() - (end - self.minutes * 60)), reconnects=self.reconnects)


# --------------------------------------------------------------------------
# a night


def channel_entries():
    with open(B.PLAYLIST_PATH, encoding="utf-8") as fh:
        entries = P.parse_m3u(fh.read())[1]
    return {int(e.attrs["tvg-chno"]): e for e in entries if e.attrs.get("tvg-chno", "").isdigit()}


def parse_until(text, now=None):
    """'06:25' -> the next 06:25 after now (epoch)."""
    now = now or time.time()
    hh, mm = (int(x) for x in text.split(":"))
    lt = time.localtime(now)
    ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    return ts if ts > now else ts + 86400


def plan_from(opts):
    numbers = [int(x) for x in str(opts["channels"]).split(",") if x.strip()]
    return [(n, opts["first_min"] if i == 0 else opts["each_min"]) for i, n in enumerate(numbers)]


def run_night(opts, folder=None):
    if not B.clock_synced():
        print("the clock isn't set yet; not starting")
        return 1
    why = nobody_watching()
    if why:
        print("not starting:", why)
        B.log("soak test not started:", why)
        return 1
    folder = folder or os.path.join(SOAK_DIR, time.strftime("%Y-%m-%d_%H%M"))
    log = Log(folder)
    try:
        os.remove(B.SOAK_FLAG + ".stop")      # an old stop reason
    except OSError:
        pass
    until = parse_until(opts["until"])
    plan = plan_from(opts)
    night = {"bytes": 0, "budget": int(float(opts["budget_gb"]) * 1e9)}
    entries = channel_entries()
    guard = Guard(until)
    B.write_json(B.SOAK_FLAG, {"pid": os.getpid(), "since": int(time.time()), "channel": None, "folder": folder})

    def on_term(signum, frame):
        try:
            with open(B.SOAK_FLAG + ".stop") as fh:
                reason = fh.read().strip()
        except OSError:
            reason = "told to stop"
        guard.trip(reason)
        if not log.writing:
            raise Abort(reason)

    signal.signal(signal.SIGTERM, on_term)
    log("night", plan=plan, until=time.strftime("%H:%M", time.localtime(until)),
        budget_gb=float(opts["budget_gb"]), pid=os.getpid())
    reason = "plan finished"
    try:
        for i, (number, minutes) in enumerate(plan):
            guard.check()
            entry = entries.get(number)
            if entry is None:
                log("skip", ch=number, reason="not in the installed playlist")
                continue
            left = (until - time.time()) / 60
            if left < 1:
                reason = "time is up"
                break
            minutes = min(minutes, left)
            B.write_json(B.SOAK_FLAG, {"pid": os.getpid(), "since": int(time.time()), "channel": entry.name,
                                       "folder": folder})
            ChannelRun(number, entry.name, entry.url.partition("|")[0], minutes, guard, log, night).run()
            write_summary(folder)
            if i + 1 < len(plan):
                guard.sleep(BETWEEN_CHANNELS)
    except Abort as exc:
        reason = str(exc)
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        guard.trip("done")
        log("night_end", reason=reason, bytes=night["bytes"])
        try:
            os.remove(B.SOAK_FLAG)
        except OSError:
            pass
        text = write_summary(folder)
    B.log(f"soak test ended: {reason}; {night['bytes'] / 1e9:.2f} GB")
    print(text)
    return 0


# --------------------------------------------------------------------------
# the summary, rebuilt from events.jsonl (so a run cut short still has one)


def read_events(folder):
    out = []
    try:
        with open(os.path.join(folder, "events.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def mmss(secs):
    secs = int(secs or 0)
    return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"


def summarize(events):
    night = next((e for e in events if e["ev"] == "night"), {})
    end = next((e for e in reversed(events) if e["ev"] == "night_end"), None)
    chans, order = {}, []
    for e in events:
        ch = e.get("ch")
        if ch is None:
            continue
        c = chans.get(ch)
        if c is None:
            c = chans[ch] = {"ch": ch, "name": "", "ticks": [], "stalls": [], "errors": {}, "drops": [],
                             "reconnects": 0, "skipped": 0, "stuck": 0, "ran_s": 0, "bytes": 0, "end": None}
            order.append(ch)
        ev = e["ev"]
        if ev == "start":
            c["name"], c["planned"], c["from"] = e.get("name", ""), e.get("planned_min"), e["t"]
        elif ev == "tick" and e.get("bytes"):
            c["ticks"].append(e["kbps"])
        elif ev == "stall":
            c["stalls"].append(e["secs"])
        elif ev == "error":
            key = str(e.get("status") or e.get("reason", "?").split(":")[0][:30])
            c["errors"][key] = c["errors"].get(key, 0) + 1
        elif ev == "drop":
            c["drops"].append((e["t"], e.get("after_s"), e.get("status") or e.get("reason")))
        elif ev == "reconnect":
            c["reconnects"] = e["n"]
        elif ev == "skipped":
            c["skipped"] += e.get("segments", 0)
        elif ev == "stuck":
            c["stuck"] += 1
        elif ev == "end":
            c["end"], c["ran_s"], c["bytes"], c["to"] = e.get("reason"), e.get("ran_s"), e.get("bytes", 0), e["t"]
    lines = []
    total = sum(c["bytes"] for c in chans.values())
    first = events[0]["t"] if events else "?"
    plan = ", ".join(f"{n}:{m:g}m" for n, m in night.get("plan") or [])
    lines.append(f"Soak test from {first}: plan (channel:minutes) {plan}; until {night.get('until')}, "
                 f"budget {night.get('budget_gb')} GB")
    if end:
        lines.append(f"Ended {end['t']}: {end['reason']}. {total / 1e9:.2f} GB in all.")
    else:
        lines.append(f"Still running, or cut off (no end recorded). {total / 1e9:.2f} GB so far.")
    lines.append("")
    lines.append(f"{'ch':>3} {'name':<16} {'ran':>7} {'MB':>6} {'Mbit/s avg (min-max)':<21} "
                 f"{'stalls':>6} {'longest':>7} {'drops':>5} {'errors':<14} ended")
    for ch in order:
        c = chans[ch]
        t = c["ticks"]
        rate = f"{sum(t) / len(t) / 1000:.2f} ({min(t) / 1000:.1f}-{max(t) / 1000:.1f})" if t else "-"
        errs = ",".join(f"{k}x{v}" for k, v in sorted(c["errors"].items())) or "-"
        longest = f"{max(c['stalls']):.0f}s" if c["stalls"] else "-"
        ended = f"{c.get('to', '?')} {c['end'] or '(cut off)'}"
        lines.append(f"{ch:>3} {c['name'][:16]:<16} {mmss(c['ran_s']):>7} {c['bytes'] / 1e6:>6.0f} {rate:<21} "
                     f"{len(c['stalls']):>6} {longest:>7} {len(c['drops']):>5} {errs:<14} {ended}")
    drops = [(ch, d) for ch in order for d in chans[ch]["drops"]]
    if drops:
        lines.append("")
        lines.append("Drops (when, channel, how long that connection had lasted, why):")
        for ch, (t, after, why) in drops:
            lines.append(f"  {t}  ch {ch}  after {mmss(after)}  {why}")
    notes = [f"ch {ch}: {chans[ch]['skipped']} segments skipped" for ch in order if chans[ch]["skipped"]]
    notes += [f"ch {ch}: playlist stopped advancing {chans[ch]['stuck']}x" for ch in order if chans[ch]["stuck"]]
    if notes:
        lines.append("")
        lines += notes
    return "\n".join(lines) + "\n"


def write_summary(folder):
    text = summarize(read_events(folder))
    B.write_atomic(os.path.join(folder, "summary.txt"), text)
    return text


def latest_folder():
    try:
        runs = sorted(d for d in os.listdir(SOAK_DIR) if os.path.isdir(os.path.join(SOAK_DIR, d)))
    except OSError:
        return None
    return os.path.join(SOAK_DIR, runs[-1]) if runs else None


# --------------------------------------------------------------------------
# schedule


DEFAULTS = {"channels": DEFAULT_CHANNELS, "first_min": 90, "each_min": 15, "until": "06:25", "budget_gb": 9}


def read_schedule():
    return B.read_json(SCHEDULE)


def cmd_scheduled():
    today = time.strftime("%Y-%m-%d")
    sched = read_schedule()
    entry = sched.get(today)
    if not entry or entry.get("done"):
        return 0
    if not B.clock_synced():
        B.log("soak test: clock not set; skipped")
        return 0
    why = nobody_watching()
    if why:
        # the timer also fires at 02:00 and 03:00; the night's --until still applies
        B.log(f"soak test not started ({why}); trying again at the next hour")
        return 0
    entry["done"] = time.strftime("%Y-%m-%d %H:%M")
    B.write_json(SCHEDULE, sched)
    opts = dict(DEFAULTS)
    opts.update({k: v for k, v in entry.items() if k in DEFAULTS})
    return run_night(opts)


def add_run_options(ap):
    ap.add_argument("--channels", default=DEFAULTS["channels"], help="channel numbers, the first gets --first-min")
    ap.add_argument("--first-min", type=float, default=DEFAULTS["first_min"])
    ap.add_argument("--each-min", type=float, default=DEFAULTS["each_min"])
    ap.add_argument("--until", default=DEFAULTS["until"], help="hard stop, HH:MM local time")
    ap.add_argument("--budget-gb", type=float, default=DEFAULTS["budget_gb"])


def options(args):
    return {"channels": args.channels, "first_min": args.first_min, "each_min": args.each_min,
            "until": args.until, "budget_gb": args.budget_gb}


def cmd_status():
    st = B.soak_active()
    if st:
        print(f"running since {time.strftime('%H:%M', time.localtime(st['since']))}: {st.get('channel')}"
              f" (pid {st['pid']})")
    else:
        print("not running")
    sched = read_schedule()
    for day in sorted(sched):
        e = sched[day]
        print(f"scheduled {day} from 01:00: channels {e.get('channels')}, first {e.get('first_min')} min, "
              f"others {e.get('each_min')} min, until {e.get('until')}"
              + (f" - ran {e['done']}" if e.get("done") else ""))
    folder = latest_folder()
    if folder:
        print("latest results:", folder)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    add_run_options(sub.add_parser("run"))
    sub.add_parser("scheduled")
    sp = sub.add_parser("schedule")
    sp.add_argument("date")
    add_run_options(sp)
    up = sub.add_parser("unschedule")
    up.add_argument("date")
    rp = sub.add_parser("report")
    rp.add_argument("folder", nargs="?")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = ap.parse_args(argv)
    B.ensure_dirs()
    if args.cmd == "run":
        return run_night(options(args))
    if args.cmd == "scheduled":
        return cmd_scheduled()
    if args.cmd == "schedule":
        time.strptime(args.date, "%Y-%m-%d")
        parse_until(args.until)
        plan_from(options(args))
        sched = read_schedule()
        sched[args.date] = options(args)
        B.write_json(SCHEDULE, sched)
        return cmd_status()
    if args.cmd == "unschedule":
        sched = read_schedule()
        sched.pop(args.date, None)
        B.write_json(SCHEDULE, sched)
        return cmd_status()
    if args.cmd == "report":
        folder = args.folder or latest_folder()
        if folder and not os.path.isabs(folder):
            folder = os.path.join(SOAK_DIR, folder)
        if not folder or not os.path.isdir(folder):
            print("no soak results yet")
            return 1
        print(folder)
        print(summarize(read_events(folder)), end="")
        return 0
    if args.cmd == "stop":
        print("stopped" if B.stop_soak("stopped by hand") else "not running")
        return 0
    if args.cmd == "status":
        return cmd_status()
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
