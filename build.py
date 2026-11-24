#!/usr/bin/env python3
import concurrent.futures
import datetime as dt
import gzip
import io
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

ROOT = pathlib.Path(__file__).resolve().parent
SITE = ROOT / "site"
PUBLIC_URL = "https://yashiels.github.io/live-tv"
IPTV_API = "https://iptv-org.github.io/api"
DSTV_GUIDE = "https://www.dstv.com/umbraco/api/TvGuide/GetProgrammes?d={day}&country=zaf"
PLATFORM_GUIDES = {
    "stvp": "https://i.mjh.nz/SamsungTVPlus/all.xml.gz",
    "plex": "https://i.mjh.nz/Plex/all.xml.gz",
    "roku": "https://i.mjh.nz/Roku/all.xml.gz",
}
GUIDE_DAYS = 2
SAST = dt.timezone(dt.timedelta(hours=2))
TRUSTED_DOMAINS = ("amagi.tv", "wurl.tv", "wurl.com")
TRUSTED_HOSTS = {
    "sabconeta.cdn.mangomolo.com", "sabconetanw.cdn.mangomolo.com",
    "sabctwota.cdn.mangomolo.com", "sabctreta.cdn.mangomolo.com",
    "sabctretalh.cdn.mangomolo.com", "sabcnews.cdn.mangomolo.com",
    "cc-yw7ztecy8do3q.akamaized.net", "d3arbp6l7f096k.cloudfront.net",
    "d2lk5u59tns74c.cloudfront.net", "d3gnyty2vddhsg.cloudfront.net",
    "dbrb49pjoymg4.cloudfront.net",
}
MIN_CHANNEL_SHARE = 0.85
MIN_PROGRAMMES = 150


def trusted(url):
    name = urllib.parse.urlsplit(url).hostname or ""
    return name in TRUSTED_HOSTS or any(name == d or name.endswith("." + d) for d in TRUSTED_DOMAINS)


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def quality(stream):
    return int(re.sub(r"\D", "", stream.get("quality") or "") or 0)


class UntrustedRedirect(Exception):
    pass


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted(newurl):
            raise UntrustedRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(TrustedRedirectHandler)
OPEN_OPENER = urllib.request.build_opener()
GEO_DENIAL_CODES = {403, 451}


def probe(stream, opener=OPENER):
    headers = {"User-Agent": stream.get("user_agent") or "Mozilla/5.0"}
    if stream.get("referrer"):
        headers["Referer"] = stream["referrer"]
    try:
        request = urllib.request.Request(stream["url"], headers=headers)
        with opener.open(request, timeout=10) as response:
            return "ok" if b"#EXTM3U" in response.read(4096) else "down"
    except UntrustedRedirect:
        return "untrusted"
    except urllib.error.HTTPError as error:
        return "geo" if error.code in GEO_DENIAL_CODES else "down"
    except Exception:
        return "down"


def candidates(entry, streams):
    matches = [
        s for s in streams
        if s.get("channel") == entry["id"]
        and (not entry.get("feed") or s.get("feed") == entry["feed"])
        and (entry.get("open") or trusted(s["url"]))
    ]
    return sorted(matches, key=quality, reverse=True)


def pick_stream(entry, streams):
    opener = OPEN_OPENER if entry.get("open") else OPENER
    results = [(probe(s, opener), s) for s in candidates(entry, streams)]
    working = next((s for state, s in results if state == "ok"), None)
    if working or not entry.get("geo"):
        return working
    return next((s for state, s in results if state == "geo"), None)


def tvg_id(entry):
    if entry.get("epg"):
        return tuple(entry["epg"])
    suffix = f"@{entry['feed']}" if entry.get("feed") else ""
    return None, entry["id"] + suffix


def select(config, channels, streams):
    names = {c["id"]: c["name"] for c in channels}
    jobs = [(group, entry) for group, entries in config.items() for entry in entries]
    with concurrent.futures.ThreadPoolExecutor(16) as pool:
        picks = list(pool.map(lambda job: pick_stream(job[1], streams), jobs))
    selected, missing = [], []
    for (group, entry), stream in zip(jobs, picks):
        if stream is None:
            missing.append(entry["id"])
            continue
        platform, channel_id = tvg_id(entry)
        selected.append({
            "group": group,
            "name": names.get(entry["id"], entry["id"]),
            "tvg_id": channel_id,
            "platform": platform,
            "dstv": entry.get("dstv"),
            "open": entry.get("open", False),
            "stream": stream,
        })
    return selected, missing


def playlist(selected):
    lines = [f'#EXTM3U x-tvg-url="{PUBLIC_URL}/guide.xml.gz"']
    for item in selected:
        stream = item["stream"]
        lines.append(
            f'#EXTINF:-1 tvg-id="{item["tvg_id"]}" group-title="{item["group"]}",{item["name"]}'
        )
        if stream.get("user_agent"):
            lines.append(f"#EXTVLCOPT:http-user-agent={stream['user_agent']}")
        if stream.get("referrer"):
            lines.append(f"#EXTVLCOPT:http-referrer={stream['referrer']}")
        lines.append(stream["url"])
    return "\n".join(lines) + "\n"


def xmltv_time(value):
    moment = dt.datetime.fromisoformat(value)
    moment = moment.replace(tzinfo=SAST) if moment.tzinfo is None else moment.astimezone(SAST)
    return moment.strftime("%Y%m%d%H%M%S %z")


def parse_xmltv_time(value):
    return dt.datetime.strptime(value.strip(), "%Y%m%d%H%M%S %z")


def dstv_programmes(selected):
    wanted = {item["dstv"]: item["tvg_id"] for item in selected if item["dstv"]}
    today = dt.datetime.now(SAST).date()
    programmes = {}
    for offset in range(GUIDE_DAYS):
        day = (today + dt.timedelta(days=offset)).isoformat()
        for channel in json.loads(fetch(DSTV_GUIDE.format(day=day)))["Channels"]:
            channel_id = wanted.get(str(channel.get("Number")))
            if not channel_id:
                continue
            for item in channel.get("Programmes") or []:
                programmes[(channel_id, item["StartTime"])] = (
                    f'  <programme start={quoteattr(xmltv_time(item["StartTime"]))} '
                    f'stop={quoteattr(xmltv_time(item["EndTime"]))} channel={quoteattr(channel_id)}>'
                    f'<title>{escape(item["Title"])}</title></programme>'
                )
    return [programmes[key] for key in sorted(programmes)]


def platform_programmes(selected):
    now = dt.datetime.now(dt.timezone.utc)
    horizon = now + dt.timedelta(days=GUIDE_DAYS)
    lines = []
    for platform, url in PLATFORM_GUIDES.items():
        wanted = {item["tvg_id"] for item in selected if item["platform"] == platform}
        if not wanted:
            continue
        for _, element in ET.iterparse(io.BytesIO(fetch(url))):
            if element.tag == "programme":
                if element.get("channel") in wanted:
                    start = parse_xmltv_time(element.get("start"))
                    stop = parse_xmltv_time(element.get("stop"))
                    if stop >= now and start <= horizon:
                        lines.append("  " + ET.tostring(element, encoding="unicode").strip())
                element.clear()
    return lines


def guide(selected):
    channels = [
        f"  <channel id={quoteattr(item['tvg_id'])}><display-name>{escape(item['name'])}</display-name></channel>"
        for item in selected
    ]
    body = dstv_programmes(selected) + platform_programmes(selected)
    document = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="live-tv">']
    return "\n".join(document + channels + body + ["</tv>"]) + "\n", len(body)


def main():
    config = json.loads((ROOT / "channels.json").read_text())
    channels = json.loads(fetch(f"{IPTV_API}/channels.json"))
    streams = json.loads(fetch(f"{IPTV_API}/streams.json"))
    selected, missing = select(config, channels, streams)
    guide_xml, programme_count = guide(selected)
    print(f"{len(selected)} channels, {programme_count} programmes")
    if missing:
        print("no trusted stream: " + ", ".join(missing), file=sys.stderr)
    expected = sum(1 for entries in config.values() for entry in entries if not entry.get("open"))
    stable = sum(1 for item in selected if not item["open"])
    if stable < expected * MIN_CHANNEL_SHARE or programme_count < MIN_PROGRAMMES:
        sys.exit(f"refusing to publish: {len(selected)}/{expected} channels, {programme_count} programmes")
    SITE.mkdir(exist_ok=True)
    (SITE / "playlist.m3u").write_text(playlist(selected))
    (SITE / "guide.xml.gz").write_bytes(gzip.compress(guide_xml.encode(), mtime=0))


if __name__ == "__main__":
    main()
