#!/usr/bin/env python3
"""
Builds a 24/7 XMLTV EPG (epg.xml) for Australian sports channels that are
missing from, or blank in, the popular free EPG feeds:

  ESPN, ESPN2, Fox League, Fox Cricket, Fox Footy,
  Fox Sports 503, Fox Sports 505, Fox Sports 506, Fox Sports More

Data sources (both reachable from an automated server):
  * Kayo feed (i.mjh.nz)  - real round-the-clock listings for the few
                            channels Foxtel actually publishes (e.g. Fox Footy).
  * ausportguide.com      - live sporting events for the rest.

To guarantee a continuous 24/7 guide with no blank spaces, any gap between
real programmes is filled with a labelled placeholder block, e.g.
"Fox League programming".

Output timestamps use the Australia/Brisbane UTC offset (+10:00).
Intended to run daily via GitHub Actions.
"""

import re
import sys
import gzip
import json
import time as _time
from datetime import date, datetime, timedelta, timezone
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

AUSPORT_URL = "https://ausportguide.com/tv-station-guide/"
KAYO_URL = "https://i.mjh.nz/Kayo/app.json"
SYDNEY = ZoneInfo("Australia/Sydney")       # timezone ausportguide displays
BRISBANE = ZoneInfo("Australia/Brisbane")   # timezone used in output
UTC = timezone.utc

OUTPUT_FILE = "epg.xml"
MAX_EVENT_HOURS = 3          # cap on a single real programme with no follow-on
FILLER_CHUNK_HOURS = 6       # split long filler gaps into blocks this size
KAYO_BLANK = "No listing available"
REQUEST_TIMEOUT = 30
RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
}

# (xmltv_id, [display names], ausportguide slug, kayo channel number or None)
CHANNELS = [
    ("espn.au",          ["ESPN", "ESPN AU", "ESPN Australia"],               "espn",               None),
    ("espn2.au",         ["ESPN 2", "ESPN2", "ESPN 2 AU", "ESPN2 Australia"], "espn2",              None),
    ("foxleague.au",     ["Fox League", "Fox Sports 502", "FOX League"],      "fox-league",         502),
    ("foxcricket.au",    ["Fox Cricket", "Fox Sports 501", "FOX Cricket"],    "fox-sports-cricket", 501),
    ("foxfooty.au",      ["Fox Footy", "Fox Sports 504", "FOX Footy"],        "fox-footy",          504),
    ("foxsports503.au",  ["Fox Sports 503", "FOX Sports 503"],                "fox-sports-503",     503),
    ("foxsports505.au",  ["Fox Sports 505", "FOX Sports 505"],                "fox-sports-505",     505),
    ("foxsports506.au",  ["Fox Sports 506", "FOX Sports 506"],                "fox-sports-506",     506),
    ("foxsportsmore.au", ["Fox Sports More", "FOX Sports More"],              "fox-sports-more",    None),
    ("racing.au",        ["Racing.com", "Racing", "RACING.COM"],              None,                 529),
]

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def fetch(url):
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            print(f"  attempt {attempt} failed: {e}", file=sys.stderr)
            _time.sleep(5 * attempt)
    raise RuntimeError(f"could not fetch {url}: {last_err}")


# ---------------------------------------------------------------------------
# ausportguide.com (live events)
# ---------------------------------------------------------------------------

def parse_day_header(text, today):
    m = re.search(r"(\d{1,2})\.\s*([A-Za-z]{3})", text)
    if not m:
        return None
    day = int(m.group(1))
    mon = MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    year = today.year
    if mon == 1 and today.month == 12:
        year += 1
    elif mon == 12 and today.month == 1:
        year -= 1
    try:
        return date(year, mon, day)
    except ValueError:
        return None


def parse_clock(text):
    text = text.strip().upper().replace(" ", "")
    try:
        return datetime.strptime(text, "%I:%M%p").time()
    except ValueError:
        return None


def parse_ausport(html, today):
    soup = BeautifulSoup(html, "html.parser")
    events = []
    current_date = None

    def is_marker(el):
        cls = el.get("class") or []
        return "panelType" in cls or "list-group-item" in cls

    for el in soup.find_all(is_marker):
        cls = el.get("class") or []
        if "panelType" in cls:
            d = parse_day_header(el.get_text(" ", strip=True), today)
            if d:
                current_date = d
            continue
        if current_date is None:
            continue
        time_el = el.find(class_=re.compile(r"^eventTime"))
        title_el = el.find("h6")
        if time_el is None or title_el is None:
            continue
        clock = parse_clock(time_el.get_text(strip=True))
        title = title_el.get_text(" ", strip=True)
        if clock is None or not title:
            continue

        sport = ""
        italic = el.find(["em", "i"])
        if italic:
            sport = italic.get_text(strip=True)

        league_el = el.find(class_="listedLeague")
        league = ""
        if league_el:
            for tag in league_el.find_all(["em", "i"]):
                tag.extract()
            league = league_el.get_text(" ", strip=True)
        league = re.sub(r"\b(.+?)\s+\1\b", r"\1", league).strip()

        start = datetime.combine(current_date, clock, tzinfo=SYDNEY)
        events.append({
            "start": start,
            "title": title,
            "subtitle": league,
            "category": sport or "Sports",
        })
    return events


# ---------------------------------------------------------------------------
# Kayo (real 24/7 listings where available)
# ---------------------------------------------------------------------------

def load_kayo():
    """Return {chno: [ {start, title, subtitle, category}, ... ]} for channels
    that have REAL programming (skips 'No listing available')."""
    try:
        data = json.loads(fetch(KAYO_URL))
    except Exception as e:
        print(f"  Kayo feed unavailable: {e}", file=sys.stderr)
        return {}
    out = {}
    for _internal, info in data.items():
        chno = info.get("chno")
        epg = info.get("epg") or []
        progs = []
        for entry in epg:
            if not entry or len(entry) < 2:
                continue
            ts, title = entry[0], entry[1]
            if not title or title.strip() == KAYO_BLANK:
                continue
            progs.append({
                "start": datetime.fromtimestamp(int(ts), tz=UTC),
                "title": title.strip(),
                "subtitle": "",
                "category": "Sports",
            })
        if progs:
            out[chno] = progs
    return out


# ---------------------------------------------------------------------------
# Timeline assembly + gap filling
# --------------------------------------------------------------------------

def dedupe_sort(events):
    events.sort(key=lambda p: p["start"])
    seen = set()
    unique = []
    for p in events:
        key = (p["start"], p["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)
    return unique


def filler_blocks(start, end, label):
    """Yield placeholder programmes covering [start, end)."""
    cursor = start
    step = timedelta(hours=FILLER_CHUNK_HOURS)
    while cursor < end:
        chunk_end = min(cursor + step, end)
        yield {
            "start": cursor,
            "stop": chunk_end,
            "title": f"{label} programming",
            "subtitle": "",
            "category": "Sports",
            "filler": True,
        }
        cursor = chunk_end


def build_timeline(events, w0, w1, label):
    """Return a gapless list of programmes across [w0, w1]."""
    # keep only events intersecting the window
    ev = [e for e in events if w0 <= e["start"] < w1]
    ev = dedupe_sort(ev)

    # natural stop = next start (back to back) else start + MAX_EVENT_HOURS
    for i, e in enumerate(ev):
        natural = e["start"] + timedelta(hours=MAX_EVENT_HOURS)
        if i + 1 < len(ev) and ev[i + 1]["start"] < natural:
            e["stop"] = ev[i + 1]["start"]
        else:
            e["stop"] = min(natural, w1)
        e["filler"] = False

    out = []
    cursor = w0
    for e in ev:
        s = max(e["start"], cursor)
        if s > cursor:
            out.extend(filler_blocks(cursor, s, label))
        if e["stop"] > s:
            e2 = dict(e)
            e2["start"] = s
            out.append(e2)
            cursor = e2["stop"]
    if cursor < w1:
        out.extend(filler_blocks(cursor, w1, label))
    return out


def fmt(dt):
    return dt.astimezone(BRISBANE).strftime("%Y%m%d%H%M%S %z")


def build_xml(channel_programmes):
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<tv generator-info-name="au-sports-epg" '
               'source-info-name="Kayo / ausportguide.com">')
    for xmltv_id, names, _slug, _chno in CHANNELS:
        out.append(f'  <channel id="{escape(xmltv_id)}">')
        for name in names:
            out.append(f'    <display-name>{escape(name)}</display-name>')
        out.append("  </channel>")

    for xmltv_id, programmes in channel_programmes.items():
        for p in programmes:
            out.append(
                f'  <programme start="{fmt(p["start"])}" '
                f'stop="{fmt(p["stop"])}" channel="{escape(xmltv_id)}">'
            )
            out.append(f'    <title lang="en">{escape(p["title"])}</title>')
            if p.get("subtitle"):
                out.append(
                    f'    <sub-title lang="en">{escape(p["subtitle"])}</sub-title>'
                )
            out.append(f'    <category lang="en">{escape(p["category"])}</category>')
            out.append("  </programme>")
    out.append("</tv>")
    return "\n".join(out) + "\n"


def main():
    now_bris = datetime.now(BRISBANE)
    today = now_bris.date()
    w0 = datetime(today.year, today.month, today.day, tzinfo=BRISBANE)

    kayo = load_kayo()
    print(f"Kayo channels with real data: {sorted(kayo.keys())}")

    # First pass: collect raw events per channel + find global end of real data
    raw = {}
    global_last = w0
    for xmltv_id, names, slug, chno in CHANNELS:
        if chno is not None and chno in kayo:
            events = list(kayo[chno])
            print(f"{names[0]}: {len(events)} programmes (Kayo 24/7)")
        elif slug is not None:
            url = AUSPORT_URL + slug
            print(f"Fetching {url} ...")
            try:
                events = parse_ausport(fetch(url), today)
            except Exception as e:
                print(f"  FAILED: {e}", file=sys.stderr)
                events = []
            print(f"  {len(events)} live events (ausportguide)")
            _time.sleep(2)
        else:
            events = []
            print(f"{names[0]}: no data source available; filler only")
        raw[xmltv_id] = events
        for e in events:
            if e["start"] > global_last:
                global_last = e["start"]

    # window end: cover through the last real programme, min 2 days, max 9 days
    w1 = max(global_last + timedelta(hours=MAX_EVENT_HOURS), w0 + timedelta(days=2))
    w1 = min(w1, w0 + timedelta(days=9))

    # Second pass: build gapless timelines
    channel_programmes = {}
    total = 0
    for xmltv_id, names, _slug, _chno in CHANNELS:
        tl = build_timeline(raw[xmltv_id], w0, w1, names[0])
        channel_programmes[xmltv_id] = tl
        total += len(tl)

    xml = build_xml(channel_programmes)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(xml)
    with gzip.open(OUTPUT_FILE + ".gz", "wb") as g:
        g.write(xml.encode("utf-8"))
    print(f"Wrote {OUTPUT_FILE}: {total} programmes across "
          f"{len(CHANNELS)} channels, window {w0.date()} -> {w1.date()}.")


if __name__ == "__main__":
    main()
