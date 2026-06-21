#!/usr/bin/env python3
"""
Scrape Calshot Castle tide extrema from tidetimes.org.uk.

Why this exists: the bought PDF tide table abridges to 2H+2L per day, but the
website's per-day pages show the full 4H+2L pattern (the Solent double high
water). We need the full pattern for upper-Southampton-Water navigation.

The site only serves dates from 2022-01-01 up to ~6 days ahead of today;
beyond that, the URL silently falls back to today's data, so we verify the
date heading on each response and skip mismatches. The dated URL for *today*
itself serves the "current day" landing page with no date heading at all —
we accept that page only when the requested date is today.

Args: --start YYYY-MM-DD --end YYYY-MM-DD [--out FILE]
Example:
    python scripts/scrape_calshot_tides.py --start 2026-06-20 --end 2026-06-26 \\
        --out tides.json
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import date, timedelta

URL = "https://www.tidetimes.org.uk/calshot-castle-tide-times-{ymd}"
UA = "Mozilla/5.0 (compatible; personal tide scrape; +contact via tidetimes account)"

# vis2 rows are BST-adjusted; vis0 is the GMT shadow set we ignore.
ROW_RE = re.compile(
    r'<tr class="vis2">\s*'
    r'<td class="tal">(High|Low)</td>\s*'
    r'<td class="tac"><span>(\d{2}:\d{2})</span></td>\s*'
    r'<td class="tar">([\d.]+)m</td>',
    re.S,
)

# Page heading like "Calshot Castle Tide Times for 25th June 2026"
HEADING_RE = re.compile(
    r"Calshot Castle Tide Times for\s+"
    r"(\d{1,2})(?:st|nd|rd|th)\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{4})",
    re.I,
)
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], 1)}


def fetch(d):
    url = URL.format(ymd=d.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(html, expected):
    m = HEADING_RE.search(html)
    if m:
        served = date(int(m.group(3)), MONTHS[m.group(2).title()], int(m.group(1)))
        if served != expected:
            return None, f"served {served.isoformat()}, not {expected.isoformat()} (paywall fall-back)"
    elif expected != date.today():
        # The dated URL for today serves the "current day" landing page, whose
        # <h1> drops the "for <date>" suffix. Accept it only when we asked for today.
        return None, "no date heading"
    rows = ROW_RE.findall(html)
    extrema = [{"t": t, "h": float(h), "type": "HW" if k == "High" else "LW"}
               for k, t, h in rows]
    return extrema, None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    p.add_argument("--out", help="write JSON here instead of stdout")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    args = p.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out = {}
    d = start
    while d <= end:
        try:
            html = fetch(d)
            extrema, err = parse(html, d)
            if err:
                print(f"# {d}: SKIP — {err}", file=sys.stderr)
            else:
                out[d.isoformat()] = {"verified": True, "extrema": extrema}
                print(f"# {d}: {len(extrema)} extrema", file=sys.stderr)
        except Exception as exc:
            print(f"# {d}: ERROR {exc}", file=sys.stderr)
        d += timedelta(days=1)
        time.sleep(args.delay)

    payload = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload)
        print(f"# wrote {args.out} ({len(out)} day(s))", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
