#!/usr/bin/env python3
"""Build grandma's channel list from the provider playlist(s).

channels.toml says which channels she gets, in which order and under which
names; this script finds them in the provider's (usually huge) M3U and writes
a small local playlist for IPTV Simple. If the provider is unreachable the
last good copy is kept; a channel missing upstream keeps its previous URL.

  playlist.py                    build + install (on the box, babua-playlist.timer)
  playlist.py --check-streams    probe every installed stream (daily timer)
  playlist.py --preview          show what would match (works on the admin PC too)
  playlist.py --codecs           ffprobe every matched stream: will a Pi 3 play it? (admin PC)
  playlist.py --list SOURCE [-g TEXT]   list names in a source, to write match rules
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import babua_lib as B  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 on an admin machine
    import tomli as tomllib  # type: ignore

STATUS_PATH = os.path.join(B.STATE_DIR, "playlist.json")
SOURCE_CACHE = os.path.join(B.STATE_DIR, "sources")
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


# --------------------------------------------------------------------- m3u


class Entry:
    def __init__(self, name, url, attrs=None, extra=None):
        self.name = name
        self.url = url
        self.attrs = dict(attrs or {})
        self.extra = list(extra or [])  # #KODIPROP / #EXTVLCOPT lines

    def render(self):
        attrs = " ".join(f'{k}="{v}"' for k, v in self.attrs.items() if v != "")
        lines = [f"#EXTINF:-1 {attrs},{self.name}" if attrs else f"#EXTINF:-1,{self.name}"]
        lines += self.extra
        lines.append(self.url)
        return "\n".join(lines)


def split_extinf(line):
    """'#EXTINF:-1 a="x, y",Name' -> ({'a': 'x, y'}, 'Name')."""
    body = line[len("#EXTINF:"):]
    in_quote = False
    for i, ch in enumerate(body):
        if ch == '"':
            in_quote = not in_quote
        elif ch == "," and not in_quote:
            return dict(ATTR_RE.findall(body[:i])), body[i + 1:].strip()
    return dict(ATTR_RE.findall(body)), ""


def parse_m3u(text):
    header, entries = {}, []
    current, extra = None, []
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            header.update(ATTR_RE.findall(line))
        elif line.startswith("#EXTINF:"):
            current, extra = split_extinf(line), []
        elif line.startswith("#"):
            if current is not None and line.startswith(("#KODIPROP", "#EXTVLCOPT")):
                extra.append(line)
            elif current is not None and line.startswith("#EXTGRP:") and "group-title" not in current[0]:
                current[0]["group-title"] = line[len("#EXTGRP:"):].strip()
        elif current is not None:
            attrs, name = current
            entries.append(Entry(name or attrs.get("tvg-name", ""), line, attrs, extra))
            current, extra = None, []
    return header, entries


def norm(text):
    return " ".join(text.casefold().replace("ё", "е").split())


# ------------------------------------------------------------------ config


def load_channels(path):
    with open(path, "rb") as fh:
        doc = tomllib.load(fh)
    sources = doc.get("sources", {})
    channels = doc.get("channel", [])
    seen, numbers = set(), set()
    auto = 0  # channels without an explicit `number` are numbered 1, 2, 3, ...
    for i, ch in enumerate(channels, 1):
        if not ch.get("name"):
            raise ValueError(f"channel #{i} has no name")
        if ch["name"] in seen:
            raise ValueError(f"duplicate channel name {ch['name']!r}")
        seen.add(ch["name"])
        if "number" not in ch:
            auto += 1
            ch["number"] = auto
        if ch["number"] in numbers:
            raise ValueError(f"duplicate channel number {ch['number']}")
        numbers.add(ch["number"])
        if isinstance(ch.get("match"), str):
            ch["match"] = [ch["match"]]
        if not (ch.get("match") or ch.get("match_re") or ch.get("tvg_id") or ch.get("url") or ch.get("url_env")):
            raise ValueError(f"channel {ch['name']!r} needs match, match_re, tvg_id, url or url_env")
        src = ch.get("source")
        if src and src not in sources:
            raise ValueError(f"channel {ch['name']!r} uses unknown source {src!r}")
    return sources, channels


def source_url(name, spec, cfg):
    if spec.get("url_env"):
        url = cfg.get(spec["url_env"], "")
        if not url:
            raise ValueError(f"source {name}: {spec['url_env']} is not set in secrets.env")
        return url
    return spec["url"]


def fetch_sources(sources, cfg, use_cache=True):
    """Return ({name: (header, entries)}, {name: status})."""
    parsed, status = {}, {}
    if use_cache:
        os.makedirs(SOURCE_CACHE, exist_ok=True)
    ua = cfg.get("IPTV_USER_AGENT") or None
    for name, spec in sources.items():
        cache = os.path.join(SOURCE_CACHE, f"{name}.m3u")
        try:
            url = source_url(name, spec, cfg)
            data = B.http_get(url, timeout=60, user_agent=ua).decode("utf-8", "replace")
            header, entries = parse_m3u(data)
            if not entries:
                raise ValueError("playlist has no entries")
            if use_cache:
                B.write_atomic(cache, data, 0o600)
            status[name] = {"ok": True, "entries": len(entries), "at": int(time.time())}
        except Exception as exc:  # noqa: BLE001
            err = re.sub(r"https?://\S+", lambda m: B.redact(m.group(0)), str(exc))
            status[name] = {"ok": False, "error": err[:200], "at": int(time.time())}
            B.log(f"source {name} failed: {err}")
            if not (use_cache and os.path.exists(cache)):
                continue
            with open(cache, encoding="utf-8") as fh:
                header, entries = parse_m3u(fh.read())
            status[name]["using_cache_from"] = int(os.path.getmtime(cache))
        parsed[name] = (header, entries)
    return parsed, status


def find(ch, parsed):
    names = [norm(m) for m in ch.get("match", [])]
    regex = re.compile(ch["match_re"], re.I) if ch.get("match_re") else None
    order = [ch["source"]] if ch.get("source") else list(parsed)
    for src in order:
        if src not in parsed:
            continue
        entries = parsed[src][1]
        if ch.get("tvg_id"):
            for e in entries:
                if e.attrs.get("tvg-id") == ch["tvg_id"]:
                    return e
        for wanted in names:  # earlier match strings win
            for e in entries:
                if norm(e.name) == wanted or norm(e.attrs.get("tvg-name", "")) == wanted:
                    return e
        if regex:
            for e in entries:
                if regex.search(e.name):
                    return e
    return None


# A channel the daily stream check finds dead is left out of her list until it
# answers again (re-tested at every rebuild while nothing plays), so nobody
# lands on a dead channel. Numbers don't shift: each keeps its tvg-chno. If
# more than this share fail at once it's an outage, not dead channels: hide none.
HIDE_AT_MOST = 0.25


def build(sources, channels, parsed, cfg, previous=None, hidden=()):
    """Return (playlist_text, resolved, missing). `resolved` includes the
    channels in `hidden`; the text leaves them out."""
    previous = previous or {}
    out, resolved, missing = [], {}, []
    epg = next((h.get("url-tvg") or h.get("x-tvg-url") for h, _ in parsed.values()
                if h.get("url-tvg") or h.get("x-tvg-url")), "")
    out.append(f'#EXTM3U x-tvg-url="{epg}"' if epg and not cfg.get("IPTV_EPG_URL") else "#EXTM3U")
    for ch in sorted(channels, key=lambda c: c["number"]):
        url = ch.get("url") or (cfg.get(ch["url_env"]) if ch.get("url_env") else None)
        src = Entry(ch["name"], url) if url else find(ch, parsed)
        if src is None:
            missing.append(ch["name"])
            src = previous.get(ch["name"])
            if src is None:
                continue
        attrs = {
            "tvg-id": ch.get("epg_id", src.attrs.get("tvg-id", "")),
            "tvg-chno": str(ch["number"]),
            "tvg-logo": ch.get("logo", src.attrs.get("tvg-logo", "")),
            "group-title": ch.get("group", ""),
        }
        entry = Entry(ch["name"], src.url, attrs, src.extra + [f"#KODIPROP:{p}" for p in ch.get("kodiprops", [])])
        resolved[ch["name"]] = entry
        if ch["name"] not in hidden:
            out.append(entry.render())
    return "\n".join(out) + "\n", resolved, missing


# ------------------------------------------------------------------ actions


def config_paths(args):
    base = args.config or B.BABUA_DIR
    return os.path.join(base, "channels.toml"), args.secrets


def load_cfg(args):
    if args.secrets:  # admin PC
        cfg = B.parse_env_file(os.path.join(args.config, "babua.conf")) if args.config else {}
        cfg.update(B.parse_env_file(args.secrets))
        return cfg
    return B.load_config()


def install(args):
    B.ensure_dirs()
    cfg = load_cfg(args)
    sources, channels = load_channels(config_paths(args)[0])
    parsed, src_status = fetch_sources(sources, cfg)
    previous = {}
    if os.path.exists(B.PLAYLIST_PATH):
        with open(B.PLAYLIST_PATH, encoding="utf-8") as fh:
            previous = {e.name: e for e in parse_m3u(fh.read())[1]}
    status = B.read_json(STATUS_PATH)
    hidden = set(status.get("hidden", []))
    text, resolved, missing = build(sources, channels, parsed, cfg, previous, hidden)
    hidden &= set(resolved)
    if hidden and not B.kodi_try(lambda k: k.is_playing(), default=False):
        back = [name for name in sorted(hidden) if probe_ok(resolved[name])]
        if back:
            B.log("working again, back in the list:", ", ".join(back))
            hidden -= set(back)
            for name in back:
                status.get("streams_bad", {}).pop(name, None)
            text, resolved, missing = build(sources, channels, parsed, cfg, previous, hidden)
    status["hidden"] = sorted(hidden)
    status.update({
        "built_at": int(time.time()),
        "sources": src_status,
        "channels": len(resolved) - len(hidden),
        "missing": missing,
        "sources_ok": all(s["ok"] for s in src_status.values()),
    })
    if status["sources_ok"]:
        status["last_full_success"] = int(time.time())

    if not resolved:
        B.log("nothing resolved; keeping the installed playlist")
    else:
        old = ""
        if os.path.exists(B.PLAYLIST_PATH):
            with open(B.PLAYLIST_PATH, encoding="utf-8") as fh:
                old = fh.read()
        if text != old:
            B.write_atomic(B.PLAYLIST_PATH, text, 0o600)
            B.write_atomic(B.RELOAD_FLAG, str(int(time.time())))
            B.log(f"playlist updated: {len(resolved) - len(hidden)} channels; reload queued")
        status["hash"] = hashlib.sha256(text.encode()).hexdigest()[:12]
    B.write_json(STATUS_PATH, status)
    if missing:
        B.log("missing upstream:", ", ".join(missing))
    if hidden:
        B.log("hidden until they work again:", ", ".join(sorted(hidden)))
    return 0


def probe(url, timeout=12):
    """Fetch the first bytes of a stream. Kodi's 'url|Header=v&H2=v' syntax is honoured."""
    url, _, hdrs = url.partition("|")
    headers = {"User-Agent": "Mozilla/5.0 (babua; LibreELEC) Kodi/21"}
    for pair in filter(None, hdrs.split("&")):
        k, _, v = pair.partition("=")
        headers[k] = urllib.request.unquote(v)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        head = resp.read(4096)
    if head.startswith(b"#EXTM3U") and b"#EXTINF" not in head and b"#EXT-X-STREAM-INF" not in head:
        raise ValueError("HLS playlist without segments")
    return True


def probe_ok(entry):
    ua = next((x.split("=", 1)[1] for x in entry.extra if "http-user-agent=" in x), None)
    url = entry.url if not ua or "|" in entry.url else f"{entry.url}|User-Agent={ua}"
    try:
        probe(url)
        return True
    except Exception:  # noqa: BLE001
        return False


def check_streams(args):
    if B.kodi_try(lambda k: k.is_playing(), default=False):
        # many providers allow one connection at a time; don't kick grandma off
        B.log("something is playing; skipping stream check")
        return 0
    with open(B.PLAYLIST_PATH, encoding="utf-8") as fh:
        entries = parse_m3u(fh.read())[1]
    results = {}
    for e in entries:
        ua = next((x.split("=", 1)[1] for x in e.extra if "http-user-agent=" in x), None)
        url = e.url if not ua or "|" in e.url else f"{e.url}|User-Agent={ua}"
        try:
            probe(url)
            results[e.name] = "ok"
        except Exception as exc:  # noqa: BLE001
            results[e.name] = str(exc)[:120]
        time.sleep(1)
    bad = {k: v for k, v in results.items() if v != "ok"}
    status = B.read_json(STATUS_PATH)
    status["streams_checked_at"] = int(time.time())
    status["streams_bad"] = dict(bad, **{n: "hidden" for n in status.get("hidden", []) if n not in bad})
    hide = set(status.get("hidden", [])) | set(bad)
    total = len(entries) + len(status.get("hidden", []))
    if len(hide) > max(1, HIDE_AT_MOST * total):
        B.log(f"{len(hide)} of {total} streams down at once: an outage, hiding none")
        hide = set(status.get("hidden", []))
    status["hidden"] = sorted(hide)
    B.write_json(STATUS_PATH, status)
    print(f"{len(entries) - len(bad)}/{len(entries)} streams answer")
    for name, err in bad.items():
        print(f"  DOWN {name}: {err}")
    if hide:
        install(args)              # hide the dead ones now, bring back any that answer again
    return 0


def pi3_verdict(streams):
    """(level, text) for ffprobe streams: can a Raspberry Pi 3 play this?
    It decodes H.264 up to 1080p in hardware; anything else is CPU-only and
    a 1.2 GHz Cortex-A53 only manages that at SD resolution."""
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return "BAD", "no video stream"
    codec = video.get("codec_name", "?")
    w, h = video.get("width", 0), video.get("height", 0)
    desc = f"{codec} {w}x{h}"
    rate = video.get("r_frame_rate", "")
    if rate and rate != "0/0":
        num, _, den = rate.partition("/")
        desc += f"@{float(num) / float(den or 1):.0f}"
    interlaced = video.get("field_order") not in (None, "", "progressive", "unknown")
    if interlaced:
        desc += " interlaced"
    audio = [s.get("codec_name", "?") for s in streams if s.get("codec_type") == "audio"]
    desc += " / " + (",".join(audio) or "no audio")
    if codec == "h264":
        if w > 1920 or h > 1088:
            return "BAD", desc + " (above 1080p)"
        if interlaced and h > 576:
            return "CHECK", desc + " (HD interlaced: check deinterlacing on the TV)"
        return "OK", desc
    if codec in ("mpeg2video", "mpeg4") and h <= 576:
        return "CHECK", desc + " (software decode; SD should be fine)"
    return "BAD", desc + " (no hardware decoder on Pi 3 - pick another variant)"


def probe_codecs(entry, timeout=20):
    url, _, hdrs = entry.url.partition("|")
    ua = next((x.split("=", 1)[1] for x in entry.extra if "http-user-agent=" in x), None)
    for pair in filter(None, hdrs.split("&")):
        k, _, v = pair.partition("=")
        if k.lower() == "user-agent":
            ua = urllib.request.unquote(v)
    cmd = ["ffprobe", "-v", "error", "-of", "json", "-rw_timeout", str(timeout * 1000000),
           "-show_entries", "stream=codec_type,codec_name,width,height,field_order,r_frame_rate"]
    if ua:
        cmd += ["-user_agent", ua]
    out = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=timeout + 15)
    if out.returncode != 0:
        raise RuntimeError(B.redact(out.stderr.strip().splitlines()[-1]) if out.stderr.strip() else "ffprobe failed")
    return json.loads(out.stdout).get("streams", [])


def codecs(args):
    if not shutil.which("ffprobe"):
        print("ffprobe not found - install ffmpeg")
        return 1
    cfg = load_cfg(args)
    sources, channels = load_channels(config_paths(args)[0])
    parsed, _ = fetch_sources(sources, cfg, use_cache=False)
    _, resolved, missing = build(sources, channels, parsed, cfg)
    worst = "OK"
    for name, entry in resolved.items():
        try:
            level, text = pi3_verdict(probe_codecs(entry))
        except Exception as exc:  # noqa: BLE001
            level, text = "BAD", f"unreachable: {exc}"
        if level == "BAD" or (level == "CHECK" and worst == "OK"):
            worst = level
        print(f"{level:<6} {entry.attrs['tvg-chno']:>3}  {name:<28} {text}")
    for name in missing:
        print(f"{'MISS':<6} {'':>3}  {name:<28} not in provider playlist")
    print(f"\noverall: {worst}  (run this from grandma's network if the provider is geo-/IP-locked)")
    return 0 if worst != "BAD" else 1


def preview(args):
    cfg = load_cfg(args)
    sources, channels = load_channels(config_paths(args)[0])
    parsed, src_status = fetch_sources(sources, cfg, use_cache=False)
    for name, st in src_status.items():
        print(f"source {name}: " + (f"{st['entries']} entries" if st["ok"] else f"FAILED {st['error']}"))
    all_names = sorted({e.name for _, entries in parsed.values() for e in entries})
    for ch in sorted(channels, key=lambda c: c["number"]):
        if ch.get("url") or ch.get("url_env"):
            print(f"{ch['number']:>3}  {ch['name']:<28} direct URL")
            continue
        hit = find(ch, parsed)
        if hit:
            print(f"{ch['number']:>3}  {ch['name']:<28} <- {hit.name}  [{B.redact(hit.url)}]")
        else:
            guesses = difflib.get_close_matches(ch.get("match", [ch["name"]])[0], all_names, n=4, cutoff=0.5)
            print(f"{ch['number']:>3}  {ch['name']:<28} MISSING  (similar: {', '.join(guesses) or '-'})")
    return 0


def list_source(args):
    cfg = load_cfg(args)
    sources, _ = load_channels(config_paths(args)[0])
    parsed, _ = fetch_sources({args.list: sources[args.list]}, cfg, use_cache=False)
    needle = norm(args.grep or "")
    for e in parsed.get(args.list, (None, []))[1]:
        if needle in norm(e.name) or needle in norm(e.attrs.get("group-title", "")):
            print(f"{e.name}\t[{e.attrs.get('group-title', '')}]\ttvg-id={e.attrs.get('tvg-id', '')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="directory with channels.toml (default: on-box config)")
    ap.add_argument("--secrets", help="secrets.env (admin PC use)")
    ap.add_argument("--check-streams", action="store_true")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--codecs", action="store_true")
    ap.add_argument("--list", metavar="SOURCE")
    ap.add_argument("-g", "--grep")
    args = ap.parse_args()
    if args.preview:
        return preview(args)
    if args.codecs:
        return codecs(args)
    if args.list:
        return list_source(args)
    if args.check_streams:
        return check_streams(args)
    return install(args)


if __name__ == "__main__":
    sys.exit(main())
