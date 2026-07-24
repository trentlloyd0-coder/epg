#!/usr/bin/env python3
"""
Builds an XMLTV EPG (epg.xml) for Australian sports channels that are
missing from the popular free EPG feeds:

  ESPN, ESPN2, Fox League, Fox Cricket, Fox Footy,
  Fox Sports 503, Fox Sports 505, Fox Sports 506, Fox Sports More

Guide data is scraped from ausportguide.com (server-rendered pages,
times displayed in Sydney local time). Output timestamps are written
with the Australia/Brisbane UTC offset (+10:00).

Intended to run daily via GitHub Actions.
"""

import re
import sys
import time as _time
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ausportguide.com/tv-station-guide/"
SYDNEY = ZoneInfo("Australia/Sydney")     # timezone the site displays
BRISBANE = ZoneInfo("Australia/Brisbane") # timezone used in output
OUTPUT_FILE = "epg.xml"
MAX_PROGRAMME_HOURS = 3   # assumed max duration when no following programme
REQUEST_TIMEOUT = 30
RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-AU,en;q=0.9",
}

# (xmltv_id, [display names], site slug)
CHANNELS = [
    ("espn.au",          ["ESPN", "ESPN AU", "ESPN Australia"],               "espn"),
    ("espn2.au",         ["ESPN 2", "ESPN2", "ESPN 2 AU", "ESPN2 Australia"], "espn2"),
    ("foxleague.au",     ["Fox League", "Fox Sports 502", "FOX League"],      "fox-league"),
    ("foxcricket.au",    ["Fox Cricket", "Fox Sports 501", "FOX Cricket"],    "fox-sports-cricket"),
    ("foxfooty.au",      ["Fox Footy", "Fox Sports 504", "FOX Footy"],        "fox-footy"),
    ("foxsports503.au",  ["Fox Sports 503", "FOX Sports 503"],                "fox-sports-503"),
    ("foxsports505.au",  ["Fox Sports 505", "FOX Sports 505"],                "fox-sports-505"),
    ("foxsports506.au",  ["Fox Sports 506", "FOX Sports 506"],                "fox-sports-506"),
    ("foxsportsmore.au", ["Fox Sports More", "FOX Sports More"],              "fox-sports-more"),
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


def parse_day_header(text, today):
    """'Friday, 24. Jul' -> date object (year inferred)."""
    m = re.search(r"(\d{1,2})\.\s*([A-Za-z]{3})", text)
    if not m:
        return None
    day = int(m.group(1))
    mon = MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    year = today.year
    # year rollover: a December page viewed in January (or vice versa)
    if mon == 1 and today.month == 12:
        year += 1
    elif mon == 12 and today.month == 1:
        year -= 1
    try:
        return date(year, mon, day)
    except ValueError:
        return None


def parse_clock(text):
    """'6:00PM' / '11:30AM' -> time object."""
    text = text.strip().upper().replace(" ", "")
    try:
        return datetime.strptime(text, "%I:%M%p").time()
    except ValueError:
        return None


def has_class(el, name):
    return name in (el.get("class") or [])


def parse_channel_page(html, today):
    """Return list of dicts: {start (aware dt), title, subtitle, category}."""
    soup = BeautifulSoup(html, "html.parser")
    programmes = []
    current_date = None

    def is_marker(el):
        cls = el.get("class") or []
        return "panelType" in cls or "list-group-item" in cls

    for el in soup.find_all(is_marker):
        if has_class(el, "panelType"):
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
                tag.extract()  # sport name lives here; keep it out of subtitle
            league = league_el.get_text(" ", strip=True)
        # collapse repeated phrases the site sometimes produces
        league = re.sub(r"\b(.+?)\s+\1\b", r"\1", league).strip()

        start = datetime.combine(current_date, clock, tzinfo=SYDNEY)
        programmes.append(
            {
                "start": start,
                "title": title,
                "subtitle": league,
                "category": sport or "Sports",
            }
        )

    # sort + de-duplicate
    programmes.sort(key=lambda p: p["start"])
    seen = set()
    unique = []
    for p in programmes:
        key = (p["start"], p["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)

    # stop time = next programme start, capped at MAX_PROGRAMME_HOURS
    for i, p in enumerate(unique):
        cap = p["start"] + timedelta(hours=MAX_PROGRAMME_HOURS)
        if i + 1 < len(unique) and unique[i + 1]["start"] < cap:
            p["stop"] = unique[i + 1]["start"]
        else:
            p["stop"] = cap
    return unique


def fmt(dt):
    return dt.astimezone(BRISBANE).strftime("%Y%m%d%H%M%S %z")


def build_xml(channel_programmes):
    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append('<!DOCTYPE tv SYSTEM "xmltv.dtd">')
    out.append('<tv generator-info-name="au-sports-epg" '
               'source-info-name="ausportguide.com">')
    for xmltv_id, names, _slug in CHANNELS:
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
            if p["subtitle"]:
                out.append(
                    f'    <sub-title lang="en">{escape(p["subtitle"])}</sub-title>'
                )
            out.append(f'    <category lang="en">{escape(p["category"])}</category>')
            out.append("  </programme>")
    out.append("</tv>")
    return "\n".join(out) + "\n"


def main():
    today = datetime.now(SYDNEY).date()
    channel_programmes = {}
    total = 0
    failures = 0

    for xmltv_id, _names, slug in CHANNELS:
        url = BASE_URL + slug
        print(f"Fetching {url} ...")
        try:
            html = fetch(url)
            programmes = parse_channel_page(html, today)
        except Exception as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            failures += 1
            programmes = []
        print(f"  {len(programmes)} programmes")
        channel_programmes[xmltv_id] = programmes
        total += len(programmes)
        _time.sleep(2)  # be polite

    if total == 0:
        print("No programmes found at all - refusing to write empty EPG.",
              file=sys.stderr)
        sys.exit(1)

    xml = build_xml(channel_programmes)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"Wrote {OUTPUT_FILE}: {total} programmes, "
          f"{len(CHANNELS)} channels, {failures} channel(s) failed.")


if __name__ == "__main__":
    main()
